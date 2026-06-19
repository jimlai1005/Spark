from decimal import Decimal
from datetime import date, datetime
from spark.exchange.fakes import FakeAdapter
from spark.exchange.base import Fill
from spark.verification.reconcile import reconcile


def _fill():
    return Fill(datetime(2026, 6, 18), "ETH", Decimal("4000"), Decimal("0.01"), "B",
                Decimal("0.008"))


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


def test_report_renders_text():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[_fill()])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    text = report.render()
    assert "ETH" in text and "0.008" in text
