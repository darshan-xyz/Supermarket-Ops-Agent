"""Money as integer paise. Never use floats for money.

All amounts flow through the system as integer paise (1 rupee = 100 paise).
Rupees appear only at the edges (parsing owner input, rendering replies/PDFs).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from num2words import num2words


def to_paise(rupees: Decimal | float | int | str) -> int:
    """Parse a rupee amount (e.g. '62', '61.50', 62.5) into integer paise."""
    d = rupees if isinstance(rupees, Decimal) else Decimal(str(rupees))
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rupees(paise: int) -> Decimal:
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))


def _group_indian(int_part: str) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567."""
    if len(int_part) <= 3:
        return int_part
    head, tail = int_part[:-3], int_part[-3:]
    # group the head in pairs from the right
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def format_inr(paise: int, symbol: str = "₹") -> str:
    """Render paise as '₹12,34,567.50' (Indian grouping)."""
    neg = paise < 0
    p = abs(paise)
    whole = p // 100
    frac = p % 100
    s = f"{symbol}{_group_indian(str(whole))}.{frac:02d}"
    return f"-{s}" if neg else s


def amount_in_words(paise: int) -> str:
    """'Rupees One Thousand Two Hundred Thirty Four and Fifty Paise Only'."""
    p = abs(paise)
    whole = p // 100
    frac = p % 100
    rupee_words = num2words(whole, lang="en_IN").title()
    out = f"Rupees {rupee_words}"
    if frac:
        out += f" and {num2words(frac, lang='en_IN').title()} Paise"
    return out + " Only"


def round_to_rupee(paise: int) -> tuple[int, int]:
    """Round a paise total to the nearest rupee.

    Returns (rounded_total_paise, round_off_paise) where
    round_off = rounded - original (signed, in paise).
    """
    remainder = paise % 100
    if remainder == 0:
        return paise, 0
    if remainder >= 50:
        rounded = paise - remainder + 100
    else:
        rounded = paise - remainder
    return rounded, rounded - paise
