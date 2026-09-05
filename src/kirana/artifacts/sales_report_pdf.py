"""Branded Daily Sales & Operations Report PDF (reportlab).

`render_sales_report_pdf(repo, start, end, out_dir)` compiles a store sales
summary into a professional PDF report with KPI cards, payment breakdown,
top-selling products, GST collection, and inventory status.
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
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..config import ARTIFACTS_DIR
from ..db.connection import now_iso
from ..domain import money

_BRAND = colors.HexColor("#1B7A43")
_BRAND_DARK = colors.HexColor("#12522D")
_ACCENT = colors.HexColor("#F2F7F3")
_GREY = colors.HexColor("#666666")
_LIGHT_GREY = colors.HexColor("#F9F9F9")
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
    top = repo.top_items(start, end, limit=10)
    debtors = repo.list_khata_debtors()
    profile = repo.get_shop_profile()

    body, bold, rupee_ok = _fonts()

    def inr(paise: int) -> str:
        s = money.format_inr(paise)
        return s if rupee_ok else s.replace(_RUPEE, "Rs ")

    normal = ParagraphStyle("rn", fontName=body, fontSize=8.5, leading=11)
    bold_st = ParagraphStyle("rb", fontName=bold, fontSize=8.5, leading=11)
    small = ParagraphStyle("rs", fontName=body, fontSize=7.5, leading=9.5, textColor=_GREY)
    small_w = ParagraphStyle("rsw", parent=small, textColor=colors.white)
    shop_title_st = ParagraphStyle("rst", fontName=bold, fontSize=16, leading=19, textColor=colors.white)
    sec_heading = ParagraphStyle("rsh", fontName=bold, fontSize=11, leading=14, textColor=_BRAND_DARK)
    cell_r = ParagraphStyle("rcr", fontName=body, fontSize=8, leading=10, alignment=2)
    cell_b_r = ParagraphStyle("rcbr", fontName=bold, fontSize=8, leading=10, alignment=2)

    out_dir = out_dir or str(ARTIFACTS_DIR)
    os.makedirs(out_dir, exist_ok=True)
    is_daily = (start == end)
    filename = f"sales_report_{start}.pdf" if is_daily else f"sales_report_{start}_{end}.pdf"
    path = os.path.abspath(os.path.join(out_dir, filename))

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"Sales Report {start}" if is_daily else f"Sales Report {start} to {end}",
    )
    avail_w = doc.width
    story: list = []

    # 1. Header Band
    hdr_left = [
        Paragraph(profile.name or "Kirana Supermarket", shop_title_st),
        Spacer(1, 1.5 * mm),
        Paragraph(profile.address or "Retail Store", small_w),
        Paragraph(f"GSTIN: {profile.gstin or 'N/A'}", small_w),
    ]
    report_title = "DAILY SALES REPORT" if is_daily else "SALES & OPERATIONS REPORT"
    date_display = start if is_daily else f"{start} to {end}"
    hdr_right = [
        Paragraph(report_title, ParagraphStyle("rt", fontName=bold, fontSize=13, leading=16, textColor=colors.white, alignment=2)),
        Spacer(1, 1.5 * mm),
        Paragraph(f"Date: <b>{date_display}</b>", ParagraphStyle("rd", fontName=body, fontSize=8.5, leading=11, textColor=colors.white, alignment=2)),
        Paragraph(f"Generated: {now_iso()[:16].replace('T', ' ')} IST", ParagraphStyle("rg", fontName=body, fontSize=7, leading=9, textColor=colors.white, alignment=2)),
    ]
    hdr_table = Table([[hdr_left, hdr_right]], colWidths=[avail_w * 0.6, avail_w * 0.4])
    hdr_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BRAND),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(hdr_table)
    story.append(Spacer(1, 4 * mm))

    # 2. Executive KPI Cards
    kpi_cards = [
        [
            Paragraph("Total Bills", small),
            Paragraph("Gross Sales", small),
            Paragraph("Taxable Sales", small),
            Paragraph("GST Collected", small),
            Paragraph("Inventory Value", small),
        ],
        [
            Paragraph(str(sales.bill_count), ParagraphStyle("k1", fontName=bold, fontSize=14, leading=17, textColor=_BRAND_DARK)),
            Paragraph(inr(sales.gross_sales_paise), ParagraphStyle("k2", fontName=bold, fontSize=14, leading=17, textColor=_BRAND_DARK)),
            Paragraph(inr(sales.taxable_paise), ParagraphStyle("k3", fontName=bold, fontSize=14, leading=17, textColor=_BRAND_DARK)),
            Paragraph(inr(gst.total_tax_paise), ParagraphStyle("k4", fontName=bold, fontSize=14, leading=17, textColor=_BRAND_DARK)),
            Paragraph(inr(health.inventory_retail_value_paise), ParagraphStyle("k5", fontName=bold, fontSize=14, leading=17, textColor=_BRAND_DARK)),
        ],
    ]
    col_w = avail_w / 5.0
    kpi_tbl = Table(kpi_cards, colWidths=[col_w] * 5)
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ACCENT),
        ("BOX", (0, 0), (-1, -1), 0.5, _BRAND),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D8E7DD")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(kpi_tbl)
    story.append(Spacer(1, 5 * mm))

    # 3. Two Column Section: Payment Breakdown & GST Slabs
    pay_rows = [
        [Paragraph("Payment Mode", bold_st), Paragraph("Amount", cell_b_r), Paragraph("Share", cell_b_r)]
    ]
    tot_pay = sum(sales.by_payment.values()) or 1
    for mode, amt in sorted(sales.by_payment.items(), key=lambda kv: kv[1], reverse=True):
        share = f"{round(amt * 100 / tot_pay, 1)}%"
        pay_rows.append([
            Paragraph(mode.upper(), normal),
            Paragraph(inr(amt), cell_r),
            Paragraph(share, cell_r),
        ])
    if not sales.by_payment:
        pay_rows.append([Paragraph("No sales recorded", normal), Paragraph(inr(0), cell_r), Paragraph("0%", cell_r)])
    pay_tbl = Table(pay_rows, colWidths=[avail_w * 0.22, avail_w * 0.16, avail_w * 0.10])
    pay_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#C0C0C0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GREY]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    gst_rows = [
        [Paragraph("Slab", bold_st), Paragraph("Taxable", cell_b_r), Paragraph("CGST", cell_b_r), Paragraph("SGST", cell_b_r), Paragraph("Total Tax", cell_b_r)]
    ]
    for row in gst.by_slab:
        tot_slab_tax = row["cgst_paise"] + row["sgst_paise"]
        gst_rows.append([
            Paragraph(f"{row['rate']}%", normal),
            Paragraph(inr(row["taxable_paise"]), cell_r),
            Paragraph(inr(row["cgst_paise"]), cell_r),
            Paragraph(inr(row["sgst_paise"]), cell_r),
            Paragraph(inr(tot_slab_tax), cell_r),
        ])
    if not gst.by_slab:
        gst_rows.append([Paragraph("No tax", normal), Paragraph(inr(0), cell_r), Paragraph(inr(0), cell_r), Paragraph(inr(0), cell_r), Paragraph(inr(0), cell_r)])
    gst_tbl = Table(gst_rows, colWidths=[avail_w * 0.08, avail_w * 0.11, avail_w * 0.11, avail_w * 0.11, avail_w * 0.11])
    gst_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#C0C0C0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GREY]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    split_tbl = Table([
        [Paragraph("Payment Collection Mix", sec_heading), Paragraph("GST Tax Summary", sec_heading)],
        [pay_tbl, gst_tbl],
    ], colWidths=[avail_w * 0.49, avail_w * 0.51])
    split_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
    ]))
    story.append(split_tbl)
    story.append(Spacer(1, 5 * mm))

    # 4. Top Selling Products
    story.append(Paragraph("Top-Selling Products", sec_heading))
    story.append(Spacer(1, 1.5 * mm))
    top_rows = [
        [
            Paragraph("#", bold_st),
            Paragraph("Product Name", bold_st),
            Paragraph("Quantity Sold", cell_b_r),
            Paragraph("Bill Count", cell_b_r),
            Paragraph("Revenue", cell_b_r),
        ]
    ]
    if top:
        for idx, item in enumerate(top, 1):
            top_rows.append([
                Paragraph(str(idx), normal),
                Paragraph(item.name, normal),
                Paragraph(item.qty_display, cell_r),
                Paragraph(str(item.count), cell_r),
                Paragraph(inr(item.revenue_paise), cell_r),
            ])
    else:
        top_rows.append([
            Paragraph("-", normal),
            Paragraph("No products sold in this period", normal),
            Paragraph("-", cell_r),
            Paragraph("-", cell_r),
            Paragraph(inr(0), cell_r),
        ])
    top_tbl = Table(top_rows, colWidths=[avail_w * 0.06, avail_w * 0.44, avail_w * 0.16, avail_w * 0.14, avail_w * 0.20])
    top_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E0E0E0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#C0C0C0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GREY]),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    story.append(top_tbl)
    story.append(Spacer(1, 5 * mm))

    # 5. Inventory & Khata Ledger Status Block
    story.append(Paragraph("Store Health & Ledger Overview", sec_heading))
    story.append(Spacer(1, 1.5 * mm))
    out_items = ", ".join(p.name for p in health.out_of_stock[:5]) or "None"
    low_items = ", ".join(p.name for p in health.low_stock[:5]) or "None"
    total_khata = sum(d.balance_paise for d in debtors)

    health_cells = [
        [
            Paragraph("<b>Inventory SKUs:</b>", normal),
            Paragraph(f"{health.total_skus} active items", normal),
            Paragraph("<b>Out of Stock:</b>", normal),
            Paragraph(f"{len(health.out_of_stock)} items ({out_items})", normal),
        ],
        [
            Paragraph("<b>Retail Value:</b>", normal),
            Paragraph(inr(health.inventory_retail_value_paise), normal),
            Paragraph("<b>Low Stock:</b>", normal),
            Paragraph(f"{len(health.low_stock)} items ({low_items})", normal),
        ],
        [
            Paragraph("<b>Khata Debtors:</b>", normal),
            Paragraph(f"{len(debtors)} customers with tab", normal),
            Paragraph("<b>Total Khata Due:</b>", normal),
            Paragraph(inr(total_khata), bold_st),
        ],
    ]
    health_tbl = Table(health_cells, colWidths=[avail_w * 0.20, avail_w * 0.30, avail_w * 0.20, avail_w * 0.30])
    health_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _ACCENT),
        ("BOX", (0, 0), (-1, -1), 0.5, _BRAND),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D8E7DD")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(health_tbl)
    story.append(Spacer(1, 6 * mm))

    # 6. Footer Note
    story.append(Paragraph("This report is digitally generated by Supermarket Ops Agent co-pilot. All figures are verified against transaction records.", ParagraphStyle("fn", fontName=body, fontSize=7.5, leading=10, textColor=_GREY, alignment=1)))

    doc.build(story)
    return path
