"""Seed a realistic Indian kirana catalog (and optional sales history).

Usage:
    uv run python scripts/seed.py                # seed catalog + shop profile
    uv run python scripts/seed.py --reset        # wipe DB first
    uv run python scripts/seed.py --with-history # + ~7 days of backdated sales

GST slabs reflect reality: unbranded loose staples 0%, branded/packaged staples
5%, FMCG (noodles/biscuits/detergent/chocolate/toothpaste) 18%, butter 12%.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kirana.config import DB_PATH  # noqa: E402
from kirana.db.connection import Database  # noqa: E402
from kirana.db.repository import Repository  # noqa: E402
from kirana.domain.money import to_paise  # noqa: E402
from kirana.domain.units import to_base  # noqa: E402

# name, brand, unit, gst, hsn, cost₹, mrp₹, sell₹, is_loose, opening_qty, reorder_qty, aliases
CATALOG = [
    # --- loose staples (0% unbranded, sold by kg) ---
    ("Loose Atta", None, "kg", 0, "1101", 38, 45, 44, True, 40, 10, "atta,aata,gehu,wheat flour,gehu aata,kothumai,kothumai maavu,கோதுமை,கோதுமை மாவு"),
    ("Loose Rice (Sona Masoori)", None, "kg", 0, "1006", 52, 65, 62, True, 60, 15, "rice,chawal,chaval,arisi,saatham,sona masoori,அரிசி,சாதம்"),
    ("Loose Toor Dal", None, "kg", 0, "0713", 110, 135, 130, True, 30, 8, "dal,toor,arhar,tur dal,paruppu,thuvaram paruppu,dhal,பருப்பு,துவரம் பருப்பு"),
    ("Loose Sugar", None, "kg", 5, "1701", 40, 46, 45, True, 50, 12, "sugar,cheeni,chini,shakkar,sarkkarai,seeni,sakkarai,சர்க்கரை,சீனி"),
    # --- packaged staples (5%) ---
    ("Aashirvaad Atta 5kg", "Aashirvaad", "packet", 5, "1101", 250, 275, 270, False, 20, 5, "aashirvaad,ashirvaad atta,packet atta,aashirvaad kothumai,ஆசீர்வாத் கோதுமை மாவு"),
    ("Tata Salt 1kg", "Tata", "packet", 0, "2501", 20, 28, 26, False, 40, 10, "salt,namak,tata namak,uppu,tata uppu,கல் உப்பு,தூள் உப்பு,உப்பு"),
    ("Fortune Sunflower Oil 1L", "Fortune", "packet", 5, "1512", 120, 145, 140, False, 24, 6, "oil,tel,sunflower oil,cooking oil,refined oil,ennai,sunflower ennai,samayal ennai,எண்ணெய்"),
    ("Tata Tea Gold 250g", "Tata", "packet", 5, "0902", 115, 140, 135, False, 18, 5, "tea,chai,chai patti,tata tea,tea thool,டீ தூள்,தேயிலை"),
    ("Amul Taaza Milk 500ml", "Amul", "packet", 0, "0401", 26, 30, 30, False, 30, 12, "milk,doodh,dudh,amul milk,taaza,paal,amul paal,பால்"),
    # --- butter (12%) ---
    ("Amul Butter 100g", "Amul", "packet", 12, "0405", 55, 62, 62, False, 24, 6, "butter,makkhan,amul butter,vennai,amul vennai,வெண்ணெய்"),
    # --- FMCG (18%) ---
    ("Maggi 70g", "Nestle", "packet", 18, "1902", 12, 14, 14, False, 80, 20, "maggi,noodles,magi,instant noodles,மேகி,நூடுல்ஸ்"),
    ("Parle-G 70g", "Parle", "packet", 18, "1905", 8, 10, 10, False, 100, 25, "parle,parle g,biscuit,biscuits,parleg,biskut,பிஸ்கட்"),
    ("Surf Excel 1kg", "HUL", "packet", 18, "3402", 105, 130, 125, False, 20, 5, "surf,detergent,washing powder,surf excel,thuni soap,detergent powder,சலவை தூள்,துணி சோப்"),
    ("Colgate MaxFresh 100g", "Colgate", "packet", 18, "3306", 45, 60, 55, False, 25, 6, "colgate,toothpaste,manjan,paste,pall vilakkum paste,பல்பொடி,டூத்பேஸ்ட்"),
    ("Cadbury Dairy Milk 40g", "Cadbury", "packet", 18, "1806", 30, 40, 40, False, 40, 10, "dairy milk,chocolate,cadbury,chocolate bar,mittai,சாக்லேட்,மிட்டாய்"),
]


# Perishables get an expiry on their opening stock so FEFO / expiry alerts have data.
PERISHABLE_DAYS = {"Amul Taaza Milk 500ml": 4, "Amul Butter 100g": 45}


def seed_catalog(repo: Repository) -> dict[str, int]:
    ids: dict[str, int] = {}
    for (name, brand, unit, gst, hsn, cost, mrp, sell, loose, open_q, reo_q, aliases) in CATALOG:
        existing = repo.search_products(name, limit=1)
        if existing and existing[0].name == name:
            ids[name] = existing[0].id
            with repo.db.transaction() as c:
                c.execute("UPDATE products SET aliases=? WHERE id=?", (aliases, existing[0].id))
            continue
        expiry = None
        if name in PERISHABLE_DAYS:
            expiry = (date.today() + timedelta(days=PERISHABLE_DAYS[name])).isoformat()
        p = repo.add_product(
            name=name, brand=brand, unit=unit, gst_rate=gst, hsn_code=hsn,
            cost_paise=to_paise(cost), mrp_paise=to_paise(mrp), sell_paise=to_paise(sell),
            is_loose=loose, aliases=aliases,
            opening_stock_base=to_base(open_q, unit, unit),
            reorder_base=to_base(reo_q, unit, unit), expiry=expiry,
        )
        ids[name] = p.id
    return ids


def seed_profile(repo: Repository) -> None:
    repo.set_shop_profile(
        name="Sharma Kirana Store",
        gstin="29ABCDE1234F1Z5",
        address="No. 14, MG Road, Bengaluru, Karnataka 560001",
        phone="+91 98450 12345",
        state_code="29",
        invoice_prefix="INV",
    )


def seed_history(repo: Repository, ids: dict[str, int]) -> None:
    """Create a handful of finalized bills across the last 7 days (backdated)."""
    import random

    random.seed(7)
    modes = ["cash", "upi", "upi", "card"]
    sellables = [n for n in ids if not n.startswith("Loose") or n == "Loose Sugar"]
    chat = 900000  # synthetic history chat
    for d in range(7, 0, -1):
        day = (date.today() - timedelta(days=d)).isoformat()
        for _ in range(random.randint(2, 4)):
            repo.cart_clear(chat)
            picks = random.sample(sellables, random.randint(2, 4))
            for name in picks:
                pid = ids[name]
                p = repo.get_product(pid)
                if p.stock_base <= 2000:
                    continue
                qty = random.randint(1, 3)
                repo.cart_add_item(chat, pid, qty)
            view = repo.cart_view(chat)
            if not view.lines:
                continue
            repo.cart_set_payment(chat, random.choice(modes), "SEED")
            bill = repo.finalize_bill(chat, f"seed-{day}-{random.randint(0, 1_000_000)}")
            # backdate finalized_at + movements to the target day
            with repo.db.transaction() as c:
                c.execute("UPDATE bills SET finalized_at=? WHERE id=?",
                          (f"{day}T12:00:00+05:30", bill.id))
                c.execute("UPDATE stock_movements SET created_at=? WHERE ref_id=? AND ref_type='bill'",
                          (f"{day}T12:00:00+05:30", bill.id))
    repo.cart_clear(chat)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the DB file first")
    ap.add_argument("--with-history", action="store_true", help="seed backdated sales")
    args = ap.parse_args()

    if args.reset and DB_PATH.exists():
        try:
            DB_PATH.unlink()
            for suffix in ("-wal", "-shm"):
                p = DB_PATH.with_name(DB_PATH.name + suffix)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except OSError:
            # File is locked by a running worker process (e.g. on Windows).
            # Cleanly wipe tables instead of failing.
            import sqlite3
            tmp_db = Database(DB_PATH)
            with tmp_db.transaction() as c:
                for tbl in (
                    "stock_movements", "batches", "bill_items", "bills",
                    "khata_entries", "customers", "preferences", "processed_updates",
                    "chat_sessions", "products", "shop_profile",
                ):
                    try:
                        c.execute(f"DELETE FROM {tbl}")
                    except sqlite3.OperationalError:
                        pass

    db = Database(DB_PATH)
    repo = Repository(db)
    repo.init()
    ids = seed_catalog(repo)
    seed_profile(repo)
    if args.with_history:
        seed_history(repo, ids)

    n = len(repo.search_products("", limit=100))
    print(f"Seeded catalog -> {len(ids)} products, {n} total in DB at {DB_PATH}")
    if args.with_history:
        print("Seeded ~7 days of backdated sales history.")


if __name__ == "__main__":
    main()
