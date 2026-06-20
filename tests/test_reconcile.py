from decimal import Decimal
from datetime import date, datetime
from spark.exchange.fakes import FakeAdapter
from spark.exchange.base import Fill
from spark.verification.reconcile import reconcile


def _fill(coin="ETH"):
    return Fill(time=datetime(2026, 6, 18), coin=coin, px=Decimal("4000"),
                sz=Decimal("0.01"), side="B", builder_fee=Decimal("0.008"))


def test_reconcile_found_match():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[_fill()])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    assert report.matched is True
    assert report.fill_count == 1
    assert report.total_builder_fee == Decimal("0.008")


def test_reconcile_no_fills_means_unmatched():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    assert report.matched is False
    assert report.fill_count == 0


def test_reconcile_without_coin_filter_counts_all():
    fake = FakeAdapter(account_value=Decimal("150"),
                       seeded_fills=[_fill("ETH"), _fill("BTC")])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18))  # 無篩選
    assert report.fill_count == 2
    assert report.expected_coin is None


def test_reconcile_coin_filter_excludes_nonmatching():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[_fill("ETH")])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="BTC")
    assert report.matched is False
    assert report.fill_count == 0


def test_report_renders_text():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[_fill()])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    text = report.render()
    assert "ETH" in text and "0.008" in text
    assert "filter_coin: ETH" in text
