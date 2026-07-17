"""tests/test_filet_aggregate.py
跨 follower 日報匯總：北極星（builder 層級查一次）不得跨 follower 加總；
per-follower summary 只做 fills 衍生指標（fills 數、taker_share），不查 accrued。
"""
from datetime import date, datetime, timezone
from decimal import Decimal

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
