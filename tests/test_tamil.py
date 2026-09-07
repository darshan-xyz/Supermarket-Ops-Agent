"""Tests for Tamil and Tanglish language support in the kirana store agent.

Verifies:
1. Product catalog resolution via native Tamil script (அரிசி, பருப்பு, etc.)
2. Product catalog resolution via Tanglish phonetics (arisi, paruppu, etc.)
3. Language preference persistence (language=tamil / language=tanglish)
4. Tamil-driven billing flow (cart add, view, finalize with Tamil item names)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.seed import seed_catalog  # noqa: E402


@pytest.fixture
def tamil_store(repo):
    ids = seed_catalog(repo)
    return repo, ids


def test_tamil_script_search(tamil_store):
    repo, ids = tamil_store
    tamil_queries = [
        ("அரிசி", ids["Loose Rice (Sona Masoori)"]),
        ("பருப்பு", ids["Loose Toor Dal"]),
        ("சர்க்கரை", ids["Loose Sugar"]),
        ("உப்பு", ids["Tata Salt 1kg"]),
        ("பால்", ids["Amul Taaza Milk 500ml"]),
        ("வெண்ணெய்", ids["Amul Butter 100g"]),
        ("எண்ணெய்", ids["Fortune Sunflower Oil 1L"]),
        ("மேகி", ids["Maggi 70g"]),
    ]
    for query, expected_id in tamil_queries:
        hits = repo.search_products(query)
        assert hits, f"No hits for Tamil query: {query}"
        assert hits[0].id == expected_id, f"Query '{query}' expected ID {expected_id}, got {hits[0].id} ({hits[0].name})"


def test_tanglish_search(tamil_store):
    repo, ids = tamil_store
    tanglish_queries = [
        ("arisi", ids["Loose Rice (Sona Masoori)"]),
        ("paruppu", ids["Loose Toor Dal"]),
        ("thuvaram paruppu", ids["Loose Toor Dal"]),
        ("sarkkarai", ids["Loose Sugar"]),
        ("seeni", ids["Loose Sugar"]),
        ("sakkarai", ids["Loose Sugar"]),
        ("uppu", ids["Tata Salt 1kg"]),
        ("tata uppu", ids["Tata Salt 1kg"]),
        ("ennai", ids["Fortune Sunflower Oil 1L"]),
        ("samayal ennai", ids["Fortune Sunflower Oil 1L"]),
        ("paal", ids["Amul Taaza Milk 500ml"]),
        ("amul paal", ids["Amul Taaza Milk 500ml"]),
        ("vennai", ids["Amul Butter 100g"]),
        ("amul vennai", ids["Amul Butter 100g"]),
        ("kothumai", ids["Loose Atta"]),
        ("aashirvaad kothumai", ids["Aashirvaad Atta 5kg"]),
    ]
    for query, expected_id in tanglish_queries:
        hits = repo.search_products(query)
        assert hits, f"No hits for Tanglish query: {query}"
        assert hits[0].id == expected_id, f"Query '{query}' expected ID {expected_id}, got {hits[0].id} ({hits[0].name})"


def test_tamil_billing_flow(tamil_store):
    repo, ids = tamil_store
    chat_id = 98765

    # Look up rice by Tamil name 'arisi'
    hits = repo.search_products("arisi")
    assert hits and hits[0].id == ids["Loose Rice (Sona Masoori)"]
    rice = hits[0]

    # Look up sugar by Tamil name 'sarkkarai'
    hits = repo.search_products("sarkkarai")
    assert hits and hits[0].id == ids["Loose Sugar"]
    sugar = hits[0]

    # Add items to cart
    repo.cart_add_item(chat_id, rice.id, 2, "kg")
    repo.cart_add_item(chat_id, sugar.id, 1, "kg")

    view = repo.cart_view(chat_id)
    assert len(view.lines) == 2
    assert view.grand_total_paise == (2 * 6200 + 1 * 4500)

    # Set payment and customer (e.g. Murugan / முருகன்)
    repo.cart_set_payment(chat_id, "upi")
    repo.cart_set_customer(chat_id, "Murugan")

    bill = repo.finalize_bill(chat_id, "k-tamil-bill-1")
    assert bill.status == "finalized"
    assert bill.customer_name == "Murugan"
    assert bill.payment_mode == "upi"


def test_tamil_language_preference(repo):
    repo.set_preference("language", "tamil")
    prefs = repo.get_preferences()
    assert prefs.get("language") == "tamil"

    repo.set_preference("language", "tanglish")
    prefs2 = repo.get_preferences()
    assert prefs2.get("language") == "tanglish"


def test_telegram_language_keyboard():
    from kirana.telegram.bot import _LANG_KEYBOARD
    buttons = [btn.callback_data for row in _LANG_KEYBOARD.inline_keyboard for btn in row]
    assert buttons == ["lang:English", "lang:Tamil", "lang:Hindi"]
    labels = [btn.text for row in _LANG_KEYBOARD.inline_keyboard for btn in row]
    assert any("🇮🇳" in l and "English" in l for l in labels)
    assert any("Tamil / Tanglish" in l for l in labels)
    assert any("Hindi / Hinglish" in l for l in labels)

