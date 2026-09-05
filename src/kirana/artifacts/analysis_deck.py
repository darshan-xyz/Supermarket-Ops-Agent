"""Render a store-analysis PPTX with native python-pptx charts.

This is a thin presentation adapter: every number comes from the repository's
analytics methods (sales_report / gst_collected / stock_health / top_items /
list_khata_debtors). Paise are converted to rupees only here, at the render
edge. Charts are REAL native python-pptx charts (CategoryChartData + add_chart),
never rasterised images, so they stay editable in PowerPoint.
"""

from __future__ import annotations

import os

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from ..config import ARTIFACTS_DIR
from ..domain import money

# 16:9 canvas
_SLIDE_W = Emu(12192000)
_SLIDE_H = Emu(6858000)

_BLANK_LAYOUT = 6  # the empty layout in the default template

_INK = RGBColor(0x1A, 0x1A, 0x2E)
_ACCENT = RGBColor(0x0F, 0x6F, 0x5C)
_MUTED = RGBColor(0x6B, 0x6B, 0x7B)


def _rs(paise: int) -> float:
    """Paise -> rupees as a float, for chart values only."""
    return float(money.rupees(paise))


def _add_title(slide, text: str, subtitle: str | None = None) -> None:
    box = slide.shapes.add_textbox(Emu(457200), Emu(304800), Emu(11277600), Emu(914400))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.size = Pt(32)
    run.font.bold = True
    run.font.color.rgb = _INK
    if subtitle:
        sp = tf.add_paragraph()
        srun = sp.add_run()
        srun.text = subtitle
        srun.font.size = Pt(14)
        srun.font.color.rgb = _MUTED


def _note(slide, text: str, top: Emu | None = None) -> None:
    box = slide.shapes.add_textbox(
        Emu(457200), top or Emu(3200400), Emu(11277600), Emu(700000)
    )
    p = box.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.size = Pt(16)
    run.font.italic = True
    run.font.color.rgb = _MUTED


def _add_bar_chart(slide, categories, values, *, title, left, top, width, height):
    data = CategoryChartData()
    data.categories = categories
    data.add_series("₹", values)
    gframe = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, left, top, width, height, data
    )
    chart = gframe.chart
    chart.has_title = True
    chart.chart_title.text_frame.text = title
    chart.has_legend = False
    return chart


def _add_slide(prs) -> object:
    return prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])


_TRANSLATIONS = {
    "en": {
        "title": "Store Analysis",
        "daily_title": "Daily Sales Analysis",
        "key_numbers": "Key Numbers",
        "total_sales": "Total Sales",
        "bills": "Bills",
        "gst_collected": "GST Collected",
        "inventory_value": "Inventory Value",
        "no_sales": "No sales in this period.",
        "top_items": "Top Items by Revenue",
        "revenue_rs": "Revenue (₹)",
        "sales_by_payment": "Sales by Payment Mode",
        "daily_sales_trend": "Daily Sales Trend",
        "gross_sales_rs": "Gross Sales (₹)",
        "gst_by_slab": "GST Collected by Slab",
        "tax_collected_rs": "Tax collected (₹)",
        "no_gst": "No GST collected in this period.",
        "stock_health_khata": "Stock Health & Khata",
        "total_skus": "Total SKUs",
        "inv_value_retail": "Inventory value (retail)",
        "out_of_stock": "Out of stock",
        "low_stock": "Low stock",
        "top_debtors": "Top khata debtors",
        "total_outstanding": "Total outstanding khata",
        "none": "none",
        "insights": "Insights",
        "best_selling": "Best-selling item",
        "busiest_day": "Busiest day",
        "payment_mix": "Payment mix",
        "avg_bill": "Average bill value",
        "need_attention": "SKU(s) need attention",
        "outstanding_khata": "Outstanding khata",
        "inventory_details": "Current Inventory & Stock Details",
        "col_product": "Product Name",
        "col_stock": "In Stock",
        "col_price": "Unit Price",
        "col_value": "Stock Value",
        "col_status": "Status",
        "status_in_stock": "In Stock",
        "status_low": "Low Stock",
        "status_out": "Out of Stock",
        "page": "Page",
    },
    "ta": {
        "title": "விற்பனை அறிக்கை",
        "daily_title": "தினசரி விற்பனை அறிக்கை",
        "key_numbers": "முக்கிய விவரங்கள்",
        "total_sales": "மொத்த விற்பனை",
        "bills": "மொத்த பில்கள்",
        "gst_collected": "ஜிஎஸ்டி வசூல்",
        "inventory_value": "சரக்கு மதிப்பு",
        "no_sales": "இந்த காலத்தில் விற்பனை இல்லை.",
        "top_items": "அதிக விற்பனையான பொருட்கள்",
        "revenue_rs": "வருமானம் (₹)",
        "sales_by_payment": "பணம் செலுத்தும் முறை",
        "daily_sales_trend": "தினசரி விற்பனை போக்கு",
        "gross_sales_rs": "மொத்த விற்பனை (₹)",
        "gst_by_slab": "ஜிஎஸ்டி விகிதங்கள்",
        "tax_collected_rs": "வரி வசூல் (₹)",
        "no_gst": "இந்த காலத்தில் ஜிஎஸ்டி வசூல் இல்லை.",
        "stock_health_khata": "சரக்கு இருப்பு & கடன் விவரம்",
        "total_skus": "மொத்த பொருட்கள் (SKUs)",
        "inv_value_retail": "சரக்கு மதிப்பு (சில்லறை)",
        "out_of_stock": "இருப்பு தீர்ந்தவை",
        "low_stock": "குறைந்த இருப்பு",
        "top_debtors": "அதிக கடன் பாக்கி வாடிக்கையாளர்கள்",
        "total_outstanding": "மொத்த கடன் பாக்கி",
        "none": "ஏதுமில்லை",
        "insights": "முக்கிய குறிப்புகள்",
        "best_selling": "அதிகம் விற்பனையான பொருள்",
        "busiest_day": "அதிக விற்பனையான நாள்",
        "payment_mix": "பணம் செலுத்திய விதம்",
        "avg_bill": "சராசரி பில் மதிப்பு",
        "need_attention": "பொருட்கள் கவனிப்பு தேவை",
        "outstanding_khata": "நிலுவையில் உள்ள கடன் பாக்கி",
        "inventory_details": "தற்போதைய சரக்கு இருப்பு விவரம்",
        "col_product": "பொருளின் பெயர்",
        "col_stock": "இருப்பு",
        "col_price": "விற்பனை விலை",
        "col_value": "சரக்கு மதிப்பு",
        "col_status": "நிலை",
        "status_in_stock": "இருப்பில் உள்ளது",
        "status_low": "குறைந்த இருப்பு",
        "status_out": "தீர்ந்துவிட்டது",
        "page": "பக்கம்",
    },
    "hi": {
        "title": "बिक्री विश्लेषण",
        "daily_title": "दैनिक बिक्री विश्लेषण",
        "key_numbers": "प्रमुख आंकड़े",
        "total_sales": "कुल बिक्री",
        "bills": "कुल बिल",
        "gst_collected": "जीएसटी संग्रह",
        "inventory_value": "स्टॉक मूल्य",
        "no_sales": "इस अवधि में कोई बिक्री नहीं।",
        "top_items": "सर्वाधिक बिकने वाले उत्पाद",
        "revenue_rs": "राजस्व (₹)",
        "sales_by_payment": "भुगतान माध्यम अनुसार बिक्री",
        "daily_sales_trend": "दैनिक बिक्री रुझान",
        "gross_sales_rs": "सकल बिक्री (₹)",
        "gst_by_slab": "जीएसटी स्लैब अनुसार",
        "tax_collected_rs": "कर संग्रह (₹)",
        "no_gst": "इस अवधि में कोई जीएसटी नहीं।",
        "stock_health_khata": "स्टॉक स्थिति और खाता",
        "total_skus": "कुल उत्पाद (SKUs)",
        "inv_value_retail": "स्टॉक मूल्य (खुदरा)",
        "out_of_stock": "स्टॉक समाप्त",
        "low_stock": "कम स्टॉक",
        "top_debtors": "शीर्ष खाता देनदार",
        "total_outstanding": "कुल बकाया खाता",
        "none": "कोई नहीं",
        "insights": "मुख्य निष्कर्ष",
        "best_selling": "सर्वाधिक बिकने वाला उत्पाद",
        "busiest_day": "सर्वाधिक बिक्री वाला दिन",
        "payment_mix": "भुगतान मिश्रण",
        "avg_bill": "औसत बिल मूल्य",
        "need_attention": "उत्पादों पर ध्यान देने की आवश्यकता",
        "outstanding_khata": "कुल बकाया खाता राशि",
        "inventory_details": "वर्तमान स्टॉक और इन्वेंटरी विवरण",
        "col_product": "उत्पाद का नाम",
        "col_stock": "उपलब्ध मात्रा",
        "col_price": "बिक्री मूल्य",
        "col_value": "स्टॉक मूल्य",
        "col_status": "स्थिति",
        "status_in_stock": "स्टॉक में है",
        "status_low": "कम स्टॉक",
        "status_out": "स्टॉक समाप्त",
        "page": "पृष्ठ",
    },
}


def render_deck(
    repo,
    start: str,
    end: str,
    out_dir: str | None = None,
    title: str | None = None,
    language: str = "en",
) -> str:
    """Build a PPTX analysis deck for [start, end] in the target language and return its absolute path."""
    lang = language.lower()
    if lang not in _TRANSLATIONS:
        lang = "en"
    tr = _TRANSLATIONS[lang]

    sales = repo.sales_report(start, end)
    gst = repo.gst_collected(start, end)
    health = repo.stock_health()
    top = repo.top_items(start, end)
    debtors = repo.list_khata_debtors()
    profile = repo.get_shop_profile()

    has_sales = sales.bill_count > 0

    prs = Presentation()
    prs.slide_width = _SLIDE_W
    prs.slide_height = _SLIDE_H

    # Default title
    is_daily = (start == end)
    default_title = tr["daily_title"] if is_daily else tr["title"]
    deck_title = title or default_title

    # ---- Slide 1: Title ----------------------------------------------------
    s = _add_slide(prs)
    box = s.shapes.add_textbox(Emu(457200), Emu(2286000), Emu(11277600), Emu(2286000))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    run = p.add_run()
    run.text = deck_title
    run.font.size = Pt(48)
    run.font.bold = True
    run.font.color.rgb = _INK

    sub = tf.add_paragraph()
    sub.alignment = PP_ALIGN.CENTER
    srun = sub.add_run()
    if is_daily:
        sub_text = f"{start} ({tr['daily_title']})"
    else:
        if lang == "ta":
            sub_text = f"{start} முதல் {end} வரை"
        elif lang == "hi":
            sub_text = f"{start} से {end}"
        else:
            sub_text = f"{start} to {end}"
    srun.text = sub_text
    srun.font.size = Pt(20)
    srun.font.color.rgb = _ACCENT

    shop = tf.add_paragraph()
    shop.alignment = PP_ALIGN.CENTER
    shrun = shop.add_run()
    shrun.text = profile.name or "Kirana Store"
    shrun.font.size = Pt(18)
    shrun.font.color.rgb = _MUTED

    # ---- Slide 2: KPIs -----------------------------------------------------
    s = _add_slide(prs)
    _add_title(s, tr["key_numbers"], sub_text)
    kpis = [
        (tr["total_sales"], money.format_inr(sales.gross_sales_paise)),
        (tr["bills"], str(sales.bill_count)),
        (tr["gst_collected"], money.format_inr(gst.total_tax_paise)),
        (tr["inventory_value"], money.format_inr(health.inventory_retail_value_paise)),
    ]
    card_w = Emu(2743200)
    gap = Emu(152400)
    left0 = Emu(457200)
    card_top = Emu(2133600)
    for i, (label, value) in enumerate(kpis):
        left = Emu(int(left0) + i * (int(card_w) + int(gap)))
        card = s.shapes.add_textbox(left, card_top, card_w, Emu(1828800))
        ctf = card.text_frame
        ctf.word_wrap = True
        vp = ctf.paragraphs[0]
        vp.alignment = PP_ALIGN.CENTER
        vrun = vp.add_run()
        vrun.text = value
        vrun.font.size = Pt(30)
        vrun.font.bold = True
        vrun.font.color.rgb = _ACCENT
        lp = ctf.add_paragraph()
        lp.alignment = PP_ALIGN.CENTER
        lrun = lp.add_run()
        lrun.text = label
        lrun.font.size = Pt(15)
        lrun.font.color.rgb = _MUTED
    if not has_sales:
        _note(s, tr["no_sales"], top=Emu(4267200))

    # ---- Slide 3: Top items by revenue (bar) -------------------------------
    s = _add_slide(prs)
    _add_title(s, tr["top_items"])
    if top:
        cats = [it.name for it in top]
        vals = [_rs(it.revenue_paise) for it in top]
        _add_bar_chart(
            s, cats, vals, title=tr["revenue_rs"],
            left=Emu(457200), top=Emu(1524000),
            width=Emu(11277600), height=Emu(4800600),
        )
    else:
        _note(s, tr["no_sales"])

    # ---- Slide 4: Sales by payment mode (pie) ------------------------------
    s = _add_slide(prs)
    _add_title(s, tr["sales_by_payment"])
    if sales.by_payment:
        modes = list(sales.by_payment.keys())
        data = CategoryChartData()
        data.categories = [m.upper() for m in modes]
        data.add_series("₹", [_rs(sales.by_payment[m]) for m in modes])
        gframe = s.shapes.add_chart(
            XL_CHART_TYPE.PIE, Emu(2743200), Emu(1524000),
            Emu(6705600), Emu(4800600), data,
        )
        chart = gframe.chart
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.RIGHT
        chart.legend.include_in_layout = False
        plot = chart.plots[0]
        plot.has_data_labels = True
        plot.data_labels.show_percentage = True
        plot.data_labels.number_format = "0.0%"
        plot.data_labels.number_format_is_linked = False
    else:
        _note(s, tr["no_sales"])

    # ---- Slide 5: Sales trend by day (column) ------------------------------
    s = _add_slide(prs)
    _add_title(s, tr["daily_sales_trend"])
    if sales.by_day:
        cats = [d for d, _ in sales.by_day]
        vals = [_rs(g) for _, g in sales.by_day]
        _add_bar_chart(
            s, cats, vals, title=tr["gross_sales_rs"],
            left=Emu(457200), top=Emu(1524000),
            width=Emu(11277600), height=Emu(4800600),
        )
    else:
        _note(s, tr["no_sales"])

    # ---- Slide 6: GST collected by slab (column) ---------------------------
    s = _add_slide(prs)
    _add_title(s, tr["gst_by_slab"])
    if gst.by_slab:
        cats = [f"{row['rate']}%" for row in gst.by_slab]
        vals = [_rs(row["cgst_paise"] + row["sgst_paise"]) for row in gst.by_slab]
        _add_bar_chart(
            s, cats, vals, title=tr["tax_collected_rs"],
            left=Emu(457200), top=Emu(1524000),
            width=Emu(11277600), height=Emu(4800600),
        )
    else:
        _note(s, tr["no_gst"])

    # ---- Slide 7: Stock health ---------------------------------------------
    s = _add_slide(prs)
    _add_title(s, tr["stock_health_khata"])
    none_str = tr["none"]
    lines: list[str] = [
        f"{tr['total_skus']}: {health.total_skus}",
        f"{tr['inv_value_retail']}: {money.format_inr(health.inventory_retail_value_paise)}",
        f"{tr['out_of_stock']} ({len(health.out_of_stock)}): "
        + (", ".join(p.name for p in health.out_of_stock[:10]) or none_str),
        f"{tr['low_stock']} ({len(health.low_stock)}): "
        + (", ".join(p.name for p in health.low_stock[:10]) or none_str),
    ]
    total_outstanding = sum(d.balance_paise for d in debtors)
    if debtors:
        top_debtors = ", ".join(
            f"{d.customer.name} ({money.format_inr(d.balance_paise)})"
            for d in debtors[:5]
        )
        lines.append(f"{tr['top_debtors']}: {top_debtors}")
        lines.append(f"{tr['total_outstanding']}: {money.format_inr(total_outstanding)}")
    else:
        lines.append(f"{tr['top_debtors']}: {none_str}")

    box = s.shapes.add_textbox(Emu(457200), Emu(1600200), Emu(11277600), Emu(4800600))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        run = para.add_run()
        run.text = "• " + line
        run.font.size = Pt(18)
        run.font.color.rgb = _INK
        para.space_after = Pt(10)

    # ---- Slide 8+: Current Inventory & Stock Details -----------------------
    try:
        prods = [repo._product(r) for r in repo.db.query_all("SELECT * FROM products WHERE active=1 ORDER BY name")]
    except Exception:
        prods = []

    if prods:
        chunk_size = 8
        num_pages = max(1, (len(prods) + chunk_size - 1) // chunk_size)

        for page_idx in range(num_pages):
            chunk = prods[page_idx * chunk_size : (page_idx + 1) * chunk_size]
            s = _add_slide(prs)
            page_sub = f"{tr['page']} {page_idx + 1} / {num_pages}" if num_pages > 1 else None
            _add_title(s, tr["inventory_details"], page_sub)

            t_rows = len(chunk) + 1
            t_cols = 5
            table_shape = s.shapes.add_table(
                t_rows, t_cols, Emu(457200), Emu(1400000), Emu(11277600), Emu(4800000)
            )
            table = table_shape.table
            col_widths = [Emu(4200000), Emu(1700000), Emu(1700000), Emu(2000000), Emu(1677600)]
            for ci, cw in enumerate(col_widths):
                table.columns[ci].width = cw

            # Header
            headers = [
                tr["col_product"],
                tr["col_stock"],
                tr["col_price"],
                tr["col_value"],
                tr["col_status"],
            ]
            for ci, h_text in enumerate(headers):
                cell = table.cell(0, ci)
                cell.text = h_text
                cell.fill.solid()
                cell.fill.fore_color.rgb = _ACCENT
                p = cell.text_frame.paragraphs[0]
                p.font.size = Pt(13)
                p.font.bold = True
                p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                p.alignment = PP_ALIGN.LEFT if ci == 0 else (PP_ALIGN.RIGHT if ci in (2, 3) else PP_ALIGN.CENTER)

            # Rows
            for ri, p_item in enumerate(chunk):
                row_idx = ri + 1
                stock_val = int(p_item.sell_paise * p_item.stock_base / 1000)
                if p_item.stock_base <= 0:
                    st_text = tr["status_out"]
                    st_color = RGBColor(0xC0, 0x39, 0x2B)
                elif p_item.stock_base <= p_item.reorder_base:
                    st_text = tr["status_low"]
                    st_color = RGBColor(0xD3, 0x54, 0x00)
                else:
                    st_text = tr["status_in_stock"]
                    st_color = RGBColor(0x27, 0xAE, 0x60)

                row_data = [
                    p_item.name,
                    p_item.stock_display,
                    money.format_inr(p_item.sell_paise),
                    money.format_inr(stock_val),
                    st_text,
                ]

                is_even = (ri % 2 == 0)
                bg_color = RGBColor(0xFF, 0xFF, 0xFF) if is_even else RGBColor(0xF5, 0xF7, 0xFA)

                for ci, val_text in enumerate(row_data):
                    cell = table.cell(row_idx, ci)
                    cell.text = val_text
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = bg_color
                    cp = cell.text_frame.paragraphs[0]
                    cp.font.size = Pt(12)
                    if ci == 0:
                        cp.font.bold = True
                        cp.font.color.rgb = _INK
                        cp.alignment = PP_ALIGN.LEFT
                    elif ci == 1:
                        cp.font.color.rgb = _INK
                        cp.alignment = PP_ALIGN.CENTER
                    elif ci in (2, 3):
                        cp.font.color.rgb = _INK
                        cp.alignment = PP_ALIGN.RIGHT
                    else:  # Status
                        cp.font.bold = True
                        cp.font.color.rgb = st_color
                        cp.alignment = PP_ALIGN.CENTER

    # ---- Slide: Insights (computed) ----------------------------------------
    s = _add_slide(prs)
    _add_title(s, tr["insights"])
    insights = _compute_insights(sales, gst, health, top, debtors, total_outstanding, lang=lang)
    box = s.shapes.add_textbox(Emu(457200), Emu(1600200), Emu(11277600), Emu(4800600))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(insights):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        run = para.add_run()
        run.text = "• " + line
        run.font.size = Pt(18)
        run.font.color.rgb = _INK
        para.space_after = Pt(12)

    # ---- Save --------------------------------------------------------------
    target_dir = out_dir or str(ARTIFACTS_DIR)
    os.makedirs(target_dir, exist_ok=True)
    suffix = f"_{lang}" if lang != "en" else ""
    path = os.path.join(target_dir, f"analysis_{start}_{end}{suffix}.pptx")
    prs.save(path)
    return os.path.abspath(path)


def _compute_insights(
    sales, gst, health, top, debtors, total_outstanding, lang: str = "en"
) -> list[str]:
    """Derive 3-5 bullet insights strictly from the fetched data in the target language."""
    tr = _TRANSLATIONS.get(lang, _TRANSLATIONS["en"])
    out: list[str] = []
    if sales.bill_count == 0:
        out.append(tr["no_sales"])
    else:
        if top:
            best = top[0]
            if lang == "ta":
                out.append(
                    f"{tr['best_selling']}: {best.name} "
                    f"({money.format_inr(best.revenue_paise)} - {best.count} பில்கள்)."
                )
            elif lang == "hi":
                out.append(
                    f"{tr['best_selling']}: {best.name} "
                    f"({money.format_inr(best.revenue_paise)} - {best.count} बिल)."
                )
            else:
                out.append(
                    f"Best-selling item: {best.name} "
                    f"({money.format_inr(best.revenue_paise)} across {best.count} bills)."
                )
        if sales.by_day:
            busiest = max(sales.by_day, key=lambda d: d[1])
            if lang == "ta":
                out.append(f"{tr['busiest_day']}: {busiest[0]} ({money.format_inr(busiest[1])} விற்பனை).")
            elif lang == "hi":
                out.append(f"{tr['busiest_day']}: {busiest[0]} ({money.format_inr(busiest[1])} बिक्री).")
            else:
                out.append(f"Busiest day: {busiest[0]} with {money.format_inr(busiest[1])} in sales.")
        if sales.by_payment:
            total = sum(sales.by_payment.values()) or 1
            split = ", ".join(
                f"{m.upper()} {round(v * 100 / total)}%"
                for m, v in sorted(
                    sales.by_payment.items(), key=lambda kv: kv[1], reverse=True
                )
            )
            out.append(f"{tr['payment_mix']}: {split}.")
        avg = sales.gross_sales_paise // sales.bill_count
        if lang == "ta":
            out.append(f"{tr['avg_bill']}: {money.format_inr(avg)} ({sales.bill_count} பில்கள்).")
        elif lang == "hi":
            out.append(f"{tr['avg_bill']}: {money.format_inr(avg)} ({sales.bill_count} बिल).")
        else:
            out.append(f"Average bill value: {money.format_inr(avg)} over {sales.bill_count} bills.")

    low_out = len(health.low_stock) + len(health.out_of_stock)
    if low_out:
        if lang == "ta":
            out.append(f"{low_out} {tr['need_attention']} ({len(health.out_of_stock)} தீர்ந்தவை, {len(health.low_stock)} குறைவு).")
        elif lang == "hi":
            out.append(f"{low_out} {tr['need_attention']} ({len(health.out_of_stock)} समाप्त, {len(health.low_stock)} कम).")
        else:
            out.append(
                f"{low_out} SKU(s) need attention "
                f"({len(health.out_of_stock)} out of stock, {len(health.low_stock)} low)."
            )
    if debtors:
        if lang == "ta":
            out.append(f"{tr['outstanding_khata']}: {money.format_inr(total_outstanding)} ({len(debtors)} வாடிக்கையாளர்கள்).")
        elif lang == "hi":
            out.append(f"{tr['outstanding_khata']}: {money.format_inr(total_outstanding)} ({len(debtors)} ग्राहक).")
        else:
            out.append(f"Outstanding khata: {money.format_inr(total_outstanding)} across {len(debtors)} customer(s).")
    return out[:6]
