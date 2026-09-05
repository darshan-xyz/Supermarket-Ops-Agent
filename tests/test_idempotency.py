"""Transport-level idempotency: Telegram redelivers updates; the same update_id
must be processed once. (Finalize idempotency is covered in test_repository.)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from tests.conftest import add


def test_update_dedupe(repo):
    assert repo.mark_update_processed(555, chat_id=1) is True
    assert repo.mark_update_processed(555, chat_id=1) is False  # redelivery
    assert repo.mark_update_processed(556, chat_id=1) is True


def test_concurrent_duplicate_finalize_single_decrement(repo):
    """Two threads finalize the SAME cart with the SAME idempotency key at once —
    stock must decrement exactly once."""
    p = add(repo, "Amul Butter 100g", "packet", 12, 62, 55, stock=8)
    chat = 77
    repo.cart_add_item(chat, p.id, 2)
    repo.cart_set_payment(chat, "upi")

    def fin(_: int):
        try:
            return repo.finalize_bill(chat, "dup-key")
        except Exception as e:  # noqa: BLE001
            return e

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(fin, range(2)))

    assert repo.get_product(p.id).stock_base == 8 * 1000 - 2 * 1000  # once only
