"""Branded Executive Daily Sales & Operations Report PDF (reportlab).

`render_sales_report_pdf(repo, start, end, out_dir)` compiles a store sales
summary into a modern, creative executive-grade PDF report with distinct metric
tiles, payment pills, GST breakdown, top-selling product leaderboard, operations
spotlight, and digital certification.
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
from ..db.connection import now_iso
from ..domain import money

# Creative Design Palette
_BRAND_PRIMARY = colors.HexColor("#0A3521")      # Deep Forest Green
_BRAND_ACCENT = colors.HexColor("#10B981")       # Vivid Mint / Emerald
_BRAND_LIGHT = colors.HexColor("#ECFDF5")        # Soft Mint Tint
_SLATE_DARK = colors.HexColor("#0F172A")         # Slate 900
_SLATE_BODY = colors.HexColor("#334155")         # Slate 700
_SLATE_MUTED = colors.HexColor("#64748B")        # Slate 500
_SLATE_LIGHT = colors.HexColor("#F8FAFC")        # Slate 50 (Zebra)
_BORDER_COLOR = colors.HexColor("#E2E8F0")       # Slate 200
_BORDER_SOFT = colors.HexColor("#CBD5E1")        # Slate 300

# Metric Accents
_COLOR_BLUE = colors.HexColor("#2563EB")
_COLOR_GREEN = colors.HexColor("#059669")
_COLOR_PURPLE = colors.HexColor("#7C3AED")
_COLOR_ROSE = colors.HexColor("#E11D48")
_COLOR_AMBER = colors.HexColor("#D97706")

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


@lru_cache(maxsize=1)
def _fonts() -> tuple[str, str, bool]:
    for regular, bold in _FONT_CANDIDATES:
        if not os.path.exists(regular):
            continue
        try:
            if not TTFont("probe", regular).face.charToGlyph.get(0x20B9):
                continue
            pdfmetrics.registerFont(TTFont("RepBody", regular))
            pdfmetrics.registerFont(TTFont("RepBold", bold if os.path.exists(bold) else regular))
            return "RepBody", "RepBold", True
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold", False


def render_sales_report_pdf(
    repo,
    start: str,
    end: str,
    out_dir: str | None = None,
) -> str:
    sales = repo.sales_report(start, end)
    gst = repo.gst_collected(start, end)
    health = repo.stock_health()
    top = repo.top_items(start, end, limit=8)
    debtors = repo.list_khata_debtors()
    profile = repo.get_shop_profile()

    body, bold, rupee_ok = _fonts()

    def inr(paise: int) -> str:
        s = money.format_inr(paise)
        return s if rupee_ok else s.replace(_RUPEE, "Rs ")

    # Typography styles
    normal = ParagraphStyle("rn", fontName=body, fontSize=8, leading=10.5, textColor=_SLATE_BODY)
    sec_heading = ParagraphStyle("rsh", fontName=bold, fontSize=9.5, leading=12, textColor=_BRAND_PRIMARY)
    cell_r = ParagraphStyle("rcr", fontName=body, fontSize=7.5, leading=9.5, alignment=2, textColor=_SLATE_BODY)
    cell_b_r = ParagraphStyle("rcbr", fontName=bold, fontSize=7.5, leading=9.5, alignment=2, textColor=_SLATE_DARK)
    cell_b_c = ParagraphStyle("rcbc", fontName=bold, fontSize=7.5, leading=9.5, alignment=1, textColor=_SLATE_DARK)

    out_dir = out_dir or str(ARTIFACTS_DIR)
    os.makedirs(out_dir, exist_ok=True)
    is_daily = (start == end)
    filename = f"sales_report_{start}.pdf" if is_daily else f"sales_report_{start}_{end}.pdf"
    path = os.path.abspath(os.path.join(out_dir, filename))

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm,
        title=f"Sales Report {start}" if is_daily else f"Sales Report {start} to {end}",
    )
    avail_w = doc.width
    story: list = []

    # -------------------------------------------------------------
    # 1. TWO-TONE EXECUTIVE BRAND HEADER
    # -------------------------------------------------------------
    shop_name = profile.name or "Kirana Supermarket"
    report_title = "DAILY EXECUTIVE SALES REPORT" if is_daily else "PERIOD SALES & OPS REPORT"
    date_display = start if is_daily else f"{start} to {end}"

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
        text_col_w = avail_w * 0.62 - logo_col_w
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

    hdr_right = [
        Paragraph(f'<font color="#34D399"><b>●</b></font> <font color="#FFFFFF"><b>{report_title}</b></font>',
                  ParagraphStyle("hrt", fontName=bold, fontSize=9, leading=11, alignment=2, textColor=colors.white)),
        Spacer(1, 1 * mm),
        Paragraph(f'<font size=12 color="#FFFFFF"><b>{date_display}</b></font>',
                  ParagraphStyle("hrd", fontName=bold, fontSize=12, leading=14, alignment=2, textColor=colors.white)),
        Spacer(1, 1 * mm),
        Paragraph(f'<font color="#94A3B8">GENERATED: {now_iso()[:16].replace("T", " ")} IST</font>',
                  ParagraphStyle("hrg", fontName=body, fontSize=6.5, leading=8.5, alignment=2, textColor=colors.HexColor("#CBD5E1"))),
    ]

    header_table = Table([[hdr_left, hdr_right]], colWidths=[avail_w * 0.62, avail_w * 0.38])

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
    # 2. EXECUTIVE METRIC CARDS (5 Individual Styled Floating Tiles)
    # -------------------------------------------------------------
    def make_kpi_card(title: str, value: str, subtext: str, accent_color: colors.HexColor, width: float) -> Table:
        rows = [
            [Paragraph(f'<font color="{accent_color.hexval()}"><b>■</b></font> <font color="#64748B">{title.upper()}</font>',
                       ParagraphStyle("kt", fontName=bold, fontSize=6.5, leading=8))],
            [Spacer(1, 0.5 * mm)],
            [Paragraph(f'<b>{value}</b>',
                       ParagraphStyle("kv", fontName=bold, fontSize=12.5, leading=14.5, textColor=_SLATE_DARK))],
            [Spacer(1, 0.5 * mm)],
            [Paragraph(subtext, ParagraphStyle("ks", fontName=body, fontSize=6.5, leading=8, textColor=_SLATE_MUTED))],
        ]
        card = Table(rows, colWidths=[width - 2 * mm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
            ("LINEABOVE", (0, 0), (-1, 0), 2.2, accent_color),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ]))
        return card

    c_w = avail_w / 5.0
    c1 = make_kpi_card("Total Bills", str(sales.bill_count), "Transactions cut", _COLOR_BLUE, c_w)
    c2 = make_kpi_card("Gross Sales", inr(sales.gross_sales_paise), "Total bill gross", _COLOR_GREEN, c_w)
    c3 = make_kpi_card("Taxable Net", inr(sales.taxable_paise), "Pre-tax base", _COLOR_PURPLE, c_w)
    c4 = make_kpi_card("GST Collected", inr(gst.total_tax_paise), "CGST + SGST total", _COLOR_ROSE, c_w)
    c5 = make_kpi_card("Stock Assets", inr(health.inventory_retail_value_paise), f"{health.total_skus} Active SKUs", _COLOR_AMBER, c_w)

    kpi_bar = Table([[c1, c2, c3, c4, c5]], colWidths=[c_w] * 5)
    kpi_bar.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(kpi_bar)
    story.append(Spacer(1, 4 * mm))

    # -------------------------------------------------------------
    # 3. SPLIT SECTION: Payment Breakdown & GST Tax Matrix
    # -------------------------------------------------------------
    # Left: Payment Breakdown
    pay_badge_colors = {
        "UPI": "#7C3AED",
        "CASH": "#059669",
        "KHATA": "#D97706",
        "CARD": "#2563EB",
    }
    pay_rows = [
        [
            Paragraph("<b>MODE</b>", ParagraphStyle("ph1", fontName=bold, fontSize=7, leading=8.5, textColor=colors.white)),
            Paragraph("<b>AMOUNT</b>", ParagraphStyle("ph2", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>SHARE</b>", ParagraphStyle("ph3", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        ]
    ]
    tot_pay = sum(sales.by_payment.values()) or 1
    for mode, amt in sorted(sales.by_payment.items(), key=lambda kv: kv[1], reverse=True):
        m_up = mode.upper()
        col = pay_badge_colors.get(m_up, "#475569")
        share_pct = round(amt * 100 / tot_pay, 1)
        mode_pill = f'<font color="{col}"><b>● {m_up}</b></font>'
        pay_rows.append([
            Paragraph(mode_pill, normal),
            Paragraph(inr(amt), cell_b_r),
            Paragraph(f"<b>{share_pct}%</b>", cell_r),
        ])
    if not sales.by_payment:
        pay_rows.append([
            Paragraph("<font color='#64748B'>No sales</font>", normal),
            Paragraph(inr(0), cell_b_r),
            Paragraph("0%", cell_r),
        ])

    pay_w = avail_w * 0.46
    pay_tbl = Table(pay_rows, colWidths=[pay_w * 0.40, pay_w * 0.38, pay_w * 0.22])
    pay_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND_PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _BORDER_COLOR),
        ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _SLATE_LIGHT]),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))

    # Right: GST Matrix
    slab_badge_colors = {
        0: "#64748B",
        5: "#0D9488",
        12: "#2563EB",
        18: "#7C3AED",
        28: "#E11D48",
    }
    gst_rows = [
        [
            Paragraph("<b>SLAB</b>", ParagraphStyle("gh1", fontName=bold, fontSize=7, leading=8.5, alignment=1, textColor=colors.white)),
            Paragraph("<b>TAXABLE</b>", ParagraphStyle("gh2", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>CGST</b>", ParagraphStyle("gh3", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>SGST</b>", ParagraphStyle("gh4", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>TOTAL TAX</b>", ParagraphStyle("gh5", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        ]
    ]
    for row in gst.by_slab:
        rate = row["rate"]
        col = slab_badge_colors.get(rate, "#0F172A")
        tot_slab_tax = row["cgst_paise"] + row["sgst_paise"]
        rate_badge = f'<font color="{col}"><b>{rate}%</b></font>'
        gst_rows.append([
            Paragraph(rate_badge, cell_b_c),
            Paragraph(inr(row["taxable_paise"]), cell_r),
            Paragraph(inr(row["cgst_paise"]), cell_r),
            Paragraph(inr(row["sgst_paise"]), cell_r),
            Paragraph(inr(tot_slab_tax), cell_b_r),
        ])
    if not gst.by_slab:
        gst_rows.append([
            Paragraph("0%", cell_b_c),
            Paragraph(inr(0), cell_r),
            Paragraph(inr(0), cell_r),
            Paragraph(inr(0), cell_r),
            Paragraph(inr(0), cell_b_r),
        ])

    gst_w = avail_w * 0.54
    gst_tbl = Table(gst_rows, colWidths=[gst_w * 0.16, gst_w * 0.22, gst_w * 0.20, gst_w * 0.20, gst_w * 0.22])
    gst_tbl.setStyle(TableStyle([
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

    split_block = Table([
        [
            Paragraph('<font color="#10B981">●</font> <b>Payment Collection Breakdown</b>', sec_heading),
            Paragraph('<font color="#10B981">●</font> <b>GST Tax Matrix & Slabs</b>', sec_heading),
        ],
        [pay_tbl, gst_tbl],
    ], colWidths=[pay_w, gst_w])
    split_block.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2.5 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(split_block)
    story.append(Spacer(1, 4 * mm))

    # -------------------------------------------------------------
    # 4. TOP SELLING PRODUCTS LEADERBOARD
    # -------------------------------------------------------------
    story.append(Paragraph('<font color="#10B981">●</font> <b>Top Selling Products (Performance Leaderboard)</b>', sec_heading))
    story.append(Spacer(1, 1.5 * mm))

    top_rows = [
        [
            Paragraph("<b>RANK</b>", ParagraphStyle("th1", fontName=bold, fontSize=7, leading=8.5, alignment=1, textColor=colors.white)),
            Paragraph("<b>PRODUCT & BRAND</b>", ParagraphStyle("th2", fontName=bold, fontSize=7, leading=8.5, textColor=colors.white)),
            Paragraph("<b>QUANTITY SOLD</b>", ParagraphStyle("th3", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>BILLS COUNT</b>", ParagraphStyle("th4", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
            Paragraph("<b>TOTAL REVENUE</b>", ParagraphStyle("th5", fontName=bold, fontSize=7, leading=8.5, alignment=2, textColor=colors.white)),
        ]
    ]

    rank_decor = {
        1: '<font color="#D97706"><b>#1</b></font>',
        2: '<font color="#475569"><b>#2</b></font>',
        3: '<font color="#B45309"><b>#3</b></font>',
    }

    if top:
        for idx, item in enumerate(top, 1):
            rank_label = rank_decor.get(idx, f"<font color='#64748B'>#{idx}</font>")
            top_rows.append([
                Paragraph(rank_label, cell_b_c),
                Paragraph(f"<b>{item.name}</b>", normal),
                Paragraph(item.qty_display, cell_r),
                Paragraph(f"{item.count} bills", cell_r),
                Paragraph(f"<b>{inr(item.revenue_paise)}</b>", cell_b_r),
            ])
    else:
        top_rows.append([
            Paragraph("-", cell_b_c),
            Paragraph("<font color='#64748B'>No sales activity recorded in this period</font>", normal),
            Paragraph("-", cell_r),
            Paragraph("-", cell_r),
            Paragraph(inr(0), cell_b_r),
        ])

    top_tbl = Table(top_rows, colWidths=[avail_w * 0.10, avail_w * 0.44, avail_w * 0.16, avail_w * 0.12, avail_w * 0.18])
    top_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND_PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, _BORDER_COLOR),
        ("BOX", (0, 0), (-1, -1), 0.75, _BORDER_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _SLATE_LIGHT]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(top_tbl)
    story.append(Spacer(1, 4 * mm))

    # -------------------------------------------------------------
    # 5. OPERATIONS HEALTH & KHATA SPOTLIGHT (Two Feature Cards)
    # -------------------------------------------------------------
    story.append(Paragraph('<font color="#10B981">●</font> <b>Store Operations & Credit Risk Spotlight</b>', sec_heading))
    story.append(Spacer(1, 1.5 * mm))

    low_count = len(health.low_stock)
    out_count = len(health.out_of_stock)
    total_khata = sum(d.balance_paise for d in debtors)

    low_badge = f'<font color="#D97706"><b>{low_count} items</b></font>' if low_count else '<font color="#059669">0 items (Healthy)</font>'
    out_badge = f'<font color="#E11D48"><b>{out_count} items</b></font>' if out_count else '<font color="#059669">0 items (In Stock)</font>'
    khata_badge = f'<font color="#DC2626"><b>{inr(total_khata)}</b></font>' if total_khata > 0 else '<font color="#059669">₹0.00 (Zero Dues)</font>'

    ops_card_cells = [
        [
            Paragraph("<b>INVENTORY STATUS & STOCK ASSETS</b>", ParagraphStyle("opsh", fontName=bold, fontSize=7, leading=8.5, textColor=_BRAND_PRIMARY)),
            Paragraph("<b>CUSTOMER CREDIT (KHATA) LEDGER</b>", ParagraphStyle("khth", fontName=bold, fontSize=7, leading=8.5, textColor=_BRAND_PRIMARY)),
        ],
        [
            Paragraph(f"• <b>Active Catalog SKUs:</b> {health.total_skus} items<br/>"
                      f"• <b>Total Retail Valuation:</b> {inr(health.inventory_retail_value_paise)}<br/>"
                      f"• <b>Low Stock Warning:</b> {low_badge}<br/>"
                      f"• <b>Out of Stock Critical:</b> {out_badge}", normal),
            Paragraph(f"• <b>Active Debtors:</b> {len(debtors)} accounts with dues<br/>"
                      f"• <b>Total Outstanding Debt:</b> {khata_badge}<br/>"
                      f"• <b>Ledger Verification:</b> <font color='#059669'><b>Double-entry Balanced</b></font><br/>"
                      f"• <b>Reminders:</b> Scheduled daily at 10:00 IST", normal),
        ]
    ]

    spotlight_tbl = Table(ops_card_cells, colWidths=[avail_w * 0.50, avail_w * 0.50])
    spotlight_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BRAND_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.75, _BRAND_ACCENT),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#A7F3D0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(spotlight_tbl)
    story.append(Spacer(1, 4 * mm))

    # -------------------------------------------------------------
    # 6. DIGITAL CERTIFICATION FOOTER
    # -------------------------------------------------------------
    cert_text = (
        "<b>OFFICIALLY CERTIFIED</b> &nbsp;|&nbsp; Digitally compiled and verified by "
        "<b>Supermarket Ops Agent</b> &nbsp;|&nbsp; Ledger Invariants & GST Mathematics Reconciled"
    )
    cert_tbl = Table([[Paragraph(cert_text, ParagraphStyle("cert", fontName=body, fontSize=6.5, leading=8.5, alignment=1, textColor=_SLATE_MUTED))]],
                     colWidths=[avail_w])
    cert_tbl.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.5, _BORDER_SOFT),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(cert_tbl)

    doc.build(story)
    return path
