"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import pytest

from kirana.db.connection import Database
from kirana.db.repository import Repository
from kirana.domain.money import to_paise
from kirana.domain.units import to_base


@pytest.fixture
def repo(tmp_path):
    """A fresh repository on a temp file DB (file-backed so threads can share)."""
    db = Database(tmp_path / "test.db")
    r = Repository(db)
    r.init()
    return r


def add(r: Repository, name, unit, gst, sell, cost, stock=0, reorder=0, mrp=None,
        brand=None, hsn=None, is_loose=False, aliases=None):
    """Convenience product factory using rupees + display units."""
    return r.add_product(
        name=name, unit=unit, gst_rate=gst, sell_paise=to_paise(sell),
        mrp_paise=to_paise(mrp if mrp is not None else sell), cost_paise=to_paise(cost),
        brand=brand, hsn_code=hsn, is_loose=is_loose, aliases=aliases,
        opening_stock_base=to_base(stock, unit, unit),
        reorder_base=to_base(reorder, unit, unit),
    )


@pytest.fixture
def seeded(repo):
    """A small realistic catalog for billing/analytics tests."""
    ids = {
        "sugar": add(repo, "Loose Sugar", "kg", 5, 45, 40, stock=10, reorder=5,
                     is_loose=True, aliases="sugar,cheeni,sarkkarai,seeni,சர்க்கரை").id,
        "atta": add(repo, "Aashirvaad Atta 5kg", "packet", 5, 270, 250, stock=6,
                     reorder=2, brand="Aashirvaad", hsn="1101", aliases="atta,aata,kothumai,ஆசீர்வாத் கோதுமை").id,
        "maggi": add(repo, "Maggi 70g", "packet", 18, 14, 12, stock=50, reorder=10,
                     brand="Nestle", hsn="1902", aliases="maggi,noodles,மேகி").id,
        "butter": add(repo, "Amul Butter 100g", "packet", 12, 62, 55, stock=8,
                      reorder=3, brand="Amul", hsn="0405", aliases="butter,makkhan,vennai,வெண்ணெய்").id,
    }
    repo.set_shop_profile(name="Test Kirana", gstin="29ABCDE1234F1Z5",
                          address="MG Road, Bengaluru", phone="9812345678")
    return repo, ids
