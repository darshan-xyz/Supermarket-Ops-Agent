import pytest

from kirana.domain.units import UnitError, dimension, format_qty, from_base, to_base


def test_to_base_mass():
    assert to_base(2, "kg", "kg") == 2000
    assert to_base(500, "g", "kg") == 500       # grams into a kg-priced product
    assert to_base("0.25", "kg", "kg") == 250   # loose weight stays exact
    assert to_base(250, "g", "g") == 250000      # base = display x 1000 (milli-units)


def test_to_base_count_and_volume():
    assert to_base(4, "packet", "packet") == 4000
    assert to_base(1, None, "packet") == 1000            # default unit = product unit
    assert to_base(0.5, "litre", "litre") == 500
    assert to_base(1, "dozen", "piece") == 12000         # 1 dozen = 12 pieces
    assert to_base(6, "piece", "dozen") == 500           # 6 pieces = 0.5 dozen


def test_dimension_mismatch_rejected():
    with pytest.raises(UnitError):
        to_base(1, "kg", "litre")
    with pytest.raises(UnitError):
        to_base(1, "packet", "piece")   # packet is isolated from piece


def test_format_qty_downshifts():
    assert format_qty(2000, "kg") == "2 kg"
    assert format_qty(500, "kg") == "500 g"
    assert format_qty(500, "litre") == "500 ml"
    assert format_qty(1000, "packet") == "1 packet"
    assert format_qty(4000, "packet") == "4 packet"


def test_from_base_and_dimension():
    assert str(from_base(2500, "kg")) == "2.5"
    assert dimension("kg") == "mass"
    assert dimension("ml") == "volume"
    assert dimension("dozen") == "count"
