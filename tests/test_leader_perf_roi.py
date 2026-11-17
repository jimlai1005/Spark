"""tests/test_leader_perf_roi.py — 策略績效數字修正（plan 2026-11-16-strategy-return-hl-roi §1）。

三個獨立錯誤的結構性防線，期望值全部由 plan §1 的方程式**手算**釘死（不抄實作輸出）：
- A1：入金與損益落在同一取樣區間時，分母要加上該段淨流入（§1.1）。
- A2：Hyperliquid 排行榜 ROI 定義（§1.3）。
- A3：比率指標依實際取樣間隔年化（§1.2）。
"""
from decimal import Decimal

import pytest

from spark.filet.leader_perf import (STATUS_OK, compute_ratio_metrics,
                                     compute_window_performance)

DAY_MS = 86_400_000
_Q6 = Decimal("0.000001")
_Q4 = Decimal("0.0001")


def _rows(points, period="allTime"):
    """points = [(day_offset, account_value, cum_pnl)] → portfolio() 形狀的回應。"""
    av = [[d * DAY_MS, str(a)] for d, a, _ in points]
    pnl = [[d * DAY_MS, str(p)] for d, _, p in points]
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "1"}]]


# ---------------------------------------------------------------------------
# A1：入金放大修正（§1.1）
# AV=[8800, 49700]、P=[0, -6100] → ΔP=-6100、ΔAV=40900、ΔF=47000
# r = -6100 / (8800 + 47000) = -6100 / 55800 = -0.109319（舊公式 -6100/8800 = -0.693182）
# ---------------------------------------------------------------------------
def test_a1_same_interval_inflow_is_added_to_denominator():
    perf = compute_window_performance(
        _rows([(0, "8800", "0"), (10, "49700", "-6100")]), "allTime")
    assert perf["status"] == STATUS_OK
    r = perf["equity_index"][1] - Decimal("1")
    assert r.quantize(_Q6) == Decimal("-0.109319")
    assert perf["twr"].quantize(_Q6) == Decimal("-0.109319")
    assert perf["twr"].quantize(_Q6) != Decimal("-0.693182")   # 舊公式的值


def test_loss_with_same_interval_deposit_has_negative_roi_and_negative_twr():
    """Ultron 型：同區間入金＋虧損，ROI 與 TWR 都必須是負的。
    D = max(100, 8800 + max(0, 47000)) = 55800；ROI = -6100/55800。"""
    perf = compute_window_performance(
        _rows([(0, "8800", "0"), (10, "49700", "-6100")]), "allTime")
    assert perf["roi"] < 0 and perf["twr"] < 0
    assert perf["roi_denominator"] == Decimal("55800")
    assert perf["roi"].quantize(_Q6) == Decimal("-0.109319")


def test_outflow_does_not_adjust_denominator():
    """只加正的流入；流出不調整分母（§1.1）。AV=[1000,400]、P=[0,-100]：
    ΔF = -600 - (-100) = -500（流出）→ r = -100/1000 = -0.1；D = 1000。"""
    perf = compute_window_performance(
        _rows([(0, "1000", "0"), (10, "400", "-100")]), "allTime")
    assert perf["twr"] == Decimal("-0.1")
    assert perf["roi_denominator"] == Decimal("1000")
    assert perf["roi"] == Decimal("-0.1")


# ---------------------------------------------------------------------------
# A2：HL ROI（§1.3）
# AV=[1000,1500,3000,2200]、P=[0,100,300,500] → ΔF=[400,1300,-1000]、F=[0,400,1700,700]
# D = max(100, 1000 + 1700) = 2700；ROI = 500/2700 = 0.185185
# roi_index = [1, 1.037037, 1.111111, 1.185185]
# ---------------------------------------------------------------------------
def _a2_perf():
    return compute_window_performance(
        _rows([(0, "1000", "0"), (10, "1500", "100"),
               (20, "3000", "300"), (30, "2200", "500")]), "allTime")


def test_a2_hl_roi_and_roi_index():
    perf = _a2_perf()
    assert perf["status"] == STATUS_OK
    assert perf["roi_denominator"] == Decimal("2700")
    assert perf["roi"].quantize(_Q6) == Decimal("0.185185")
    assert [x.quantize(_Q6) for x in perf["roi_index"]] == [
        Decimal("1.000000"), Decimal("1.037037"),
        Decimal("1.111111"), Decimal("1.185185")]
    assert isinstance(perf["roi_index"], tuple)
    assert len(perf["roi_index"]) == 4
    # 淨外部現金流（F_n）＝400 + 1300 - 1000 = 700
    assert perf["net_external_flow"] == Decimal("700")
    # 30 天剛好達門檻：不是不足
    assert perf["roi_insufficient_data"] is False


def test_a2_roi_annualized_follows_d5():
    """D5：ROI > -1 時 roi_annualized = (1+ROI)^(365/covered_days) - 1。"""
    perf = _a2_perf()
    expected = (Decimal("1") + perf["roi"]) ** (Decimal("365") / Decimal("30")) - 1
    assert perf["roi_annualized"] == expected
    assert perf["roi_annualized_insufficient_data"] is True      # 30 < 90 天
    assert perf["roi_annualized_extrapolated_from_days"] == Decimal("30.0000")


def test_roi_annualized_absent_when_roi_le_minus_one():
    """ROI <= -1（虧光）→ 年化無定義：roi_annualized 與它的標記一起缺席。
    AV=[1000,0]、P=[0,-1000]：r=-1（合法歸零、最後一段）、D=1000、ROI=-1。"""
    perf = compute_window_performance(
        _rows([(0, "1000", "0"), (40, "0", "-1000")]), "allTime")
    assert perf["status"] == STATUS_OK
    assert perf["roi"] == Decimal("-1")
    assert "roi_annualized" not in perf
    assert "roi_annualized_insufficient_data" not in perf
    assert "roi_annualized_extrapolated_from_days" not in perf


def test_roi_insufficient_below_30_days():
    perf = compute_window_performance(
        _rows([(0, "1000", "0"), (10, "1100", "100")]), "allTime")
    assert perf["roi_insufficient_data"] is True
    assert perf["twr_insufficient_data"] is True      # 同一判定


def test_roi_denominator_floor_is_100():
    """D = max(100, AV_0 + max_t F_t)：AV_0=50 低於地板時分母取 100。
    AV=[50,120,130]、P=[0,70,80]：第一段 prev_av=50 < 100 被跳過（開頭未入金），
    F 恆 0 → D = max(100, 50) = 100；ROI = 80/100 = 0.8。"""
    perf = compute_window_performance(
        _rows([(0, "50", "0"), (20, "120", "70"), (40, "130", "80")]), "allTime")
    assert perf["status"] == STATUS_OK
    assert perf["roi_denominator"] == Decimal("100")
    assert perf["roi"] == Decimal("0.8")


# ---------------------------------------------------------------------------
# A3：比率指標依取樣間隔年化（§1.2）
# r = [0.01, -0.005, 0.02]，periods_per_year = 36.5（平均 10 天一筆）
# → Sharpe 4.0011 / SE 3.8516 / vol 0.0760 / Sortino 17.4404
# （預設 365 的錨例 12.6526 / 12.1798 / 0.2404 / 55.1513 在 test_leader_perf_ratios.py）
# ---------------------------------------------------------------------------
_A3_RETURNS = [Decimal("0.01"), Decimal("-0.005"), Decimal("0.02")]


def test_a3_ratio_metrics_annualize_by_periods_per_year():
    r = compute_ratio_metrics(_A3_RETURNS, periods_per_year=Decimal("36.5"))
    assert r["sharpe"].quantize(_Q4) == Decimal("4.0011")
    assert r["sharpe_se"].quantize(_Q4) == Decimal("3.8516")
    assert r["annualized_vol"].quantize(_Q4) == Decimal("0.0760")
    assert r["sortino"].quantize(_Q4) == Decimal("17.4404")


def test_ratio_metrics_default_periods_per_year_is_365_unchanged():
    r = compute_ratio_metrics(_A3_RETURNS)
    assert r["sharpe"].quantize(_Q4) == Decimal("12.6526")
    assert r["sharpe_se"].quantize(_Q4) == Decimal("12.1798")
    assert r["annualized_vol"].quantize(_Q4) == Decimal("0.2404")
    assert r["sortino"].quantize(_Q4) == Decimal("55.1513")


def test_ratio_metrics_rejects_non_positive_periods_per_year():
    with pytest.raises(ValueError):
        compute_ratio_metrics(_A3_RETURNS, periods_per_year=Decimal("0"))


def test_a3_window_performance_passes_average_sampling_interval():
    """3 個區間涵蓋 30 天 → periods_per_year = 365 / (30/3) = 36.5；
    AV/P 構造成 r = [0.01, -0.005, 0.02]（無外部現金流）。"""
    perf = compute_window_performance(
        _rows([(0, "1000", "0"), (10, "1010", "10"),
               (20, "1004.95", "4.95"), (30, "1025.049", "25.049")]), "allTime")
    assert perf["periods_per_year"] == Decimal("36.5")
    assert perf["sharpe"].quantize(_Q4) == Decimal("4.0011")
    assert perf["sharpe_se"].quantize(_Q4) == Decimal("3.8516")
    assert perf["annualized_vol"].quantize(_Q4) == Decimal("0.0760")
    assert perf["sortino"].quantize(_Q4) == Decimal("17.4404")
    # 內部鍵名不改（plan §1.2）
    assert perf["best_day_return"].quantize(_Q6) == Decimal("0.020000")
    assert perf["worst_day_return"].quantize(_Q6) == Decimal("-0.005000")


def test_old_flow_dominated_scenario_is_now_a_valid_window():
    """舊 test_flow_dominated_interval 情境：前值 1000、同區間入金 5000 且虧 1500
    （av=[1000,4500,4600]、pnl=[0,-1500,-1400]）。ΔF = 3500 - (-1500) = 5000，
    新分母 = 1000 + 5000 = 6000 → r = -1500/6000 = -0.25（> -1，整窗有效）；
    舊分母 1000 → r = -1.5 被判 flow_dominated_interval。"""
    perf = compute_window_performance(
        _rows([(0, "1000", "0"), (10, "4500", "-1500"), (20, "4600", "-1400")]),
        "allTime")
    assert perf["status"] == STATUS_OK
    r1 = perf["equity_index"][1] - Decimal("1")
    assert r1 == Decimal("-0.25")
    assert perf["roi_denominator"] == Decimal("6000")
    assert perf["roi"].quantize(_Q6) == Decimal("-0.233333")   # -1400/6000


def test_jsonable_performance_handles_roi_index_like_equity_index():
    """`roi_index` 是 tuple of Decimal：快照落檔（json.dumps）前必須被轉掉，否則整份
    watchlist 快照寫檔 TypeError。預設只留長度，要畫圖才輸出字串序列。"""
    import json

    from spark.filet.leader_perf import jsonable_performance
    perf = _a2_perf()
    j = jsonable_performance(perf)
    assert "roi_index" not in j and j["roi_index_len"] == 4
    json.dumps(j)                                    # 不得拋 TypeError
    j2 = jsonable_performance(perf, include_equity_index=True)
    assert [Decimal(x).quantize(_Q6) for x in j2["roi_index"]] == [
        Decimal("1.000000"), Decimal("1.037037"),
        Decimal("1.111111"), Decimal("1.185185")]
    json.dumps(j2)


# ---------------------------------------------------------------------------
# 2026-11-17 Task 5 W3：periods_per_year 由原始毫秒時間戳算，不經 quantize 過的
# covered_days（兩點相距 1 ms 時 covered_days 量化為 0.0000 → 舊寫法除以零）。
# periods_per_year = 365 * n * 86400000 / (t_n - t_0)
# ---------------------------------------------------------------------------
def test_w3_two_points_one_millisecond_apart_does_not_raise():
    rows = [["allTime", {
        "accountValueHistory": [[0, "1000"], [1, "1100"]],
        "pnlHistory": [[0, "0"], [1, "100"]], "vlm": "1"}]]
    perf = compute_window_performance(rows, "allTime")
    assert perf["status"] == STATUS_OK
    # n=1、Δt=1ms → 365 * 1 * 86_400_000 / 1
    assert perf["periods_per_year"] == Decimal(365 * 86_400_000)
