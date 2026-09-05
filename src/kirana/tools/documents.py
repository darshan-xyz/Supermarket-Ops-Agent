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
    "the bill by bill_id (from a finalize result) OR by invoice_no (e.g. 'INV-00007'). "
    "If neither is given, the most recently finalized bill in this chat is used "
    "(handy for 'send me that bill as a PDF').",
    {
        "type": "object",
        "properties": {
            "bill_id": {"type": "integer"},
            "invoice_no": {"type": "string"},
        },
        "required": [],
    },
)
@guard
async def generate_invoice_pdf(args: dict) -> dict:
    repo = get_repo()
    bill_id = arg(args, "bill_id")
    invoice_no = arg(args, "invoice_no")

    if bill_id is None and invoice_no:
        bd = repo.get_bill_by_invoice(invoice_no)
        if bd is None:
            raise ValidationError(f"No bill found with invoice number {invoice_no}.")
        bill_id = bd.bill.id
    if bill_id is None:
        bill_id = repo.latest_finalized_bill_id(current_chat.get())
        if bill_id is None:
            raise ValidationError("No finalized bill yet to make a PDF from.")

    path = render_invoice(repo, bill_id)
    queue_output_file(path, caption="Invoice PDF")
    return ok("Invoice PDF is ready and will be sent.", bill_id=bill_id, file=path)


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
