"""Branded, GST-correct Tax Invoice PDF (reportlab).

`render_invoice(repo, bill_id)` pulls a finalized bill + shop profile from the
repository and lays out a professional Indian tax invoice: branded header band,
item table with per-line CGST/SGST, a totals block, a rate-wise tax summary,
amount in words, and payment details. All money/qty/word formatting is delegated
to the domain layer (`money`, and the repo-supplied `qty_display`); this module
only arranges already-computed values on the page.
"""

from __future__ import annotations

import os
from functools import lru_cache

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..config import ARTIFACTS_DIR
from ..domain import money

# Brand palette (kirana green).
_BRAND = colors.HexColor("#1B7A43")
_BRAND_DARK = colors.HexColor("#12522D")
_ACCENT = colors.HexColor("#F2F7F3")
_GREY = colors.HexColor("#666666")
_RUPEE = "₹"

# Candidate (regular, bold) TTF pairs that carry the ₹ (U+20B9) glyph, tried in
# order across common Windows/Linux/macOS locations. First hit wins; else Helvetica.
_WINDIR = os.environ.get("WINDIR", "C:\\Windows")
_FONT_CANDIDATES = [
    (os.path.join(_WINDIR, "Fonts", "segoeui.ttf"),
     os.path.join(_WINDIR, "Fonts", "segoeuib.ttf")),
    (os.path.join(_WINDIR, "Fonts", "arial.ttf"),
     os.path.join(_WINDIR, "Fonts", "arialbd.ttf")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Georgia.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf",
     "/Library/Fonts/Arial Unicode.ttf"),
]

_ASSETS_LOGO = os.path.join(os.getcwd(), "assets", "logo.png")


@lru_cache(maxsize=1)
def _fonts() -> tuple[str, str, bool]:
    """Return (body_font, bold_font, rupee_ok). Registers a Unicode font that
    can render ₹ when one is available; otherwise falls back to Helvetica and
    signals that the ₹ symbol must be spelled "Rs"."""
    for regular, bold in _FONT_CANDIDATES:
        if not os.path.exists(regular):
            continue
        try:
            if not TTFont("probe", regular).face.charToGlyph.get(0x20B9):
                continue
            pdfmetrics.registerFont(TTFont("InvBody", regular))
            pdfmetrics.registerFont(TTFont("InvBold", bold if os.path.exists(bold) else regular))
            return "InvBody", "InvBold", True
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold", False


def render_invoice(repo, bill_id: int, out_dir: str | None = None) -> str:
    """Render bill ``bill_id`` to a branded GST tax-invoice PDF.

    Returns the absolute path to the written file.
    """
    bd = repo.get_bill(bill_id)
    bill = bd.bill
    profile = repo.get_shop_profile()

    body, bold, rupee_ok = _fonts()
    rupee = _RUPEE if rupee_ok else "Rs"

    def inr(paise: int) -> str:
        s = money.format_inr(paise)
        return s if rupee_ok else s.replace(_RUPEE, "Rs ")

    # --- styles ---
    normal = ParagraphStyle("n", fontName=body, fontSize=8.5, leading=11)
    small = ParagraphStyle("s", fontName=body, fontSize=7.5, leading=9.5, textColor=_GREY)
    small_w = ParagraphStyle("sw", parent=small, textColor=colors.white)
    shop_name_st = ParagraphStyle("sn", fontName=bold, fontSize=18, leading=21,
                                  textColor=colors.white)
    title_st = ParagraphStyle("t", fontName=bold, fontSize=15, leading=18,
                              textColor=_BRAND_DARK)
    item_st = ParagraphStyle("it", fontName=body, fontSize=8, leading=10)
    words_st = ParagraphStyle("wd", fontName=body, fontSize=8.5, leading=12)
    foot_st = ParagraphStyle("f", fontName=body, fontSize=7.5, leading=10,
                             textColor=_GREY, alignment=1)

    out_dir = out_dir or str(ARTIFACTS_DIR)
    os.makedirs(out_dir, exist_ok=True)
    filename = f"invoice_{bill.invoice_no or bill_id}.pdf"
    path = os.path.abspath(os.path.join(out_dir, filename))

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=13 * mm, bottomMargin=15 * mm,
        title=f"Tax Invoice {bill.invoice_no or bill_id}",
    )
    avail_w = doc.width
    story: list = []

    # Branded header band: logo (optional), shop name, address, GSTIN
    shop_name = profile.name or "Kirana Store"
    contact_bits = []
    if profile.address:
        contact_bits.append(profile.address)
    if profile.phone:
        contact_bits.append(f"Ph: {profile.phone}")
    gstin_line = f"GSTIN: {profile.gstin}" if profile.gstin else ""

    text_block = [Paragraph(shop_name, shop_name_st)]
    if contact_bits:
        text_block.append(Paragraph(" &nbsp;|&nbsp; ".join(contact_bits), small_w))
    if gstin_line:
        text_block.append(Paragraph(gstin_line, small_w))

    if os.path.exists(_ASSETS_LOGO):
        try:
            logo = Image(_ASSETS_LOGO, width=18 * mm, height=18 * mm)
            logo.hAlign = "CENTER"
            header_cells = [[logo, text_block]]
            header = Table(header_cells, colWidths=[22 * mm, avail_w - 22 * mm])
        except Exception:
            header = Table([[text_block]], colWidths=[avail_w])
    else:
        header = Table([[text_block]], colWidths=[avail_w])

    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BRAND),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(header)
    story.append(Spacer(1, 6))

    # Title and invoice meta
    inv_date = (bill.finalized_at or bill.created_at or "")[:10]
    meta_right = (
        f'<b>Invoice No:</b> {bill.invoice_no or "—"}<br/>'
        f'<b>Date:</b> {inv_date or "—"}'
    )
    title_tbl = Table(
        [[Paragraph("TAX INVOICE", title_st),
          Paragraph(meta_right, ParagraphStyle("mr", parent=normal, alignment=2))]],
        colWidths=[avail_w * 0.5, avail_w * 0.5],
    )
    title_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 1, _BRAND),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(title_tbl)
    story.append(Spacer(1, 5))

    customer = bill.customer_name or "Walk-in / Cash"
    story.append(Paragraph(f'<b>Bill to:</b> {customer}', normal))
    story.append(Spacer(1, 6))

    # Item table
    head = ["#", "Item", "HSN", "Qty", f"Rate ({rupee})", f"Taxable ({rupee})",
            "GST%", f"CGST ({rupee})", f"SGST ({rupee})", f"Amount ({rupee})"]
    data = [head]
    for i, ln in enumerate(bd.lines, start=1):
        zero = ln.gst_rate == 0
        data.append([
            str(i),
            Paragraph(ln.name, item_st),
            "-",  # HSN filled below if snapshot carries it
            ln.qty_display,
            inr(ln.unit_sell_paise),
            inr(ln.taxable_paise),
            f"{ln.gst_rate}%",
            "" if zero else inr(ln.cgst_paise),
            "" if zero else inr(ln.sgst_paise),
            inr(ln.line_total_paise),
        ])

    # HSN comes from the product (not snapshotted on the line); look it up.
    for i, ln in enumerate(bd.lines, start=1):
        prod = repo.get_product(ln.product_id)
        data[i][2] = (prod.hsn_code if prod and prod.hsn_code else "-")

    col_w = [
        avail_w * 0.035, avail_w * 0.245, avail_w * 0.075, avail_w * 0.085,
        avail_w * 0.095, avail_w * 0.105, avail_w * 0.055, avail_w * 0.09,
        avail_w * 0.09, avail_w * 0.125,
    ]
    items = Table(data, colWidths=col_w, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 1), (-1, -1), body),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),      # #
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),      # Qty..Amount numerics
        ("ALIGN", (6, 0), (6, -1), "CENTER"),      # GST%
        ("ALIGN", (2, 0), (2, -1), "CENTER"),      # HSN
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, _BRAND_DARK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ACCENT]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    items.setStyle(TableStyle(style))
    story.append(items)
    story.append(Spacer(1, 10))

    # Tax summary (left) + totals (right)
    tax_rows = [["Rate", f"Taxable ({rupee})", f"CGST ({rupee})", f"SGST ({rupee})"]]
    for s in bd.slabs:
        tax_rows.append([
            f"{s['rate']}%",
            inr(s["taxable_paise"]),
            "" if s["rate"] == 0 else inr(s["cgst_paise"]),
            "" if s["rate"] == 0 else inr(s["sgst_paise"]),
        ])
    tax_tbl = Table(tax_rows, colWidths=[avail_w * 0.09, avail_w * 0.14,
                                         avail_w * 0.115, avail_w * 0.115])
    tax_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTNAME", (0, 1), (-1, -1), body),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), _ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), _BRAND_DARK),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    def trow(label, value, boldrow=False):
        lst = ParagraphStyle("tl", fontName=bold if boldrow else body,
                             fontSize=9 if boldrow else 8.5, leading=12,
                             textColor=_BRAND_DARK if boldrow else colors.black)
        vst = ParagraphStyle("tv", parent=lst, alignment=2)
        return [Paragraph(label, lst), Paragraph(value, vst)]

    totals_rows = [
        trow("Taxable subtotal", inr(bill.subtotal_paise)),
        trow("CGST", inr(bill.cgst_paise)),
        trow("SGST", inr(bill.sgst_paise)),
        trow("Round-off", inr(bill.round_off_paise)),
        trow("GRAND TOTAL", inr(bill.total_paise), boldrow=True),
    ]
    totals_tbl = Table(totals_rows, colWidths=[avail_w * 0.24, avail_w * 0.20])
    totals_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, _BRAND),
        ("BACKGROUND", (0, -1), (-1, -1), _ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))

    summary_head = ParagraphStyle("sh", fontName=bold, fontSize=8.5,
                                  leading=11, textColor=_BRAND_DARK)
    left_block = Table([[Paragraph("Tax Summary", summary_head)], [tax_tbl]],
                       colWidths=[avail_w * 0.46])
    left_block.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (0, 0), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))

    combo = Table([[left_block, totals_tbl]],
                  colWidths=[avail_w * 0.50, avail_w * 0.50])
    combo.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(combo)
    story.append(Spacer(1, 10))

    # Amount in words and payment mode
    words = money.amount_in_words(bill.total_paise)
    story.append(Paragraph(f'<b>Amount in words:</b> {words}', words_st))
    story.append(Spacer(1, 4))

    if bill.payment_mode:
        pay = f'<b>Payment:</b> {bill.payment_mode.upper()}'
        if bill.payment_ref:
            pay += f' &nbsp; <b>Ref:</b> {bill.payment_ref}'
    else:
        pay = '<b>Payment:</b> —'
    story.append(Paragraph(pay, normal))
    story.append(Spacer(1, 16))

    # Footer
    story.append(Paragraph(f"Thank you for shopping with {shop_name}!", foot_st))
    story.append(Paragraph("This is a computer-generated invoice.", foot_st))

    doc.build(story)
    return path
