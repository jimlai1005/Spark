"""tests/test_public_explore.py — `hl_explore` 純函式 ＋ `ExploreIndex` ＋
`GET /api/public/explore`（M3 round3 Task 1）。

全離線（autouse socket-ban，見 conftest.py）；上游一律靠注入的 `get_fn`／
`FakeHL`，不會真連網。
"""
import json
import socket
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark.publicapi import hl_explore
from spark.publicapi.app import create_app
from spark.filet.trader_stats import WindowStats
from spark.publicapi.hl_explore import (SORT_FIELDS, ExploreConfig, ExploreIndex,
                                        ExploreRow, candidate_addresses, classify,
                                        clamp_explore_params, enrich_candidate,
                                        paginate, qualify, sort_key, sort_rows)
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import FakeHL, FakeKeysvc, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（沿既有慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair；上游全靠注入假資料，
    結構上不會真連網（見 test_public_leaderboard.py 同名 fixture）。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_A = "0x" + "a1" * 20
_B = "0x" + "b2" * 20
_FILET_OWN = "0x" + "f0" * 20

_FIXTURES = Path(__file__).parent / "fixtures"


# ============================================================
# 共用 fixture 建構
# ============================================================

def _leaderboard_payload(*rows):
    return {"leaderboardRows": list(rows)}


def _lb_row(address, display_name=None, roi="0.10"):
    return {"ethAddress": address, "displayName": display_name,
           "windowPerformances": [["month", {"pnl": "1", "roi": roi, "vlm": "1"}]]}


def _av_series(start_ms, values, step_ms=86_400_000):
    """`[[ts_ms, "val"], ...]`，逐日一點（`step_ms` 預設 1 天）。"""
    return [[start_ms + i * step_ms, str(v)] for i, v in enumerate(values)]


def _pnl_from_av(av_series):
    """`[[t, "v"], ...]` → 同時間戳的 pnlHistory，`pnl[i] = v[i] - v[0]`（模擬
    無現金流帳戶：`trader_stats.window_stats` 算出的權益指數 MDD 與舊版直接用
    accountValueHistory 算的 running-peak 回撤數值相同，pnl_usd＝累積損益金額，
    見 2026-09-05 Task 3 修正——`window_stats` 吃 pnlHistory 而非 AV 本身）。"""
    base = Decimal(av_series[0][1])
    return [[t, str(Decimal(v) - base)] for t, v in av_series]


def _portfolio_raw(month_values, alltime_values, start_ms=1_700_000_000_000):
    """最小 `portfolio()` 原始回應：只填 month／allTime（本模組只吃這兩窗）。
    pnlHistory 與 accountValueHistory 同步（見 `_pnl_from_av`）。"""
    month_series = _av_series(start_ms, month_values)
    alltime_series = _av_series(start_ms, alltime_values)
    return [
        ["month", {"accountValueHistory": month_series,
                       "pnlHistory": _pnl_from_av(month_series), "vlm": "0"}],
        ["allTime", {"accountValueHistory": alltime_series,
                         "pnlHistory": _pnl_from_av(alltime_series), "vlm": "0"}],
    ]


def _ch_state(account_value="50000", positions=None):
    return {"marginSummary": {"accountValue": account_value},
           "assetPositions": positions or []}


def _position(coin, szi, leverage="10", margin_used="9000"):
    return {"position": {"coin": coin, "szi": szi, "entryPx": "100",
                         "unrealizedPnl": "0", "marginUsed": margin_used,
                         "leverage": {"type": "cross", "value": leverage}}}


def _raw_fill(oid, dir_, start_position, sz, closed_pnl, coin="ETH", px="100", time=1):
    """原始 HL `userFillsByTime` 形狀（含 dir/oid/startPosition/closedPnl，
    `trader_stats.fills_stats` 需要這些欄位，2026-09-05 Task 3 起 fills 一律
    走這個形狀，不再是 `hl.get_fills_detail()` 的裁切版）。"""
    return {"coin": coin, "oid": oid, "dir": dir_, "startPosition": str(start_position),
           "sz": str(sz), "px": str(px), "closedPnl": str(closed_pnl), "time": time}


def _sample_fills():
    """`tests/fixtures/hl_user_fills_sample.json` 已是原始 HL 形狀（含 dir/oid/
    startPosition/closedPnl），2026-09-05 起不需再轉換——直接餵給
    `trader_stats.fills_stats`。兩筆皆 `dir="Close Short"` 但都是部分平倉
    （`startPosition` 絕對值 != `sz`），生命週期未歸零 → `closed_positions=0`
    （Hyperbot 生命週期定義，見 D4；與舊版「closedPnl != 0 就算一次結倉」不同）。"""
    return json.loads((_FIXTURES / "hl_user_fills_sample.json").read_text())


def _row(**over):
    """`qualify`/`sort_key` 測試用的最小 `ExploreRow`（預設全部剛好卡在門檻上，
    呼叫端逐一覆寫想測的欄位）。R4-3：`windows` 改 dict 形狀——`pnl_usd`/
    `dd_pct` 是便利參數，套到 `"month"`／`"allTime"` 兩窗（`enrich_candidate`
    保證這兩鍵恆非 None，這裡的假資料維持同一個不變量）；要測特定窗（day/week）
    或某窗缺席（None）時直接傳 `windows=` 整包覆寫。"""
    pnl_usd = over.pop("pnl_usd", 1000.0)
    dd_pct = over.pop("dd_pct", -5.0)
    windows = over.pop("windows", None)
    if windows is None:
        stats = WindowStats(pnl_usd=pnl_usd, max_dd_pct=dd_pct, max_dd_reason=None, spark=())
        windows = {"day": None, "week": None, "month": stats, "allTime": stats}
    if "win_rate" in over:
        # Task 11：`sort_rows` 測試用 `win_rate=` 當 `close_win_rate_pct` 的簡寫
        # （欄位真名較長，且與 `sort` 查詢參數同名容易誤讀）。
        over["close_win_rate_pct"] = over.pop("win_rate")
    # P6（D13，2026-09-20）：`classify()` 只在 `fills_coverage.state == "complete"`
    # 時才對 `order_count_30d`/`concentration_pct` 做「已知不合格」判定——這批
    # `qualify`/`sort_key`/`sort_rows` 邊界測試是在測「單一維度剛好卡在門檻上」
    # 這件事本身，資料本來就是完整的（不是探索 P6 的成交完整性語意），預設給
    # `"complete"` 才不會讓每一列都因為「成交未知」掉進 `pending`（見
    # `hl_explore.classify` docstring；覆寫方式同其他欄位，傳 `fills_coverage=`）。
    fills_coverage = over.pop("fills_coverage", None) or {
        "state": "complete", "observed_from": 0, "observed_to": 1, "reason": None}
    base = dict(address=_A, display_name=None, label="0xaaaa…aaaa", coins=(),
               account_bucket="<$10K", windows=windows,
               live_days=60, order_count_30d=200, closed_positions_30d=10,
               realized_pnl_30d_usd=0.0,
               close_win_rate_pct=50.0, concentration_pct=10.0,
               exposure_dir=None, exposure_pct=None, tags=(), fills_truncated=False,
               fills_coverage=fills_coverage)
    base.update(over)
    return ExploreRow(**base)


# ============================================================
# 純函式：fills_truncated 透傳（`fills_stats` 本身的定義測試在
# tests/test_trader_stats.py；本檔只驗證 `enrich_candidate` 正確透傳分頁層
# 傳入的 `fills_truncated` 旗標，2026-09-04 Task 3 改用 trader_stats.fills_stats）
# ============================================================

def test_enrich_candidate_propagates_fills_truncated_flag_end_to_end():
    """分頁滿頁（`hl.get_fills_raw_paged` 回 `truncated=True`）→ `ExploreRow.
    fills_truncated=True`；`order_count_30d` 只是已抓到樣本的下限值，
    `qualify` 的 `>=` 比較方向不受影響（見 hl_explore.py `enrich_candidate`
    檔頭）。"""
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state(), fills_truncated=True)
    assert row is not None
    assert row.fills_truncated is True


# ============================================================
# 純函式：enrich_candidate（含共用 fills fixture）
# ============================================================

def test_enrich_candidate_computes_pnl_drawdown_live_days_and_fills_stats():
    portfolio_raw = _portfolio_raw(
        month_values=[1000, 900, 1100],       # 首900跌至900（-10%）後回升至1100（+10%）
        alltime_values=[1000] * 65,             # 65 個逐日點（1 天一點，跨 64 天）
    )
    fills = _sample_fills()                     # 2 筆部分平倉（原始 HL 形狀）
    ch_state = _ch_state(account_value="50000", positions=[_position("BTC", "1.5")])

    row = enrich_candidate(_A, "Alice", portfolio_raw, fills, ch_state)

    assert row is not None
    assert row.address == _A
    assert row.display_name == "Alice"
    assert row.label == "Alice"
    assert row.windows["month"].pnl_usd == 100.0         # pnlHistory 末值(100)-首值(0)
    assert row.windows["month"].max_dd_pct == -10.0      # 權益指數 MDD（無現金流時＝AV running-peak）
    assert row.live_days == 64                          # W1：首末點日曆跨距（65 點、
                                                          # 逐日一點 → 首末相差 64 天）
    assert row.order_count_30d == 2                      # distinct oid（D3，Hyperbot 定義）
    assert row.close_win_rate_pct is None                 # 2 筆皆部分平倉，未歸零 → 無生命週期樣本
    assert row.realized_pnl_30d_usd == 356.28             # Σ closedPnl（217.356772+138.92464）
    assert row.account_bucket == "$10K–$100K"
    assert row.exposure_dir == "long"
    assert len(row.windows["month"].spark) == 3
    assert row.windows["allTime"] is not None            # R4-3：allTime 窗也一併算好
    assert row.windows["day"] is None                    # R4-3：_portfolio_raw 只填 month/allTime
    assert row.windows["week"] is None
    assert row.fills_truncated is False


def test_enrich_candidate_label_falls_back_to_abbreviated_address_when_no_display_name():
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state())
    assert row is not None
    assert row.display_name is None
    assert row.label == f"{_A[:6]}…{_A[-4:]}"


def test_enrich_candidate_skipped_when_perp_month_missing():
    """讀不到（month 視窗缺席）→ 跳過該列，不進榜、不編數字。"""
    portfolio_raw = [["allTime", {"accountValueHistory": _av_series(0, [1000] * 60),
                                     "pnlHistory": []}]]
    assert enrich_candidate(_A, None, portfolio_raw, [], _ch_state()) is None


def test_enrich_candidate_skipped_when_perp_all_time_missing():
    portfolio_raw = [["month", {"accountValueHistory": _av_series(0, [1000, 1100]),
                                    "pnlHistory": []}]]
    assert enrich_candidate(_A, None, portfolio_raw, [], _ch_state()) is None


def test_enrich_candidate_short_exposure_when_short_dominant():
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    ch_state = _ch_state(positions=[_position("BTC", "-2.0", leverage="5", margin_used="4000")])
    row = enrich_candidate(_A, None, portfolio_raw, [], ch_state)
    assert row is not None
    assert row.exposure_dir == "short"
    assert row.exposure_pct == 100.0


# ============================================================
# 純函式：enrich_candidate — R4-3 四窗（day/week/month/allTime 單次 portfolio()
# 回應一次抽出，不多打上游）
# ============================================================

def test_enrich_candidate_computes_all_four_windows_from_single_portfolio_response():
    """R4-3：`portfolio()` 單次回應本就含四窗——`enrich_candidate` 一次讀出
    day/week/month/allTime 各自獨立的 pnl_usd/dd（同源同基準，各窗各自的序列）。
    pnlHistory 與 accountValueHistory 同步（見 `_pnl_from_av`）。"""
    day_series = _av_series(0, [1000, 1010], step_ms=3_600_000)
    week_series = _av_series(0, [1000, 950, 1050])
    month_series = _av_series(0, [1000, 900, 1100])
    alltime_series = _av_series(0, [1000] * 65)
    portfolio_raw = [
        ["day", {"accountValueHistory": day_series,
                     "pnlHistory": _pnl_from_av(day_series), "vlm": "0"}],
        ["week", {"accountValueHistory": week_series,
                     "pnlHistory": _pnl_from_av(week_series), "vlm": "0"}],
        ["month", {"accountValueHistory": month_series,
                      "pnlHistory": _pnl_from_av(month_series), "vlm": "0"}],
        ["allTime", {"accountValueHistory": alltime_series,
                         "pnlHistory": _pnl_from_av(alltime_series), "vlm": "0"}],
    ]
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state())
    assert row is not None
    assert set(row.windows.keys()) == {"day", "week", "month", "allTime"}
    assert row.windows["day"].pnl_usd == 10.0             # 1010-1000
    assert row.windows["week"].pnl_usd == 50.0            # 1050-1000
    assert row.windows["week"].max_dd_pct == -5.0         # 950/1000 - 1
    assert row.windows["month"].pnl_usd == 100.0          # 1100-1000
    assert row.windows["allTime"].pnl_usd == 0.0          # 全序列恆為 1000


def test_enrich_candidate_day_week_missing_stores_none_not_fabricated():
    """R4-3：day／week 是 best-effort——`_portfolio_raw` 只填 month／allTime，
    缺席的兩窗各自存 `None`，不得借別的窗數字冒充（不編數字）。"""
    portfolio_raw = _portfolio_raw([1000, 1100], [1000] * 60)
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state())
    assert row is not None
    assert row.windows["day"] is None
    assert row.windows["week"] is None
    assert row.windows["month"] is not None
    assert row.windows["allTime"] is not None


def test_enrich_candidate_day_week_invalid_series_stores_none_not_skip_whole_row():
    """R4-3：day／week 序列本身無效（pnlHistory 空、不足兩點）只讓那一鍵是
    `None`——不像 month／allTime 那樣連坐整列（gating 只在 month／allTime，
    見模組檔頭）。"""
    portfolio_raw = _portfolio_raw([1000, 1100], [1000] * 60)
    portfolio_raw = [["day", {"accountValueHistory": _av_series(0, [0, 1000]),
                                  "pnlHistory": [], "vlm": "0"}], *portfolio_raw]
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state())
    assert row is not None
    assert row.windows["day"] is None


# ============================================================
# 純函式：qualify — 資格過濾邊界（等號行為釘死）
# ============================================================

def test_qualify_live_days_exactly_at_threshold_passes():
    cfg = ExploreConfig(min_trading_days=60, min_fills=0)
    assert qualify(_row(live_days=60, order_count_30d=0), cfg) is True


def test_qualify_live_days_one_below_threshold_fails():
    cfg = ExploreConfig(min_trading_days=60, min_fills=0)
    assert qualify(_row(live_days=59, order_count_30d=0), cfg) is False


def test_qualify_fill_count_exactly_at_threshold_passes():
    cfg = ExploreConfig(min_trading_days=0, min_fills=200)
    assert qualify(_row(live_days=0, order_count_30d=200), cfg) is True


def test_qualify_fill_count_one_below_threshold_fails():
    cfg = ExploreConfig(min_trading_days=0, min_fills=200)
    assert qualify(_row(live_days=0, order_count_30d=199), cfg) is False


def test_qualify_drawdown_exactly_at_cap_passes():
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(_row(dd_pct=-30.0), cfg) is True


def test_qualify_drawdown_just_over_cap_fails():
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(_row(dd_pct=-30.01), cfg) is False


def test_qualify_concentration_exactly_at_cap_passes():
    cfg = ExploreConfig(max_concentration_pct=Decimal("90"))
    assert qualify(_row(concentration_pct=90.0), cfg) is True


def test_qualify_concentration_just_over_cap_fails():
    cfg = ExploreConfig(max_concentration_pct=Decimal("90"))
    assert qualify(_row(concentration_pct=90.01), cfg) is False


def test_qualify_concentration_none_passes_no_evidence_no_penalty():
    cfg = ExploreConfig(max_concentration_pct=Decimal("90"))
    assert qualify(_row(concentration_pct=None), cfg) is True


def test_qualify_chips_can_be_toggled_off_independently():
    cfg = ExploreConfig(min_trading_days=60, min_fills=200,
                        max_drawdown_pct=Decimal("30"), max_concentration_pct=Decimal("90"))
    bad_row = _row(live_days=1, order_count_30d=1, dd_pct=-99.0,
                   concentration_pct=99.0)
    assert qualify(bad_row, cfg, require_sample=False, max_dd_filter=False,
                  exclude_concentrated=False) is True
    assert qualify(bad_row, cfg) is False


# ============================================================
# 純函式：qualify — R4-3 window 參數（回撤過濾看所選窗，不是永遠 month）
# ============================================================

def test_qualify_max_dd_uses_selected_window_not_always_month():
    windows = {
        "day": WindowStats(pnl_usd=1.0, max_dd_pct=-50.0, max_dd_reason=None, spark=()),
        "week": None,
        "month": WindowStats(pnl_usd=10.0, max_dd_pct=-5.0, max_dd_reason=None, spark=()),
        "allTime": WindowStats(pnl_usd=20.0, max_dd_pct=-5.0, max_dd_reason=None, spark=()),
    }
    row = _row(windows=windows)
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(row, cfg, window="month") is True    # month dd=-5 <= 30
    assert qualify(row, cfg, window="day") is False      # day dd=-50 > 30


def test_qualify_missing_window_stats_passes_max_dd_no_evidence_no_penalty():
    stats = WindowStats(10.0, -5.0, None, ())
    windows = {"day": None, "week": None, "month": stats, "allTime": stats}
    row = _row(windows=windows)
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(row, cfg, window="week") is True


def test_qualify_max_dd_none_within_present_window_passes_no_evidence_no_penalty():
    """D1／2026-09-04：該窗存在但 `max_dd_pct is None`（perf 非 ok，例如
    `flow_dominated_interval`）→ 視為無證據、通過（同「窗缺席」既有慣例）。"""
    windows = {"day": None, "week": None,
              "month": WindowStats(pnl_usd=100.0, max_dd_pct=None,
                                   max_dd_reason="flow_dominated_interval", spark=()),
              "allTime": WindowStats(pnl_usd=100.0, max_dd_pct=-5.0, max_dd_reason=None, spark=())}
    row = _row(windows=windows)
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(row, cfg, window="month") is True


# ============================================================
# 純函式：classify（P6／D13，2026-09-20：三態資格，取代 qualify 的布林）
# Task 6.1 第 8 點列舉的情境，逐一釘死。
# ============================================================

_PARTIAL_COVERAGE = {"state": "backfilling", "observed_from": None,
                     "observed_to": None, "reason": None}
_COMPLETE_COVERAGE = {"state": "complete", "observed_from": 1, "observed_to": 2, "reason": None}


def test_classify_known_ineligible_overrides_unknown_fills():
    """已知條件（live_days）確定不合格＋成交未知（partial coverage）→
    `ineligible`（已知的不合格優先於「未定」，不會被沖淡成 pending）。"""
    row = _row(live_days=10, fills_coverage=_PARTIAL_COVERAGE)
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=90.0)
    assert result == ("ineligible", "live_days")


def test_classify_all_known_pass_but_fills_unknown_is_pending():
    """已知條件全部通過（live_days／dd）＋成交未知（partial coverage、集中度
    門檻仍生效 <100）→ `pending`／`fills_unknown`（不是 eligible，也不是
    ineligible——`qualify(None)` 不得回傳合格，D13）。"""
    row = _row(live_days=60, dd_pct=-5.0, order_count_30d=500,
              fills_coverage=_PARTIAL_COVERAGE)
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=90.0)
    assert result == ("pending", "fills_unknown")


def test_classify_complete_coverage_min_fills_insufficient_is_ineligible():
    """`fills_coverage.state == "complete"` 時才有證據判 `min_fills`——不足即
    `ineligible/min_fills`（不是 pending，因為資料已經齊全、確定不足）。"""
    row = _row(live_days=60, order_count_30d=50, fills_coverage=_COMPLETE_COVERAGE)
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=90.0)
    assert result == ("ineligible", "min_fills")


def test_classify_partial_coverage_order_count_sufficient_but_concentration_filter_active_is_pending():
    """partial coverage 下即使 `order_count_30d >= min_fills`（該條已滿足），
    集中度門檻仍生效（`max_concentration_pct < 100`）時集中度本身「未定」
    （`enrich_candidate` 已把 `concentration_pct` 遮成 None）→ 整體仍是
    `pending`，不是 `eligible`。"""
    row = _row(live_days=60, order_count_30d=500, concentration_pct=None,
              fills_coverage=_PARTIAL_COVERAGE)
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=90.0)
    assert result == ("pending", "fills_unknown")


def test_classify_partial_coverage_no_concentration_filter_is_eligible():
    """同上，但 `max_concentration_pct=100`（等於不過濾，見 `classify`
    docstring）——集中度維度視為已滿足，其餘皆已知通過 → `eligible`。"""
    row = _row(live_days=60, order_count_30d=500, concentration_pct=None,
              fills_coverage=_PARTIAL_COVERAGE)
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=100.0)
    assert result == ("eligible", None)


def test_classify_portfolio_missing_is_pending():
    """`live_days is None`（`portfolio_raw` 缺席）→ `pending`／
    `portfolio_missing`（不是 ineligible、也不是 eligible）。"""
    row = _row(live_days=None, windows={k: None for k in ("day", "week", "month", "allTime")})
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=90.0)
    assert result == ("pending", "portfolio_missing")


def test_classify_all_known_and_complete_coverage_pass_is_eligible():
    """已知條件全部通過＋成交完整（`complete`）且未超門檻 → `eligible`。"""
    row = _row(live_days=60, dd_pct=-5.0, order_count_30d=500, concentration_pct=10.0,
              fills_coverage=_COMPLETE_COVERAGE)
    result = classify(row, ExploreConfig(), window="month",
                      min_live_days=30, min_fills=200,
                      max_dd_pct=30.0, max_concentration_pct=90.0)
    assert result == ("eligible", None)


# ============================================================
# 純函式：sort_key（風險調整排序鍵，D2）
# ============================================================

def test_sort_key_orders_by_selected_window_pnl_desc():
    high = _row(pnl_usd=5000.0)
    low = _row(pnl_usd=-200.0)
    assert sort_key(high) > sort_key(low)


def test_sort_key_falls_back_to_month_when_window_missing():
    row = _row(pnl_usd=123.0)
    row.windows["day"] = None
    assert sort_key(row, window="day") == sort_key(row, window="month")


# ============================================================
# 純函式：sort_rows（Task 11：後端 sort/order，D12／D13）
# ============================================================

def test_sort_rows_pnl_desc_default_and_asc():
    a, b, c = _row(pnl_usd=100.0), _row(pnl_usd=300.0), _row(pnl_usd=-50.0)
    assert [r.windows["month"].pnl_usd for r in sort_rows([a, b, c], window="month")] == [300.0, 100.0, -50.0]
    assert [r.windows["month"].pnl_usd for r in sort_rows([a, b, c], window="month", order="asc")] == [-50.0, 100.0, 300.0]


def test_sort_rows_max_dd_none_always_last_regardless_of_order():
    ok1, ok2, none = _row(dd_pct=-10.0), _row(dd_pct=-40.0), _row(dd_pct=None)
    desc = sort_rows([none, ok1, ok2], window="month", sort="max_dd", order="desc")
    asc = sort_rows([none, ok1, ok2], window="month", sort="max_dd", order="asc")
    assert [r.windows["month"].max_dd_pct for r in desc] == [-10.0, -40.0, None]   # 回撤小者在前
    assert [r.windows["month"].max_dd_pct for r in asc] == [-40.0, -10.0, None]


def test_sort_rows_live_days_and_win_rate():
    a, b = _row(live_days=10, win_rate=None), _row(live_days=500, win_rate=55.5)
    assert sort_rows([a, b], window="month", sort="live_days")[0] is b
    assert sort_rows([a, b], window="month", sort="win_rate")[0] is b       # None 排最後
    assert sort_rows([a, b], window="month", sort="win_rate", order="asc")[0] is b


def test_sort_rows_missing_window_falls_back_to_month():
    a = _row(pnl_usd=5.0)
    a.windows["day"] = None
    b = _row(pnl_usd=1.0)
    b.windows["day"] = WindowStats(pnl_usd=999.0, max_dd_pct=None, max_dd_reason="x", spark=())
    assert sort_rows([a, b], window="day")[0] is b


def test_sort_fields_constant():
    assert SORT_FIELDS == ("pnl", "max_dd", "live_days", "win_rate")


# ============================================================
# 純函式：sort_rows — P6 契約 B 擴充：eligibility 分組＋次排序鍵 address 升冪
# ============================================================

def test_sort_rows_groups_eligible_before_pending_ineligible_excluded():
    """`eligible` 全部在前、`pending` 全部在後、`ineligible` 整批不列——即使
    `ineligible` 列的 `pnl_usd` 排序值本來會排最前面。"""
    hi_ineligible = _row(address="0x" + "a1" * 20, pnl_usd=9999.0, eligibility="ineligible")
    lo_eligible = _row(address="0x" + "b2" * 20, pnl_usd=1.0, eligibility="eligible")
    hi_pending = _row(address="0x" + "c3" * 20, pnl_usd=500.0, eligibility="pending")
    result = sort_rows([hi_ineligible, lo_eligible, hi_pending], window="month")
    assert [r.address for r in result] == [lo_eligible.address, hi_pending.address]


def test_sort_rows_secondary_key_is_address_ascending_stable_within_group():
    """P6 契約 B：組內排序次鍵固定 `address` 升冪、與 `order` 無關——同一
    `sort` 值的多列在 `desc`／`asc` 兩種排序方向下，address 順序都要一致
    （升冪），不隨主排序方向翻轉。"""
    addr_lo, addr_hi = "0x" + "01" * 20, "0x" + "02" * 20
    r_hi_addr = _row(address=addr_hi, pnl_usd=100.0, eligibility="eligible")
    r_lo_addr = _row(address=addr_lo, pnl_usd=100.0, eligibility="eligible")   # 同一 pnl 值
    desc = sort_rows([r_hi_addr, r_lo_addr], window="month", sort="pnl", order="desc")
    asc = sort_rows([r_hi_addr, r_lo_addr], window="month", sort="pnl", order="asc")
    assert [r.address for r in desc] == [addr_lo, addr_hi]
    assert [r.address for r in asc] == [addr_lo, addr_hi]


def test_sort_rows_missing_sort_value_tiebreak_by_address_within_missing_group():
    """`sort_value` 為 `None` 的列（例如 `max_dd_pct` 算不出）彼此之間也用
    `address` 升冪排序，不是輸入順序。"""
    addr_lo, addr_hi = "0x" + "01" * 20, "0x" + "02" * 20
    r_hi_addr = _row(address=addr_hi, dd_pct=None, eligibility="eligible")
    r_lo_addr = _row(address=addr_lo, dd_pct=None, eligibility="eligible")
    result = sort_rows([r_hi_addr, r_lo_addr], window="month", sort="max_dd")
    assert [r.address for r in result] == [addr_lo, addr_hi]


# ============================================================
# 純函式：paginate
# ============================================================

def test_paginate_first_page():
    rows = [_row(address=f"0x{i:040x}") for i in range(5)]
    assert paginate(rows, page=1, page_size=2) == rows[0:2]


def test_paginate_second_page():
    rows = [_row(address=f"0x{i:040x}") for i in range(5)]
    assert paginate(rows, page=2, page_size=2) == rows[2:4]


def test_paginate_page_beyond_range_is_empty():
    rows = [_row(address=f"0x{i:040x}") for i in range(3)]
    assert paginate(rows, page=99, page_size=25) == []


def test_paginate_non_positive_page_or_size_is_empty():
    rows = [_row()]
    assert paginate(rows, page=0, page_size=25) == []
    assert paginate(rows, page=1, page_size=0) == []


# ============================================================
# 純函式：candidate_addresses（roi 降冪 ＋ D8 排除 Filet 自營）
# ============================================================

def test_candidate_addresses_sorts_by_roi_descending():
    payload = _leaderboard_payload(_lb_row(_A, roi="0.05"), _lb_row(_B, roi="0.50"))
    out = candidate_addresses(payload, pool_size=10, excluded=set())
    assert [a for a, _ in out] == [_B, _A]


def test_candidate_addresses_respects_pool_size():
    payload = _leaderboard_payload(*[_lb_row(f"0x{i:040x}", roi=str(i)) for i in range(10)])
    out = candidate_addresses(payload, pool_size=3, excluded=set())
    assert len(out) == 3


def test_candidate_addresses_excludes_filet_own_leaders():
    """D8：探索榜不含 Filet 自營 leader（精選白名單地址集合）。"""
    payload = _leaderboard_payload(_lb_row(_A, roi="0.90"), _lb_row(_FILET_OWN, roi="0.99"))
    out = candidate_addresses(payload, pool_size=10, excluded={_FILET_OWN.lower()})
    assert [a for a, _ in out] == [_A]


# ============================================================
# 純函式：clamp_explore_params（R4-3：伺服器夾取範圍，防濫用不是驗證錯誤）
# ============================================================

def test_clamp_explore_params_within_range_is_unchanged():
    assert clamp_explore_params(min_live_days=30, min_fills=200,
                                max_dd_pct=30.0, max_concentration_pct=90.0) \
        == (30, 200, 30.0, 90.0)


def test_clamp_explore_params_clamps_values_below_lower_bound():
    assert clamp_explore_params(min_live_days=-5, min_fills=-1,
                                max_dd_pct=0.0, max_concentration_pct=0.0) \
        == (0, 0, 1.0, 1.0)


def test_clamp_explore_params_clamps_values_above_upper_bound():
    assert clamp_explore_params(min_live_days=9999, min_fills=999_999,
                                max_dd_pct=500.0, max_concentration_pct=500.0) \
        == (365, 100_000, 100.0, 100.0)


def test_clamp_explore_params_boundary_values_pass_through_unchanged():
    """邊界值本身合法（含），不被夾成別的數字。"""
    assert clamp_explore_params(min_live_days=0, min_fills=0,
                                max_dd_pct=1.0, max_concentration_pct=1.0) \
        == (0, 0, 1.0, 1.0)
    assert clamp_explore_params(min_live_days=365, min_fills=100_000,
                                max_dd_pct=100.0, max_concentration_pct=100.0) \
        == (365, 100_000, 100.0, 100.0)


# ============================================================
# ExploreIndex：building 態、fail-open、分頁（endpoint 前的直接測試）
# ============================================================

def _seed_hl(hl: FakeHL, address: str, *, roi_ret_pct=("1000", "1100"),
            alltime_days=60):
    hl.portfolios[address.lower()] = _portfolio_raw(
        [int(v) for v in roi_ret_pct], [1000] * alltime_days)
    hl.fills_raw[address.lower()] = []
    hl.clearinghouse[address.lower()] = _ch_state()


def _built_rows(payload, hl, *, excluded=frozenset(), pool_size=1000, cfg=None):
    """Task 3.4（D6）：`ExploreIndex.build_sync` 已刪除——候選池選取、逐地址
    enrich、429／額度節流全部移交 `ExploreScheduler`／`ExplorePublisher`（見
    `tests/test_explore_scheduler.py`／`tests/test_explore_publisher.py`）。
    這裡用同一套純函式串接（`candidate_addresses` → `enrich_candidate` →
    `_apply_tags`）重現舊版 `build_sync` 產生的 `(rows, total_scanned)`，
    純粹是為了不必為每個既有的 `ExploreIndex.query()` 測試各自重寫資料組裝，
    不代表這是正式的資料流（正式資料流見 `explore_scheduler.py`／
    `explore_publisher.py`）。"""
    candidates = candidate_addresses(payload, pool_size, {a.lower() for a in excluded})
    rows = []
    for address, display_name in candidates:
        portfolio_raw = hl.portfolio(address)
        fills, truncated = hl.get_fills_raw_paged(address, None, None)
        ch_state = hl.clearinghouse_state(address)
        row = enrich_candidate(address, display_name, portfolio_raw, fills, ch_state,
                               fills_truncated=truncated)
        if row is not None:
            rows.append(row)
    rows = hl_explore._apply_tags(rows, cfg or ExploreConfig())
    return rows, len(candidates)


def _publish(index: ExploreIndex, rows, total_scanned: int, *, now: float = 1000.0) -> None:
    index.set_published(list(rows), {"published_at": now, "candidates": total_scanned})


def test_index_query_never_built_returns_building_true_and_empty_rows_without_blocking():
    """尚無任何可用版本 → `building`／`initializing: True` ＋空 rows，讀路徑
    不阻塞（Task 3.4：`ExploreIndex` 不再接受 `hl`／`leaderboard_source_fn`／
    `excluded_fn`，也不再自己觸發任何建置——「不阻塞、不觸發背景建置」這條
    既有語意現在是結構性保證，不必再用假時鐘／背景 thread 驗證）。"""
    index = ExploreIndex(cfg=ExploreConfig(), now_fn=lambda: 1000.0)

    start = time.monotonic()
    result = index.query()
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"query() 耗時過長（{elapsed}s）"
    assert result.items() >= {"rows": [], "page": 1, "page_size": ExploreConfig().page_size,
                              "total_qualified": 0, "total_scanned": 0, "pool": 0,
                              "updated_at": None, "building": True,
                              "initializing": True, "published_at": None,
                              "coverage_counts": {}}.items()

    index.set_published([], {"published_at": 1000.0, "candidates": 0})
    built = index.query()
    assert built["building"] is False


def test_index_pagination_across_pages():
    rows_payload = _leaderboard_payload(
        *[_lb_row(f"0x{i:040x}", roi=str(i)) for i in range(60)])
    hl = FakeHL()
    for i in range(60):
        _seed_hl(hl, f"0x{i:040x}")
    cfg = ExploreConfig(page_size=25, min_trading_days=0, min_fills=0)
    index = ExploreIndex(cfg=cfg, now_fn=lambda: 1000.0)
    rows, total_scanned = _built_rows(rows_payload, hl, cfg=cfg)
    _publish(index, rows, total_scanned)
    page1 = index.query(page=1)
    page2 = index.query(page=2)
    page3 = index.query(page=3)
    assert len(page1["rows"]) == 25
    assert len(page2["rows"]) == 25
    assert len(page3["rows"]) == 10
    assert page1["total_qualified"] == 60
    assert page1["total_scanned"] == 60
    all_addrs = ({r["address"] for r in page1["rows"]}
                | {r["address"] for r in page2["rows"]}
                | {r["address"] for r in page3["rows"]})
    assert len(all_addrs) == 60  # 三頁不重疊、無遺漏


def test_index_query_window_selects_ranking_and_response_row_content():
    """R4-3 端到端：`window` 參數改變回傳列的排序（`sort_key` 依所選窗 `pnl_usd`
    降冪，D2）。地址 A 是 day 窗強、month 窗弱；地址 B 相反——window 切換應讓
    排名對調。pnlHistory 與 accountValueHistory 同步（見 `_pnl_from_av`）。"""
    hl = FakeHL()
    a_day = _av_series(0, [1000, 1100], step_ms=3_600_000)
    a_week = _av_series(0, [1000, 1000])
    a_month = _av_series(0, [1000, 1010])
    a_alltime = _av_series(0, [1000] * 60)
    hl.portfolios[_A.lower()] = [
        ["day", {"accountValueHistory": a_day, "pnlHistory": _pnl_from_av(a_day), "vlm": "0"}],
        ["week", {"accountValueHistory": a_week, "pnlHistory": _pnl_from_av(a_week), "vlm": "0"}],
        ["month", {"accountValueHistory": a_month, "pnlHistory": _pnl_from_av(a_month), "vlm": "0"}],
        ["allTime", {"accountValueHistory": a_alltime,
                         "pnlHistory": _pnl_from_av(a_alltime), "vlm": "0"}],
    ]
    hl.fills_raw[_A.lower()] = []
    hl.clearinghouse[_A.lower()] = _ch_state()
    b_day = _av_series(0, [1000, 1000])
    b_week = _av_series(0, [1000, 1000])
    b_month = _av_series(0, [1000, 1200])
    b_alltime = _av_series(0, [1000] * 60)
    hl.portfolios[_B.lower()] = [
        ["day", {"accountValueHistory": b_day, "pnlHistory": _pnl_from_av(b_day), "vlm": "0"}],
        ["week", {"accountValueHistory": b_week, "pnlHistory": _pnl_from_av(b_week), "vlm": "0"}],
        ["month", {"accountValueHistory": b_month, "pnlHistory": _pnl_from_av(b_month), "vlm": "0"}],
        ["allTime", {"accountValueHistory": b_alltime,
                         "pnlHistory": _pnl_from_av(b_alltime), "vlm": "0"}],
    ]
    hl.fills_raw[_B.lower()] = []
    hl.clearinghouse[_B.lower()] = _ch_state()
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"), _lb_row(_B, roi="0.4"))
    cfg = ExploreConfig(min_trading_days=0, min_fills=0)
    index = ExploreIndex(cfg=cfg, now_fn=lambda: 1000.0)
    rows, total_scanned = _built_rows(payload, hl, cfg=cfg)
    _publish(index, rows, total_scanned)

    by_day = index.query(window="day")
    assert [r["address"] for r in by_day["rows"]] == [_A, _B]   # A 的 day pnl 較高（100 vs 0）

    by_month = index.query(window="month")
    assert [r["address"] for r in by_month["rows"]] == [_B, _A]  # B 的 month pnl 較高（200 vs 10）

    row = by_month["rows"][0]
    assert set(row["windows"]["month"]) == {"pnl_usd", "max_dd_pct", "max_dd_reason", "spark"}
    assert {"order_count_30d", "closed_positions_30d", "realized_pnl_30d_usd"} <= set(row)


# ============================================================
# ExploreIndex：P6（D13）eligibility 參數——"eligible" 不含 pending
# ============================================================

def test_index_query_eligibility_eligible_excludes_pending_rows():
    """`eligibility="eligible"` → rows 只含 eligible；`"all"`（預設）含
    eligible＋pending；`total_pending`／`total_ineligible` 分開計數。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=60)               # 完整資料、通過門檻 → eligible
    cfg = ExploreConfig(min_trading_days=0, min_fills=0)
    index = ExploreIndex(cfg=cfg, now_fn=lambda: 1000.0)
    # 直接構造兩列而不經 `_built_rows`／候選池，更清楚地控制 B 的「分析待完成」
    # 狀態（B 從未被 enrich 過，`portfolio_raw`／`ch_state` 皆 `None`）。
    row_a = enrich_candidate(_A, "Alice", hl.portfolio(_A), [], hl.clearinghouse_state(_A))
    row_b = enrich_candidate(_B, "Bob", None, [], None)
    rows = hl_explore._apply_tags([row_a, row_b], cfg)
    _publish(index, rows, 2)

    all_result = index.query(eligibility="all")
    assert {r["address"] for r in all_result["rows"]} == {_A, _B}
    assert all_result["total_qualified"] == 1
    assert all_result["total_pending"] == 1
    assert all_result["total_ineligible"] == 0
    assert all_result["eligibility"] == "all"

    eligible_only = index.query(eligibility="eligible")
    assert [r["address"] for r in eligible_only["rows"]] == [_A]
    assert eligible_only["eligibility"] == "eligible"


# ============================================================
# ExploreIndex：R4-3 index 結構版本——不相容快照視同未發布，等待重新換版
# ============================================================

def test_index_version_mismatch_forces_rebuild_even_within_ttl():
    """把記憶體內快照的版本標記竄改成舊版後，`query()` 也必須回
    `building: True`（不得把不相容形狀的舊列序列化給前端）。"""
    hl = FakeHL()
    _seed_hl(hl, _A)
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"))
    cfg = ExploreConfig(min_trading_days=0, min_fills=0)
    index = ExploreIndex(cfg=cfg, now_fn=lambda: 1000.0)
    rows, total_scanned = _built_rows(payload, hl, cfg=cfg)
    _publish(index, rows, total_scanned)
    first = index.query()
    assert first["building"] is False
    assert len(first["rows"]) == 1

    # 模擬「上一版程式碼建置出的舊形狀快照」殘留在記憶體（結構性測試：直接
    # 竄改內部版本標記，不必真的構造一份舊 dataclass shape）。
    index._rows_version = hl_explore.EXPLORE_INDEX_VERSION - 1

    stale = index.query()
    assert stale["building"] is True
    assert stale["rows"] == []


def test_index_starts_with_no_rows_version_before_first_build():
    index = ExploreIndex(cfg=ExploreConfig(), now_fn=lambda: 1000.0)
    assert index._rows_version is None


def test_snapshot_version_bumped_to_4():
    """D7（2026-09-04）3；Task 4.1（2026-09-20）3→4：`ExploreRow` 新增
    `as_of`／`fills_coverage` 兩欄（漸進發布，spec §9.2）——`load_snapshot`
    對 v3 快照有專門的相容遷移路徑（見 `test_snapshot_load_version_mismatch_
    returns_none` 旁的 v3 測試），本測試只釘住目前版號本身。"""
    assert hl_explore.EXPLORE_INDEX_VERSION == 4


# ============================================================
# I-17（2026-08-31 使用者裁決）：候選池 300 ＋ 常駐磁碟快照快取
# ============================================================

def test_default_candidate_pool_is_300():
    assert hl_explore.DEFAULT_CANDIDATE_POOL == 300


def test_snapshot_dump_and_load_round_trips_rows(tmp_path):
    """磁碟快照落檔/載入 round-trip：`ExploreRow`（含 `windows` dict、tags、
    coins 等 tuple 欄位）序列化再反序列化後內容不變。"""
    path = str(tmp_path / "explore_snapshot.json")
    row = _row(address=_A, coins=("BTC", "ETH"), tags=("low_drawdown",))
    hl_explore.dump_snapshot(path, rows=[row], built_at=1234.5, total_scanned=7)

    loaded = hl_explore.load_snapshot(path)

    assert loaded is not None
    assert loaded["built_at"] == 1234.5
    assert loaded["total_scanned"] == 7
    assert len(loaded["rows"]) == 1
    restored = loaded["rows"][0]
    assert restored.address == row.address
    assert restored.coins == row.coins
    assert restored.tags == row.tags
    assert restored.windows["month"].pnl_usd == row.windows["month"].pnl_usd
    assert restored.windows["month"].max_dd_pct == row.windows["month"].max_dd_pct
    assert restored.windows["month"].max_dd_reason == row.windows["month"].max_dd_reason
    assert restored.windows["day"] is None   # `_row()` 預設 day/week 缺席


def test_snapshot_load_missing_file_returns_none(tmp_path):
    assert hl_explore.load_snapshot(str(tmp_path / "nope.json")) is None


def test_snapshot_load_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "explore_snapshot.json"
    path.write_text("{not valid json")
    assert hl_explore.load_snapshot(str(path)) is None


def test_snapshot_load_version_mismatch_returns_none(tmp_path):
    """版本不符（例如上一版程式碼寫的舊形狀快照）→ 忽略，視同沒有可用快照
    （呼叫端走既有冷建語意）。Task 4.1：`EXPLORE_INDEX_VERSION - 1`（＝3）現在
    是有專門遷移路徑的相容版本（見 `test_explore_publisher.py` 的 v3 遷移
    測試），不再適合當「不相容」的例子——這裡改用真正沒有遷移路徑的舊版號。"""
    path = tmp_path / "explore_snapshot.json"
    path.write_text(json.dumps({"version": hl_explore.EXPLORE_INDEX_VERSION - 2,
                                "built_at": 1.0, "total_scanned": 0, "rows": []}))
    assert hl_explore.load_snapshot(str(path)) is None


def test_index_loads_snapshot_at_construction_and_is_immediately_queryable(tmp_path):
    """I-17：啟動時載入——版本相符 → 建構子跑完當下就能查，不必等
    `ExplorePublisher` 換上第一版。"""
    path = str(tmp_path / "explore_snapshot.json")
    row = _row(address=_A)
    hl_explore.dump_snapshot(path, rows=[row], built_at=1000.0, total_scanned=1)
    index = ExploreIndex(cfg=ExploreConfig(min_trading_days=0, min_fills=0),
                         now_fn=lambda: 1000.0, snapshot_path=path)

    result = index.query()

    assert result["building"] is False
    assert len(result["rows"]) == 1
    assert result["rows"][0]["address"] == _A


def test_index_snapshot_version_mismatch_on_disk_ignored_falls_back_to_cold_build(tmp_path):
    """Task 4.1：同上，`EXPLORE_INDEX_VERSION - 1`（3）已是相容版本，改用
    `- 2` 當真正不相容的例子。"""
    path = tmp_path / "explore_snapshot.json"
    path.write_text(json.dumps({"version": hl_explore.EXPLORE_INDEX_VERSION - 2,
                                "built_at": 1.0, "total_scanned": 0, "rows": []}))
    index = ExploreIndex(cfg=ExploreConfig(), now_fn=lambda: 1000.0,
                         snapshot_path=str(path))
    assert index._rows is None
    assert index._rows_version is None


def test_index_ttl_expired_serves_stale_snapshot_rows_not_empty_building_rows(tmp_path):
    """Task 3.4：`ExploreIndex` 不再有 TTL 概念（新鮮度完全交給
    `ExploreScheduler`／`ExplorePublisher` 的更新頻率）——磁碟快照無論建於
    多久之前，`query()` 都直接服務它，不會退化成「building:true＋空 rows」
    （那只允許出現在從未成功發布過**且**磁碟無可用快照的唯一情況）。"""
    path = str(tmp_path / "explore_snapshot.json")
    row = _row(address=_A)
    hl_explore.dump_snapshot(path, rows=[row], built_at=0.0, total_scanned=1)
    index = ExploreIndex(cfg=ExploreConfig(min_trading_days=0, min_fills=0),
                         now_fn=lambda: 100_000.0, snapshot_path=path)

    result = index.query()

    assert result["building"] is False
    assert len(result["rows"]) == 1
    assert result["updated_at"] == 0


def test_query_response_includes_pool_field_not_hardcoded():
    """回應需帶 `pool` 大小欄位（I-17：前端榜首常駐提示句用它，不寫死 300）。"""
    hl = FakeHL()
    for i in range(3):
        _seed_hl(hl, f"0x{i:040x}")
    payload = _leaderboard_payload(*[_lb_row(f"0x{i:040x}", roi=str(i)) for i in range(3)])
    cfg = ExploreConfig(page_size=25, min_trading_days=0, min_fills=0)
    index = ExploreIndex(cfg=cfg, now_fn=lambda: 1000.0)
    rows, total_scanned = _built_rows(payload, hl, cfg=cfg)
    _publish(index, rows, total_scanned)
    result = index.query()
    assert result["pool"] == 3 == result["total_scanned"]


# ============================================================
# 端點：GET /api/public/explore
# ============================================================

def _app(tmp_path, *, hl=None, leaderboard_get_fn=None, now_fn=None, leaders=None):
    cfg = make_cfg(tmp_path)
    if leaders is not None:
        Path(cfg.leaders_path).write_text(json.dumps({"leaders": leaders}))
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    kw = {} if now_fn is None else {"now_fn": now_fn}
    return create_app(cfg, store, keysvc, hl or FakeHL(),
                      leaderboard_get_fn=leaderboard_get_fn, **kw)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def test_endpoint_never_built_returns_building_true(tmp_path):
    def get_fn(url):
        return _leaderboard_payload()
    app = _app(tmp_path, leaderboard_get_fn=get_fn)
    r = _client(app).get("/api/public/explore")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["building"] is True
    assert body["rows"] == []


def test_endpoint_rejects_bad_window(tmp_path):
    app = _app(tmp_path, leaderboard_get_fn=lambda url: _leaderboard_payload())
    r = _client(app).get("/api/public/explore", params={"window": "7d"})
    assert r.status_code == 422


def test_endpoint_rejects_non_positive_page(tmp_path):
    app = _app(tmp_path, leaderboard_get_fn=lambda url: _leaderboard_payload())
    r = _client(app).get("/api/public/explore", params={"page": 0})
    assert r.status_code == 422


@pytest.mark.parametrize("param", ["min_live_days", "min_fills", "max_dd_pct",
                                   "max_concentration_pct"])
def test_endpoint_rejects_non_numeric_threshold_params(tmp_path, param):
    """R4-3：三個布林 chip（qualified/max_dd/exclude_concentrated）已從端點移除，
    改成四個自由數值——非數值輸入（型別驗證失敗）仍是 422；超出合法範圍的
    數值不是型別錯誤，是被 `clamp_explore_params` 夾取，見下面 clamp 測試。"""
    app = _app(tmp_path, leaderboard_get_fn=lambda url: _leaderboard_payload())
    r = _client(app).get("/api/public/explore", params={param: "not-a-number"})
    assert r.status_code == 422


def test_endpoint_out_of_range_thresholds_are_clamped_not_rejected(tmp_path):
    """R4-3：超出範圍的數值門檻不是驗證錯誤——伺服器直接夾回邊界內，回應仍
    200（`clamp_explore_params` 的邊界見模組常數）。"""
    app = _app(tmp_path, leaderboard_get_fn=lambda url: _leaderboard_payload())
    r = _client(app).get("/api/public/explore", params={
        "min_live_days": -5, "min_fills": 999_999,
        "max_dd_pct": 0, "max_concentration_pct": 500,
    })
    assert r.status_code == 200, r.text


def test_endpoint_no_auth_required_and_no_cookie_side_effect(tmp_path):
    app = _app(tmp_path, leaderboard_get_fn=lambda url: _leaderboard_payload())
    r = _client(app).get("/api/public/explore")
    assert r.status_code == 200
    assert r.cookies.get("filet_session") is None


def test_endpoint_full_flow_after_build_completes(tmp_path):
    """走完整條管線（enrich → 過濾 → 排序 → 分頁）一路到 HTTP 回應。Filet
    自營地址排除（D8）已下放到 `ExploreScheduler`（見 `app.py` 接線裡的
    `_explore_excluded_addresses`）——它不在 `ExploreIndex`／端點層做，
    純函式層的排除邏輯覆蓋見 `test_candidate_addresses_excludes_filet_own_
    leaders`，本測試不再重複驗證。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=65)
    payload = _leaderboard_payload(_lb_row(_A, display_name="Alice", roi="0.5"))
    app = _app(tmp_path)
    rows, total_scanned = _built_rows(payload, hl)
    _publish(app.state.explore_index, rows, total_scanned)
    client = _client(app)

    # R4-3：`qualified=0` chip 已移除——改送 min_live_days=0/min_fills=0
    # 停用樣本門檻（`_seed_hl` 的假地址 fills_raw 是空清單，預設
    # min_fills=200 門檻進不了榜）。
    r = client.get("/api/public/explore", params={"min_live_days": 0, "min_fills": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["building"] is False
    row = next(row for row in body["rows"] if row["address"] == _A)
    assert row["display_name"] == "Alice"
    assert row["label"] == "Alice"
    assert row["windows"]["month"]["pnl_usd"] == 100.0   # 1100-1000
    assert body["pool"] == body["total_scanned"] == 1  # I-17：pool 欄位來自後端，不寫死


# ============================================================
# 端點：GET /api/public/explore?sort=&order=（Task 11，D12／D13）
# ============================================================

def _many_perp_fills(n):
    """`n` 筆 distinct-oid 開倉單（不平倉），只用來墊高 `order_count_30d`
    好過 `min_fills` 預設門檻——`fills_stats` 的 order_count 只數 distinct
    `oid`，與是否平倉無關。兩幣輪流下單（非單一幣），讓 `concentration_pct`
    落在 50%（過 `qualify` 預設 90% 集中度上限；只用一種幣會頂到 100%）。"""
    coins = ("BTC", "ETH")
    return [_raw_fill(i, "Open Long", "0", "1", "0", coin=coins[i % 2], time=i)
           for i in range(n)]


@pytest.fixture
def client_after_build(tmp_path):
    """建置完成、含兩筆合格列（不同 `live_days`）的 client，供 sort/order
    端點測試共用（Task 11）。兩地址都墊 200 筆 fills 過 `min_fills` 預設門檻，
    `alltime_days` 不同讓 `live_days` 可排序區分。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=100)
    _seed_hl(hl, _B, alltime_days=50)
    hl.fills_raw[_A.lower()] = _many_perp_fills(200)
    hl.fills_raw[_B.lower()] = _many_perp_fills(200)
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"), _lb_row(_B, roi="0.4"))
    app = _app(tmp_path)
    rows, total_scanned = _built_rows(payload, hl)
    _publish(app.state.explore_index, rows, total_scanned)
    return _client(app)


def test_endpoint_rejects_bad_sort_and_order(client_after_build):
    assert client_after_build.get("/api/public/explore?sort=foo").status_code == 422
    assert client_after_build.get("/api/public/explore?order=sideways").status_code == 422


def test_endpoint_sort_order_passthrough_and_echo(client_after_build):
    body = client_after_build.get("/api/public/explore?sort=live_days&order=asc").json()
    assert body["sort"] == "live_days" and body["order"] == "asc"
    days = [r["live_days"] for r in body["rows"]]
    assert len(days) == 2               # 兩地址都應通過 min_fills=200/min_live_days=30 預設門檻
    assert days == sorted(days)


# ============================================================
# 端點：GET /api/public/explore?eligibility=（P6 契約 B）
# ============================================================

def test_endpoint_rejects_bad_eligibility_with_400_not_422(client_after_build):
    """P6 Task 6.1 第 6 點：`eligibility` 非法值 → 400（與 window/sort/order 等
    封閉列舉的 422 慣例刻意不同，plan 明確指定）。"""
    r = client_after_build.get("/api/public/explore?eligibility=bogus")
    assert r.status_code == 400


def test_endpoint_eligibility_echo_and_default(client_after_build):
    body = client_after_build.get("/api/public/explore").json()
    assert body["eligibility"] == "all"
    body_eligible = client_after_build.get("/api/public/explore?eligibility=eligible").json()
    assert body_eligible["eligibility"] == "eligible"


# --- 2026-09-05 複審修正（Task 10 Step 2）：`fills_max_pages_from_env` 原本無條件讀
# `os.environ`，`ExploreConfig.from_env(env=...)` 傳進來的 `env` 字典被忽略——單元測試
# 若用假 env dict（不動真正的程序環境）驗這個欄位，會靜默讀到真實環境值。 -----------
def test_explore_config_from_env_reads_fills_max_pages_from_given_env_not_os_environ(monkeypatch):
    monkeypatch.delenv("EXPLORE_FILLS_MAX_PAGES", raising=False)
    cfg = ExploreConfig.from_env(env={"EXPLORE_FILLS_MAX_PAGES": "7"})
    assert cfg.fills_max_pages == 7
    # 假 env 沒設這個鍵時，即使程序環境有值也不該滲入——from_env 傳進去的 env 是唯一來源。
    monkeypatch.setenv("EXPLORE_FILLS_MAX_PAGES", "9")
    cfg2 = ExploreConfig.from_env(env={})
    assert cfg2.fills_max_pages == hl_explore.DEFAULT_FILLS_MAX_PAGES


# ============================================================
# Task 0.1（2026-09-20 止血）：query() 不再觸發上游重建
# ============================================================

class _CallCountingHL:
    """包一層共用 `FakeHL`，記錄所有被呼叫的方法名——不改共用 helper 本身
    （`tests/publicapi_helpers.py` 被其他測試檔共用，避免動到 plan 範圍外的檔案），
    每個對外方法呼叫都會 append 自己的名字到 `self.calls`。"""

    def __init__(self):
        self._inner = FakeHL()
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr):
            def wrapper(*a, **kw):
                self.calls.append(name)
                return attr(*a, **kw)
            return wrapper
        return attr


def test_explore_get_never_calls_upstream_nor_builds(tmp_path):
    """spec §3 不變條件一：刷新 Explore 頁面不發 HL info、不開 rebuild。"""
    hl = _CallCountingHL()
    client = _client(_app(tmp_path, hl=hl))
    for _ in range(1000):
        r = client.get("/api/public/explore")
        assert r.status_code == 200
    assert hl.calls == []
    idx = client.app.state.explore_index
    assert idx._rows is None  # 從未發布過任何一版——沒有背景建置動過它
