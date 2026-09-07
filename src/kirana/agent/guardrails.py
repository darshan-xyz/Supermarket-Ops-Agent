"""Guardrail Engine for the Kirana Supermarket Ops Agent.

Enforces multi-layer safety invariants across:
1. Domain Boundary & Conversational Scope (zero tolerance for coding, homework, trivia).
2. Prompt Injection & Confidentiality Defense.
3. Legal Metrology Act (selling price <= MRP).
4. FSSAI Food Safety (blocking inward receipt of already-expired stock).
5. Sanitization of customer names, numeric bounds, and analytics date ranges.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Tuple

from ..domain import money

# --------------------------------------------------------------------------- #
# Domain Boundary & Out-of-Scope Detection
# --------------------------------------------------------------------------- #

# Explicit patterns matching programming, software development, academic tutoring,
# homework, and general trivia outside supermarket / retail operations.
_OUT_OF_SCOPE_REGEX = re.compile(
    r"""(?ix)
    \b(?:
        # Programming & code generation
        (?:write|give|create|generate|show|provide|code|script|program)\b.*\b(?:python|javascript|typescript|c\+\+|java|c\#|ruby|rust|golang|html|css|sql|bash|powershell|regex|function|class|algorithm|code|script|program)\b|
        \b(?:python|javascript|typescript|c\+\+|java|c\#|ruby|rust|golang)\b.*\b(?:palindrome|fibonacci|factorial|binary\s*search|bubble\s*sort|two\s*sum|leetcode|linked\s*list|matrix|recursion|prime\s*number)\b|
        \b(?:debug|fix|compile|execute|run)\b.*\b(?:code|script|bug|error|function|program|c\+\+|python)\b|
        \b(?:how\s+to\s+code|how\s+to\s+program|leetcode|hackerrank|codeforces)\b|
        # Academic homework / non-retail trivia / general essays
        \b(?:write\s+an?\s+essay|write\s+a\s+poem|solve\s+this\s+(?:math\s+)?equation|who\s+won\b.*world\s+cup|who\s+is\s+the\s+president|what\s+is\s+photosynthesis)\b|
        # Prompt injections / instruction overrides
        \b(?:ignore\s+all\s+(?:previous|prior)\s+instructions|you\s+are\s+now\s+dan|developer\s+mode\s+enabled|reveal\s+your\s+system\s+prompt|print\s+your\s+(?:system\s+)?instructions|what\s+is\s+your\s+system\s+prompt)\b
    )\b
    """
)

# Operational grocery/retail keywords that guarantee a prompt is IN SCOPE even if it has ambiguous words
_RETAIL_WHITELIST_REGEX = re.compile(
    r"""(?ix)
    \b(?:
        bill|billing|invoice|cart|item|stock|inventory|khata|kadan|udhaar|udhar|
        sarakku|paruppu|arisi|seeni|sugar|atta|maggi|biscuit|oil|milk|paal|
        mrp|gst|cgst|sgst|tax|payment|cash|upi|card|customer|supplier|debtor|
        sales|revenue|profit|discount|reorder|batch|expiry|fefo|store|shop|kadai
    )\b
    """
)


def is_out_of_scope_query(text: str) -> Tuple[bool, str]:
    """Inspect text for out-of-scope tasks (coding, homework, trivia, prompt injection).

    Returns:
        (is_out_of_scope, detected_language)
    """
    clean = text.strip()
    if not clean:
        return False, "en"

    # Detect language of request
    clean_lower = clean.lower()
    is_tamil = bool(re.search(r"[\u0B80-\u0BFF]", clean)) or any(
        w in clean_lower for w in ("sarakku", "kadan", "innaiku", "vanthuchu", "podu", "mudichidu", "evvalavu", "kudunga", "ezhudhu", "ezhudhi", "solli", "theriyuma", "kodu", "enna")
    )
    is_hindi = bool(re.search(r"[\u0900-\u097F]", clean)) or any(
        w in clean_lower for w in ("aaj", "kitna", "batao", "karo", "diye", "udhar", "khata", "namaste", "bhejo", "karein", "chahiye")
    )
    lang = "ta" if is_tamil else ("hi" if is_hindi else "en")

    # If the user explicitly asks for code or trivia
    if _OUT_OF_SCOPE_REGEX.search(clean):
        # Ensure it's not a false positive on legitimate grocery queries
        if not _RETAIL_WHITELIST_REGEX.search(clean):
            return True, lang
        # If it matches programming explicitly (e.g. "python palindrome"), it's out of scope regardless
        if re.search(r"(?i)\b(python|c\+\+|javascript|typescript|java|leetcode|palindrome|fibonacci)\b", clean):
            return True, lang

    return False, lang


def get_out_of_scope_reply(language: str = "en") -> str:
    """Return polite 1-sentence localized refusal and redirection back to store operations."""
    lang = language.lower()
    if "ta" in lang or "tamil" in lang:
        return (
            "நான் உங்கள் அங்காடி செயல்பாட்டு உதவியாளர் (Kirana Operations Co-Pilot). "
            "பில்லிங், சரக்கு இருப்பு, கடன் கணக்கு (Khata) மற்றும் விற்பனை அறிக்கைகள் போன்ற கடை பணிகளுக்கு மட்டுமே என்னால் உதவ முடியும். "
            "உங்கள் கடையில் என்ன செய்ய வேண்டும்?"
        )
    if "hi" in lang or "hindi" in lang:
        return (
            "मैं आपका किराना ऑपरेशंस को-पायलट हूँ और केवल बिलिंग, इन्वेंटरी, खाता और बिक्री रिपोर्ट "
            "जैसी स्टोर प्रबंधन गतिविधियों में ही मदद कर सकता हूँ। "
            "आपके स्टोर के लिए क्या करना है?"
        )
    return (
        "I am your Kirana Operations Co-Pilot and can only assist with store operations "
        "like billing, inventory, khata, and sales reports. How can I assist with your store today?"
    )


# --------------------------------------------------------------------------- #
# Regulatory & Legal Metrology Guardrails (India)
# --------------------------------------------------------------------------- #

class LegalMetrologyError(ValueError):
    """Raised when selling price exceeds Maximum Retail Price (MRP)."""


class FSSAIFoodSafetyError(ValueError):
    """Raised when receiving already-expired food/inventory into store stock."""


def validate_legal_metrology(sell_paise: int, mrp_paise: int) -> None:
    """Enforce the Legal Metrology Act, 2009 (Packaged Commodities Rules).

    Selling price cannot legally exceed the printed Maximum Retail Price (MRP).
    """
    if sell_paise > mrp_paise:
        raise LegalMetrologyError(
            f"Selling price ({money.format_inr(sell_paise)}) cannot exceed "
            f"Maximum Retail Price MRP ({money.format_inr(mrp_paise)}) under the Legal Metrology Act."
        )


def validate_fssai_inward_expiry(expiry_iso: str | None, today_iso_str: str) -> None:
    """Enforce FSSAI Food Safety regulations for stock receipts.

    Stores cannot receive inventory that has already passed its expiry date.
    """
    if not expiry_iso:
        return
    try:
        exp_date = date.fromisoformat(expiry_iso.strip())
        today_date = date.fromisoformat(today_iso_str.strip())
        if exp_date < today_date:
            raise FSSAIFoodSafetyError(
                f"Cannot receive stock with past expiry date {expiry_iso}. "
                f"Item has already expired (today is {today_iso_str})."
            )
    except ValueError as exc:
        if isinstance(exc, FSSAIFoodSafetyError):
            raise
        raise ValueError(f"Invalid expiry date format '{expiry_iso}'. Expected YYYY-MM-DD.") from exc


# --------------------------------------------------------------------------- #
# Data Sanitization & Numeric Limits
# --------------------------------------------------------------------------- #

MAX_QTY_BASE = 100_000_000       # 100,000 display units (e.g. 100,000 packets or kg)
MAX_PRICE_PAISE = 100_000_000     # ₹10,00,000 (10 Lakh Rupees)


def validate_quantity_bounds(qty_base: int, unit_name: str = "units") -> None:
    """Guard against negative, zero, or astronomical quantities that cause overflow."""
    if qty_base <= 0:
        raise ValueError("Quantity must be strictly positive.")
    if qty_base > MAX_QTY_BASE:
        raise ValueError(
            f"Quantity exceeds maximum allowable single-transaction limit ({MAX_QTY_BASE // 1000} {unit_name})."
        )


def validate_price_bounds(price_paise: int, field_name: str = "Price") -> None:
    """Guard against negative, zero, or astronomical prices."""
    if price_paise <= 0:
        raise ValueError(f"{field_name} must be strictly positive.")
    if price_paise > MAX_PRICE_PAISE:
        raise ValueError(
            f"{field_name} exceeds maximum allowable limit of {money.format_inr(MAX_PRICE_PAISE)}."
        )


def sanitize_customer_name(name: str) -> str:
    """Sanitize customer names: enforce non-empty, trim whitespace, check length & characters."""
    clean = name.strip()
    if not clean:
        raise ValueError("Customer name cannot be blank.")
    if len(clean) > 100:
        raise ValueError("Customer name cannot exceed 100 characters.")
    # Remove control characters and newlines
    clean = re.sub(r"[\r\n\t\x00-\x1f\x7f-\x9f]", " ", clean).strip()
    # Normalize multiple spaces
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        raise ValueError("Customer name contains only invalid characters.")
    return clean


def sanitize_employee_name(name: str) -> str:
    """Sanitize employee / cashier name: non-empty, trim whitespace, check length & characters."""
    clean = name.strip()
    if not clean:
        raise ValueError("Employee name cannot be blank.")
    if len(clean) > 100:
        raise ValueError("Employee name cannot exceed 100 characters.")
    clean = re.sub(r"[\r\n\t\x00-\x1f\x7f-\x9f]", " ", clean).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        raise ValueError("Employee name contains only invalid characters.")
    return clean


def validate_phone_number(phone: str | None) -> str | None:
    """Validate and normalize an Indian 10-digit mobile number.

    Accepts: '+91 98450 12345', '+91-9845012345', '09845012345', '919845012345', '9845012345'.
    Returns: Clean 10-digit string e.g. '9845012345', or None if input is empty or None.
    Raises: ValueError if the phone string is non-empty but cannot be resolved to a valid
            10-digit mobile number starting with 6, 7, 8, or 9.
    """
    if phone is None:
        return None
    raw = str(phone).strip()
    if not raw:
        return None

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) != 10 or digits[0] not in "6789":
        raise ValueError(
            f"Invalid mobile number '{raw}'. Please provide a valid 10-digit Indian mobile number."
        )

    return digits


def validate_date_range(start: str, end: str, max_days: int = 366) -> tuple[str, str]:
    """Validate ISO date range to prevent Denial of Service via huge range queries."""
    try:
        d_start = date.fromisoformat(start.strip())
        d_end = date.fromisoformat(end.strip())
    except ValueError as exc:
        raise ValueError(f"Dates must be YYYY-MM-DD. Got start='{start}', end='{end}'.") from exc

    if d_start > d_end:
        raise ValueError(f"Start date ({start}) cannot be after end date ({end}).")

    span_days = (d_end - d_start).days
    if span_days > max_days:
        raise ValueError(
            f"Date range span of {span_days} days exceeds maximum allowable period of {max_days} days."
        )

    return d_start.isoformat(), d_end.isoformat()
