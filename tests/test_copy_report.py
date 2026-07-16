"""TE + fee 日報計算測試（純函式，不觸網）。"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from spark.copytrade.report import (
    DailyReport,
    _median,
    build_daily_report,
    pair_fills,
    render_report,
)
from spark.exchange.base import UserFill
from spark.verification.reconcile import ReconcileReport

DAY = date(2026, 7, 15)


def dt(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 15, hour, minute, second, tzinfo=timezone.utc)


def uf(t: datetime, coin: str = "ETH", px: str = "2000", sz: str = "1",
       side: str = "B", crossed: bool = True, oid: int = 1) -> UserFill:
    return UserFill(time=t, coin=coin, px=Decimal(px), sz=Decimal(sz),
                    side=side, crossed=crossed, oid=oid, fee=Decimal("0.1"))


def empty_csv_report() -> ReconcileReport:
    return ReconcileReport(builder="0xb", day=DAY, expected_coin=None,
                           total_builder_fee=Decimal("0"), fills=[])


@dataclass(frozen=True)
class StubCsvReport:
    """duck-typed csv_report：真 ReconcileReport 的 matched 由 fills 衍生，
    無法構造 fill_count>0 且 matched=False；用 stub 驗 build_daily_report 的分支邏輯。"""
    fill_count: int
    matched: bool


class TestMedian:
    def test_odd_count(self):
        assert _median([Decimal("5"), Decimal("1"), Decimal("3")]) == Decimal("3")

    def test_even_count_averages_two_middles(self):
        vals = [Decimal("4"), Decimal("1"), Decimal("3"), Decimal("2")]
        assert _median(vals) == Decimal("2.5")

    def test_empty_returns_none(self):
        assert _median([]) is None


class TestPairFills:
    def test_pair_within_window_delay_correct(self):
        leader = uf(dt(10, 0, 0), oid=1)
        mine = uf(dt(10, 0, 30), px="2001", oid=2)
        paired, unpaired = pair_fills([leader], [mine], window_s=120)
        assert len(paired) == 1
        assert paired[0].leader == leader
        assert paired[0].mine == mine
        assert paired[0].delay_s == Decimal("30")
        assert unpaired == []

    def test_outside_window_not_paired(self):
        leader = uf(dt(10, 0, 0), oid=1)
        mine = uf(dt(10, 3, 0), oid=2)  # 180s > 120s 窗
        paired, unpaired = pair_fills([leader], [mine], window_s=120)
        assert paired == []
        assert unpaired == [mine]

    def test_different_coin_not_paired(self):
        leader = uf(dt(10, 0, 0), coin="ETH", oid=1)
        mine = uf(dt(10, 0, 30), coin="BTC", oid=2)
        paired, unpaired = pair_fills([leader], [mine])
        assert paired == []
        assert unpaired == [mine]

    def test_different_side_not_paired(self):
        leader = uf(dt(10, 0, 0), side="B", oid=1)
        mine = uf(dt(10, 0, 30), side="A", oid=2)
        paired, unpaired = pair_fills([leader], [mine])
        assert paired == []
        assert unpaired == [mine]

    def test_one_leader_multiple_candidates_takes_nearest(self):
        leader = uf(dt(10, 0, 0), oid=1)
        far = uf(dt(10, 0, 50), oid=2)   # 50s 差
        near = uf(dt(10, 0, 20), oid=3)  # 20s 差 ← 應被選
        paired, unpaired = pair_fills([leader], [far, near])
        assert len(paired) == 1
        assert paired[0].mine == near
        assert unpaired == [far]

    def test_my_fill_not_reused_greedy_by_time_diff(self):
        # 一筆 my fill、兩筆 leader：貪婪依時間差排序 → 較近的 leader2（5s）取得配對，
        # leader1（15s）落空；my fill 不重複使用。
        leader1 = uf(dt(10, 0, 0), oid=1)
        leader2 = uf(dt(10, 0, 10), oid=2)
        mine = uf(dt(10, 0, 15), oid=3)
        paired, unpaired = pair_fills([leader1, leader2], [mine])
        assert len(paired) == 1
        assert paired[0].leader == leader2
        assert paired[0].delay_s == Decimal("5")
        assert unpaired == []

    def test_negative_delay_mine_before_leader(self):
        leader = uf(dt(10, 0, 30), oid=1)
        mine = uf(dt(10, 0, 0), oid=2)
        paired, _ = pair_fills([leader], [mine])
        assert len(paired) == 1
        assert paired[0].delay_s == Decimal("-30")


class TestSlippageDirection:
    def test_buy_more_expensive_positive_5bp(self):
        # 買：mine 2001 vs leader 2000 → (2001-2000)/2000×10000 = +5bp（不利）
        leader = uf(dt(10, 0, 0), side="B", px="2000", oid=1)
        mine = uf(dt(10, 0, 10), side="B", px="2001", oid=2)
        paired, _ = pair_fills([leader], [mine])
        assert paired[0].slippage_bp == Decimal("5")

    def test_sell_lower_positive_5bp(self):
        # 賣：mine 1999 vs leader 2000 → (2000-1999)/2000×10000 = +5bp（不利）
        leader = uf(dt(10, 0, 0), side="A", px="2000", oid=1)
        mine = uf(dt(10, 0, 10), side="A", px="1999", oid=2)
        paired, _ = pair_fills([leader], [mine])
        assert paired[0].slippage_bp == Decimal("5")

    def test_buy_cheaper_negative(self):
        # 買便宜 → 負滑價（有利）
        leader = uf(dt(10, 0, 0), side="B", px="2000", oid=1)
        mine = uf(dt(10, 0, 10), side="B", px="1999", oid=2)
        paired, _ = pair_fills([leader], [mine])
        assert paired[0].slippage_bp == Decimal("-5")


class TestMedianDelayInReport:
    def test_odd_number_of_pairs(self):
        leaders = [uf(dt(10, 0, 0), oid=1), uf(dt(11, 0, 0), oid=2), uf(dt(12, 0, 0), oid=3)]
        mines = [uf(dt(10, 0, 10), oid=4), uf(dt(11, 0, 30), oid=5), uf(dt(12, 0, 50), oid=6)]
        r = build_daily_report(DAY, leaders, mines, [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.pair_count == 3
        assert r.median_delay_s == Decimal("30")

    def test_even_number_of_pairs_averages(self):
        leaders = [uf(dt(10, 0, 0), oid=1), uf(dt(11, 0, 0), oid=2)]
        mines = [uf(dt(10, 0, 10), oid=3), uf(dt(11, 0, 30), oid=4)]
        r = build_daily_report(DAY, leaders, mines, [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.pair_count == 2
        assert r.median_delay_s == Decimal("20")  # (10+30)/2

    def test_no_pairs_is_none(self):
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.median_delay_s is None


class TestTakerShare:
    def test_volume_weighted(self):
        # 兩筆 crossed（2000×1 + 3000×2 = 8000）一筆 maker（1000×4 = 4000）
        # taker_share = 8000 / 12000 = 2/3
        mines = [
            uf(dt(10, 0, 0), px="2000", sz="1", crossed=True, oid=1),
            uf(dt(10, 1, 0), px="3000", sz="2", crossed=True, oid=2),
            uf(dt(10, 2, 0), px="1000", sz="4", crossed=False, oid=3),
        ]
        r = build_daily_report(DAY, [], mines, [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.taker_share == Decimal("8000") / Decimal("12000")

    def test_zero_volume_is_zero(self):
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.taker_share == Decimal("0")


class TestSkippedRatio:
    def test_hand_computed(self):
        # 我方名目 2000×1 = 2000；skipped 100 → ratio = 100/2100
        mines = [uf(dt(10, 0, 0), px="2000", sz="1", oid=1)]
        skipped = [("BTC", Decimal("60")), ("SOL", Decimal("40"))]
        r = build_daily_report(DAY, [], mines, skipped, Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.skipped_small_notional == Decimal("100")
        assert r.skipped_small_ratio == Decimal("100") / Decimal("2100")

    def test_zero_denominator_is_zero(self):
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.skipped_small_notional == Decimal("0")
        assert r.skipped_small_ratio == Decimal("0")


class TestTakerSlippageMedian:
    def test_only_crossed_mine_counted(self):
        leaders = [uf(dt(10, 0, 0), px="2000", oid=1), uf(dt(11, 0, 0), px="2000", oid=2)]
        mines = [
            uf(dt(10, 0, 5), px="2001", crossed=True, oid=3),    # +5bp，計入
            uf(dt(11, 0, 5), px="2010", crossed=False, oid=4),   # +50bp，maker 不計
        ]
        r = build_daily_report(DAY, leaders, mines, [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.pair_count == 2
        assert r.taker_slippage_bp_median == Decimal("5")

    def test_no_crossed_pairs_is_none(self):
        leaders = [uf(dt(10, 0, 0), oid=1)]
        mines = [uf(dt(10, 0, 5), crossed=False, oid=2)]
        r = build_daily_report(DAY, leaders, mines, [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.taker_slippage_bp_median is None


class TestAccruedDelta:
    def test_delta(self):
        r = build_daily_report(DAY, [], [], [], Decimal("100.5"), Decimal("60.25"),
                               empty_csv_report())
        assert r.accrued_delta == Decimal("40.25")


class TestCsvMatchedThreeStates:
    def test_fill_count_positive_matched_true(self):
        csv = StubCsvReport(fill_count=3, matched=True)
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"), csv)
        assert r.csv_matched is True

    def test_fill_count_positive_matched_false(self):
        csv = StubCsvReport(fill_count=3, matched=False)
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"), csv)
        assert r.csv_matched is False

    def test_fill_count_zero_is_none(self):
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"),
                               empty_csv_report())
        assert r.csv_matched is None

    def test_real_reconcile_report_with_fills_is_true(self):
        from spark.exchange.base import Fill
        csv = ReconcileReport(
            builder="0xb", day=DAY, expected_coin=None,
            total_builder_fee=Decimal("0.1"),
            fills=[Fill(time=dt(10, 0, 0), coin="ETH", px=Decimal("2000"),
                        sz=Decimal("1"), side="B", builder_fee=Decimal("0.1"))],
        )
        r = build_daily_report(DAY, [], [], [], Decimal("0"), Decimal("0"), csv)
        assert r.csv_matched is True


class TestRenderReport:
    def test_render_normal(self):
        r = DailyReport(day=DAY, pair_count=5, median_delay_s=Decimal("30"),
                        taker_share=Decimal("0.5"),
                        taker_slippage_bp_median=Decimal("10"),
                        skipped_small_notional=Decimal("100"),
                        skipped_small_ratio=Decimal("0.1"),
                        accrued_delta=Decimal("50"), csv_matched=True)
        text = render_report(r)
        assert "2026-07-15" in text
        assert "相符" in text
        assert "不相符" not in text

    def test_render_csv_none_has_warning_text(self):
        r = DailyReport(day=DAY, pair_count=0, median_delay_s=None,
                        taker_share=Decimal("0"), taker_slippage_bp_median=None,
                        skipped_small_notional=Decimal("0"),
                        skipped_small_ratio=Decimal("0"),
                        accrued_delta=Decimal("0"), csv_matched=None)
        text = render_report(r)
        assert "CSV 無資料或無成交（含 403 被拒情形），不得視為相符" in text
