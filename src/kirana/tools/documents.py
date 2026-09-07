"""Document generation tools: invoice PDF and analysis deck (PPTX).

These tools render files via the artifact renderers and queue them for the
Telegram layer to send. They are thin adapters — all data access is handled
inside the render functions.
"""

from __future__ import annotations

from datetime import date, timedelta

from .base import tool

from ..artifacts.analysis_deck import render_deck
from ..artifacts.invoice_pdf import render_invoice
from ..artifacts.sales_report_pdf import render_sales_report_pdf
from ..db.connection import today_iso
from ..db.repository import ValidationError
from ._helpers import arg, guard, ok
from .context import current_chat, get_repo, queue_output_file


@tool(
    "generate_invoice_pdf",
    "Generate a PDF invoice for a finalized bill and queue it to be sent. Identify "
    "the bill by bill_id, by invoice_no (e.g. 'INV-20260905-0001'), or by daily bill number and date "
    "(e.g. bill_no=2, date='today' or '2026-09-03'). If none given, the most recently finalized bill is used.",
    {
        "type": "object",
        "properties": {
            "bill_id": {"type": "integer"},
            "invoice_no": {"type": "string"},
            "bill_no": {"type": "integer", "description": "Daily bill sequence number (e.g. 1, 2, 3)"},
            "date": {"type": "string", "description": "Date of the bill: 'today', 'yesterday', or YYYY-MM-DD"},
        },
        "required": [],
    },
)
@guard
async def generate_invoice_pdf(args: dict) -> dict:
    repo = get_repo()
    bill_id = arg(args, "bill_id")
    invoice_no = arg(args, "invoice_no")
    bill_no = arg(args, "bill_no")
    date_arg = arg(args, "date")

    if bill_id is None and invoice_no:
        bd = repo.get_bill_by_invoice(invoice_no)
        if bd is None:
            raise ValidationError(f"No bill found with invoice number {invoice_no}.")
        bill_id = bd.bill.id
    elif bill_id is None and bill_no is not None:
        target_date = today_iso()
        if date_arg:
            clean = str(date_arg).strip().lower()
            if clean in ("today", "aaj", "indru"):
                target_date = today_iso()
            elif clean in ("yesterday", "kal", "netru"):
                target_date = (date.fromisoformat(today_iso()) - timedelta(days=1)).isoformat()
            else:
                try:
                    target_date = date.fromisoformat(clean).isoformat()
                except Exception:
                    pass
        bd = repo.get_bill_by_date_and_seq(target_date, int(bill_no))
        if bd is not None:
            bill_id = bd.bill.id
        else:
            matches = repo.find_bills_by_seq(int(bill_no))
            if len(matches) == 1:
                bill_id = matches[0].bill.id
            elif len(matches) > 1:
                cands = ", ".join(m.bill.reference_display(today_iso()) for m in matches[:3])
                raise ValidationError(f"Found multiple bills with number #{bill_no}: {cands}. Please specify date.")
            else:
                raise ValidationError(f"No Bill #{bill_no} found for {target_date}.")

    if bill_id is None:
        bill_id = repo.latest_finalized_bill_id(current_chat.get())
        if bill_id is None:
            raise ValidationError("No finalized bill yet to make a PDF from.")

    path = render_invoice(repo, bill_id)
    bd = repo.get_bill(bill_id)
    caption = f"Invoice PDF ({bd.bill.reference_display(today_iso())})"
    queue_output_file(path, caption=caption)
    return ok(f"Invoice PDF is ready for {bd.bill.reference_display(today_iso())} and will be sent.", bill_id=bill_id, invoice_no=bd.bill.invoice_no, file=path)


@tool(
    "generate_analysis_deck",
    "Generate a business analysis PowerPoint deck for a date range in the requested language "
    "(English, Tamil, or Hindi) and queue it to be sent. "
    "Use when the owner asks for a sales report, analysis, daily sales PPT, or summary presentation. "
    "For daily sales PPT, set start=today and end=today. "
    "When the owner requests a daily sales PPT, always ask them in which language they prefer before generating, "
    "unless they already specified the language in their message.",
    {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD format (inclusive). For daily sales PPT, use today's date.",
            },
            "end": {
                "type": "string",
                "description": "End date in YYYY-MM-DD format (inclusive). Defaults to today.",
            },
            "title": {
                "type": "string",
                "description": "Optional custom title for the deck.",
            },
            "language": {
                "type": "string",
                "description": "Language code: 'en' (English), 'ta' (Tamil / தமிழ்), or 'hi' (Hindi / हिंदी). Defaults to 'en'.",
            },
        },
        "required": [],
    },
)
@guard
async def generate_analysis_deck(args: dict) -> dict:
    end: str = arg(args, "end") or today_iso()
    start: str = arg(args, "start") or (
        date.fromisoformat(end) - timedelta(days=6)
    ).isoformat()
    title: str | None = arg(args, "title")
    lang_raw: str = str(arg(args, "language", "en")).strip().lower()
    if "tam" in lang_raw or "தமிழ்" in lang_raw or lang_raw == "ta" or "2" == lang_raw:
        language = "ta"
    elif "hin" in lang_raw or "हिंदी" in lang_raw or lang_raw == "hi" or "3" == lang_raw:
        language = "hi"
    else:
        language = "en"

    path = render_deck(get_repo(), start, end, title=title, language=language)
    lang_label = {"en": "English", "ta": "Tamil (தமிழ்)", "hi": "Hindi (हिंदी)"}.get(language, "English")
    queue_output_file(path, caption=f"Sales Analysis Deck ({lang_label})")
    return ok(
        f"Analysis deck (PPTX in {lang_label}) is ready and will be sent.",
        period=f"{start}..{end}",
        language=language,
        file=path,
    )


@tool(
    "generate_sales_report_pdf",
    "Generate an executive Daily / Periodic Sales & Operations Report in PDF format and queue it to be sent. "
    "Use when the owner requests a sales document in PDF format, or asks for 'sales report PDF' or 'daily sales PDF'.",
    {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD format (inclusive). Defaults to today.",
            },
            "end": {
                "type": "string",
                "description": "End date in YYYY-MM-DD format (inclusive). Defaults to today.",
            },
        },
        "required": [],
    },
)
@guard
async def generate_sales_report_pdf(args: dict) -> dict:
    end: str = arg(args, "end") or today_iso()
    start: str = arg(args, "start") or end
    path = render_sales_report_pdf(get_repo(), start, end)
    is_daily = (start == end)
    caption = f"Daily Sales Report ({start})" if is_daily else f"Sales Report ({start}..{end})"
    queue_output_file(path, caption=caption)
    return ok(
        "Sales report PDF is ready and will be sent.",
        period=f"{start}..{end}",
        file=path,
    )


DOCUMENT_TOOLS = [generate_invoice_pdf, generate_analysis_deck, generate_sales_report_pdf]
