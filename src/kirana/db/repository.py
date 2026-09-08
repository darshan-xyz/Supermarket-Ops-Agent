"""The repository: the single access layer between the tool layer and the database.
Business invariants are enforced here inside transactions.

Guardrails enforced here:
  * oversell        -> conditional UPDATE + rowcount check (stock can't go < 0)
  * idempotent bill -> UNIQUE idempotency_key; retried finalize returns the invoice
  * below-cost sale -> refused unless confirm_below_cost=True
  * khata integrity -> payment needs an existing customer; no silent overpay
  * no hard delete  -> products are deactivated / bills voided with reversals
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from ..agent.guardrails import (
    sanitize_customer_name,
    sanitize_employee_name,
    validate_date_range,
    validate_fssai_inward_expiry,
    validate_legal_metrology,
    validate_phone_number,
    validate_price_bounds,
    validate_quantity_bounds,
)
from ..domain import gst, money
from ..domain.models import (
    Batch,
    Bill,
    BillDetail,
    CartLine,
    CartView,
    Customer,
    CustomerHistory,
    CustomerPurchaseItem,
    DailyReport,
    Employee,
    ExpiringBatch,
    GstReport,
    ItemSales,
    KhataEntry,
    KhataState,
    Product,
    ReorderSuggestion,
    SalesReport,
    ShopProfile,
    StockHealth,
    StockInfo,
)
from ..domain.units import format_qty, normalize_unit, to_base
from ..security import hash_password, verify_password
from .connection import Database, now_iso, today_iso

VALID_PAYMENT_MODES = ("cash", "upi", "card", "khata")


# --------------------------------------------------------------------------- #
# Errors (tools translate these into is_error messages the model relays)
# --------------------------------------------------------------------------- #
class RepoError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ProductNotFound(RepoError):
    code = "product_not_found"


class CustomerNotFound(RepoError):
    code = "customer_not_found"


class InsufficientStock(RepoError):
    code = "insufficient_stock"


class BelowCostSale(RepoError):
    code = "below_cost"


class NeedsConfirmation(RepoError):
    code = "needs_confirmation"


class EmptyCart(RepoError):
    code = "empty_cart"


class ValidationError(RepoError):
    code = "validation_error"


# --------------------------------------------------------------------------- #
class Repository:
    def __init__(self, db: Database):
        self.db = db

    def init(self) -> None:
        self.db.init_schema()
        with self.db.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO shop_profile (id, updated_at) VALUES (1, ?)",
                (now_iso(),),
            )
            # Backfill a no-expiry batch for any pre-batch stock so that
            # SUM(batches.qty_base) == products.stock_base holds everywhere.
            legacy = c.execute(
                "SELECT id, stock_base, cost_paise FROM products WHERE stock_base > 0 "
                "AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.product_id = products.id)"
            ).fetchall()
            for p in legacy:
                c.execute(
                    "INSERT INTO batches (product_id, qty_base, expiry, cost_paise, note, "
                    "created_at) VALUES (?,?,NULL,?, 'opening balance', ?)",
                    (p["id"], p["stock_base"], p["cost_paise"], now_iso()),
                )

            # Ensure daily_seq exists on bills for legacy databases
            cols = [col["name"] for col in c.execute("PRAGMA table_info(bills)").fetchall()]
            if "daily_seq" not in cols:
                c.execute("ALTER TABLE bills ADD COLUMN daily_seq INTEGER DEFAULT 0")
                dates = [row["d"] for row in c.execute(
                    "SELECT DISTINCT substr(finalized_at, 1, 10) AS d FROM bills "
                    "WHERE status='finalized' AND finalized_at IS NOT NULL"
                ).fetchall()]
                for d in dates:
                    b_rows = c.execute(
                        "SELECT id FROM bills WHERE status='finalized' "
                        "AND substr(finalized_at, 1, 10) = ? ORDER BY finalized_at, id",
                        (d,)
                    ).fetchall()
                    for idx, br in enumerate(b_rows, 1):
                        c.execute("UPDATE bills SET daily_seq=? WHERE id=?", (idx, br["id"]))
            c.execute("CREATE INDEX IF NOT EXISTS idx_bills_date_seq ON bills(substr(finalized_at, 1, 10), daily_seq)")

            # Ensure billed_by and customer_phone exist on bills for legacy databases
            if "billed_by" not in cols:
                c.execute("ALTER TABLE bills ADD COLUMN billed_by TEXT")
            if "customer_phone" not in cols:
                c.execute("ALTER TABLE bills ADD COLUMN customer_phone TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bills_customer ON bills(customer_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bills_phone ON bills(customer_phone)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_bills_billed_by ON bills(billed_by)")
            cust_cols = [col["name"] for col in c.execute("PRAGMA table_info(customers)").fetchall()]
            if "phone" not in cust_cols:
                c.execute("ALTER TABLE customers ADD COLUMN phone TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone)")

            # Ensure logo_path and owner_pin_hash exist on shop_profile for legacy databases
            prof_cols = [col["name"] for col in c.execute("PRAGMA table_info(shop_profile)").fetchall()]
            if "logo_path" not in prof_cols:
                c.execute("ALTER TABLE shop_profile ADD COLUMN logo_path TEXT")
            if "owner_pin_hash" not in prof_cols:
                c.execute("ALTER TABLE shop_profile ADD COLUMN owner_pin_hash TEXT")

            # Ensure employees table exists for legacy databases
            c.execute("""CREATE TABLE IF NOT EXISTS employees (
              id             INTEGER PRIMARY KEY AUTOINCREMENT,
              emp_code       TEXT UNIQUE NOT NULL,
              name           TEXT NOT NULL,
              role           TEXT NOT NULL DEFAULT 'cashier',
              phone          TEXT,
              is_active      INTEGER NOT NULL DEFAULT 1,
              created_at     TEXT NOT NULL,
              deactivated_at TEXT
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_employees_code ON employees(emp_code)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_employees_name ON employees(name COLLATE NOCASE)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_employees_active ON employees(is_active)")

    # ------------------------------------------------------------------ #
    # Row -> dataclass mappers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _employee_from_row(r: sqlite3.Row) -> Employee:
        return Employee(
            id=r["id"],
            emp_code=r["emp_code"],
            name=r["name"],
            role=r["role"] if "role" in r.keys() and r["role"] else "cashier",
            phone=r["phone"] if "phone" in r.keys() else None,
            is_active=bool(r["is_active"]),
            created_at=r["created_at"],
            deactivated_at=r["deactivated_at"] if "deactivated_at" in r.keys() else None,
        )

    @staticmethod
    def _product(r: sqlite3.Row) -> Product:
        return Product(
            id=r["id"], name=r["name"], brand=r["brand"], aliases=r["aliases"],
            sku_code=r["sku_code"], hsn_code=r["hsn_code"], unit=r["unit"],
            is_loose=bool(r["is_loose"]), gst_rate=r["gst_rate"],
            cost_paise=r["cost_paise"], mrp_paise=r["mrp_paise"],
            sell_paise=r["sell_paise"], stock_base=r["stock_base"],
            reorder_base=r["reorder_base"], active=bool(r["active"]),
        )

    @staticmethod
    def _customer(r: sqlite3.Row) -> Customer:
        return Customer(id=r["id"], name=r["name"], phone=r["phone"])

    # ------------------------------------------------------------------ #
    # Batches / FEFO helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_expiry(expiry: str | None) -> str | None:
        if not expiry:
            return None
        try:
            return date.fromisoformat(expiry.strip()).isoformat()
        except ValueError:
            raise ValidationError(f"Expiry must be YYYY-MM-DD, got '{expiry}'.")

    def _add_batch(self, c: sqlite3.Connection, product_id: int, qty_base: int,
                   cost_paise: int | None, expiry: str | None, note: str | None) -> None:
        c.execute(
            "INSERT INTO batches (product_id, qty_base, expiry, cost_paise, note, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (product_id, qty_base, expiry, cost_paise, note, now_iso()))

    def _consume_batches_fefo(self, c: sqlite3.Connection, product_id: int, qty_base: int) -> None:
        """Draw qty from batches, earliest expiry first (NULL expiry last). The
        stock_base guard has already passed, so the batches always cover qty."""
        remaining = qty_base
        rows = c.execute(
            "SELECT id, qty_base FROM batches WHERE product_id=? AND qty_base > 0 "
            "ORDER BY (expiry IS NULL), expiry ASC, id ASC", (product_id,)).fetchall()
        for b in rows:
            if remaining <= 0:
                break
            take = min(remaining, b["qty_base"])
            c.execute("UPDATE batches SET qty_base = qty_base - ? WHERE id=?", (take, b["id"]))
            remaining -= take

    def expiring_soon(self, within_days: int = 7) -> list[ExpiringBatch]:
        today = date.today()
        cutoff = (today + timedelta(days=within_days)).isoformat()
        rows = self.db.query_all(
            """SELECT b.qty_base, b.expiry, p.name, p.unit
               FROM batches b JOIN products p ON p.id = b.product_id
               WHERE b.qty_base > 0 AND b.expiry IS NOT NULL AND b.expiry <= ?
               ORDER BY b.expiry ASC""", (cutoff,))
        out = []
        for r in rows:
            days_left = (date.fromisoformat(r["expiry"]) - today).days
            out.append(ExpiringBatch(
                name=r["name"], qty_display=format_qty(r["qty_base"], r["unit"]),
                expiry=r["expiry"], days_left=days_left))
        return out

    def product_batches(self, product_id: int) -> list[Batch]:
        rows = self.db.query_all(
            "SELECT * FROM batches WHERE product_id=? AND qty_base > 0 "
            "ORDER BY (expiry IS NULL), expiry ASC, id ASC", (product_id,))
        return [Batch(id=r["id"], product_id=r["product_id"], qty_base=r["qty_base"],
                      expiry=r["expiry"], cost_paise=r["cost_paise"]) for r in rows]

    # ================================================================== #
    # INVENTORY
    # ================================================================== #
    def add_product(
        self, *, name: str, unit: str, gst_rate: int, mrp_paise: int,
        sell_paise: int, cost_paise: int = 0, brand: str | None = None,
        hsn_code: str | None = None, is_loose: bool = False,
        aliases: str | None = None, sku_code: str | None = None,
        opening_stock_base: int = 0, reorder_base: int = 0, expiry: str | None = None,
    ) -> Product:
        unit = normalize_unit(unit)
        if gst_rate not in gst.VALID_SLABS:
            raise ValidationError(f"GST slab must be one of {gst.VALID_SLABS}.")
        try:
            validate_price_bounds(sell_paise, "Selling price")
            validate_price_bounds(mrp_paise, "MRP")
            validate_legal_metrology(sell_paise, mrp_paise)
            if opening_stock_base < 0:
                raise ValidationError("Opening stock cannot be negative.")
            if opening_stock_base > 0:
                validate_quantity_bounds(opening_stock_base, unit)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        expiry = self._parse_expiry(expiry)
        ts = now_iso()
        clean_name = re.sub(r"\s+", " ", name.strip())
        with self.db.transaction() as c:
            existing = c.execute(
                "SELECT id, name, unit, stock_base, sell_paise FROM products WHERE lower(trim(name)) = ? AND active = 1",
                (clean_name.lower(),),
            ).fetchone()
            if existing:
                stock_disp = format_qty(existing["stock_base"], existing["unit"])
                sell_disp = money.format_inr(existing["sell_paise"])
                raise ValidationError(
                    f"Product '{clean_name}' already exists in your store catalogue "
                    f"(ID #{existing['id']}, Stock: {stock_disp}, Selling Price: {sell_disp}). "
                    f"To add incoming inventory, use receive_stock. To update prices or details, use update_product."
                )
            cur = c.execute(
                """INSERT INTO products
                   (name, brand, aliases, sku_code, hsn_code, unit, is_loose,
                    gst_rate, cost_paise, mrp_paise, sell_paise, stock_base,
                    reorder_base, active, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (name, brand, (aliases or "").lower(), sku_code, hsn_code, unit,
                 int(is_loose), gst_rate, cost_paise, mrp_paise, sell_paise,
                 opening_stock_base, reorder_base, ts, ts),
            )
            pid = cur.lastrowid
            if opening_stock_base:
                c.execute(
                    """INSERT INTO stock_movements
                       (product_id, delta_base, reason, cost_paise, note, created_at)
                       VALUES (?,?,'stock_in',?, 'opening stock', ?)""",
                    (pid, opening_stock_base, cost_paise, ts),
                )
                self._add_batch(c, pid, opening_stock_base, cost_paise, expiry, "opening stock")
        return self.get_product(pid)  # type: ignore[return-value]

    def receive_stock(
        self, product_id: int, qty_base: int, *, cost_paise: int | None = None,
        mrp_paise: int | None = None, sell_paise: int | None = None,
        note: str | None = None, expiry: str | None = None,
    ) -> Product:
        try:
            validate_quantity_bounds(qty_base, "units")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        expiry = self._parse_expiry(expiry)
        if expiry:
            try:
                validate_fssai_inward_expiry(expiry, today_iso())
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if row is None:
                raise ProductNotFound(f"No product with id {product_id}.")
            eff_sell = sell_paise if sell_paise is not None else row["sell_paise"]
            eff_mrp = mrp_paise if mrp_paise is not None else row["mrp_paise"]
            try:
                validate_price_bounds(eff_sell, "Selling price")
                validate_price_bounds(eff_mrp, "MRP")
                validate_legal_metrology(eff_sell, eff_mrp)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            sets = ["stock_base = stock_base + ?", "updated_at = ?"]
            params: list = [qty_base, now_iso()]
            if cost_paise is not None:
                sets.append("cost_paise = ?"); params.append(cost_paise)
            if mrp_paise is not None:
                sets.append("mrp_paise = ?"); params.append(mrp_paise)
            if sell_paise is not None:
                sets.append("sell_paise = ?"); params.append(sell_paise)
            params.append(product_id)
            c.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", params)
            batch_cost = cost_paise if cost_paise is not None else row["cost_paise"]
            c.execute(
                """INSERT INTO stock_movements
                   (product_id, delta_base, reason, cost_paise, note, created_at)
                   VALUES (?,?,'stock_in',?,?,?)""",
                (product_id, qty_base, batch_cost, note, now_iso()),
            )
            self._add_batch(c, product_id, qty_base, batch_cost, expiry, note)
        return self.get_product(product_id)  # type: ignore[return-value]

    def update_product(
        self,
        product_id: int,
        *,
        name: str | None = None,
        sell_paise: int | None = None,
        mrp_paise: int | None = None,
        cost_paise: int | None = None,
        reorder_base: int | None = None,
        brand: str | None = None,
        aliases: str | None = None,
        hsn_code: str | None = None,
        gst_rate: int | None = None,
        active: bool | None = None,
    ) -> Product:
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if row is None:
                raise ProductNotFound(f"No product with id {product_id}.")

            eff_sell = sell_paise if sell_paise is not None else row["sell_paise"]
            eff_mrp = mrp_paise if mrp_paise is not None else row["mrp_paise"]
            try:
                validate_price_bounds(eff_sell, "Selling price")
                validate_price_bounds(eff_mrp, "MRP")
                validate_legal_metrology(eff_sell, eff_mrp)
                if cost_paise is not None:
                    validate_price_bounds(cost_paise, "Cost price")
                if reorder_base is not None and reorder_base < 0:
                    raise ValidationError("Reorder level cannot be negative.")
                if gst_rate is not None and gst_rate not in gst.VALID_SLABS:
                    raise ValidationError(f"GST slab must be one of {gst.VALID_SLABS}.")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            sets: list[str] = ["updated_at = ?"]
            params: list = [now_iso()]
            if name is not None:
                sets.append("name = ?"); params.append(name.strip())
            if sell_paise is not None:
                sets.append("sell_paise = ?"); params.append(sell_paise)
            if mrp_paise is not None:
                sets.append("mrp_paise = ?"); params.append(mrp_paise)
            if cost_paise is not None:
                sets.append("cost_paise = ?"); params.append(cost_paise)
            if reorder_base is not None:
                sets.append("reorder_base = ?"); params.append(reorder_base)
            if brand is not None:
                sets.append("brand = ?"); params.append(brand.strip())
            if aliases is not None:
                sets.append("aliases = ?"); params.append(aliases.strip().lower())
            if hsn_code is not None:
                sets.append("hsn_code = ?"); params.append(hsn_code.strip())
            if gst_rate is not None:
                sets.append("gst_rate = ?"); params.append(gst_rate)
            if active is not None:
                sets.append("active = ?"); params.append(int(active))

            params.append(product_id)
            c.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", params)

        prod = self.get_product(product_id)
        if prod is None:
            raise ProductNotFound(f"No product with id {product_id}.")
        return prod

    def adjust_stock(
        self,
        product_id: int,
        delta_base: int,
        *,
        reason: str = "audit_correction",
        note: str | None = None,
    ) -> Product:
        """Adjust product stock by delta_base (positive to increase, negative to decrease).

        Ensures stock does not drop below zero, keeps batch quantities aligned with
        FEFO consumption on reduction, and records the movement with reason in stock_movements.
        """
        if delta_base == 0:
            prod = self.get_product(product_id)
            if prod is None:
                raise ProductNotFound(f"No product with id {product_id}.")
            return prod

        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if row is None:
                raise ProductNotFound(f"No product with id {product_id}.")

            current_stock = row["stock_base"]
            new_stock = current_stock + delta_base
            if new_stock < 0:
                prod_unit = row["unit"]
                cur_display = format_qty(current_stock, prod_unit)
                needed_display = format_qty(abs(delta_base), prod_unit)
                raise InsufficientStock(
                    f"Cannot reduce stock of '{row['name']}' by {needed_display}: current stock is only {cur_display}."
                )

            c.execute(
                "UPDATE products SET stock_base = stock_base + ?, updated_at = ? WHERE id = ?",
                (delta_base, now_iso(), product_id),
            )

            # Keep batches aligned with product stock
            if delta_base < 0:
                self._consume_batches_fefo(c, product_id, abs(delta_base))
            else:
                self._add_batch(c, product_id, delta_base, row["cost_paise"], None, note)

            movement_note = f"Stock adjustment ({reason}): {note}" if note else f"Stock adjustment ({reason})"
            c.execute(
                """INSERT INTO stock_movements
                   (product_id, delta_base, reason, cost_paise, note, created_at)
                   VALUES (?,?,'adjustment',?,?,?)""",
                (product_id, delta_base, row["cost_paise"], movement_note, now_iso()),
            )

        prod = self.get_product(product_id)
        if prod is None:
            raise ProductNotFound(f"No product with id {product_id}.")
        return prod

    def get_product(self, product_id: int) -> Product | None:
        r = self.db.query_one("SELECT * FROM products WHERE id=?", (product_id,))
        return self._product(r) if r else None

    def search_products(self, query: str, limit: int = 10) -> list[Product]:
        clean = query.strip().lower()
        q = f"%{clean}%"
        comma_exact = f"%,{clean},%"
        prefix = f"{clean}%"
        comma_prefix = f"%,{clean}%"
        rows = self.db.query_all(
            """SELECT * FROM products
               WHERE active=1 AND (
                   lower(name) LIKE ? OR lower(coalesce(brand,'')) LIKE ?
                   OR lower(aliases) LIKE ? OR lower(coalesce(sku_code,'')) LIKE ?)
               ORDER BY
                   CASE
                       WHEN lower(name) = ? OR (',' || lower(aliases) || ',') LIKE ? THEN 1
                       WHEN lower(name) LIKE ? OR (',' || lower(aliases) || ',') LIKE ? THEN 2
                       ELSE 3
                   END,
                   name LIMIT ?""",
            (q, q, q, q, clean, comma_exact, prefix, comma_prefix, limit),
        )
        return [self._product(r) for r in rows]

    def list_inventory(self, active_only: bool = True) -> list[Product]:
        where = "WHERE active=1" if active_only else ""
        rows = self.db.query_all(f"SELECT * FROM products {where} ORDER BY name ASC")
        return [self._product(r) for r in rows]

    def get_stock(self, product_id: int) -> StockInfo:
        p = self.get_product(product_id)
        if p is None:
            raise ProductNotFound(f"No product with id {product_id}.")
        return StockInfo(
            product=p, stock_display=p.stock_display,
            is_low=p.stock_base <= p.reorder_base, is_out=p.stock_base <= 0,
        )

    def list_low_stock(self) -> list[Product]:
        rows = self.db.query_all(
            "SELECT * FROM products WHERE active=1 AND stock_base <= reorder_base "
            "ORDER BY (stock_base - reorder_base) ASC"
        )
        return [self._product(r) for r in rows]

    def sales_velocity(self, product_id: int, days: int = 14) -> float:
        row = self.db.query_one(
            """SELECT COALESCE(SUM(-delta_base),0) AS sold
               FROM stock_movements
               WHERE product_id=? AND reason='sale'
                 AND created_at >= date('now', ?, '+5 hours', '+30 minutes')""",
            (product_id, f"-{days} days"),
        )
        sold_base = row["sold"] if row else 0
        return float(sold_base) / days if days else 0.0

    def reorder_suggestions(self, cover_days: int = 7, lookback_days: int = 14
                            ) -> list[ReorderSuggestion]:
        out: list[ReorderSuggestion] = []
        for p in [self._product(r) for r in
                  self.db.query_all("SELECT * FROM products WHERE active=1")]:
            vel = self.sales_velocity(p.id, lookback_days)
            cover = (p.stock_base / vel / 1000) if vel > 0 else float("inf")
            below_reorder = p.stock_base <= p.reorder_base
            if vel <= 0 and not below_reorder:
                continue
            if vel > 0 and cover >= cover_days and not below_reorder:
                continue
            target_base = int(vel * cover_days) if vel > 0 else max(p.reorder_base, 0)
            suggest_base = max(target_base - p.stock_base, 0)
            if suggest_base <= 0 and below_reorder:
                suggest_base = max(p.reorder_base - p.stock_base, 0) or p.reorder_base
            if suggest_base <= 0:
                continue
            reason = (
                f"{format_qty(int(vel), p.unit)}/day sold; "
                f"{cover:.1f} days cover left" if vel > 0 else "below reorder level"
            )
            out.append(ReorderSuggestion(
                product=p, velocity_per_day=vel / 1000,
                days_of_cover=cover if vel > 0 else 0.0,
                suggested_qty_display=format_qty(suggest_base, p.unit), reason=reason,
            ))
        return out

    # ================================================================== #
    # CART / BILLING
    # ================================================================== #
    def cart_get_or_create(self, chat_id: int) -> Bill:
        r = self.db.query_one(
            "SELECT * FROM bills WHERE chat_id=? AND status='draft' "
            "ORDER BY id DESC LIMIT 1", (chat_id,))
        if r is None:
            with self.db.transaction() as c:
                cur = c.execute(
                    "INSERT INTO bills (chat_id, status, billed_by, created_at) VALUES (?, 'draft', NULL, ?)",
                    (chat_id, now_iso()))
                bid = cur.lastrowid
            r = self.db.query_one("SELECT * FROM bills WHERE id=?", (bid,))
        return self._bill_from_row(r)

    def _draft_row(self, chat_id: int) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM bills WHERE chat_id=? AND status='draft' "
            "ORDER BY id DESC LIMIT 1", (chat_id,))

    def cart_add_item(self, chat_id: int, product_id: int, qty_display,
                      unit: str | None = None) -> CartView:
        return self._cart_mutate(chat_id, product_id, qty_display, unit, mode="add")

    def cart_update_item(self, chat_id: int, product_id: int, qty_display,
                         unit: str | None = None) -> CartView:
        return self._cart_mutate(chat_id, product_id, qty_display, unit, mode="set")

    def cart_remove_item(self, chat_id: int, product_id: int) -> CartView:
        bill = self.cart_get_or_create(chat_id)
        with self.db.transaction() as c:
            c.execute("DELETE FROM bill_items WHERE bill_id=? AND product_id=?",
                      (bill.id, product_id))
        return self.cart_view(chat_id)

    def _cart_mutate(self, chat_id: int, product_id: int, qty_display,
                     unit: str | None, mode: str) -> CartView:
        bill = self.cart_get_or_create(chat_id)
        p = self.get_product(product_id)
        if p is None:
            raise ProductNotFound(f"No product with id {product_id}.")
        add_base = to_base(qty_display, unit, p.unit)
        if mode == "add" and add_base <= 0:
            raise ValidationError("Quantity to add must be strictly positive.")
        try:
            if add_base > 0:
                validate_quantity_bounds(add_base, p.unit)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        with self.db.transaction() as c:
            existing = c.execute(
                "SELECT qty_base FROM bill_items WHERE bill_id=? AND product_id=?",
                (bill.id, product_id)).fetchone()
            if mode == "add" and existing:
                new_base = existing["qty_base"] + add_base
            else:
                new_base = add_base
            if new_base <= 0:
                c.execute("DELETE FROM bill_items WHERE bill_id=? AND product_id=?",
                          (bill.id, product_id))
            else:
                lt = gst.line_total_paise(p.sell_paise, new_base)
                if lt < 100:
                    raise ValidationError(
                        f"Line item amount for '{p.name}' is too small ({money.format_inr(lt)}). "
                        "Minimum billable amount per line item is ₹1.00."
                    )
                tax = gst.compute_line_tax(lt, p.gst_rate)
                c.execute(
                    """INSERT INTO bill_items
                       (bill_id, product_id, name_snapshot, hsn_snapshot, qty_base,
                        unit, unit_sell_paise, gst_rate, taxable_paise, cgst_paise,
                        sgst_paise, line_total_paise)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(bill_id, product_id) DO UPDATE SET
                         qty_base=excluded.qty_base,
                         unit_sell_paise=excluded.unit_sell_paise,
                         gst_rate=excluded.gst_rate,
                         taxable_paise=excluded.taxable_paise,
                         cgst_paise=excluded.cgst_paise,
                         sgst_paise=excluded.sgst_paise,
                         line_total_paise=excluded.line_total_paise""",
                    (bill.id, product_id, p.name, p.hsn_code, new_base, p.unit,
                     p.sell_paise, p.gst_rate, tax.taxable_paise, tax.cgst_paise,
                     tax.sgst_paise, tax.line_total_paise),
                )
        return self.cart_view(chat_id)

    def cart_set_payment(self, chat_id: int, mode: str, ref: str | None = None) -> CartView:
        mode = mode.strip().lower()
        if mode not in VALID_PAYMENT_MODES:
            raise ValidationError(f"Payment mode must be one of {VALID_PAYMENT_MODES}.")
        bill = self.cart_get_or_create(chat_id)
        with self.db.transaction() as c:
            c.execute("UPDATE bills SET payment_mode=?, payment_ref=? WHERE id=?",
                      (mode, ref, bill.id))
        return self.cart_view(chat_id)

    def cart_set_biller(self, chat_id: int, employee_name: str) -> CartView:
        matched = self.get_employee(employee_name, active_only=True)
        if matched:
            display_name = matched.name
        else:
            display_name = sanitize_employee_name(employee_name)
        bill = self.cart_get_or_create(chat_id)
        with self.db.transaction() as c:
            c.execute("UPDATE bills SET billed_by=? WHERE id=?", (display_name, bill.id))
        self.set_preference("active_cashier", display_name)
        return self.cart_view(chat_id)

    def cart_set_customer(self, chat_id: int, customer_name: str, phone: str | None = None) -> CartView:
        cust = self.find_or_create_customer(customer_name, phone=phone)
        bill = self.cart_get_or_create(chat_id)
        with self.db.transaction() as c:
            c.execute("UPDATE bills SET customer_id=?, customer_phone=? WHERE id=?",
                      (cust.id, cust.phone, bill.id))
        return self.cart_view(chat_id)

    def cart_clear(self, chat_id: int) -> None:
        row = self._draft_row(chat_id)
        if row:
            with self.db.transaction() as c:
                c.execute("DELETE FROM bills WHERE id=?", (row["id"],))

    def cart_view(self, chat_id: int) -> CartView:
        row = self._draft_row(chat_id)
        if row is None:
            row = self.db.query_one(
                "SELECT * FROM bills WHERE id=?", (self.cart_get_or_create(chat_id).id,))
        bill = self._bill_from_row(row)
        item_rows = self.db.query_all(
            "SELECT * FROM bill_items WHERE bill_id=? ORDER BY id", (bill.id,))
        lines: list[CartLine] = []
        warnings: list[str] = []
        taxes = []
        for ir in item_rows:
            p = self.get_product(ir["product_id"])
            avail = p.stock_base if p else 0
            shortfall = ir["qty_base"] > avail
            if shortfall and p:
                warnings.append(
                    f"Only {format_qty(avail, p.unit)} of {p.name} in stock "
                    f"(cart wants {format_qty(ir['qty_base'], p.unit)}).")
            lines.append(CartLine(
                product_id=ir["product_id"], name=ir["name_snapshot"], unit=ir["unit"],
                qty_base=ir["qty_base"], qty_display=format_qty(ir["qty_base"], ir["unit"]),
                unit_sell_paise=ir["unit_sell_paise"], gst_rate=ir["gst_rate"],
                line_total_paise=ir["line_total_paise"], taxable_paise=ir["taxable_paise"],
                cgst_paise=ir["cgst_paise"], sgst_paise=ir["sgst_paise"],
                available_base=avail, shortfall=shortfall,
            ))
            taxes.append(gst.LineTax(ir["gst_rate"], ir["line_total_paise"],
                                     ir["taxable_paise"], ir["cgst_paise"], ir["sgst_paise"]))
        totals = gst.summarize(taxes)
        biller_confirmed = bool(bill.biller_name)
        active_biller = bill.biller_name or self.get_preference("active_cashier") or self.get_preference("last_biller")
        return CartView(
            bill_id=bill.id, chat_id=chat_id, status=bill.status,
            customer_name=bill.customer_name, payment_mode=bill.payment_mode,
            payment_ref=bill.payment_ref, lines=lines,
            subtotal_paise=totals.subtotal_paise, cgst_paise=totals.cgst_paise,
            sgst_paise=totals.sgst_paise, round_off_paise=totals.round_off_paise,
            grand_total_paise=totals.grand_total_paise, warnings=warnings,
            biller_name=active_biller, customer_phone=bill.customer_phone,
            biller_confirmed=biller_confirmed,
        )

    def finalize_bill(self, chat_id: int, idempotency_key: str,
                      confirm_below_cost: bool = False) -> Bill:
        # Idempotency: a prior finalize with this key already produced an invoice.
        prior = self.db.query_one(
            "SELECT * FROM bills WHERE idempotency_key=?", (idempotency_key,))
        if prior is not None:
            b = self._bill_from_row(prior)
            b.was_retry = True
            return b

        with self.db.transaction() as c:
            row = c.execute(
                "SELECT * FROM bills WHERE chat_id=? AND status='draft' "
                "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
            if row is None:
                raise EmptyCart("There's no open bill to finalize.")
            bill_id = row["id"]
            items = c.execute(
                "SELECT * FROM bill_items WHERE bill_id=?", (bill_id,)).fetchall()
            if not items:
                raise EmptyCart("The bill has no items yet.")

            mode = row["payment_mode"]
            if not mode:
                raise ValidationError(
                    "Set a payment mode (cash/UPI/card/khata) before finalizing.")

            # below-cost guard
            below = []
            for it in items:
                prod = c.execute("SELECT name, cost_paise FROM products WHERE id=?",
                                 (it["product_id"],)).fetchone()
                if prod and it["unit_sell_paise"] < prod["cost_paise"]:
                    below.append(prod["name"])
            if below and not confirm_below_cost:
                raise BelowCostSale(
                    "Selling below cost: " + ", ".join(below) +
                    ". Confirm to proceed (this loses money).")

            # khata needs a customer
            if mode == "khata" and row["customer_id"] is None:
                raise ValidationError(
                    "Khata sale needs a customer name — whose credit is this?")

            # atomic stock decrement (oversell guard)
            taxes = []
            for it in items:
                res = c.execute(
                    "UPDATE products SET stock_base = stock_base - ?, updated_at=? "
                    "WHERE id=? AND stock_base >= ?",
                    (it["qty_base"], now_iso(), it["product_id"], it["qty_base"]))
                if res.rowcount == 0:
                    prod = c.execute("SELECT name, unit, stock_base FROM products WHERE id=?",
                                     (it["product_id"],)).fetchone()
                    raise InsufficientStock(
                        f"Not enough {prod['name']}: have "
                        f"{format_qty(prod['stock_base'], prod['unit'])}, "
                        f"need {format_qty(it['qty_base'], it['unit'])}.")
                self._consume_batches_fefo(c, it["product_id"], it["qty_base"])
                c.execute(
                    """INSERT INTO stock_movements
                       (product_id, delta_base, reason, ref_type, ref_id, created_at)
                       VALUES (?,?,'sale','bill',?,?)""",
                    (it["product_id"], -it["qty_base"], bill_id, now_iso()))
                taxes.append(gst.LineTax(it["gst_rate"], it["line_total_paise"],
                                         it["taxable_paise"], it["cgst_paise"],
                                         it["sgst_paise"]))

            totals = gst.summarize(taxes)
            ts = now_iso()
            date_str = ts[:10]
            daily_seq, invoice_no = self._next_invoice_no(c, date_str)

            biller = row["billed_by"] if ("billed_by" in row.keys() and row["billed_by"]) else (self.get_preference("active_cashier") or self.get_preference("last_biller"))
            phone = row["customer_phone"] if ("customer_phone" in row.keys() and row["customer_phone"]) else None
            if not phone and row["customer_id"]:
                cr = c.execute("SELECT phone FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
                if cr and cr["phone"]:
                    phone = cr["phone"]

            c.execute(
                """UPDATE bills SET status='finalized', invoice_no=?, daily_seq=?, subtotal_paise=?,
                   cgst_paise=?, sgst_paise=?, round_off_paise=?, total_paise=?,
                   idempotency_key=?, finalized_at=?, billed_by=?, customer_phone=? WHERE id=?""",
                (invoice_no, daily_seq, totals.subtotal_paise, totals.cgst_paise, totals.sgst_paise,
                 totals.round_off_paise, totals.grand_total_paise, idempotency_key,
                 ts, biller, phone, bill_id))

            if mode == "khata":
                c.execute(
                    """INSERT INTO khata_entries
                       (customer_id, entry_type, amount_paise, note, bill_id, created_at)
                       VALUES (?, 'debit', ?, ?, ?, ?)""",
                    (row["customer_id"], totals.grand_total_paise,
                     f"Bill {invoice_no}", bill_id, ts))

        return self.get_bill(bill_id).bill

    def _next_invoice_no(self, c: sqlite3.Connection, date_str: str) -> tuple[int, str]:
        prof = c.execute("SELECT invoice_prefix FROM shop_profile WHERE id=1").fetchone()
        prefix = (prof["invoice_prefix"] if prof else "INV") or "INV"
        n = c.execute(
            "SELECT COUNT(*) AS n FROM bills WHERE status='finalized' AND substr(finalized_at, 1, 10) = ?",
            (date_str,)
        ).fetchone()["n"]
        daily_seq = n + 1
        date_slug = date_str.replace("-", "")
        invoice_no = f"{prefix}-{date_slug}-{daily_seq:04d}"
        return daily_seq, invoice_no

    def _bill_from_row(self, r: sqlite3.Row) -> Bill:
        cust_name = None
        phone = r["customer_phone"] if ("customer_phone" in r.keys() and r["customer_phone"]) else None
        if r["customer_id"]:
            cr = self.db.query_one("SELECT name, phone FROM customers WHERE id=?", (r["customer_id"],))
            if cr:
                cust_name = cr["name"]
                if not phone:
                    phone = cr["phone"]
        daily_seq = r["daily_seq"] if ("daily_seq" in r.keys() and r["daily_seq"]) else (r["id"] or 1)
        billed_by = r["billed_by"] if ("billed_by" in r.keys() and r["billed_by"]) else None
        return Bill(
            id=r["id"], invoice_no=r["invoice_no"], status=r["status"], chat_id=r["chat_id"],
            customer_name=cust_name, payment_mode=r["payment_mode"], payment_ref=r["payment_ref"],
            subtotal_paise=r["subtotal_paise"], cgst_paise=r["cgst_paise"],
            sgst_paise=r["sgst_paise"], round_off_paise=r["round_off_paise"],
            total_paise=r["total_paise"], created_at=r["created_at"],
            finalized_at=r["finalized_at"], daily_seq=daily_seq,
            biller_name=billed_by, customer_phone=phone,
        )

    def get_bill(self, bill_id: int) -> BillDetail:
        r = self.db.query_one("SELECT * FROM bills WHERE id=?", (bill_id,))
        if r is None:
            raise ValidationError(f"No bill with id {bill_id}.")
        bill = self._bill_from_row(r)
        item_rows = self.db.query_all(
            "SELECT * FROM bill_items WHERE bill_id=? ORDER BY id", (bill_id,))
        lines, taxes = [], []
        for ir in item_rows:
            lines.append(CartLine(
                product_id=ir["product_id"], name=ir["name_snapshot"], unit=ir["unit"],
                qty_base=ir["qty_base"], qty_display=format_qty(ir["qty_base"], ir["unit"]),
                unit_sell_paise=ir["unit_sell_paise"], gst_rate=ir["gst_rate"],
                line_total_paise=ir["line_total_paise"], taxable_paise=ir["taxable_paise"],
                cgst_paise=ir["cgst_paise"], sgst_paise=ir["sgst_paise"],
                available_base=0, shortfall=False))
            taxes.append(gst.LineTax(ir["gst_rate"], ir["line_total_paise"],
                                     ir["taxable_paise"], ir["cgst_paise"], ir["sgst_paise"]))
        totals = gst.summarize(taxes)
        slabs = [{"rate": s.rate, "taxable_paise": s.taxable_paise,
                  "cgst_paise": s.cgst_paise, "sgst_paise": s.sgst_paise}
                 for s in totals.slabs]
        return BillDetail(bill=bill, lines=lines, slabs=slabs)

    def get_bill_by_invoice(self, invoice_no: str) -> BillDetail | None:
        r = self.db.query_one("SELECT id FROM bills WHERE invoice_no = ? COLLATE NOCASE",
                              (invoice_no.strip(),))
        return self.get_bill(r["id"]) if r else None

    def get_bill_by_date_and_seq(self, date_str: str, seq: int) -> BillDetail | None:
        """Find bill for a specific date (YYYY-MM-DD) with daily_seq = seq."""
        r = self.db.query_one(
            "SELECT id FROM bills WHERE status='finalized' AND substr(finalized_at, 1, 10) = ? "
            "AND (daily_seq = ? OR (daily_seq = 0 AND id = ?)) ORDER BY id LIMIT 1",
            (date_str.strip(), seq, seq),
        )
        return self.get_bill(r["id"]) if r else None

    def find_bills_by_seq(self, seq: int) -> list[BillDetail]:
        """Find all finalized bills across any date with daily_seq = seq."""
        rows = self.db.query_all(
            "SELECT id FROM bills WHERE status='finalized' "
            "AND (daily_seq = ? OR (daily_seq = 0 AND id = ?)) ORDER BY id DESC",
            (seq, seq),
        )
        return [self.get_bill(r["id"]) for r in rows]

    def upcoming_daily_seq(self) -> int:
        today = today_iso()
        n = self.db.query_one(
            "SELECT COUNT(*) AS n FROM bills WHERE status='finalized' AND substr(finalized_at, 1, 10) = ?",
            (today,),
        )["n"]
        return n + 1

    def latest_finalized_bill_id(self, chat_id: int | None = None) -> int | None:
        if chat_id is None:
            r = self.db.query_one(
                "SELECT id FROM bills WHERE status='finalized' ORDER BY id DESC LIMIT 1")
        else:
            r = self.db.query_one(
                "SELECT id FROM bills WHERE status='finalized' AND chat_id=? "
                "ORDER BY id DESC LIMIT 1", (chat_id,))
        return r["id"] if r else None

    def void_bill(self, bill_id: int) -> Bill:
        with self.db.transaction() as c:
            r = c.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
            if r is None:
                raise ValidationError(f"No bill with id {bill_id}.")
            if r["status"] != "finalized":
                raise ValidationError("Only finalized bills can be voided.")
            for it in c.execute("SELECT * FROM bill_items WHERE bill_id=?", (bill_id,)).fetchall():
                c.execute("UPDATE products SET stock_base = stock_base + ? WHERE id=?",
                          (it["qty_base"], it["product_id"]))
                self._add_batch(c, it["product_id"], it["qty_base"], None, None,
                                f"void {r['invoice_no']}")
                c.execute(
                    """INSERT INTO stock_movements
                       (product_id, delta_base, reason, ref_type, ref_id, created_at)
                       VALUES (?,?,'void_reversal','bill',?,?)""",
                    (it["product_id"], it["qty_base"], bill_id, now_iso()))
            if r["payment_mode"] == "khata" and r["customer_id"]:
                c.execute(
                    """INSERT INTO khata_entries
                       (customer_id, entry_type, amount_paise, note, bill_id, created_at)
                       VALUES (?, 'credit', ?, ?, ?, ?)""",
                    (r["customer_id"], r["total_paise"],
                     f"Void {r['invoice_no']}", bill_id, now_iso()))
            c.execute("UPDATE bills SET status='void' WHERE id=?", (bill_id,))
        return self.get_bill(bill_id).bill

    # ================================================================== #
    # KHATA
    # ================================================================== #
    def find_or_create_customer(self, name: str, phone: str | None = None) -> Customer:
        try:
            name = sanitize_customer_name(name)
            phone = validate_phone_number(phone)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        # 1. If phone is provided, phone is the unique primary anchor
        if phone:
            r = self.db.query_one("SELECT * FROM customers WHERE phone = ?", (phone,))
            if r:
                if r["name"].lower() != name.lower():
                    with self.db.transaction() as c:
                        try:
                            c.execute("UPDATE customers SET name=? WHERE id=?", (name, r["id"]))
                        except sqlite3.IntegrityError:
                            pass
                return self._customer(self.db.query_one("SELECT * FROM customers WHERE id=?", (r["id"],)))

            # If not found by phone, check if a customer with this name exists without a phone
            r_name = self.db.query_one("SELECT * FROM customers WHERE name = ? COLLATE NOCASE", (name,))
            if r_name:
                if not r_name["phone"]:
                    with self.db.transaction() as c:
                        c.execute("UPDATE customers SET phone=? WHERE id=?", (phone, r_name["id"]))
                    return self._customer(self.db.query_one("SELECT * FROM customers WHERE id=?", (r_name["id"],)))
                else:
                    # Disambiguate name so insert doesn't fail unique(name) constraint
                    name = f"{name} ({phone[-4:]})"

            with self.db.transaction() as c:
                cur = c.execute("INSERT INTO customers (name, phone, created_at) VALUES (?,?,?)",
                                (name, phone, now_iso()))
                cid = cur.lastrowid
            return self._customer(self.db.query_one("SELECT * FROM customers WHERE id=?", (cid,)))

        # 2. No phone provided (walk-in or legacy)
        r = self.db.query_one("SELECT * FROM customers WHERE name = ? COLLATE NOCASE", (name,))
        if r:
            return self._customer(r)
        with self.db.transaction() as c:
            cur = c.execute("INSERT INTO customers (name, phone, created_at) VALUES (?,?,?)",
                            (name, None, now_iso()))
            cid = cur.lastrowid
        return self._customer(self.db.query_one("SELECT * FROM customers WHERE id=?", (cid,)))

    def _find_customer(self, name: str) -> Customer:
        try:
            name = sanitize_customer_name(name)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        r = self.db.query_one("SELECT * FROM customers WHERE name = ? COLLATE NOCASE",
                              (name,))
        if r is None:
            raise CustomerNotFound(f"No khata customer named '{name}'.")
        return self._customer(r)

    def _balance_paise(self, customer_id: int) -> int:
        r = self.db.query_one(
            """SELECT COALESCE(SUM(CASE WHEN entry_type='debit' THEN amount_paise
                                        ELSE -amount_paise END),0) AS bal
               FROM khata_entries WHERE customer_id=?""", (customer_id,))
        return r["bal"] if r else 0

    def _khata_state(self, cust: Customer) -> KhataState:
        last = self.db.query_one(
            "SELECT * FROM khata_entries WHERE customer_id=? ORDER BY id DESC LIMIT 1",
            (cust.id,))
        last_entry = None
        if last:
            last_entry = KhataEntry(last["id"], last["entry_type"], last["amount_paise"],
                                    last["note"], last["bill_id"], last["created_at"])
        return KhataState(customer=cust, balance_paise=self._balance_paise(cust.id),
                          last_entry=last_entry)

    def khata_add_credit(self, name: str, amount_paise: int, note: str | None = None,
                         bill_id: int | None = None) -> KhataState:
        if amount_paise <= 0:
            raise ValidationError("Credit amount must be positive.")
        cust = self.find_or_create_customer(name)
        with self.db.transaction() as c:
            c.execute(
                """INSERT INTO khata_entries
                   (customer_id, entry_type, amount_paise, note, bill_id, created_at)
                   VALUES (?, 'debit', ?, ?, ?, ?)""",
                (cust.id, amount_paise, note, bill_id, now_iso()))
        return self._khata_state(cust)

    def khata_record_payment(self, name: str, amount_paise: int, note: str | None = None,
                             allow_overpay: bool = False) -> KhataState:
        if amount_paise <= 0:
            raise ValidationError("Payment amount must be positive.")
        cust = self._find_customer(name)
        bal = self._balance_paise(cust.id)
        if amount_paise > bal and not allow_overpay:
            raise NeedsConfirmation(
                f"{cust.name} owes only {money.format_inr(bal)}, but payment is "
                f"{money.format_inr(amount_paise)}. Confirm to record an advance/overpay.")
        with self.db.transaction() as c:
            c.execute(
                """INSERT INTO khata_entries
                   (customer_id, entry_type, amount_paise, note, created_at)
                   VALUES (?, 'credit', ?, ?, ?)""",
                (cust.id, amount_paise, note, now_iso()))
        return self._khata_state(cust)

    def khata_balance(self, name: str) -> KhataState:
        return self._khata_state(self._find_customer(name))

    def khata_statement(self, name: str, limit: int = 20) -> list[KhataEntry]:
        cust = self._find_customer(name)
        rows = self.db.query_all(
            "SELECT * FROM khata_entries WHERE customer_id=? ORDER BY id DESC LIMIT ?",
            (cust.id, limit))
        return [KhataEntry(r["id"], r["entry_type"], r["amount_paise"], r["note"],
                           r["bill_id"], r["created_at"]) for r in rows]

    def list_khata_debtors(self) -> list[KhataState]:
        out = []
        for r in self.db.query_all("SELECT * FROM customers"):
            cust = self._customer(r)
            bal = self._balance_paise(cust.id)
            if bal > 0:
                out.append(self._khata_state(cust))
        out.sort(key=lambda s: s.balance_paise, reverse=True)
        return out

    def get_customer_history(self, query: str) -> CustomerHistory:
        """Fetch complete purchase history, lifetime metrics, top items, and past bills for a customer.

        `query` can be a 10-digit mobile number, or a customer name.
        """
        raw = query.strip()
        clean_phone = None
        try:
            clean_phone = validate_phone_number(raw)
        except ValueError:
            clean_phone = None

        cust_row = None
        if clean_phone:
            cust_row = self.db.query_one("SELECT * FROM customers WHERE phone = ?", (clean_phone,))
        if not cust_row:
            cust_row = self.db.query_one("SELECT * FROM customers WHERE name = ? COLLATE NOCASE", (raw,))
        if not cust_row:
            # Try search prefix/contains
            cust_row = self.db.query_one(
                "SELECT * FROM customers WHERE phone LIKE ? OR name LIKE ? LIMIT 1",
                (f"%{raw}%", f"%{raw}%"),
            )

        # If still not found in customers table, check if any bills exist with customer_phone
        if not cust_row and clean_phone:
            bill_with_phone = self.db.query_one("SELECT * FROM bills WHERE customer_phone = ? LIMIT 1", (clean_phone,))
            if bill_with_phone:
                cust = self.find_or_create_customer(bill_with_phone["customer_name"] or "Customer", phone=clean_phone)
                cust_row = self.db.query_one("SELECT * FROM customers WHERE id = ?", (cust.id,))

        if not cust_row:
            raise CustomerNotFound(f"No customer or purchase history found for '{query}'.")

        cust = self._customer(cust_row)

        # Find all finalized bills for this customer
        params = [cust.id]
        phone_clause = ""
        if cust.phone:
            phone_clause = " OR customer_phone = ?"
            params.append(cust.phone)

        bills = self.db.query_all(
            f"SELECT * FROM bills WHERE status='finalized' AND (customer_id = ?{phone_clause}) ORDER BY finalized_at DESC, id DESC",
            tuple(params),
        )

        total_visits = len(bills)
        total_spend = sum(b["total_paise"] for b in bills)
        avg_spend = (total_spend // total_visits) if total_visits > 0 else 0
        first_visit = bills[-1]["finalized_at"][:10] if bills and bills[-1]["finalized_at"] else None
        last_visit = bills[0]["finalized_at"][:10] if bills and bills[0]["finalized_at"] else None
        khata_bal = self._balance_paise(cust.id)

        recent_bills = []
        for b in bills[:10]:
            recent_bills.append({
                "id": b["id"],
                "invoice_no": b["invoice_no"],
                "daily_seq": b["daily_seq"],
                "date": (b["finalized_at"] or b["created_at"] or "")[:10],
                "total_paise": b["total_paise"],
                "payment_mode": b["payment_mode"] or "cash",
                "billed_by": b["billed_by"] if "billed_by" in b.keys() else None,
            })

        top_products = []
        bill_ids = [b["id"] for b in bills]
        if bill_ids:
            placeholders = ",".join("?" for _ in bill_ids)
            prod_rows = self.db.query_all(
                f"""SELECT bi.product_id, bi.name_snapshot, bi.unit,
                           SUM(bi.qty_base) as total_qty,
                           SUM(bi.line_total_paise) as total_spend,
                           COUNT(DISTINCT bi.bill_id) as order_count
                    FROM bill_items bi
                    WHERE bi.bill_id IN ({placeholders})
                    GROUP BY bi.product_id
                    ORDER BY total_spend DESC
                    LIMIT 10""",
                tuple(bill_ids),
            )
            for pr in prod_rows:
                top_products.append(CustomerPurchaseItem(
                    product_id=pr["product_id"],
                    name=pr["name_snapshot"],
                    unit=pr["unit"],
                    total_qty_display=format_qty(pr["total_qty"], pr["unit"]),
                    total_spend_paise=pr["total_spend"],
                    order_count=pr["order_count"],
                ))

        return CustomerHistory(
            customer=cust,
            phone=cust.phone,
            total_visits=total_visits,
            total_spend_paise=total_spend,
            average_bill_paise=avg_spend,
            first_visit_date=first_visit,
            last_visit_date=last_visit,
            khata_balance_paise=khata_bal,
            recent_bills=recent_bills,
            top_products=top_products,
        )

    # ================================================================== #
    # ANALYTICS
    # ================================================================== #
    def daily_summary(self, date: str | None = None) -> DailyReport:
        date = date or today_iso()
        rep = self.sales_report(date, date)
        return DailyReport(
            date=date, bill_count=rep.bill_count, gross_sales_paise=rep.gross_sales_paise,
            total_tax_paise=rep.total_tax_paise,
            cgst_paise=rep.total_tax_paise // 2,
            sgst_paise=rep.total_tax_paise - rep.total_tax_paise // 2,
            by_payment=rep.by_payment, top_items=rep.top_items)

    def sales_report(self, start: str, end: str) -> SalesReport:
        try:
            start, end = validate_date_range(start, end)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        pred = ("status='finalized' AND substr(finalized_at,1,10) BETWEEN ? AND ?")
        bills = self.db.query_all(f"SELECT * FROM bills WHERE {pred}", (start, end))
        gross = sum(b["total_paise"] for b in bills)
        taxable = sum(b["subtotal_paise"] for b in bills)
        tax = sum(b["cgst_paise"] + b["sgst_paise"] for b in bills)
        by_payment: dict[str, int] = {}
        for b in bills:
            by_payment[b["payment_mode"] or "unknown"] = (
                by_payment.get(b["payment_mode"] or "unknown", 0) + b["total_paise"])
        by_day_rows = self.db.query_all(
            f"""SELECT substr(finalized_at,1,10) AS d, SUM(total_paise) AS g
                FROM bills WHERE {pred} GROUP BY d ORDER BY d""", (start, end))
        by_day = [(r["d"], r["g"]) for r in by_day_rows]
        return SalesReport(
            start=start, end=end, bill_count=len(bills), gross_sales_paise=gross,
            taxable_paise=taxable, total_tax_paise=tax, by_payment=by_payment,
            by_day=by_day, top_items=self.top_items(start, end))

    def top_items(self, start: str, end: str, limit: int = 10) -> list[ItemSales]:
        try:
            start, end = validate_date_range(start, end)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        rows = self.db.query_all(
            """SELECT bi.product_id AS pid, bi.name_snapshot AS name, bi.unit AS unit,
                      SUM(bi.qty_base) AS qty, SUM(bi.line_total_paise) AS rev,
                      COUNT(*) AS cnt
               FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
               WHERE b.status='finalized' AND substr(b.finalized_at,1,10) BETWEEN ? AND ?
               GROUP BY bi.product_id ORDER BY rev DESC LIMIT ?""",
            (start, end, limit))
        return [ItemSales(r["pid"], r["name"], format_qty(r["qty"], r["unit"]),
                          r["rev"], r["cnt"]) for r in rows]

    def gst_collected(self, start: str, end: str) -> GstReport:
        try:
            start, end = validate_date_range(start, end)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        rows = self.db.query_all(
            """SELECT bi.gst_rate AS rate, SUM(bi.taxable_paise) AS taxable,
                      SUM(bi.cgst_paise) AS cgst, SUM(bi.sgst_paise) AS sgst
               FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
               WHERE b.status='finalized' AND substr(b.finalized_at,1,10) BETWEEN ? AND ?
               GROUP BY bi.gst_rate ORDER BY bi.gst_rate""", (start, end))
        by_slab = [{"rate": r["rate"], "taxable_paise": r["taxable"],
                    "cgst_paise": r["cgst"], "sgst_paise": r["sgst"]} for r in rows]
        cgst = sum(r["cgst"] for r in rows)
        sgst = sum(r["sgst"] for r in rows)
        return GstReport(start=start, end=end, by_slab=by_slab, cgst_paise=cgst,
                         sgst_paise=sgst, total_tax_paise=cgst + sgst)

    def stock_health(self) -> StockHealth:
        prods = [self._product(r) for r in
                 self.db.query_all("SELECT * FROM products WHERE active=1")]
        out = [p for p in prods if p.stock_base <= 0]
        low = [p for p in prods if 0 < p.stock_base <= p.reorder_base]
        cost_val = sum(int(Decimal(p.cost_paise) * p.stock_base / 1000) for p in prods)
        retail_val = sum(int(Decimal(p.sell_paise) * p.stock_base / 1000) for p in prods)
        return StockHealth(total_skus=len(prods), out_of_stock=out, low_stock=low,
                           inventory_cost_value_paise=cost_val,
                           inventory_retail_value_paise=retail_val)

    # ================================================================== #
    # MEMORY (durable preferences + shop profile)
    # ================================================================== #
    def set_preference(self, key: str, value: str) -> None:
        with self.db.transaction() as c:
            c.execute(
                """INSERT INTO preferences (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""", (key, value, now_iso()))

    def get_preferences(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.db.query_all("SELECT * FROM preferences")}

    def get_preference(self, key: str) -> str | None:
        r = self.db.query_one("SELECT value FROM preferences WHERE key=?", (key,))
        return r["value"] if r else None

    def get_shop_profile(self) -> ShopProfile:
        r = self.db.query_one("SELECT * FROM shop_profile WHERE id=1")
        if not r:
            return ShopProfile(name=None, gstin=None, address=None, phone=None, state_code="29", invoice_prefix="INV", logo_path=None, owner_pin_hash=None)
        logo = r["logo_path"] if "logo_path" in r.keys() else None
        pin_hash = r["owner_pin_hash"] if "owner_pin_hash" in r.keys() else None
        return ShopProfile(name=r["name"], gstin=r["gstin"], address=r["address"],
                           phone=r["phone"], state_code=r["state_code"],
                           invoice_prefix=r["invoice_prefix"], logo_path=logo,
                           owner_pin_hash=pin_hash)

    def set_shop_profile(self, **fields) -> ShopProfile:
        allowed = {"name", "gstin", "address", "phone", "state_code", "invoice_prefix", "logo_path", "owner_pin_hash"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?"); params.append(v)
        if sets:
            params.append(now_iso())
            with self.db.transaction() as c:
                c.execute(f"UPDATE shop_profile SET {', '.join(sets)}, updated_at=? WHERE id=1",
                          params)
        return self.get_shop_profile()

    # ================================================================== #
    # STORE OWNER SECURITY PIN / PASSWORD MANAGEMENT
    # ================================================================== #
    def set_owner_password(self, password: str) -> None:
        """Hash the provided plain password/PIN with salt and store in shop_profile."""
        pin_hash = hash_password(password)
        self.set_shop_profile(owner_pin_hash=pin_hash)

    def set_owner_password_hash(self, pin_hash: str) -> None:
        """Store an already hashed password in shop_profile."""
        self.set_shop_profile(owner_pin_hash=pin_hash)

    def verify_owner_password(self, password: str) -> bool:
        """Verify password against shop_profile.owner_pin_hash using constant-time comparison."""
        prof = self.get_shop_profile()
        return verify_password(password, prof.owner_pin_hash)

    def has_owner_password(self) -> bool:
        """Return True if the store has configured an owner security PIN / password."""
        prof = self.get_shop_profile()
        return bool(prof.owner_pin_hash)

    # ================================================================== #
    # EMPLOYEES / STAFF MANAGEMENT
    # ================================================================== #
    def next_employee_code(self) -> str:
        """Generate the next unique sequential employee code, e.g. EMP-001, EMP-002."""
        rows = self.db.query_all("SELECT emp_code FROM employees")
        max_num = 0
        for r in rows:
            code = r["emp_code"]
            m = re.match(r"^EMP-(\d+)$", code, re.IGNORECASE)
            if m:
                try:
                    num = int(m.group(1))
                    if num > max_num:
                        max_num = num
                except ValueError:
                    pass
        return f"EMP-{max_num + 1:03d}"

    def add_employee(
        self,
        name: str,
        role: str = "cashier",
        phone: str | None = None,
        emp_code: str | None = None,
    ) -> Employee:
        """Register a new employee. Generates sequential EMP-xxx code if omitted."""
        name = sanitize_employee_name(name)
        role = (role or "cashier").strip().lower()
        if not emp_code:
            emp_code = self.next_employee_code()
        else:
            emp_code = emp_code.strip().upper()

        existing = self.db.query_one("SELECT id FROM employees WHERE UPPER(emp_code) = ?", (emp_code,))
        if existing:
            raise ValidationError(f"Employee code '{emp_code}' is already assigned.")

        phone_clean = validate_phone_number(phone) if phone else None
        now = now_iso()

        with self.db.transaction() as c:
            try:
                cur = c.execute(
                    """INSERT INTO employees (emp_code, name, role, phone, is_active, created_at)
                       VALUES (?, ?, ?, ?, 1, ?)""",
                    (emp_code, name, role, phone_clean, now),
                )
            except sqlite3.IntegrityError as e:
                raise ValidationError(f"Employee code '{emp_code}' is already assigned.") from e
            eid = cur.lastrowid
            r = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        return self._employee_from_row(r)

    def list_employees(self, active_only: bool = True, include_inactive: bool | None = None) -> list[Employee]:
        """List all employees ordered by id."""
        if include_inactive is not None:
            active_only = not include_inactive
        if active_only:
            rows = self.db.query_all("SELECT * FROM employees WHERE is_active=1 ORDER BY id")
        else:
            rows = self.db.query_all("SELECT * FROM employees ORDER BY id")
        return [self._employee_from_row(r) for r in rows]

    def get_employee(self, code_or_name: str | int, active_only: bool = True) -> Employee | None:
        """Search employee by id, emp_code (e.g. EMP-001), or name (case-insensitive)."""
        if isinstance(code_or_name, int):
            sql = "SELECT * FROM employees WHERE id=?"
            if active_only:
                sql += " AND is_active=1"
            r = self.db.query_one(sql, (code_or_name,))
            return self._employee_from_row(r) if r else None

        code_or_name = str(code_or_name or "").strip()
        if not code_or_name:
            return None
        sql = "SELECT * FROM employees WHERE (emp_code=? COLLATE NOCASE OR name=? COLLATE NOCASE)"
        if active_only:
            sql += " AND is_active=1"
        sql += " ORDER BY id DESC LIMIT 1"
        r = self.db.query_one(sql, (code_or_name, code_or_name))
        if not r:
            # Fallback to substring/partial name match (e.g. 'Ramesh' for 'Ramesh Kumar')
            like_pat = f"%{code_or_name}%"
            sql_like = "SELECT * FROM employees WHERE (name LIKE ?)"
            if active_only:
                sql_like += " AND is_active=1"
            sql_like += " ORDER BY id ASC LIMIT 1"
            r = self.db.query_one(sql_like, (like_pat,))

        return self._employee_from_row(r) if r else None

    def remove_employee(self, code_or_name: str | int) -> Employee:
        """Deactivate an employee (soft delete). Historical invoice attribution is preserved."""
        emp = self.get_employee(code_or_name, active_only=False)
        if emp is None:
            raise ValidationError(f"No employee found matching '{code_or_name}'.")
        now = now_iso()
        with self.db.transaction() as c:
            c.execute(
                "UPDATE employees SET is_active=0, deactivated_at=? WHERE id=?",
                (now, emp.id),
            )
        # If this employee is currently set as active_cashier preference, reset to last_biller / owner
        current_biller = self.get_preference("active_cashier")
        if current_biller and (current_biller.lower() == emp.name.lower() or current_biller.lower() == emp.emp_code.lower()):
            owner_biller = self.get_preference("last_biller") or "Owner"
            self.set_preference("active_cashier", owner_biller)

        r = self.db.query_one("SELECT * FROM employees WHERE id=?", (emp.id,))
        return self._employee_from_row(r)


    # ================================================================== #
    # TRANSPORT (idempotency + session mapping)
    # ================================================================== #
    def mark_update_processed(self, update_id: int, chat_id: int) -> bool:
        """Return True if newly recorded; False if this update was already seen."""
        try:
            with self.db.transaction() as c:
                c.execute(
                    "INSERT INTO processed_updates (update_id, chat_id, created_at) "
                    "VALUES (?,?,?)", (update_id, chat_id, now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False

    def known_chat_ids(self) -> list[int]:
        """Chats that have interacted with the bot — recipients for scheduled messages."""
        rows = self.db.query_all(
            """SELECT DISTINCT chat_id FROM (
                   SELECT chat_id FROM chat_sessions WHERE chat_id IS NOT NULL AND chat_id != 0
                   UNION
                   SELECT chat_id FROM processed_updates WHERE chat_id IS NOT NULL AND chat_id != 0
                   UNION
                   SELECT chat_id FROM bills WHERE chat_id IS NOT NULL AND chat_id != 0
               ) ORDER BY chat_id"""
        )
        return [r["chat_id"] for r in rows]

    def get_session(self, chat_id: int) -> str | None:
        r = self.db.query_one("SELECT session_id FROM chat_sessions WHERE chat_id=?", (chat_id,))
        return r["session_id"] if r else None

    def set_session(self, chat_id: int, session_id: str) -> None:
        with self.db.transaction() as c:
            c.execute(
                """INSERT INTO chat_sessions (chat_id, session_id, updated_at) VALUES (?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id,
                   updated_at=excluded.updated_at""", (chat_id, session_id, now_iso()))

    def clear_session(self, chat_id: int) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM chat_sessions WHERE chat_id=?", (chat_id,))
