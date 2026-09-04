import pytest

from kirana.domain import gst


def _reconciles(lt: gst.LineTax) -> bool:
    return lt.taxable_paise + lt.cgst_paise + lt.sgst_paise == lt.line_total_paise


def test_line_tax_butter_12pct():
    lt = gst.compute_line_tax(6200, 12)  # ₹62 inclusive
    assert lt.taxable_paise == 5536
    assert lt.cgst_paise == 332 and lt.sgst_paise == 332
    assert _reconciles(lt)


def test_line_tax_18pct():
    lt = gst.compute_line_tax(4200, 18)  # 3 x ₹14 Maggi
    assert lt.taxable_paise == 3559
    assert lt.cgst_paise + lt.sgst_paise == 641
    assert lt.cgst_paise == 321 and lt.sgst_paise == 320  # odd paise -> CGST
    assert _reconciles(lt)


def test_line_tax_zero_slab():
    lt = gst.compute_line_tax(9000, 0)
    assert lt.taxable_paise == 9000 and lt.cgst_paise == 0 and lt.sgst_paise == 0


@pytest.mark.parametrize("total,rate", [(105, 5), (233, 18), (999, 12), (1, 5), (67, 28)])
def test_split_always_reconciles(total, rate):
    assert _reconciles(gst.compute_line_tax(total, rate))


def test_invalid_slab():
    with pytest.raises(ValueError):
        gst.compute_line_tax(1000, 7)


def test_summarize_and_round_off():
    lines = [
        gst.compute_line_tax(9000, 0),    # loose, 0%
        gst.compute_line_tax(27000, 5),   # atta
        gst.compute_line_tax(5600, 18),   # maggi x4
    ]
    totals = gst.summarize(lines)
    assert totals.subtotal_paise == sum(l.taxable_paise for l in lines)
    assert totals.cgst_paise == sum(l.cgst_paise for l in lines)
    assert totals.total_before_round_paise == sum(l.line_total_paise for l in lines)
    # grand total is rounded to whole rupees
    assert totals.grand_total_paise % 100 == 0
    assert totals.grand_total_paise == totals.total_before_round_paise + totals.round_off_paise
    # slabs grouped and sorted
    assert [s.rate for s in totals.slabs] == [0, 5, 18]
