"""tests/test_leader_perf.py — leader 績效計算（perp 基準，純函式、零網路）。

盯住四件事，每一件都對應一個會讓客戶照著錯數字投真錢的失效：
(1) 出入金不得污染 TWR——期望值一律**手算**釘死，不抄實作的輸出；
(2) MDD 必須算在權益指數 I_t 上（AV 基準會被入金遮成 0、被提領放大成幻影）；
(3) ⚠️ 2026-07-19 使用者裁決改版：薄資料的指標**照樣回傳**，但每一個都必須帶
    自己的不足標記，年化另帶「由幾天外推」。標記漏掉＝一個沒有警示的外推數字。
(4) 資料不足／缺欄位一律回明確狀態，不回 0、不回 NaN、不炸。
"""
from decimal import Decimal

import pytest

from spark.filet.leader_perf import (COMBINED_PERIODS, INSUFFICIENCY_MARKERS,
                                     MAX_SKIPPED_RATIO, MIN_DAYS_FOR_ANNUALIZATION,
                                     PERP_PERIODS, STATUS_INSUFFICIENT, STATUS_OK,
                                     TIER_ANNUALIZABLE, TIER_INSUFFICIENT,
                                     TIER_PNL_ONLY, TIER_WINDOW_RETURN,
                                     compute_perp_performance,
                                     compute_window_performance, extract_window,
                                     jsonable_performance)

DAY_MS = 86_400_000


def rows(points, period="perpMonth", extra=()):
    """points = [(day_offset, account_value, cum_pnl)] → portfolio() 形狀的回應。

    刻意連同一列**非 perp 窗**一起塞（extra 之外預設加 "month"）：真實回應含 8 個
    期別，若實作不小心抓錯列，測試要看得出來。
    """
    av = [[d * DAY_MS, str(a)] for d, a, _ in points]
    pnl = [[d * DAY_MS, str(p)] for d, _, p in points]
    out = [["month", {"accountValueHistory": [[0, "999999"]],
                      "pnlHistory": [[0, "999999"]], "vlm": "0"}]]
    out.append([period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "1"}])
    out.extend(extra)
    return out


# --- 手算基準 A：含兩次出入金，60 天窗 -------------------------------------
# 區間 1: ΔP=100, ΔAV=100 → ΔF=0    r=100/1000  = 0.1
# 區間 2: ΔP=0,   ΔAV=1000 → ΔF=1000（入金） r=0/1100 = 0
# 區間 3: ΔP=210, ΔAV=210  → ΔF=0    r=210/2100 = 0.1
# TWR = 1.1 × 1.0 × 1.1 − 1 = 0.21   cum_pnl = 310 − 0 = 310
_FLOWS_60D = [(0, "1000", "0"), (20, "1100", "100"),
              (40, "2100", "100"), (60, "2310", "310")]

# --- 手算基準 B：入金讓 AV 單調上升，但 I_t 下降（幻影回撤的反向） ----------
# 區間 1: ΔP=-100, ΔAV=1900 → ΔF=2000（入金） r=-100/1000  = -0.1 → I=0.9
# 區間 2: ΔP=-290, ΔAV=2610 → ΔF=2900（入金） r=-290/2900  = -0.1 → I=0.81
# AV 序列 1000→2900→5510 **嚴格遞增** → AV 基準的 MDD 必為 0；I_t 基準為 0.19。
_MASKED_LOSS_40D = [(0, "1000", "0"), (20, "2900", "-100"), (40, "5510", "-390")]


def test_twr_neutralizes_deposits():
    """出入金不得污染 TWR：期望值 0.21 為手算（見 _FLOWS_60D 上方推導）。"""
    r = compute_window_performance(rows(_FLOWS_60D), "perpMonth")
    assert r["status"] == STATUS_OK
    assert r["twr"] == Decimal("0.21")
    assert r["cum_pnl"] == Decimal("310")
    # ΔF 總和＝那一筆 1000 的入金；由同一回應解出，不需第二個端點。
    assert r["net_external_flow"] == Decimal("1000")
    assert r["skipped_intervals"] == 0
    # 天真作法（末/初 − 1 = 2310/1000 − 1 = 1.31）會把入金算成獲利。
    assert r["twr"] != Decimal("1.31")


def test_equity_index_is_the_compounded_series():
    r = compute_window_performance(rows(_FLOWS_60D), "perpMonth")
    assert list(r["equity_index"]) == [Decimal("1"), Decimal("1.1"),
                                       Decimal("1.1"), Decimal("1.21")]


def test_mdd_is_computed_on_equity_index_not_account_value():
    """⭐ 幻影回撤防線：AV 因入金單調上升，MDD 仍必須反映 I_t 的下跌。

    改成算在 accountValue 上 → AV 嚴格遞增 → MDD 變 0 → 本測試轉紅。
    """
    r = compute_window_performance(rows(_MASKED_LOSS_40D), "perpMonth")
    assert list(r["equity_index"]) == [Decimal("1"), Decimal("0.9"), Decimal("0.81")]
    assert r["max_drawdown"] == Decimal("0.19")   # (1 − 0.81) / 1
    assert r["twr"] == Decimal("-0.19")
    # 這一組資料的 AV 是嚴格遞增的，所以 AV 基準的 MDD 恆為 0——
    # 斷言「不等於 0」正是在釘住「用錯基準會發生什麼」。
    assert r["max_drawdown"] != Decimal("0")


def test_below_90_days_still_annualizes_but_marks_it_extrapolated():
    """⭐ 改版後的硬規則：不足 90 天**照樣**給年化，但必須帶不足標記＋外推天數。

    ⭐ 變異測試點：拿掉 `annualized_return_insufficient_data` 或
    `annualized_return_extrapolated_from_days` 的賦值 → 本測試 KeyError 轉紅。
    """
    r = compute_window_performance(rows(_FLOWS_60D), "perpMonth")
    assert r["covered_days"] == Decimal("60.0000")
    assert r["covered_days"] < MIN_DAYS_FOR_ANNUALIZATION
    assert "annualized_return" in r                     # 數字要給
    assert r["annualized_return_insufficient_data"] is True     # 但要說它不足
    # ⭐ 外推天數 = **實際涵蓋天數**，前端才寫得出「由 60 天外推」。
    assert r["annualized_return_extrapolated_from_days"] == Decimal("60.0000")
    assert r["annualized_return_extrapolated_from_days"] == r["covered_days"]
    assert r["disclosure_tier"] == TIER_WINDOW_RETURN   # 分級＝資料充足度
    # 序列化後標記不得消失（Decimal 轉字串、bool 保持 bool）。
    j = jsonable_performance(r)
    assert j["annualized_return_insufficient_data"] is True
    assert j["annualized_return_extrapolated_from_days"] == "60.0000"


def test_sufficient_data_is_marked_sufficient_not_everything_insufficient():
    """⭐ 反面釘死：≥90 天的窗，三個不足標記全部 False。

    沒有這一條，實作把所有標記寫死成 True 也會過——那等於沒有標記。
    """
    r = compute_window_performance(
        rows([(0, "1000", "0"), (90, "1100", "100")]), "perpMonth")
    assert r["covered_days"] == Decimal("90.0000")
    assert r["disclosure_tier"] == TIER_ANNUALIZABLE
    assert "annualized_return" in r
    assert r["twr_insufficient_data"] is False
    assert r["max_drawdown_insufficient_data"] is False
    assert r["annualized_return_insufficient_data"] is False
    # 充足與否是兩個獨立的判定，但外推天數**恆存在**：90 天窗的年化仍是外推。
    assert r["annualized_return_extrapolated_from_days"] == Decimal("90.0000")


def test_annualized_markers_never_appear_without_the_number():
    """⭐ 標記與數字同生共死：年化在數學上無定義（帳戶歸零）→ 三個 annualized_* 鍵
    一起缺席。只留標記會讓前端顯示「由 N 天外推」卻沒有被外推的數字。"""
    # 100 天窗，權益指數走到 0（ΔP = −1000 於 AV=1000 → r = −1 → I = 0）。
    r = compute_window_performance(
        rows([(0, "1000", "0"), (100, "0", "-1000")]), "perpMonth")
    assert r["status"] == STATUS_OK
    assert r["twr"] == Decimal("-1")
    assert r["disclosure_tier"] == TIER_ANNUALIZABLE      # 天數夠，是數學無解
    for k in ("annualized_return", "annualized_return_insufficient_data",
              "annualized_return_extrapolated_from_days"):
        assert k not in r, f"{k} 不該在年化無定義時單獨存在"


def test_annualized_over_one_full_year_equals_window_twr():
    """365 天窗：年化係數為 1，年化值必須等於窗口 TWR（手算 0.21）。"""
    r = compute_window_performance(
        rows([(0, "1000", "0"), (100, "1100", "100"),
              (200, "2100", "100"), (365, "2310", "310")],
             period="perpAllTime"), "perpAllTime")
    assert r["covered_days"] == Decimal("365.0000")
    assert r["twr"] == Decimal("0.21")
    assert r["annualized_return"] == Decimal("0.21")


def test_below_30_days_shows_percentages_but_marks_each_metric_insufficient():
    """⭐⭐ 改版核心：6 天的資料照樣給 twr／MDD，但**每一個指標各自**帶不足標記。

    標記做在指標層級（不是只有 disclosure_tier 一個全域欄位）的理由：前端可能只
    渲染 MDD 一個數字，全域旗標在那個畫面上完全不會出現。

    ⭐ 變異測試點：拿掉 `twr_insufficient_data`／`max_drawdown_insufficient_data`
    的賦值 → 本測試 KeyError 轉紅。
    """
    r = compute_window_performance(
        rows([(0, "1000", "0"), (6, "1380", "380")], period="perpWeek"), "perpWeek")
    assert r["status"] == STATUS_OK
    assert r["disclosure_tier"] == TIER_PNL_ONLY      # 分級＝「資料很薄」
    assert r["cum_pnl"] == Decimal("380")
    # 數字要給（使用者裁決：顯示但註記）
    assert r["twr"] == Decimal("0.38")
    assert "max_drawdown" in r and "equity_index" in r
    # 每個受影響的指標各自帶標記——缺一個都會讓那個指標在前端裸奔
    assert r["twr_insufficient_data"] is True
    assert r["max_drawdown_insufficient_data"] is True
    assert r["annualized_return_insufficient_data"] is True
    assert r["annualized_return_extrapolated_from_days"] == Decimal("6.0000")


def test_every_declared_marker_is_actually_emitted():
    """`INSUFFICIENCY_MARKERS` 是下游投影白名單的唯一來源（publicapi/app.py 由它拼出
    `_LEADER_PERF_FIELDS`）。宣告了卻沒被發出的欄位＝白名單裡的死字串，會讓「標記
    有沒有到前端」這件事無法被任何測試證偽。"""
    r = compute_window_performance(rows(_FLOWS_60D), "perpMonth")
    assert set(INSUFFICIENCY_MARKERS) <= set(r)


def test_single_sample_is_insufficient_not_zero():
    """1 點：明確的「資料不足」，而不是 cum_pnl=0（0 是有意義且錯誤的訊息）。"""
    r = compute_window_performance(rows([(0, "1000", "0")]), "perpMonth")
    assert r["status"] == STATUS_INSUFFICIENT
    assert r["reason"] == "need_at_least_two_samples"
    assert r["sample_count"] == 1
    assert r["disclosure_tier"] == TIER_INSUFFICIENT
    for k in ("cum_pnl", "twr", "max_drawdown", "annualized_return"):
        assert k not in r


@pytest.mark.parametrize("payload,reason", [
    ([], "window_missing"),                                     # 空回應
    ([["perpMonth", {}]], "window_missing"),                    # 缺兩個序列
    ([["perpMonth", {"accountValueHistory": [], "pnlHistory": []}]],
     "need_at_least_two_samples"),                              # 空序列
    ([["perpMonth", {"accountValueHistory": [[0, "1"]]}]], "window_missing"),  # 缺 pnl
    ([["perpMonth", {"accountValueHistory": [[0, "x"]],
                     "pnlHistory": [[0, "1"]]}]], "window_missing"),  # 值不可解析
    ([["perpMonth", "not-a-dict"]], "window_missing"),
    ("not-a-list", "window_missing"),
])
def test_malformed_input_degrades_without_crashing(payload, reason):
    r = compute_window_performance(payload, "perpMonth")
    assert r["status"] == STATUS_INSUFFICIENT
    assert r["reason"] == reason
    assert r["basis"] == "perp"          # 極限註記在不足狀態下也必須帶著
    assert r["mdd_note"] and r["upper_bound_note"] and r["basis_note"]


def test_misaligned_series_refuses_to_compute():
    """兩序列時間戳不同步 = 非同源（工程原則 1）→ 拒絕計算，不硬配對。"""
    bad = [["perpMonth", {"accountValueHistory": [[0, "1000"], [DAY_MS * 40, "1100"]],
                          "pnlHistory": [[0, "0"], [DAY_MS * 39, "100"]]}]]
    r = compute_window_performance(bad, "perpMonth")
    assert r["status"] == STATUS_INSUFFICIENT
    assert r["reason"] == "series_misaligned"
    assert "twr" not in r


def test_denominator_floor_blocks_exploding_returns():
    """提領到近乎 0 後的小額交易不得產生爆炸性報酬；跳過的區間要誠實計數。
    2026-09-05：補兩個平盤點（r=0），讓跳過比例 1/4 = 25% 落在 MAX_SKIPPED_RATIO 之內，
    這條測的是地板，不是比例閘門（比例閘門另有 test_too_many_skipped_intervals_*）。"""
    r = compute_window_performance(
        rows([(0, "10000", "0"), (20, "5", "-9995"), (40, "105", "-9895"),
              (60, "105", "-9895"), (80, "105", "-9895")]),
        "perpMonth")
    assert r["skipped_intervals"] == 1          # 第二段分母 5 < 100，不計入
    # 若不設地板，第二段 r = 100/5 = +2000%，TWR 會從 −99.95% 反彈成正的。
    assert r["twr"] == Decimal("-0.9995")


def test_extract_window_accepts_combined_periods_rejects_unknown():
    """⭐ 2026-08-31 issue log I-15 使用者裁決：`COMBINED_PERIODS`（day/week/month/
    allTime，spot+perp 合併，僅供展示端點在錢包資金停泊 spot 時當正確 basis）現在
    也是合法期別——`PERP_PERIODS` 本身這條閘門完全不變，仍拒絕任何不在兩個白名單
    聯集裡的字串（閘門沒有被拿掉，只是多開一扇窄門）。"""
    payload = rows(_FLOWS_60D)
    # "month" 是 `rows()` 固定加的誘餌列，其餘合併窗（day/week/allTime）不在
    # 這份 payload 裡——重點是「不拋 ValueError」（合法期別），不是「一定找得到列」。
    for combined in COMBINED_PERIODS:
        extract_window(payload, combined)   # 不拋例外＝閘門已放行
    assert extract_window(payload, "month") is not None
    assert extract_window(payload, "perpMonth") is not None
    with pytest.raises(ValueError, match="perp"):
        extract_window(payload, "bogus_period")


def test_perp_and_combined_windows_coexist_compute_uses_requested_family():
    """⭐ 2026-08-31 I-15：真實 HL `portfolio()` 回應同時含 perp 與合併兩個家族的
    視窗（鍵不同、值不同，一如生產環境）——`compute_window_performance` 必須嚴格
    照 `period` 取值，不得混用／回退到另一家族。這裡刻意讓兩個家族算出**相反號**
    的 TWR（合併窗獲利、perp-only 窗因為把內部轉帳算成損益而變成虧損），
    確保拿到的數字真的來自呼叫端指定的那一窗，不是不小心撿到同一份回應裡的另一窗。
    """
    payload = [
        ["perpAllTime", {"accountValueHistory": [[0, "1000"], [DAY_MS * 30, "500"]],
                         "pnlHistory": [[0, "0"], [DAY_MS * 30, "-500"]], "vlm": "0"}],
        ["allTime", {"accountValueHistory": [[0, "1000"], [DAY_MS * 30, "1200"]],
                     "pnlHistory": [[0, "0"], [DAY_MS * 30, "200"]], "vlm": "0"}],
    ]
    combined = compute_window_performance(payload, "allTime")
    assert combined["status"] == STATUS_OK
    assert combined["twr"] == Decimal("0.2")
    assert combined["basis"] == "combined"
    assert combined["basis_note"] != ""

    perp_only = compute_window_performance(payload, "perpAllTime")
    assert perp_only["status"] == STATUS_OK
    assert perp_only["twr"] == Decimal("-0.5")
    assert perp_only["basis"] == "perp"


def test_compute_perp_performance_covers_all_four_perp_windows():
    got = compute_perp_performance(rows(_FLOWS_60D))
    assert set(got) == set(PERP_PERIODS)
    assert got["perpMonth"]["status"] == STATUS_OK
    assert got["perpDay"]["status"] == STATUS_INSUFFICIENT   # 該窗不在 fixture 裡


def test_jsonable_drops_equity_index_but_keeps_its_length():
    r = compute_window_performance(rows(_FLOWS_60D), "perpMonth")
    j = jsonable_performance(r)
    assert "equity_index" not in j and j["equity_index_len"] == 4
    assert j["twr"] == "0.21" and isinstance(j["covered_days"], str)
    j2 = jsonable_performance(r, include_equity_index=True)
    assert j2["equity_index"] == ["1", "1.1", "1.1", "1.21"]


# --- 2026-09-04 閘門 4／5（explore/detail 指標統一 plan，D8）：flow_dominated_interval
# ／too_many_skipped_intervals ---------------------------------------------------
def _portfolio(period, av, pnl):
    ts = [1_700_000_000_000 + i * 3_600_000 for i in range(len(av))]
    return [[period, {"accountValueHistory": [[t, str(a)] for t, a in zip(ts, av)],
                      "pnlHistory": [[t, str(p)] for t, p in zip(ts, pnl)]}]]


def test_flow_dominated_interval_marks_window_insufficient():
    # 前值 1000，同一區間入金 5000 且虧 1500：r = -1500/1000 = -1.5 <= -1 → 整窗無效
    rows = _portfolio("month", av=[1000, 4500, 4600], pnl=[0, -1500, -1400])
    perf = compute_window_performance(rows, "month")
    assert perf["status"] == STATUS_INSUFFICIENT
    assert perf["reason"] == "flow_dominated_interval"
    assert "twr" not in perf and "max_drawdown" not in perf


def test_too_many_skipped_intervals_marks_window_insufficient():
    # 10 點 → 9 區間；入金後又提光 5 段（i=3..7 前值 10 < 100）→ 5/9 = 0.556 > 0.30
    av = [1000, 1010, 10, 10, 10, 10, 10, 1000, 1010, 1020]
    pnl = [0, 10, 10, 10, 10, 10, 10, 10, 20, 30]
    perf = compute_window_performance(_portfolio("month", av, pnl), "month")
    assert perf["status"] == STATUS_INSUFFICIENT
    assert perf["reason"] == "too_many_skipped_intervals"
    assert perf["skipped_intervals"] == 5


def test_exact_total_loss_is_not_flow_dominated():
    # r == -1（上一期淨值全部虧光，無入金）是合法的歸零，不是資金流主導：狀態 ok、TWR = -1
    rows = _portfolio("month", av=[1000, 0], pnl=[0, -1000])
    perf = compute_window_performance(rows, "month")
    assert perf["status"] == "ok" and perf["twr"] == Decimal("-1")


def test_skipped_ratio_exactly_at_threshold_passes():
    # 11 點 → 10 區間；入金後提光 3 段（i=2..4 前值 5）= 0.30，不大於門檻 → ok
    av = [1000, 5, 5, 5, 1000, 1010, 1020, 1030, 1040, 1050, 1060]
    pnl = [0, -995, -995, -995, -995, -985, -975, -965, -955, -945, -935]
    perf = compute_window_performance(_portfolio("month", av, pnl), "month")
    assert perf["status"] == "ok"
    assert perf["skipped_intervals"] == 3
    assert MAX_SKIPPED_RATIO == Decimal("0.30")


def test_leading_unfunded_run_is_not_counted_toward_skipped_ratio():
    # 新 follower：前 20 點 AV=0（尚未入金），入金後 8 個區間全部正常 → ok，cum_pnl 照算
    av = [0] * 20 + [500, 505, 510, 515, 520, 525, 530, 535, 540]
    pnl = [0] * 20 + [0, 5, 10, 15, 20, 25, 30, 35, 40]
    perf = compute_window_performance(_portfolio("perpMonth", av, pnl), "perpMonth")
    assert perf["status"] == "ok"
    assert perf["skipped_intervals"] == 20           # 總跳過數語意不變
    assert perf["cum_pnl"] == Decimal("40")
