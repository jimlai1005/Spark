"""tests/test_filet_aggregate.py
跨 follower 日報匯總：北極星（builder 層級查一次）不得跨 follower 加總；
per-follower summary 只做 fills 衍生指標（fills 數、taker_share），不查 accrued。
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import scripts.filet_daily_report as fdr
from spark.filet.followers import FollowerRef
from spark.filet.aggregate import (
    FollowerSummary,
    aggregate,
    render_aggregate,
    builder_fee_delta,
    collect_follower_summary,
)


def _ref(aid, net="mainnet"):
    return FollowerRef(aid, "0x" + "a" * 40, "0x" + "b" * 40, net)


def test_north_star_is_builder_level_not_summed():
    # 北極星＝傳入的 builder 層級日增量，與 per-follower summary 無關、不加總
    summaries = [FollowerSummary(_ref("alice"), fills=8, taker_share=Decimal("0.2"),
                                 error=None),
                 FollowerSummary(_ref("bob", "testnet"), fills=3,
                                 taker_share=Decimal("0.1"), error=None)]
    agg = aggregate(date(2026, 7, 17), summaries,
                    north_star_fee_delta=Decimal("1.84"))
    assert agg.north_star_fee_delta == Decimal("1.84")   # 查一次的值，非相加
    assert agg.follower_count == 2 and agg.ok_count == 2


def test_failed_follower_excluded_from_ok_but_listed():
    summaries = [FollowerSummary(_ref("alice"), fills=8, taker_share=Decimal("0.2"),
                                 error=None),
                 FollowerSummary(_ref("bob"), fills=0, taker_share=Decimal("0"),
                                 error="API timeout")]
    agg = aggregate(date(2026, 7, 17), summaries, north_star_fee_delta=Decimal("1.0"))
    assert agg.ok_count == 1 and agg.follower_count == 2
    assert any("API timeout" in (s.error or "") for s in agg.summaries)


def test_builder_fee_delta_single_query():
    # builder_fee_delta(today, prev) = today - prev（純函式，查一次的差）
    assert builder_fee_delta(Decimal("5.5"), Decimal("3.66")) == Decimal("1.84")


def test_render_has_single_daily_northstar_line():
    agg = aggregate(date(2026, 7, 17), [], north_star_fee_delta=Decimal("0"))
    out = render_aggregate(agg)
    assert "單日 builder fee 增量" in out   # opus m6：正名為單日，非「30日日增」


def test_empty_no_crash():
    agg = aggregate(date(2026, 7, 17), [], north_star_fee_delta=Decimal("0"))
    assert agg.north_star_fee_delta == Decimal("0") and agg.follower_count == 0
    # render 也不得炸
    render_aggregate(agg)


# --- collect_follower_summary：三案（正常算 taker_share、例外入 summary、空 fills） ---

class _FakeFill:
    def __init__(self, sz, px, crossed):
        self.sz = Decimal(sz)
        self.px = Decimal(px)
        self.crossed = crossed


class _FakeAdapter:
    def __init__(self, fills=None, raises=None):
        self._fills = fills if fills is not None else []
        self._raises = raises

    def get_user_fills(self, address, start, end):
        if self._raises is not None:
            raise self._raises
        return self._fills


_START = datetime(2026, 7, 17, tzinfo=timezone.utc)
_END = datetime(2026, 7, 17, 23, 59, tzinfo=timezone.utc)


def test_collect_follower_summary_computes_taker_share():
    # 手算：taker(crossed) 名目 = 2*100 + 1*50 = 250；總名目 = 250 + 3*10 = 280
    fills = [
        _FakeFill("2", "100", True),
        _FakeFill("1", "50", True),
        _FakeFill("3", "10", False),
    ]
    adapter = _FakeAdapter(fills=fills)
    s = collect_follower_summary(_ref("alice"), adapter, _START, _END)
    assert s.error is None
    assert s.fills == 3
    assert s.taker_share == Decimal("250") / Decimal("280")


def test_collect_follower_summary_error_captured_not_raised():
    adapter = _FakeAdapter(raises=RuntimeError("boom: connection reset"))
    s = collect_follower_summary(_ref("bob"), adapter, _START, _END)
    assert s.error is not None and "boom" in s.error
    assert s.fills == 0
    assert s.taker_share == Decimal("0")


def test_collect_follower_summary_empty_fills():
    adapter = _FakeAdapter(fills=[])
    s = collect_follower_summary(_ref("carol"), adapter, _START, _END)
    assert s.error is None
    assert s.fills == 0
    assert s.taker_share == Decimal("0")


# --- filet_daily_report.main() wiring 整合測試 ---


class _CountingAdapter:
    """記數 query_builder_accrued 呼叫；get_user_fills 回可設定的空/非空 fills。"""

    def __init__(self, accrued, fills=None):
        self._accrued = accrued
        self._fills = fills if fills is not None else []
        self.accrued_calls = []          # 每次呼叫記錄 builder 參數
        self.fills_calls = []            # 每次呼叫記錄 address 參數

    def query_builder_accrued(self, builder):
        self.accrued_calls.append(builder)
        return self._accrued

    def get_user_fills(self, address, start, end):
        self.fills_calls.append(address)
        return list(self._fills)


def _ref_full(aid, builder, net="mainnet"):
    return FollowerRef(aid, "0x" + "a" * 40, builder, net)


def test_wiring_shared_builder_queried_once(tmp_path, monkeypatch):
    # 快照/報表目錄導向 tmp_path，避免碰 repo var/
    monkeypatch.setattr(fdr, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(fdr, "REPORTS_DIR", tmp_path / "reports")

    builder_mixed = "0x" + "B" * 40      # checksum 大小寫
    builder_lower = "0x" + "b" * 40      # 同一位址、小寫
    refs = [
        _ref_full("alice", builder_mixed, "mainnet"),
        _ref_full("bob", builder_lower, "mainnet"),   # 與 alice 同一 builder（大小寫不同）
        _ref_full("carol", "0x" + "c" * 40, "testnet"),  # testnet 不計入北極星
    ]

    # 快照無檔（tmp_path 無 snap.json）→ prev=0；今日 accrued=5.5 → 北極星 delta=5.5（單次）
    adapter = _CountingAdapter(accrued=Decimal("5.5"), fills=[])

    def adapter_for(_network):
        return adapter

    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    report = fdr.generate_report(refs, [], adapter_for, now)

    # ⭐ 共用 builder（含大小寫不同）只查一次
    assert adapter.accrued_calls == ["0x" + "b" * 40]
    # 北極星＝單次 delta，非兩個 follower 相加
    assert report.north_star_fee_delta == Decimal("5.5")
    # 三個 follower 都在明細（含 testnet）
    assert report.follower_count == 3
    # per-follower fills 對三個 follower 位址都查了（含 testnet 的活動歸屬）
    assert len(adapter.fills_calls) == 3
    # 快照寫入單一 builder 位址（小寫）
    snap = json.loads((tmp_path / "snap.json").read_text())
    assert list(snap["builders"].keys()) == ["0x" + "b" * 40]
    assert snap["builders"]["0x" + "b" * 40] == "5.5"


def test_wiring_testnet_only_no_accrued_query(tmp_path, monkeypatch):
    monkeypatch.setattr(fdr, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(fdr, "REPORTS_DIR", tmp_path / "reports")

    refs = [_ref_full("carol", "0x" + "c" * 40, "testnet")]
    adapter = _CountingAdapter(accrued=Decimal("9.9"), fills=[])
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    report = fdr.generate_report(refs, [], lambda _n: adapter, now)

    # testnet builder 完全不觸發 accrued 查詢
    assert adapter.accrued_calls == []
    assert report.north_star_fee_delta == Decimal("0")
    # 快照無 mainnet builder → 不寫檔（維持無檔=0 語意）
    assert not (tmp_path / "snap.json").exists()


def test_wiring_north_star_uses_prev_snapshot(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(
        {"date": "2026-07-16", "builders": {"0x" + "b" * 40: "3.66"}}))
    monkeypatch.setattr(fdr, "SNAPSHOT_PATH", snap_path)
    monkeypatch.setattr(fdr, "REPORTS_DIR", tmp_path / "reports")

    refs = [_ref_full("alice", "0x" + "B" * 40, "mainnet")]
    adapter = _CountingAdapter(accrued=Decimal("5.5"), fills=[])
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    report = fdr.generate_report(refs, [], lambda _n: adapter, now)

    # 北極星＝今日 5.5 - 昨日快照 3.66 = 1.84（查一次的差）
    assert adapter.accrued_calls == ["0x" + "b" * 40]
    assert report.north_star_fee_delta == Decimal("1.84")
