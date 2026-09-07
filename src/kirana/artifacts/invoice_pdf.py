"""Branded, GST-correct Tax Invoice PDF (reportlab).

`render_invoice(repo, bill_id, out_dir)` compiles a finalized bill into a modern,
creative executive-grade Indian tax invoice featuring two-tone brand headers,
metadata cards, zebra-striped item tables, rate-wise tax matrix, highlighted
grand total cards, amount in words, and digital verification seal.
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
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from PIL import Image as PILImage


from ..config import ARTIFACTS_DIR
from ..domain import money

# Brand palette
_BRAND_PRIMARY = colors.HexColor("#0A3521")      # Deep Forest Green
_BRAND_ACCENT = colors.HexColor("#10B981")       # Emerald Accent
_BRAND_LIGHT = colors.HexColor("#ECFDF5")        # Mint Tint
_SLATE_DARK = colors.HexColor("#0F172A")         # Slate 900
_SLATE_BODY = colors.HexColor("#334155")         # Slate 700
_SLATE_MUTED = colors.HexColor("#64748B")        # Slate 500
_SLATE_LIGHT = colors.HexColor("#F8FAFC")        # Slate 50 (Zebra)
_BORDER_COLOR = colors.HexColor("#E2E8F0")       # Slate 200
_BORDER_SOFT = colors.HexColor("#CBD5E1")        # Slate 300

_RUPEE = "₹"

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
    bd = repo.get_bill(bill_id)
    bill = bd.bill
    profile = repo.get_shop_profile()

    body, bold, rupee_ok = _fonts()

    def inr(paise: int) -> str:
        s = money.format_inr(paise)
        return s if rupee_ok else s.replace(_RUPEE, "Rs ")

    # Typography styles
    normal = ParagraphStyle("in", fontName=body, fontSize=8, leading=10.5, textColor=_SLATE_BODY)
    small_muted = ParagraphStyle("ism", fontName=body, fontSize=7, leading=9, textColor=_SLATE_MUTED)
    item_st = ParagraphStyle("iitem", fontName=bold, fontSize=8, leading=10, textColor=_SLATE_DARK)
    cell_r = ParagraphStyle("icr", fontName=body, fontSize=7.5, leading=9.5, alignment=2, textColor=_SLATE_BODY)
    cell_b_r = ParagraphStyle("icbr", fontName=bold, fontSize=7.5, leading=9.5, alignment=2, textColor=_SLATE_DARK)
    cell_b_c = ParagraphStyle("icbc", fontName=bold, fontSize=7.5, leading=9.5, alignment=1, textColor=_SLATE_DARK)

    out_dir = out_dir or str(ARTIFACTS_DIR)
    os.makedirs(out_dir, exist_ok=True)
    filename = f"invoice_{bill.invoice_no or bill_id}.pdf"
    path = os.path.abspath(os.path.join(out_dir, filename))

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm,
        title=f"Tax Invoice {bill.invoice_no or bill_id}",
    )
    avail_w = doc.width
    story: list = []

    # -------------------------------------------------------------
    # 1. TWO-TONE BRAND HEADER WITH INVOICE CHIP
    # -------------------------------------------------------------
    shop_name = profile.name or "Kirana Supermarket"
    inv_no = bill.invoice_no or f"INV-{bill_id:05d}"
    inv_date = (bill.finalized_at or bill.created_at or "")[:10]

    # Optional store logo
    logo_flowable = None
    if profile.logo_path and os.path.exists(profile.logo_path):
        try:
            with PILImage.open(profile.logo_path) as pil_img:
                lw, lh = pil_img.size
            max_w_mm, max_h_mm = 18.0, 16.0
            scale = min(max_w_mm / (lw or 1), max_h_mm / (lh or 1))
            logo_w = max(lw * scale, 8.0) * mm
            logo_h = max(lh * scale, 8.0) * mm
            logo_flowable = RLImage(profile.logo_path, width=logo_w, height=logo_h)
        except Exception:
            logo_flowable = None

    text_lines = [
        Paragraph(f'<font size=15 color="#FFFFFF"><b>{shop_name}</b></font>',
                  ParagraphStyle("hn", fontName=bold, fontSize=15, leading=18, textColor=colors.white)),
        Spacer(1, 1.2 * mm),
        Paragraph(f'<font color="#A7F3D0"><b>LOCATION:</b></font> {profile.address or "Retail Store"} &nbsp; '
                  f'<font color="#A7F3D0"><b>GSTIN:</b></font> {profile.gstin or "UNREGISTERED"}',
                  ParagraphStyle("hsub", fontName=body, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#E2E8F0"))),
    ]

    if logo_flowable:
        logo_col_w = 20 * mm
        text_col_w = avail_w * 0.60 - logo_col_w
        logo_subtable = Table([[logo_flowable, text_lines]], colWidths=[logo_col_w, text_col_w])
        logo_subtable.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        hdr_left = [logo_subtable]
    else:
        hdr_left = text_lines

    bill_seq = bill.daily_seq or bill.id
    biller_tag = f" &nbsp;|&nbsp; CASHIER: <b>{bill.biller_name}</b>" if bill.biller_name else ""
    hdr_right = [
        Paragraph(f'<font color="#34D399"><b>●</b></font> <font color="#FFFFFF"><b>TAX INVOICE &nbsp;|&nbsp; BILL #{bill_seq}</b></font>',
                  ParagraphStyle("hrt", fontName=bold, fontSize=8.5, leading=10.5, alignment=2, textColor=colors.white)),
        Spacer(1, 1 * mm),
        Paragraph(f'<font size=12 color="#FFFFFF"><b>{inv_no}</b></font>',
                  ParagraphStyle("hrd", fontName=bold, fontSize=12, leading=14, alignment=2, textColor=colors.white)),
        Spacer(1, 1 * mm),
        Paragraph(f'<font color="#A7F3D0"><b>DATE:</b> {inv_date}</font>{biller_tag} &nbsp;<font color="#94A3B8">| ORIGINAL</font>',
                  ParagraphStyle("hrg", fontName=body, fontSize=6.5, leading=8.5, alignment=2, textColor=colors.HexColor("#CBD5E1"))),
    ]

    header_table = Table([[hdr_left, hdr_right]], colWidths=[avail_w * 0.58, avail_w * 0.42])

    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BRAND_PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 2.5, _BRAND_ACCENT),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 3.5 * mm))

    # -------------------------------------------------------------
    # 2. METADATA CARDS: Billed To & Payment Specifics
    # -------------------------------------------------------------
    customer = bill.customer_name or "Walk-in Customer"
    payment_mode = (bill.payment_mode or "CASH").upper()

    pay_badge_colors = {
        "UPI": "#7C3AED",
        "CASH": "#059669",
        "KHATA": "#D97706",
        "CARD": "#2563EB",
    }
    mode_color = pay_badge_colors.get(payment_mode, "#059669")
    ref_part = f" &nbsp;•&nbsp; Ref: <b>{bill.payment_ref}</b>" if bill.payment_ref else ""
    phone_line = f" &nbsp;•&nbsp; Mob: <b>+91 {bill.customer_phone}</b>" if bill.customer_phone else ""
    cashier_note = f" &nbsp;•&nbsp; Cashier: <b>{bill.biller_name}</b>" if bill.biller_name else ""

    billed_to_card = [
        Paragraph("<b>BILLED TO (CUSTOMER)</b>", ParagraphStyle("bc1", fontName=bold, fontSize=7, leading=8.5, textColor=_BRAND_PRIMARY)),
        Spacer(1, 1 * mm),
        Paragraph(f'<font size=9.5 color="#0F172A"><b>{customer}</b></font>{phone_line}',
                  ParagraphStyle("bc2", fontName=bold, fontSize=9.5, leading=11.5, textColor=_SLATE_DARK)),
        Spacer(1, 0.5 * mm),
        Paragraph('<font color="#64748B">Account: Retail Tab &nbsp;|&nbsp; Place of Supply: Intra-state</font>', small_muted),
    ]

    payment_card = [
        Paragraph("<b>PAYMENT STATUS & SETTLEMENT</b>", ParagraphStyle("bp1", fontName=bold, fontSize=7, leading=8.5, textColor=_BRAND_PRIMARY)),
        Spacer(1, 1 * mm),
        Paragraph(f'<font color="{mode_color}"><b>● {payment_mode}</b></font>{ref_part}',
                  ParagraphStyle("bp2", fontName=bold, fontSize=9, leading=11, textColor=_SLATE_DARK)),
        Spacer(1, 0.5 * mm),
        Paragraph(f'<font color="#059669"><b>● Paid & Settled</b></font>{cashier_note}', small_muted),
    ]

    meta_tbl = Table([[billed_to_card, payment_card]], colWidths=[avail_w * 0.50, avail_w * 0.50])
    meta_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _SLATE_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _BORDER_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 3.5 * mm))

    # -------------------------------------------------------------
    # 3. ITEMIZED TAX BILLING TABLE
    # -------------------------------------------------------------
    head = [
        Paragraph("<b>#</b>", ParagraphStyle("ih0", fontName=bold, fontSize=7, leading=8.5, alignment=1, textColor=colors.white)),
        Paragraph("<b>ITEM DESCRIPTION</b>", ParagraphStyle("ih1", fontName=bold, fontSize=7, leading=8.5, textColor=colors.white)),
        Paragraph("<b>HSN</b>", ParagraphStyle("ih2", fontName=bold, fontSize=7, leading=8.5, alignment=1, textColor=colors.white)),
        Paragraph("<b>QTY</b>", ParagraphStyle("ih3", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        Paragraph("<b>RATE</b>", ParagraphStyle("ih4", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        Paragraph("<b>TAXABLE</b>", ParagraphStyle("ih5", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        Paragraph("<b>GST</b>", ParagraphStyle("ih6", fontName=bold, fontSize=7, leading=8.5, alignment=1, textColor=colors.white)),
        Paragraph("<b>CGST</b>", ParagraphStyle("ih7", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        Paragraph("<b>SGST</b>", ParagraphStyle("ih8", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        Paragraph("<b>TOTAL</b>", ParagraphStyle("ih9", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
    ]

    data = [head]
    for i, ln in enumerate(bd.lines, start=1):
        zero = (ln.gst_rate == 0)
        prod = repo.get_product(ln.product_id)
        hsn = prod.hsn_code if prod and prod.hsn_code else "-"
        data.append([
            Paragraph(f"<font color='#64748B'>{i}</font>", cell_b_c),
            Paragraph(ln.name, item_st),
            Paragraph(f"<font color='#64748B'>{hsn}</font>", cell_b_c),
            Paragraph(ln.qty_display, cell_r),
            Paragraph(inr(ln.unit_sell_paise), cell_r),
            Paragraph(inr(ln.taxable_paise), cell_r),
            Paragraph(f"<font color='#059669'><b>{ln.gst_rate}%</b></font>", cell_b_c),
            Paragraph("-" if zero else inr(ln.cgst_paise), cell_r),
            Paragraph("-" if zero else inr(ln.sgst_paise), cell_r),
            Paragraph(f"<b>{inr(ln.line_total_paise)}</b>", cell_b_r),
        ])

    col_w = [
        avail_w * 0.04,  # #
        avail_w * 0.28,  # Item
        avail_w * 0.08,  # HSN
        avail_w * 0.08,  # Qty
        avail_w * 0.09,  # Rate
        avail_w * 0.10,  # Taxable
        avail_w * 0.06,  # GST%
        avail_w * 0.08,  # CGST
        avail_w * 0.08,  # SGST
        avail_w * 0.11,  # Total
    ]

    items_table = Table(data, colWidths=col_w, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND_PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _BORDER_COLOR),
        ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _SLATE_LIGHT]),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 3.5 * mm))

    # -------------------------------------------------------------
    # 4. TAX SUMMARY & GRAND TOTAL SPOTLIGHT
    # -------------------------------------------------------------
    tax_rows = [
        [
            Paragraph("<b>RATE</b>", ParagraphStyle("th1", fontName=bold, fontSize=7, leading=8.5, alignment=1, textColor=colors.white)),
            Paragraph("<b>TAXABLE</b>", ParagraphStyle("th2", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>CGST</b>", ParagraphStyle("th3", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>SGST</b>", ParagraphStyle("th4", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>TAX TOTAL</b>", ParagraphStyle("th5", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        ]
    ]
    for s in bd.slabs:
        slab_tax = s["cgst_paise"] + s["sgst_paise"]
        tax_rows.append([
            Paragraph(f"<b>{s['rate']}%</b>", cell_b_c),
            Paragraph(inr(s["taxable_paise"]), cell_r),
            Paragraph("-" if s["rate"] == 0 else inr(s["cgst_paise"]), cell_r),
            Paragraph("-" if s["rate"] == 0 else inr(s["sgst_paise"]), cell_r),
            Paragraph(inr(slab_tax), cell_b_r),
        ])

    tax_w = avail_w * 0.52
    tax_tbl = Table(tax_rows, colWidths=[tax_w * 0.16, tax_w * 0.23, tax_w * 0.19, tax_w * 0.19, tax_w * 0.23])
    tax_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND_PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _BORDER_COLOR),
        ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _SLATE_LIGHT]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))

    # Totals Card
    tot_w = avail_w * 0.45
    totals_rows = [
        [Paragraph("Taxable Subtotal:", normal), Paragraph(inr(bill.subtotal_paise), cell_r)],
        [Paragraph("Total CGST:", normal), Paragraph(inr(bill.cgst_paise), cell_r)],
        [Paragraph("Total SGST:", normal), Paragraph(inr(bill.sgst_paise), cell_r)],
        [Paragraph("Round-off Adjustment:", normal), Paragraph(inr(bill.round_off_paise), cell_r)],
        [
            Paragraph('<font color="#FFFFFF"><b>NET GRAND TOTAL:</b></font>',
                      ParagraphStyle("gth", fontName=bold, fontSize=9, leading=11, textColor=colors.white)),
            Paragraph(f'<font color="#FFFFFF"><b>{inr(bill.total_paise)}</b></font>',
                      ParagraphStyle("gtv", fontName=bold, fontSize=12, leading=14, alignment=2, textColor=colors.white)),
        ],
    ]
    totals_tbl = Table(totals_rows, colWidths=[tot_w * 0.55, tot_w * 0.45])
    totals_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, _BORDER_COLOR),
        ("BACKGROUND", (0, -1), (-1, -1), _BRAND_PRIMARY),
        ("TOPPADDING", (0, 0), (-1, -2), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -2), 2.5),
        ("TOPPADDING", (0, -1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
    ]))

    combo_tbl = Table([
        [
            Paragraph('<font color="#10B981">●</font> <b>GST Rate-wise Breakdown</b>', ParagraphStyle("thl", fontName=bold, fontSize=8.5, leading=10.5, textColor=_BRAND_PRIMARY)),
            Paragraph('<font color="#10B981">●</font> <b>Invoice Financial Summary</b>', ParagraphStyle("thr", fontName=bold, fontSize=8.5, leading=10.5, textColor=_BRAND_PRIMARY)),
        ],
        [tax_tbl, totals_tbl],
    ], colWidths=[avail_w * 0.53, avail_w * 0.47])
    combo_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2 * mm),
    ]))
    story.append(combo_tbl)
    story.append(Spacer(1, 3.5 * mm))

    # -------------------------------------------------------------
    # 5. AMOUNT IN WORDS & SETTLEMENT CALLOUT
    # -------------------------------------------------------------
    words = money.amount_in_words(bill.total_paise)
    words_cell = [
        Paragraph(f'<b>AMOUNT IN WORDS:</b> <font color="#0F172A"><b>{words}</b></font>',
                  ParagraphStyle("wd", fontName=bold, fontSize=7.5, leading=10, textColor=_BRAND_PRIMARY)),
        Spacer(1, 0.5 * mm),
        Paragraph('<font color="#64748B">Certified that all particulars are true and correct. '
                  'Goods once sold cannot be returned without original bill.</font>', small_muted),
    ]
    words_tbl = Table([[words_cell]], colWidths=[avail_w])
    words_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BRAND_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.75, _BRAND_ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(words_tbl)
    story.append(Spacer(1, 3.5 * mm))

    # -------------------------------------------------------------
    # 6. VERIFIED FOOTER WITH BRAND NOTE
    # -------------------------------------------------------------
    foot_text = (
        f"Thank you for shopping with <b>{shop_name}</b>! &nbsp;|&nbsp; "
        "Digitally Generated by <b>Supermarket Ops Agent</b> &nbsp;|&nbsp; "
        "Computer-Generated GST Tax Invoice (No Signature Required)"
    )
    foot_tbl = Table([[Paragraph(foot_text, ParagraphStyle("fn", fontName=body, fontSize=6.5, leading=8.5, alignment=1, textColor=_SLATE_MUTED))]],
                     colWidths=[avail_w])
    foot_tbl.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.5, _BORDER_SOFT),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(foot_tbl)

    doc.build(story)
    return path
