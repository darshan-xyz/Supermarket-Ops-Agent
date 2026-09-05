"""Concurrency: the hard part. Many bills racing for scarce stock must never
oversell, and stock must never go negative — enforced by WAL + BEGIN IMMEDIATE +
the conditional decrement UPDATE, not by luck.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from kirana.db.repository import InsufficientStock, RepoError
from tests.conftest import add


def test_concurrent_finalize_never_oversells(repo):
    # 10 packets in stock; 25 buyers each want 1 packet, all at once.
    p = add(repo, "Maggi 70g", "packet", 18, 14, 12, stock=10)
    n_buyers = 25

    def buy(chat_id: int) -> str:
        try:
            repo.cart_add_item(chat_id, p.id, 1)
            repo.cart_set_payment(chat_id, "cash")
            repo.finalize_bill(chat_id, f"race-{chat_id}")
            return "ok"
        except InsufficientStock:
            return "blocked"
        except RepoError:
            return "err"

    with ThreadPoolExecutor(max_workers=n_buyers) as ex:
        results = list(ex.map(buy, range(1000, 1000 + n_buyers)))

    ok = results.count("ok")
    blocked = results.count("blocked")
    assert ok == 10, f"expected exactly 10 sales, got {ok}"
    assert blocked == n_buyers - 10
    final = repo.get_product(p.id).stock_base
    assert final == 0, f"stock should be exactly 0, got {final}"
    assert final >= 0  # never negative


def test_concurrent_sale_and_restock_consistent(repo):
    # Start at 20; 10 buyers of 1 each + 5 restocks of 4 each, concurrently.
    p = add(repo, "Parle-G", "packet", 18, 10, 8, stock=20)

    def sell(chat_id: int) -> str:
        try:
            repo.cart_add_item(chat_id, p.id, 1)
            repo.cart_set_payment(chat_id, "upi")
            repo.finalize_bill(chat_id, f"s-{chat_id}")
            return "sold"
        except RepoError:
            return "blocked"

    def restock(_: int) -> str:
        repo.receive_stock(p.id, 4 * 1000)
        return "restocked"

    tasks = [("sell", i) for i in range(2000, 2010)] + [("restock", i) for i in range(10)]
    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = [ex.submit(sell if kind == "sell" else restock, i) for kind, i in tasks]
        outcomes = [f.result() for f in futs]

    sold = outcomes.count("sold")
    restocked = outcomes.count("restocked")  # each adds 4 packets
    expected = 20 * 1000 + restocked * 4 * 1000 - sold * 1000
    assert repo.get_product(p.id).stock_base == expected
    assert repo.get_product(p.id).stock_base >= 0
