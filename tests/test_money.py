import pytest
from spark.money import f_to_percent_str, assert_fee_within_cap, FEE_CAP_TENTHS_BP


def test_f_to_percent_str_basic():
    assert f_to_percent_str(20) == "0.02%"      # 2 bp
    assert f_to_percent_str(100) == "0.1%"      # 協議上限 0.1%
    assert f_to_percent_str(10) == "0.01%"      # 1 bp
    assert f_to_percent_str(1) == "0.001%"      # 最小有效值


def test_fee_cap_constant_is_protocol_limit():
    assert FEE_CAP_TENTHS_BP == 100


def test_assert_fee_within_cap_passes_at_and_below_cap():
    assert_fee_within_cap(20)
    assert_fee_within_cap(100)


def test_assert_fee_within_cap_rejects_above_cap():
    with pytest.raises(ValueError, match="builder fee f=101"):
        assert_fee_within_cap(101)


def test_assert_fee_within_cap_rejects_zero_and_negative():
    with pytest.raises(ValueError):
        assert_fee_within_cap(0)
    with pytest.raises(ValueError):
        assert_fee_within_cap(-5)
