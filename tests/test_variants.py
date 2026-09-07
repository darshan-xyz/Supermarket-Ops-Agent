"""Tests for product weight, volume, and pack size variant disambiguation."""

from kirana.domain.variants import (
    extract_base_and_variant,
    get_sibling_variants,
    is_variant_specified_in_query,
)
from tests.conftest import add


def test_extract_base_and_variant():
    assert extract_base_and_variant("Maggi 70g") == ("maggi", "70g")
    assert extract_base_and_variant("Maggi 10g") == ("maggi", "10g")
    assert extract_base_and_variant("Aashirvaad Atta 5kg") == ("aashirvaad atta", "5kg")
    assert extract_base_and_variant("Amul Taaza Milk 500ml") == ("amul taaza milk", "500ml")
    assert extract_base_and_variant("Fortune Sunflower Oil 1L") == ("fortune sunflower oil", "1l")
    assert extract_base_and_variant("Tata Tea Gold 250g") == ("tata tea gold", "250g")
    assert extract_base_and_variant("Loose Sugar") == ("loose sugar", None)


def test_is_variant_specified_in_query():
    # Direct match
    assert is_variant_specified_in_query("order 1 packet maggi 70g", "70g") is True
    assert is_variant_specified_in_query("order 1 packet maggi 10g", "70g") is False
    assert is_variant_specified_in_query("order 1 packet maggi", "70g") is False

    # Unit variations: gm, grams, spaces
    assert is_variant_specified_in_query("add 70 gm maggi", "70g") is True
    assert is_variant_specified_in_query("give 70 grams of maggi", "70g") is True
    assert is_variant_specified_in_query("bill 10 gm maggi", "10g") is True

    # Compound instructions without variant
    assert is_variant_specified_in_query("if you have sugar in inventory add maggi 1 packet", "70g") is False
    assert is_variant_specified_in_query("if you have sugar in inventory add maggi 1 packet", "10g") is False


def test_get_sibling_variants(repo):
    p1 = add(repo, "Maggi 70g", "packet", 18, 14, 12, stock=50)
    p2 = add(repo, "Maggi 10g", "packet", 18, 5, 4, stock=100)
    p3 = add(repo, "Tata Salt 1kg", "packet", 0, 28, 24, stock=40)

    # Maggi has siblings
    siblings_70 = get_sibling_variants(repo, p1.id)
    assert len(siblings_70) == 2
    assert {p.id for p in siblings_70} == {p1.id, p2.id}

    siblings_10 = get_sibling_variants(repo, p2.id)
    assert len(siblings_10) == 2

    # Tata Salt has no other variant in catalogue
    siblings_salt = get_sibling_variants(repo, p3.id)
    assert siblings_salt == []


def test_unit_aware_product_resolution(repo):
    from kirana.tools.billing import _resolve_product

    atta_loose = repo.add_product(
        name="Loose Atta",
        unit="kg",
        gst_rate=0,
        cost_paise=3800,
        mrp_paise=4500,
        sell_paise=4400,
        is_loose=True,
        opening_stock_base=40000,
    )
    atta_pack = repo.add_product(
        name="Aashirvaad Atta 5kg",
        unit="packet",
        gst_rate=5,
        cost_paise=25000,
        mrp_paise=27500,
        sell_paise=27000,
        is_loose=False,
        opening_stock_base=20000,
    )
    butter = repo.add_product(
        name="Amul Butter 100g",
        unit="packet",
        gst_rate=12,
        cost_paise=5500,
        mrp_paise=6200,
        sell_paise=6200,
        is_loose=False,
        opening_stock_base=24000,
    )

    # Unit 'kg' matches Loose Atta directly
    resolved_kg = _resolve_product(repo, None, "Atta", unit="kg", quantity=4)
    assert resolved_kg.id == atta_loose.id
    assert resolved_kg.name == "Loose Atta"

    # Unit 'packet' matches Aashirvaad Atta 5kg directly
    resolved_pkt = _resolve_product(repo, None, "Atta", unit="packet", quantity=1)
    assert resolved_pkt.id == atta_pack.id
    assert resolved_pkt.name == "Aashirvaad Atta 5kg"

    # Single match: "Butter" resolves to Amul Butter 100g with no siblings
    resolved_butter = _resolve_product(repo, None, "Butter")
    assert resolved_butter.id == butter.id
    assert get_sibling_variants(repo, butter.id) == []

