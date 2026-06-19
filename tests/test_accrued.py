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
