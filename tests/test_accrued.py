from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.verification.accrued import wait_for_accrual, AccrualTimeout


def test_returns_accrued_when_positive():
    fake = FakeAdapter(account_value=Decimal("150"))
    fake._accrued = Decimal("0.008")  # 模擬已累計
    got = wait_for_accrual(fake, "0xbuilder", attempts=3, sleep_s=0)
    assert got == Decimal("0.008")


def test_polls_until_positive():
    fake = FakeAdapter(account_value=Decimal("150"))
    seq = [Decimal("0"), Decimal("0"), Decimal("0.005")]
    fake.query_builder_accrued = lambda builder: seq.pop(0)
    got = wait_for_accrual(fake, "0xbuilder", attempts=5, sleep_s=0)
    assert got == Decimal("0.005")


def test_times_out_when_never_positive():
    fake = FakeAdapter(account_value=Decimal("150"))  # 一直回 0
    with pytest.raises(AccrualTimeout):
        wait_for_accrual(fake, "0xbuilder", attempts=3, sleep_s=0)


def test_baseline_prevents_false_positive_from_stale_cumulative_value():
    fake = FakeAdapter(account_value=Decimal("150"))
    fake._accrued = Decimal("0.100")  # 歷史累計（非本單）
    with pytest.raises(AccrualTimeout):
        wait_for_accrual(fake, "0xbuilder", attempts=3, sleep_s=0,
                         baseline=Decimal("0.100"))


def test_returns_when_growth_exceeds_baseline():
    fake = FakeAdapter(account_value=Decimal("150"))
    seq = [Decimal("0.100"), Decimal("0.108")]
    fake.query_builder_accrued = lambda builder: seq.pop(0)
    got = wait_for_accrual(fake, "0xbuilder", attempts=5, sleep_s=0,
                           baseline=Decimal("0.100"))
    assert got == Decimal("0.108")
