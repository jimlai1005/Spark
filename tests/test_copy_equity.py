"""perp-basis 權益取樣測試（findings F1 迴歸）。"""
from decimal import Decimal
from pathlib import Path

from spark.copytrade.equity import perp_equity_view, reset_samples


class _FakeAdapter:
    def __init__(self, value: str):
        self.value = Decimal(value)

    def get_account_value(self, address: str) -> Decimal:
        return self.value


def test_uses_perp_value_not_total(tmp_path: Path):
    """F1 迴歸：current 必須是 perp accountValue，不是含 spot 的總值。"""
    ad = _FakeAdapter("500")
    ev = perp_equity_view(ad, "0xabc", tmp_path, now_fn=lambda: 1000.0)
    assert ev.current == Decimal("500")
    assert ev.recent_peak == Decimal("500")


def test_dilution_regression_perp_drop_trips(tmp_path: Path):
    """F1 核心迴歸：perp 500→400（跌 20%）必須算出 0.2 回撤。

    舊行為（portfolio 含 spot 999）只會算出約 0.1，導致 20% 門檻不觸發。
    """
    ev1 = perp_equity_view(_FakeAdapter("500"), "0xabc", tmp_path, now_fn=lambda: 1000.0)
    assert ev1.recent_peak == Decimal("500")
    ev2 = perp_equity_view(_FakeAdapter("400"), "0xabc", tmp_path, now_fn=lambda: 2000.0)
    assert ev2.current == Decimal("400")
    assert ev2.recent_peak == Decimal("500")
    dd = (ev2.recent_peak - ev2.current) / ev2.recent_peak
    assert dd == Decimal("0.2")


def test_rolling_window_prunes_old_peak(tmp_path: Path):
    """窗外的舊高點不再影響 peak（滾動 7 天語意，非終身高水位）。"""
    perp_equity_view(_FakeAdapter("1000"), "0xabc", tmp_path, now_fn=lambda: 0.0)
    ev = perp_equity_view(_FakeAdapter("500"), "0xabc", tmp_path,
                          now_fn=lambda: 8 * 24 * 3600.0)
    assert ev.recent_peak == Decimal("500"), "8 天前的 1000 應已出窗"


def test_reset_samples_clears_peak(tmp_path: Path):
    """re-arm 情境：清樣本後 peak 回到 current，不會被崩跌前的舊 peak 立刻再熔斷。"""
    perp_equity_view(_FakeAdapter("1000"), "0xabc", tmp_path, now_fn=lambda: 1000.0)
    reset_samples(tmp_path)
    ev = perp_equity_view(_FakeAdapter("800"), "0xabc", tmp_path, now_fn=lambda: 1100.0)
    assert ev.recent_peak == Decimal("800")
    assert ev.current == Decimal("800")


def test_corrupt_samples_file_does_not_break(tmp_path: Path):
    """樣本檔壞掉不阻斷交易：peak 退回 current。"""
    p = tmp_path / "var" / "copytrade" / "equity_samples.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not valid json")
    ev = perp_equity_view(_FakeAdapter("300"), "0xabc", tmp_path, now_fn=lambda: 5.0)
    assert ev.current == Decimal("300")
    assert ev.recent_peak == Decimal("300")


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    perp_equity_view(_FakeAdapter("100"), "0xabc", tmp_path, now_fn=lambda: 1.0)
    leftovers = list((tmp_path / "var" / "copytrade").glob("*.tmp"))
    assert leftovers == []
