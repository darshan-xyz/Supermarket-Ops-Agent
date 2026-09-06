"""Master Tenant Directory & Multi-Tenant Store Manager.

Maintains the control plane in `master.db`:
  - `tenants`: Store registrations, store codes, store profile, and isolated database paths.
  - `tenant_users`: Maps Telegram chat IDs and user IDs to specific tenants.
  - `auth_otps`: In-chat Telegram OTP codes, attempt tracking, and 5-minute TTL.
  - `auth_audit`: Security audit events.

Each tenant has a dedicated SQLite database in `data/stores/store_{store_code}.db`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..config import MASTER_DB_PATH, STORES_DIR
from .connection import Database, IST, now_iso
from .repository import Repository

log = logging.getLogger("kirana.master")

_MASTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_code TEXT UNIQUE NOT NULL,
    store_name TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    phone_e164 TEXT UNIQUE NOT NULL,
    address TEXT NOT NULL,
    gstin TEXT,
    tagline TEXT,
    db_filename TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    telegram_chat_id INTEGER UNIQUE NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    is_verified INTEGER NOT NULL DEFAULT 0,
    preferred_language TEXT DEFAULT 'en',
    last_active_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_otps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_e164 TEXT NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    otp_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    phone_e164 TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenants_store_code ON tenants(store_code);
CREATE INDEX IF NOT EXISTS idx_tenants_phone ON tenants(phone_e164);
CREATE INDEX IF NOT EXISTS idx_tenant_users_chat ON tenant_users(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_auth_otps_lookup ON auth_otps(phone_e164, telegram_chat_id);
"""


@dataclass
class Tenant:
    id: int
    store_code: str
    store_name: str
    owner_name: str
    phone_e164: str
    address: str
    gstin: str | None
    tagline: str | None
    db_filename: str
    status: str
    created_at: str
    updated_at: str

    @property
    def db_path(self) -> Path:
        return STORES_DIR / self.db_filename


@dataclass
class TenantUser:
    id: int
    tenant_id: int
    telegram_chat_id: int
    telegram_user_id: int
    role: str
    is_verified: bool
    preferred_language: str
    last_active_at: str
    created_at: str


def normalize_phone(raw: str) -> str:
    """Normalize phone string into standard formatted digits."""
    cleaned = re.sub(r"[^\d+]", "", raw.strip())
    if cleaned.startswith("+"):
        digits = cleaned[1:]
        return f"+{digits}"
    if len(cleaned) == 10:
        return f"+91{cleaned}"
    if len(cleaned) == 12 and cleaned.startswith("91"):
        return f"+{cleaned}"
    return f"+{cleaned}" if cleaned else ""


class MasterRegistry:
    """Control plane registry for multi-tenant stores, authentication, and connection caching."""

    def __init__(self, db_path: Path | str = MASTER_DB_PATH):
        self.db = Database(db_path)
        self._repo_pool: dict[str, Repository] = {}
        self.init()

    def init(self) -> None:
        """Initialize the master directory tables."""
        STORES_DIR.mkdir(parents=True, exist_ok=True)
        with self.db._write_lock:
            self.db._conn().executescript(_MASTER_SCHEMA)

    def _generate_store_code(self) -> str:
        """Generate a random unique store code like KIR-58291."""
        for _ in range(50):
            num = secrets.randbelow(90000) + 10000
            code = f"KIR-{num}"
            existing = self.db.query_one("SELECT id FROM tenants WHERE store_code=?", (code,))
            if not existing:
                return code
        return f"KIR-{int(datetime.now().timestamp()) % 100000}"

    def register_tenant(
        self,
        store_name: str,
        owner_name: str,
        phone_e164: str,
        address: str,
        gstin: str | None = None,
        tagline: str | None = None,
    ) -> Tenant:
        """Create a new tenant record, provision its isolated SQLite database, and seed standard catalog."""
        norm_phone = normalize_phone(phone_e164)
        existing = self.get_tenant_by_phone(norm_phone)
        if existing:
            return existing

        store_code = self._generate_store_code()
        db_filename = f"store_{store_code}.db"
        now = now_iso()

        with self.db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO tenants
                   (store_code, store_name, owner_name, phone_e164, address, gstin, tagline, db_filename, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (store_code, store_name.strip(), owner_name.strip(), norm_phone, address.strip(),
                 gstin.strip() if gstin else None, tagline.strip() if tagline else None,
                 db_filename, now, now),
            )
            tenant_id = cursor.lastrowid

        # Provision the tenant's isolated SQLite database
        store_db_path = STORES_DIR / db_filename
        tenant_db = Database(store_db_path)
        tenant_repo = Repository(tenant_db)
        tenant_repo.init()

        # Seed standard Indian Kirana catalog so store can immediately transact
        from ..domain.money import to_paise
        from ..domain.units import to_base

        seed_items = [
            ("Loose Atta", None, "kg", 0, "1101", 38, 45, 44, True, 40, 10, "atta,aata,gehu,wheat flour,kothumai,கோதுமை"),
            ("Loose Rice (Sona Masoori)", None, "kg", 0, "1006", 52, 65, 62, True, 60, 15, "rice,chawal,arisi,saatham,அரிசி,சாதம்"),
            ("Loose Toor Dal", None, "kg", 0, "0713", 110, 135, 130, True, 30, 8, "dal,toor,paruppu,thuvaram paruppu,பருப்பு"),
            ("Loose Sugar", None, "kg", 5, "1701", 40, 46, 45, True, 50, 12, "sugar,cheeni,chini,sarkkarai,seeni,சர்க்கரை"),
            ("Aashirvaad Atta 5kg", "Aashirvaad", "packet", 5, "1101", 250, 275, 270, False, 20, 5, "aashirvaad,ashirvaad atta,ஆசீர்வாத்"),
            ("Tata Salt 1kg", "Tata", "packet", 0, "2501", 20, 28, 26, False, 40, 10, "salt,namak,uppu,tata uppu,உப்பு"),
            ("Fortune Sunflower Oil 1L", "Fortune", "packet", 5, "1512", 120, 145, 140, False, 24, 6, "oil,tel,sunflower oil,ennai,எண்ணெய்"),
            ("Tata Tea Gold 250g", "Tata", "packet", 5, "0902", 115, 140, 135, False, 18, 5, "tea,chai,chai patti,டீ தூள்"),
            ("Amul Taaza Milk 500ml", "Amul", "packet", 0, "0401", 26, 30, 30, False, 30, 12, "milk,doodh,paal,amul milk,பால்"),
            ("Amul Butter 100g", "Amul", "packet", 12, "0405", 55, 62, 62, False, 24, 6, "butter,makkhan,vennai,வெண்ணெய்"),
            ("Maggi 70g", "Nestle", "packet", 18, "1902", 12, 14, 14, False, 80, 20, "maggi,noodles,மேகி,நூடுல்ஸ்"),
            ("Parle-G 70g", "Parle", "packet", 18, "1905", 8, 10, 10, False, 100, 25, "parle,parle g,biscuit,பிஸ்கட்"),
            ("Surf Excel 1kg", "HUL", "packet", 18, "3402", 105, 130, 125, False, 20, 5, "surf,detergent,washing powder,சலவை தூள்"),
            ("Colgate MaxFresh 100g", "Colgate", "packet", 18, "3306", 45, 60, 55, False, 25, 6, "colgate,toothpaste,டூத்பேஸ்ட்"),
            ("Cadbury Dairy Milk 40g", "Cadbury", "packet", 18, "1806", 30, 40, 40, False, 40, 10, "dairy milk,chocolate,சாக்லேட்"),
        ]

        today = datetime.now(IST).date()
        for name, brand, unit, gst, hsn, cost, mrp, sell, loose, open_q, reo_q, aliases in seed_items:
            exp_str = None
            if "Milk" in name:
                exp_str = (today + timedelta(days=4)).isoformat()
            elif "Butter" in name:
                exp_str = (today + timedelta(days=45)).isoformat()

            tenant_repo.add_product(
                name=name,
                brand=brand,
                unit=unit,
                gst_rate=gst,
                hsn_code=hsn,
                cost_paise=to_paise(cost),
                mrp_paise=to_paise(mrp),
                sell_paise=to_paise(sell),
                is_loose=loose,
                opening_stock_base=to_base(open_q, None, unit),
                reorder_base=to_base(reo_q, None, unit),
                aliases=aliases,
                expiry=exp_str,
            )

        # Update the store's shop profile
        tenant_repo.set_shop_profile(
            name=store_name.strip(),
            address=address.strip(),
            gstin=gstin.strip() if gstin else None,
            phone=norm_phone,
            tagline=tagline.strip() if tagline else f"Welcome to {store_name}!",
        )

        tenant = self.get_tenant_by_id(tenant_id)
        assert tenant is not None
        log.info("Provisioned new tenant store_code=%s db=%s", store_code, db_filename)
        return tenant

    def get_tenant_by_id(self, tenant_id: int) -> Tenant | None:
        row = self.db.query_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
        return self._tenant_from_row(row) if row else None

    def get_tenant_by_code(self, store_code: str) -> Tenant | None:
        row = self.db.query_one("SELECT * FROM tenants WHERE store_code=? COLLATE NOCASE", (store_code.strip(),))
        return self._tenant_from_row(row) if row else None

    def get_tenant_by_phone(self, phone_e164: str) -> Tenant | None:
        norm = normalize_phone(phone_e164)
        row = self.db.query_one("SELECT * FROM tenants WHERE phone_e164=?", (norm,))
        return self._tenant_from_row(row) if row else None

    def get_tenant_for_chat(self, chat_id: int) -> Tenant | None:
        row = self.db.query_one(
            """SELECT t.* FROM tenants t
               JOIN tenant_users u ON u.tenant_id = t.id
               WHERE u.telegram_chat_id = ? AND t.status = 'active'""",
            (chat_id,),
        )
        return self._tenant_from_row(row) if row else None

    def link_chat_to_tenant(
        self,
        chat_id: int,
        user_id: int,
        tenant_id: int,
        role: str = "owner",
        language: str = "en",
    ) -> TenantUser:
        """Bind a Telegram chat and user to a tenant store."""
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO tenant_users
                   (tenant_id, telegram_chat_id, telegram_user_id, role, is_verified, preferred_language, last_active_at, created_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(telegram_chat_id) DO UPDATE SET
                     tenant_id=excluded.tenant_id,
                     telegram_user_id=excluded.telegram_user_id,
                     role=excluded.role,
                     is_verified=1,
                     last_active_at=excluded.last_active_at""",
                (tenant_id, chat_id, user_id, role, language, now, now),
            )
        user = self.get_user_for_chat(chat_id)
        assert user is not None
        return user

    def get_user_for_chat(self, chat_id: int) -> TenantUser | None:
        row = self.db.query_one("SELECT * FROM tenant_users WHERE telegram_chat_id=?", (chat_id,))
        if not row:
            return None
        return TenantUser(
            id=row["id"],
            tenant_id=row["tenant_id"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_user_id=row["telegram_user_id"],
            role=row["role"],
            is_verified=bool(row["is_verified"]),
            preferred_language=row["preferred_language"] or "en",
            last_active_at=row["last_active_at"],
            created_at=row["created_at"],
        )

    def set_user_language(self, chat_id: int, language: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE tenant_users SET preferred_language=? WHERE telegram_chat_id=?",
                (language, chat_id),
            )

    def get_store_repo(self, store_code: str) -> Repository:
        """Retrieve (or cache) an open Repository connection for an isolated tenant store database."""
        if store_code in self._repo_pool:
            return self._repo_pool[store_code]

        tenant = self.get_tenant_by_code(store_code)
        if not tenant:
            raise ValueError(f"No tenant found with store_code={store_code}")

        db_path = STORES_DIR / tenant.db_filename
        db = Database(db_path)
        repo = Repository(db)
        repo.init()
        self._repo_pool[store_code] = repo
        return repo

    def get_repo_for_chat(self, chat_id: int) -> Repository | None:
        """Resolve the tenant store repository for a given Telegram chat ID."""
        tenant = self.get_tenant_for_chat(chat_id)
        if not tenant:
            return None
        return self.get_store_repo(tenant.store_code)

    # --- In-Chat Telegram OTP Handling ---

    def create_otp(self, phone_e164: str, chat_id: int) -> str:
        """Generate a 6-digit cryptographic OTP, hash it, store with 5-minute TTL, and return code."""
        norm_phone = normalize_phone(phone_e164)
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        otp_hash = hashlib.sha256(code.encode()).hexdigest()

        now = datetime.now(IST)
        expires_at = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
        now_str = now.isoformat(timespec="seconds")

        with self.db.transaction() as conn:
            # Invalidate older active OTPs for this phone/chat
            conn.execute(
                "UPDATE auth_otps SET consumed=1 WHERE phone_e164=? AND telegram_chat_id=?",
                (norm_phone, chat_id),
            )
            conn.execute(
                """INSERT INTO auth_otps
                   (phone_e164, telegram_chat_id, otp_hash, attempts, expires_at, consumed, created_at)
                   VALUES (?, ?, ?, 0, ?, 0, ?)""",
                (norm_phone, chat_id, otp_hash, expires_at, now_str),
            )
            conn.execute(
                """INSERT INTO auth_audit (event_type, telegram_chat_id, phone_e164, details, created_at)
                   VALUES ('otp_issued', ?, ?, ?, ?)""",
                (chat_id, norm_phone, "In-chat Telegram OTP issued", now_str),
            )
        return code

    def verify_otp(self, phone_e164: str, chat_id: int, entered_code: str) -> tuple[bool, str]:
        """Verify an entered 6-digit OTP against the active challenge."""
        norm_phone = normalize_phone(phone_e164)
        clean_code = entered_code.strip()
        now_str = datetime.now(IST).isoformat(timespec="seconds")

        row = self.db.query_one(
            """SELECT * FROM auth_otps
               WHERE phone_e164=? AND telegram_chat_id=? AND consumed=0
               ORDER BY id DESC LIMIT 1""",
            (norm_phone, chat_id),
        )
        if not row:
            return False, "no_active_otp"

        if row["expires_at"] < now_str:
            with self.db.transaction() as conn:
                conn.execute("UPDATE auth_otps SET consumed=1 WHERE id=?", (row["id"],))
            return False, "expired"

        if row["attempts"] >= 3:
            with self.db.transaction() as conn:
                conn.execute("UPDATE auth_otps SET consumed=1 WHERE id=?", (row["id"],))
            return False, "max_attempts"

        entered_hash = hashlib.sha256(clean_code.encode()).hexdigest()
        if entered_hash == row["otp_hash"]:
            with self.db.transaction() as conn:
                conn.execute("UPDATE auth_otps SET consumed=1 WHERE id=?", (row["id"],))
                conn.execute(
                    """INSERT INTO auth_audit (event_type, telegram_chat_id, phone_e164, details, created_at)
                       VALUES ('otp_verified', ?, ?, 'OTP verified successfully', ?)""",
                    (chat_id, norm_phone, now_str),
                )
            return True, "success"

        # Incorrect code: increment attempt count
        with self.db.transaction() as conn:
            conn.execute("UPDATE auth_otps SET attempts = attempts + 1 WHERE id=?", (row["id"],))
        return False, "invalid_code"

    def _tenant_from_row(self, row) -> Tenant:
        return Tenant(
            id=row["id"],
            store_code=row["store_code"],
            store_name=row["store_name"],
            owner_name=row["owner_name"],
            phone_e164=row["phone_e164"],
            address=row["address"],
            gstin=row["gstin"],
            tagline=row["tagline"],
            db_filename=row["db_filename"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


_master_registry: MasterRegistry | None = None


def get_master_registry() -> MasterRegistry:
    """Singleton getter for MasterRegistry."""
    global _master_registry
    if _master_registry is None:
        _master_registry = MasterRegistry()
    return _master_registry
