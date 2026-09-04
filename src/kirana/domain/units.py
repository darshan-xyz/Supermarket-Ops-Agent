"""Unit handling for a kirana store.

Quantities are stored as an integer count of **milli-units of the product's own
display unit** (`base = display_qty * 1000`). This keeps loose weights like
0.25 kg exact (250 milli-kg) while never using floats for storage.

A product has exactly one display unit (e.g. sugar -> kg, Maggi -> packet). The
owner may phrase a quantity in a compatible unit within the same *dimension*
(e.g. "500 g" of a kg-priced product, or "half dozen" eggs), which we convert
into the product's unit before scaling to base.

Dimensions:
  - mass   : kg, g          (canonical grams)
  - volume : litre, ml      (canonical millilitres)
  - count  : piece, dozen   (canonical pieces; 1 dozen = 12 pieces)
  - packet : packet         (isolated; cannot convert to piece)
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MILLI = 1000  # base granularity: 1 display unit == 1000 base units

# unit -> (dimension, canonical factor within dimension)
_UNITS: dict[str, tuple[str, Decimal]] = {
    "kg": ("mass", Decimal(1000)),
    "g": ("mass", Decimal(1)),
    "litre": ("volume", Decimal(1000)),
    "l": ("volume", Decimal(1000)),
    "ml": ("volume", Decimal(1)),
    "piece": ("count", Decimal(1)),
    "pc": ("count", Decimal(1)),
    "dozen": ("count", Decimal(12)),
    "packet": ("packet", Decimal(1)),
    "pkt": ("packet", Decimal(1)),
}

# canonical spelling for storage / display
_CANON = {
    "l": "litre",
    "pc": "piece",
    "pkt": "packet",
}

VALID_UNITS = ("kg", "g", "litre", "ml", "packet", "piece", "dozen")


class UnitError(ValueError):
    """Raised when a quantity's unit is incompatible with the product's unit."""


def normalize_unit(unit: str) -> str:
    u = unit.strip().lower()
    u = _CANON.get(u, u)
    if u not in VALID_UNITS:
        raise UnitError(f"Unknown unit '{unit}'. Valid: {', '.join(VALID_UNITS)}")
    return u


def dimension(unit: str) -> str:
    return _UNITS[normalize_unit(unit)][0]


def to_base(qty: Decimal | float | int | str, input_unit: str | None, product_unit: str) -> int:
    """Convert a display quantity into integer base units of `product_unit`.

    If `input_unit` is None, `product_unit` is assumed. Raises UnitError if the
    input unit lives in a different dimension than the product unit.
    """
    q = qty if isinstance(qty, Decimal) else Decimal(str(qty))
    if q < 0:
        raise UnitError("Quantity cannot be negative.")
    prod = normalize_unit(product_unit)
    inp = prod if input_unit is None else normalize_unit(input_unit)

    dim_p, canon_p = _UNITS[prod]
    dim_i, canon_i = _UNITS[inp]
    if dim_p != dim_i:
        raise UnitError(
            f"Cannot express {inp} as {prod} (different measurement types)."
        )
    # qty in product units = qty * (canon_input / canon_product)
    qty_in_product = q * (canon_i / canon_p)
    base = (qty_in_product * MILLI).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(base)


def from_base(base: int, product_unit: str) -> Decimal:
    """Return the display quantity (in product units) for a base amount."""
    return (Decimal(base) / MILLI).normalize()


def format_qty(base: int, product_unit: str) -> str:
    """Human-friendly quantity, downshifting to sub-units when < 1 (e.g. 500 g)."""
    prod = normalize_unit(product_unit)
    display = Decimal(base) / MILLI
    dim = _UNITS[prod][0]
    if dim == "mass" and prod == "kg" and 0 < display < 1:
        grams = (display * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"{_trim(grams)} g"
    if dim == "volume" and prod == "litre" and 0 < display < 1:
        ml = (display * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return f"{_trim(ml)} ml"
    return f"{_trim(display)} {prod}"


def _trim(d: Decimal) -> str:
    """Render a Decimal without trailing zeros ('2.0' -> '2', '0.50' -> '0.5')."""
    d = d.normalize()
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
