from kirana.domain.money import (
    amount_in_words,
    format_inr,
    round_to_rupee,
    rupees,
    to_paise,
)


def test_to_paise():
    assert to_paise("62") == 6200
    assert to_paise(61.5) == 6150
    assert to_paise("0.01") == 1
    assert to_paise(0) == 0


def test_format_inr_indian_grouping():
    assert format_inr(6200) == "₹62.00"
    assert format_inr(1234550) == "₹12,345.50"
    assert format_inr(100000000) == "₹10,00,000.00"  # 10 lakh
    assert format_inr(-5000) == "-₹50.00"


def test_rupees_roundtrip():
    assert str(rupees(31200)) == "312.00"


def test_round_to_rupee():
    assert round_to_rupee(12300) == (12300, 0)
    assert round_to_rupee(12345) == (12300, -45)   # <50 paise -> down
    assert round_to_rupee(12355) == (12400, 45)    # >=50 paise -> up
    assert round_to_rupee(12350) == (12400, 50)


def test_amount_in_words():
    w = amount_in_words(31200)
    assert w.startswith("Rupees") and w.endswith("Only")
    assert "Three Hundred" in w and "Twelve" in w
    assert "Paise" in amount_in_words(31250)  # 50 paise part present
