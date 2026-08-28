"""tests/test_leader_perf_ratios.py — leader_perf 比率型指標
（Sharpe/Sortino/年化波動/日勝率/最佳最差日）。

規格與數值錨例來源：
docs/superpowers/plans/2026-08-28-redesign-strategy-platform.md Task 4。

⚠️ 錨例校正說明（本檔 `test_anchor_ratio_metrics_match_plan_spec` docstring 重述）：
plan 文字錨例的 `mean`/`s`/`σ_ann`/`Sortino`(含其 DD)/`勝率`/`best`/`worst` 七個數字，
用 Decimal 50 位精度依 plan 給的公式逐步重算，逐一與 plan 文字**精確相符**（相差
< 1e-6），驗證了公式實作無誤、且輸入沒有被誤解。但 `SR`（plan 寫 12.6535）與
`SE`（plan 寫 12.1800）兩個數字用同一組已驗證公式重算得到 12.652578 / 12.179817，
與 plan 文字相差 ~0.0009 / ~0.0002，超出 plan 自訂的 1e-4 精度要求——嘗試了
ddof=0 變體、用 plan 文字裡已四捨五入的 mean/s 代入重算等替代解讀，皆無法重現
plan 文字的 12.6535/12.1800。判定這兩個字面錨例本身含有 plan 撰寫時的計算誤差
（而非公式歧義：LaTeX 公式明確、且其餘七個錨例分毫不差地驗證了同一份實作），
因此本檔對 SR/SE 斷言使用重新推導、可獨立驗證的精確值（12.6526 / 12.1798），
而非逐字抄 plan 文字。已於任務回報中向主線程揭露此發現。
"""
from decimal import Decimal, getcontext

import pytest

from spark.filet.leader_perf import (RATIO_MIN_DAYS, compute_ratio_metrics,
                                     compute_window_performance,
                                     jsonable_performance)

getcontext().prec = 50
DAY_MS = 86_400_000


def rows(points, period="perpMonth"):
    """`points = [(day_offset, account_value, cum_pnl)]` → `portfolio()` 回應形狀。"""
    av = [[d * DAY_MS, str(a)] for d, a, _ in points]
    pnl = [[d * DAY_MS, str(p)] for d, _, p in points]
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "1"}]]


# 4 點、3 段報酬 [0.1, -0.1, 0.1]：非零變異數＋含下檔日，可同時驗證 sharpe/vol
# （需要 N>=2 且 s!=0）與 sortino（需要 DD!=0）都算得出來的一般情況。
_MIXED_RETURNS = [(0, "1000", "0"), (15, "1100", "100"),
                  (35, "990", "-10")]


def _mixed_returns_points(last_day: Decimal) -> list[tuple[int, str, str]]:
    return _MIXED_RETURNS + [(int(last_day), "1089", "89")]


# --- 錨例（純函式，繞過 60 天閘門）------------------------------------------

def test_anchor_ratio_metrics_match_plan_spec():
    """r = [0.01, -0.005, 0.02]（N=3）——見檔頭「錨例校正說明」。"""
    returns = [Decimal("0.01"), Decimal("-0.005"), Decimal("0.02")]
    r = compute_ratio_metrics(returns)

    assert r["sample_count"] == 3
    assert float(r["win_rate"]) == pytest.approx(2 / 3, abs=1e-4)
    assert r["best_day_return"] == Decimal("0.02")
    assert r["worst_day_return"] == Decimal("-0.005")

    # mean/s 不是直接輸出鍵，但用於交叉驗證公式無誤（見檔頭說明）。
    mean = sum(returns, Decimal("0")) / 3
    assert float(mean) == pytest.approx(0.0083333, abs=1e-6)
    variance = sum((x - mean) ** 2 for x in returns) / 2
    s = variance.sqrt()
    assert float(s) == pytest.approx(0.0125831, abs=1e-6)

    assert float(r["annualized_vol"]) == pytest.approx(0.2404, abs=1e-4)
    assert float(r["sortino"]) == pytest.approx(55.1513, abs=1e-4)

    # ⚠️ SR/SE：重新推導後的精確值（非 plan 文字逐字值，見檔頭說明）。
    assert float(r["sharpe"]) == pytest.approx(12.6526, abs=1e-4)
    assert float(r["sharpe_se"]) == pytest.approx(12.1798, abs=1e-4)


def test_sortino_absent_when_dd_zero():
    """全樣本無下檔日（DD=0）→ Sortino 分母為 0，數學上無定義 → 整組缺席。"""
    r = compute_ratio_metrics([Decimal("0.01"), Decimal("0"), Decimal("0.02")])
    assert "sortino" not in r
    # 其他不受影響：sharpe/vol/win_rate 照樣算得出來
    assert "sharpe" in r
    assert "annualized_vol" in r


def test_sharpe_family_absent_when_fewer_than_two_returns():
    """N=1：ddof=1 樣本標準差需要至少 2 點 → sharpe/sharpe_se/annualized_vol 缺席。

    win_rate/best/worst 仍照算（不設任何門檻）。
    """
    r = compute_ratio_metrics([Decimal("0.02")])
    assert r["sample_count"] == 1
    for k in ("sharpe", "sharpe_se", "annualized_vol"):
        assert k not in r
    assert r["win_rate"] == Decimal("1")
    assert r["best_day_return"] == Decimal("0.02")
    assert r["worst_day_return"] == Decimal("0.02")
    # 單點無下檔 → DD=0 → sortino 也缺席
    assert "sortino" not in r


def test_empty_returns_only_has_sample_count():
    r = compute_ratio_metrics([])
    assert r == {"sample_count": 0}


# --- 樣本閘門：covered_days < RATIO_MIN_DAYS(60) → *_insufficient_data=True ---

def test_ratio_metrics_below_60_days_flagged_insufficient():
    r = compute_window_performance(rows(_mixed_returns_points(Decimal("59"))),
                                    "perpMonth")
    assert r["covered_days"] == Decimal("59.0000")
    assert r["covered_days"] < RATIO_MIN_DAYS
    # 數字要給（沿用既有「顯示但註記」慣例）
    assert "sharpe" in r and "sortino" in r and "annualized_vol" in r
    assert r["sharpe_insufficient_data"] is True
    assert r["sortino_insufficient_data"] is True
    assert r["annualized_vol_insufficient_data"] is True
    # 勝率/最佳最差日不設閘：存在但沒有對應的 insufficient 標記
    assert "win_rate" in r
    assert "win_rate_insufficient_data" not in r


def test_ratio_metrics_at_60_days_not_flagged_insufficient():
    r = compute_window_performance(rows(_mixed_returns_points(Decimal("60"))),
                                    "perpMonth")
    assert r["covered_days"] == Decimal("60.0000")
    assert not (r["covered_days"] < RATIO_MIN_DAYS)
    assert r["sharpe_insufficient_data"] is False
    assert r["sortino_insufficient_data"] is False
    assert r["annualized_vol_insufficient_data"] is False


def test_ratio_insufficient_markers_never_appear_without_the_number():
    """標記與數字同生共死（同一慣例套用到比率指標）：DD=0 時整窗只有 sortino 缺席，
    sortino_insufficient_data 也必須跟著缺席，不能單獨留下一個沒有數字的標記。"""
    # 全部非負報酬 → DD=0（沿用 test_leader_perf.py 的 _FLOWS_60D 形狀）
    r = compute_window_performance(
        rows([(0, "1000", "0"), (20, "1100", "100"),
              (40, "2100", "100"), (60, "2310", "310")]), "perpMonth")
    assert "sortino" not in r
    assert "sortino_insufficient_data" not in r
    # sharpe/vol 這組不受 DD=0 影響，照樣存在並帶標記
    assert "sharpe" in r and "sharpe_insufficient_data" in r


# --- jsonable_performance 帶出新欄位（Decimal → str） -----------------------

def test_jsonable_performance_serializes_ratio_fields():
    r = compute_window_performance(rows(_mixed_returns_points(Decimal("60"))),
                                    "perpMonth")
    j = jsonable_performance(r)
    assert isinstance(j["sharpe"], str)
    assert isinstance(j["sharpe_se"], str)
    assert isinstance(j["annualized_vol"], str)
    assert isinstance(j["sortino"], str)
    assert isinstance(j["win_rate"], str)
    assert isinstance(j["best_day_return"], str)
    assert isinstance(j["worst_day_return"], str)
    # bool 標記原樣通過（不轉字串——"False" 是真值為 True 的字串，致命）
    assert j["sharpe_insufficient_data"] is False
    assert j["sortino_insufficient_data"] is False
    assert j["annualized_vol_insufficient_data"] is False
