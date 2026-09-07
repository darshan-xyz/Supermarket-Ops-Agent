"""Domain logic for product weight, volume, and pack size variants.

In retail, products often share the same parent brand/name but differ by weight,
volume, or pack count (e.g. 'Maggi 70g' vs 'Maggi 10g', 'Aashirvaad Atta 1kg' vs '5kg').

This module extracts base product names, detects active sibling variants in a store,
and determines whether a customer's query explicitly specified a variant.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.repository import Repository
    from .models import Product

# Matches Indian FMCG weight, volume, and pack size specifiers at word boundaries.
# Examples: 70g, 10gm, 500 gms, 1 kg, 5kg, 500ml, 1L, 1.5 ltr, 100 packet, 12 piece.
VARIANT_SPEC_PATTERN = re.compile(
    r"(?:\s*[-–—/]\s*)?\b(\d+(?:\.\d+)?)\s*(kg|kgs|kilo|kilos|g|gm|gms|gram|grams|l|ltr|litre|litres|ml|piece|pieces|pc|pcs|packet|packets|pkt|pkts|pack|packs)\b",
    re.IGNORECASE,
)


def extract_base_and_variant(product_name: str) -> tuple[str, str | None]:
    """Extract the normalized base name and the weight/pack variant string.

    Examples:
        'Maggi 70g' -> ('maggi', '70g')
        'Maggi 10g' -> ('maggi', '10g')
        'Aashirvaad Atta 5kg' -> ('aashirvaad atta', '5kg')
        'Amul Butter 100g' -> ('amul butter', '100g')
        'Loose Sugar' -> ('loose sugar', None)
    """
    cleaned = product_name.strip()
    match = VARIANT_SPEC_PATTERN.search(cleaned)
    if not match:
        return cleaned.lower(), None

    variant_str = match.group(0).strip().lstrip("-–—/ ").lower()
    base_name = VARIANT_SPEC_PATTERN.sub("", cleaned).strip().lower()
    base_name = re.sub(r"\s+", " ", base_name)
    return base_name, variant_str


def is_variant_specified_in_query(user_query: str, target_variant: str) -> bool:
    """Check if the user's message contains the weight or size variant.

    Handles natural variations like '70g', '70 g', '70 gm', '70 grams', or isolated numeric values.
    """
    if not user_query or not target_variant:
        return False

    q_lower = user_query.lower()
    t_lower = target_variant.lower().strip()

    # Direct match: e.g. '70g' or '70 g'
    if t_lower in q_lower:
        return True

    # Parse numeric value and unit from the target variant
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", t_lower)
    if m:
        val_str, unit_str = m.groups()
        patterns = [
            rf"\b{re.escape(val_str)}\s*{re.escape(unit_str)}\b",
            rf"\b{re.escape(val_str)}\s*g(?:m|ms|ram|rams)?\b" if unit_str in ("g", "gm", "gram") else "",
            rf"\b{re.escape(val_str)}\s*k(?:g|gs|ilo|ilos)?\b" if unit_str in ("kg", "kilo") else "",
            rf"\b{re.escape(val_str)}\s*l(?:tr|itre|itres)?\b" if unit_str in ("l", "ltr", "litre") else "",
            rf"\b{re.escape(val_str)}\s*ml\b" if unit_str == "ml" else "",
            rf"\b{re.escape(val_str)}\s*(?:packet|packets|pkt|pkts|pack|packs|pc|pcs|piece|pieces)\b"
            if unit_str in ("packet", "pkt", "pack", "piece", "pc")
            else "",
        ]
        for pat in patterns:
            if pat and re.search(pat, q_lower):
                return True

    return False


def get_sibling_variants(repo: Repository, product_id: int) -> list[Product]:
    """Find all active products in the store sharing the same normalized base name.

    Returns a list of sibling products only if there are at least 2 distinct
    weight/size variants in active inventory.
    """
    target = repo.get_product(product_id)
    if target is None:
        return []

    target_base, target_var = extract_base_and_variant(target.name)
    if not target_var:
        return []

    all_products = repo.list_inventory(active_only=True)
    siblings: list[Product] = []
    distinct_variants: set[str] = set()

    for p in all_products:
        base, var = extract_base_and_variant(p.name)
        if base == target_base and var:
            siblings.append(p)
            distinct_variants.add(var)

    if len(distinct_variants) >= 2:
        return siblings

    return []
