"""GST computation for intra-state kirana sales.

Pricing convention (Indian kirana norm): the shelf/sell price is **GST-inclusive**
(the MRP-style "₹62 for butter, tax included"). We therefore back-calculate the
taxable value out of the inclusive line total, then split the tax into equal
CGST + SGST for an intra-state sale. Everything is integer paise; the CGST/SGST
split always reconciles exactly to the line tax (odd paise go to CGST).

Valid slabs: 0, 5, 12, 18, 28 (%).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .money import round_to_rupee
from .units import MILLI

VALID_SLABS = (0, 5, 12, 18, 28)


@dataclass(frozen=True)
class LineTax:
    rate: int
    line_total_paise: int  # GST-inclusive
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int

    @property
    def tax_paise(self) -> int:
        return self.cgst_paise + self.sgst_paise


@dataclass(frozen=True)
class SlabSummary:
    rate: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int


@dataclass
class BillTotals:
    subtotal_paise: int = 0  # sum of taxable values
    cgst_paise: int = 0
    sgst_paise: int = 0
    total_before_round_paise: int = 0
    round_off_paise: int = 0
    grand_total_paise: int = 0
    slabs: list[SlabSummary] = field(default_factory=list)

    @property
    def total_tax_paise(self) -> int:
        return self.cgst_paise + self.sgst_paise


def line_total_paise(unit_sell_paise: int, base_qty: int) -> int:
    """Inclusive line total = per-display-unit price × quantity (in display units)."""
    val = Decimal(unit_sell_paise) * Decimal(base_qty) / Decimal(MILLI)
    return int(val.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def compute_line_tax(line_total: int, rate: int) -> LineTax:
    """Split a GST-inclusive line total into taxable + CGST + SGST."""
    if rate not in VALID_SLABS:
        raise ValueError(f"Invalid GST slab {rate}. Valid: {VALID_SLABS}")
    if rate == 0:
        return LineTax(0, line_total, line_total, 0, 0)
    taxable = int(
        (Decimal(line_total) * Decimal(100) / Decimal(100 + rate)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )
    tax = line_total - taxable
    cgst = tax - tax // 2  # ceil half → CGST gets the odd paise
    sgst = tax // 2
    return LineTax(rate, line_total, taxable, cgst, sgst)


def summarize(lines: list[LineTax]) -> BillTotals:
    """Aggregate line taxes into a bill-level breakup with rupee round-off."""
    totals = BillTotals()
    by_slab: dict[int, list[int]] = {}
    for lt in lines:
        totals.subtotal_paise += lt.taxable_paise
        totals.cgst_paise += lt.cgst_paise
        totals.sgst_paise += lt.sgst_paise
        agg = by_slab.setdefault(lt.rate, [0, 0, 0])
        agg[0] += lt.taxable_paise
        agg[1] += lt.cgst_paise
        agg[2] += lt.sgst_paise

    totals.total_before_round_paise = (
        totals.subtotal_paise + totals.cgst_paise + totals.sgst_paise
    )
    grand, round_off = round_to_rupee(totals.total_before_round_paise)
    totals.grand_total_paise = grand
    totals.round_off_paise = round_off
    totals.slabs = [
        SlabSummary(rate, v[0], v[1], v[2]) for rate, v in sorted(by_slab.items())
    ]
    return totals
