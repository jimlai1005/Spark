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


def test_persist_false_writes_nothing(tmp_path: Path):
    """`--status` 與 panic 的零寫入契約：persist=False 不得建立或修改樣本檔。"""
    from spark.copytrade.equity import SAMPLES_RELPATH

    ev = perp_equity_view(_FakeAdapter("500"), "0xabc", tmp_path,
                          now_fn=lambda: 1000.0, persist=False)
    assert ev.current == Decimal("500")
    assert not (tmp_path / SAMPLES_RELPATH).exists(), "persist=False 不得建檔"


def test_persist_false_does_not_mutate_existing(tmp_path: Path):
    """已有樣本時，persist=False 讀得到 peak 但不改動檔案內容。"""
    from spark.copytrade.equity import SAMPLES_RELPATH

    perp_equity_view(_FakeAdapter("1000"), "0xabc", tmp_path, now_fn=lambda: 1000.0)
    p = tmp_path / SAMPLES_RELPATH
    before = p.read_text()
    ev = perp_equity_view(_FakeAdapter("800"), "0xabc", tmp_path,
                          now_fn=lambda: 1100.0, persist=False)
    assert ev.recent_peak == Decimal("1000"), "應讀得到既有 peak"
    assert p.read_text() == before, "persist=False 不得改動既有檔案"


def test_future_timestamp_sample_is_discarded(tmp_path: Path):
    """I4：時鐘前跳寫下的未來戳樣本必須被丟棄，否則永不出窗、永久污染 peak。"""
    import json
    from spark.copytrade.equity import SAMPLES_RELPATH

    p = tmp_path / SAMPLES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([[9_999_999_999.0, "5000"]]))  # 遠未來的高點
    ev = perp_equity_view(_FakeAdapter("500"), "0xabc", tmp_path, now_fn=lambda: 1000.0)
    assert ev.recent_peak == Decimal("500"), "未來戳樣本不得影響 peak"


def test_reset_samples_never_raises(tmp_path: Path, monkeypatch):
    """I3：清理失敗絕不能拋例外（會吃掉 kill switch 的總結告警）。"""
    import spark.copytrade.equity as eq

    perp_equity_view(_FakeAdapter("100"), "0xabc", tmp_path, now_fn=lambda: 1.0)

    def _boom(*a, **k):
        raise PermissionError("read-only fs")

    monkeypatch.setattr(eq.Path, "unlink", _boom)
    eq.reset_samples(tmp_path)  # 不得拋出


def test_reset_samples_missing_file_is_noop(tmp_path: Path):
    """檔案不存在時不得拋錯（missing_ok）。"""
    reset_samples(tmp_path)
