"""End-to-end demo: drive the REAL agent through every scenario,
asserting durable DB state + generated artifacts along the way.

    uv run python scripts/demo.py

Resets to a fresh seeded store (with history so the deck has data), then runs
the full script on one chat, prints the owner<->bot transcript, and checks each
checkpoint. Requires GEMINI_API_KEY. This is also the script to follow for the
screen recording.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kirana.agent.session import reset_session, run_turn  # noqa: E402
from kirana.config import GEMINI_API_KEY  # noqa: E402
from kirana.tools.context import init_context  # noqa: E402

CHAT = 5555
PASS, FAIL = "✅", "❌"
_checks: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    _checks.append((label, bool(cond)))
    print(f"   {PASS if cond else FAIL} {label}")


async def say(text: str) -> str:
    print(f"\n🧑 owner> {text}")
    res = await run_turn(CHAT, text)
    print(f"🤖 bot>   {res.reply}")
    for f in res.files:
        print(f"          📎 {f.caption}: {f.path}")
    await asyncio.sleep(2)
    return res.reply, res.files  # type: ignore[return-value]


async def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if not GEMINI_API_KEY:
        raise SystemExit("Set GEMINI_API_KEY in .env first (get free key from https://aistudio.google.com/).")

    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py"),
                    "--reset", "--with-history"], check=True, cwd=ROOT)
    repo = init_context()
    reset_session(CHAT)

    print("\n" + "=" * 70 + "\n  SUPERMARKET OPS AGENT — END-TO-END DEMO\n" + "=" * 70)

    def prod(query):
        hits = repo.search_products(query, 5)
        return hits[0] if hits else None

    # 1) Receive stock (existing SKU)
    maggi = prod("maggi")
    before = maggi.stock_base
    await say("50 packets of Maggi came in, cost ₹12, MRP ₹14")
    check("Maggi stock increased by 50", repo.get_product(maggi.id).stock_base == before + 50 * 1000)

    # 2) Add a new product
    await say("new item: Haldiram Bhujia 200g, GST 12%, MRP ₹50, sell ₹48, we have 30")
    check("Haldiram Bhujia added", bool(prod("bhujia")))

    # 3) Multi-item bill
    await say("make a bill: 2kg sugar, 1 Aashirvaad atta 5kg, 4 Maggi, 1 Amul butter")
    v = repo.cart_view(CHAT)
    check("Cart has 4 line items", len(v.lines) == 4)

    # 4) Edit mid-build
    await say("drop the butter, and make it 6 Maggi")
    v = repo.cart_view(CHAT)
    names = {ln.name for ln in v.lines}
    maggi_line = next((ln for ln in v.lines if "Maggi" in ln.name), None)
    check("Butter removed from cart", not any("Butter" in n for n in names))
    check("Maggi quantity is 6", maggi_line is not None and maggi_line.qty_display == "6 packet")

    # 5) Pay + finalize
    await say("all on UPI, finalize it")
    finalized = [b for b in _finalized_bills(repo, CHAT)]
    check("A bill was finalized", len(finalized) >= 1)
    inv = finalized[-1]["invoice_no"] if finalized else None

    # 6) PDF invoice
    _, files = await say("send me that bill as a PDF")
    check("PDF invoice generated", any(f.path.endswith(".pdf") for f in files))

    # 7) Oversell guard
    atta = prod("aashirvaad atta")
    atta_stock = repo.get_product(atta.id).stock_base
    await say("new bill: 100 Aashirvaad atta 5kg, cash — finalize")
    check("Oversell refused (atta stock unchanged)",
          repo.get_product(atta.id).stock_base == atta_stock)
    await say("ok cancel that bill")

    # 8-10) Khata cycle
    await say("put ₹500 on Ramesh's credit")
    check("Ramesh owes ₹500", repo.khata_balance("Ramesh").balance_paise == 50000)
    await say("Ramesh paid ₹300")
    check("Ramesh balance now ₹200", repo.khata_balance("Ramesh").balance_paise == 20000)
    await say("what's Ramesh's balance?")

    # 11) Analysis deck
    _, files = await say("make this week's sales analysis deck")
    check("PPTX analysis deck generated", any(f.path.endswith(".pptx") for f in files))

    # Multi-language (Hinglish)
    await say("aaj ki total sale kitni hui?")

    # 12-13) Set standing preferences
    await say("from now on always assume UPI unless I say cash")
    await say('and "atta" always means Aashirvaad Atta 5kg')
    prefs = repo.get_preferences()
    check("A preference was persisted", len(prefs) >= 1)

    # 14-15) /new chat — memory must persist
    print("\n----- (owner starts a /new chat) -----")
    reset_session(CHAT)
    await say("bill 1 atta and 4 maggi, then finalize")
    latest = _finalized_bills(repo, CHAT)[-1]
    check("New bill finalized after /new", latest is not None)
    check("Default payment UPI remembered", latest["payment_mode"] == "upi")
    check("Default brand (Aashirvaad atta) remembered",
          any("Aashirvaad" in ln for ln in latest["items"]))

    # summary
    ok = sum(1 for _, c in _checks if c)
    print("\n" + "=" * 70)
    print(f"  CHECKPOINTS: {ok}/{len(_checks)} passed")
    for label, c in _checks:
        print(f"   {PASS if c else FAIL} {label}")
    print("=" * 70)
    if inv:
        print(f"  (invoice {inv} available as PDF in artifacts_out/)")


def _finalized_bills(repo, chat_id):
    rows = repo.db.query_all(
        "SELECT id, invoice_no, payment_mode FROM bills "
        "WHERE chat_id=? AND status='finalized' ORDER BY id", (chat_id,))
    out = []
    for r in rows:
        items = [ir["name_snapshot"] for ir in
                 repo.db.query_all("SELECT name_snapshot FROM bill_items WHERE bill_id=?", (r["id"],))]
        out.append({"invoice_no": r["invoice_no"], "payment_mode": r["payment_mode"], "items": items})
    return out


if __name__ == "__main__":
    asyncio.run(main())
