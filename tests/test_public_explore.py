"""tests/test_public_explore.py — `hl_explore` 純函式 ＋ `ExploreIndex` ＋
`GET /api/public/explore`（M3 round3 Task 1）。

全離線（autouse socket-ban，見 conftest.py）；上游一律靠注入的 `get_fn`／
`FakeHL`，不會真連網。
"""
import json
import socket
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark.publicapi import hl_explore
from spark.publicapi.app import create_app
from spark.publicapi.hl_explore import (ExploreConfig, ExploreIndex, ExploreRow,
                                        WindowStats, candidate_addresses,
                                        clamp_explore_params, enrich_candidate,
                                        paginate, qualify, sort_key)
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


def _portfolio_raw(month_values, alltime_values, start_ms=1_700_000_000_000):
    """最小 `portfolio()` 原始回應：只填 perpMonth／perpAllTime（本模組只吃這兩窗）。"""
    month_series = _av_series(start_ms, month_values)
    alltime_series = _av_series(start_ms, alltime_values)
    return [
        ["perpMonth", {"accountValueHistory": month_series,
                       "pnlHistory": [[t, "0"] for t, _ in month_series], "vlm": "0"}],
        ["perpAllTime", {"accountValueHistory": alltime_series,
                         "pnlHistory": [[t, "0"] for t, _ in alltime_series], "vlm": "0"}],
    ]


def _ch_state(account_value="50000", positions=None):
    return {"marginSummary": {"accountValue": account_value},
           "assetPositions": positions or []}


def _position(coin, szi, leverage="10", margin_used="9000"):
    return {"position": {"coin": coin, "szi": szi, "entryPx": "100",
                         "unrealizedPnl": "0", "marginUsed": margin_used,
                         "leverage": {"type": "cross", "value": leverage}}}


def _to_fills_detail_shape(raw_list):
    """把 `tests/fixtures/hl_user_fills_sample.json`（HL 原始回應形狀，
    closedPnl 駝峰）轉成 `hl.get_fills_detail()` 的輸出形狀（closed_pnl 蛇形）
    ——鏡像 `hl.py.get_fills_detail` 的欄位映射，見該函式 docstring。"""
    return [{"time": int(f["time"]), "coin": f["coin"], "side": f["side"],
            "px": str(f["px"]), "sz": str(f["sz"]),
            "fee": str(f.get("fee", "0") or "0"),
            "closed_pnl": str(f.get("closedPnl", "0") or "0"),
            "hash": f.get("hash", "")} for f in raw_list]


def _sample_fills():
    raw = json.loads((_FIXTURES / "hl_user_fills_sample.json").read_text())
    return _to_fills_detail_shape(raw)


def _row(**over):
    """`qualify`/`sort_key` 測試用的最小 `ExploreRow`（預設全部剛好卡在門檻上，
    呼叫端逐一覆寫想測的欄位）。R4-3：`windows` 改 dict 形狀——`ret_pct`/
    `dd_pct` 是便利參數，套到 `"month"`／`"allTime"` 兩窗（`enrich_candidate`
    保證這兩鍵恆非 None，這裡的假資料維持同一個不變量）；要測特定窗（day/week）
    或某窗缺席（None）時直接傳 `windows=` 整包覆寫。"""
    ret_pct = over.pop("ret_pct", 10.0)
    dd_pct = over.pop("dd_pct", -5.0)
    windows = over.pop("windows", None)
    if windows is None:
        stats = WindowStats(ret_pct=ret_pct, max_dd_pct=dd_pct, spark=())
        windows = {"day": None, "week": None, "month": stats, "allTime": stats}
    base = dict(address=_A, display_name=None, label="0xaaaa…aaaa", coins=(),
               account_bucket="<$10K", windows=windows,
               live_days=60, fill_count_30d=200,
               close_win_rate_pct=50.0, concentration_pct=10.0,
               exposure_dir=None, exposure_pct=None, tags=(), fills_truncated=False)
    base.update(over)
    return ExploreRow(**base)


# ============================================================
# 純函式：勝率值域校驗（R2-02）
# ============================================================

def test_win_rate_out_of_range_returns_none_not_a_fabricated_number(caplog):
    """值域外（>100%，例如上游計數矛盾造成 wins>closed）→ `None`，不得顯示
    一個不可信的百分比。"""
    with caplog.at_level("ERROR"):
        assert hl_explore._win_rate_pct(wins=15, closed=10) is None
    assert "值域外" in caplog.text


def test_win_rate_negative_wins_returns_none():
    assert hl_explore._win_rate_pct(wins=-1, closed=10) is None


def test_win_rate_no_closed_fills_returns_none():
    assert hl_explore._win_rate_pct(wins=0, closed=0) is None


def test_win_rate_normal_value():
    assert hl_explore._win_rate_pct(wins=3, closed=10) == 30.0


def test_win_rate_boundary_100_and_0_are_valid():
    assert hl_explore._win_rate_pct(wins=10, closed=10) == 100.0
    assert hl_explore._win_rate_pct(wins=0, closed=10) == 0.0


# ============================================================
# 純函式：_calendar_span_days（W1：live_days 對取樣密度穩健）
# ============================================================

def test_calendar_span_days_uses_first_last_point_span_not_distinct_count():
    """W1 數值錨例：稀疏（雙週間隔）序列，5 點跨 60 天 → live_days=60
    （不是 distinct 點數 5，也不受中間取樣密度影響——只看首末兩點）。"""
    two_weeks_ms = 15 * 86_400_000
    points = [(i * two_weeks_ms, Decimal("1000")) for i in range(5)]  # 天數: 0,15,30,45,60
    assert hl_explore._calendar_span_days(points) == 60


def test_calendar_span_days_dense_daily_series_matches_point_count_minus_one():
    points = [(i * 86_400_000, Decimal("1000")) for i in range(65)]  # 逐日 65 點
    assert hl_explore._calendar_span_days(points) == 64


def test_calendar_span_days_empty_series_is_zero():
    assert hl_explore._calendar_span_days([]) == 0


# ============================================================
# 純函式：_fills_stats 滿頁偵測（W2 最小版：R-A 分頁 helper 落地前）
# ============================================================

def _synthetic_fill(coin="BTC", px="100", sz="1", closed_pnl="1"):
    return {"coin": coin, "px": px, "sz": sz, "closed_pnl": closed_pnl}


def test_fills_stats_full_page_marks_truncated_and_fill_count_is_lower_bound():
    fills = [_synthetic_fill() for _ in range(hl_explore.FILLS_PAGE_LIMIT)]
    fill_count, _win_rate, _concentration, _coins, truncated = hl_explore._fills_stats(fills)
    assert fill_count == hl_explore.FILLS_PAGE_LIMIT
    assert truncated is True


def test_fills_stats_under_page_limit_is_not_truncated():
    fills = [_synthetic_fill() for _ in range(5)]
    *_rest, truncated = hl_explore._fills_stats(fills)
    assert truncated is False


def test_fills_stats_empty_is_not_truncated():
    fill_count, win_rate, concentration, coins, truncated = hl_explore._fills_stats([])
    assert (fill_count, win_rate, concentration, coins, truncated) == (0, None, None, (), False)


def test_enrich_candidate_propagates_fills_truncated_flag_end_to_end():
    """滿頁 fills → `ExploreRow.fills_truncated=True`，`qualify` 的 `>=` 比較
    不受影響（真實筆數只會 ≥ 這個下限，見 hl_explore.py `_fills_stats` 檔頭）。"""
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    fills = [_synthetic_fill() for _ in range(hl_explore.FILLS_PAGE_LIMIT)]
    row = enrich_candidate(_A, None, portfolio_raw, fills, _ch_state())
    assert row is not None
    assert row.fills_truncated is True
    assert row.fill_count_30d == hl_explore.FILLS_PAGE_LIMIT
    cfg = ExploreConfig(min_trading_days=0, min_fills=hl_explore.FILLS_PAGE_LIMIT)
    # exclude_concentrated 關掉：這裡的合成 fills 全是同一幣種（單純測滿頁/
    # fill_count 下限語意），不是本測試要驗的東西。
    assert qualify(row, cfg, exclude_concentrated=False) is True


# ============================================================
# 純函式：enrich_candidate（含共用 fills fixture）
# ============================================================

def test_enrich_candidate_computes_return_drawdown_live_days_and_win_rate():
    portfolio_raw = _portfolio_raw(
        month_values=[1000, 900, 1100],       # 首900跌至900（-10%）後回升至1100（+10%）
        alltime_values=[1000] * 65,             # 65 個逐日點（1 天一點，跨 64 天）
    )
    fills = _sample_fills()                     # 2 筆，closedPnl 皆 >0 → 勝率 100%
    ch_state = _ch_state(account_value="50000", positions=[_position("BTC", "1.5")])

    row = enrich_candidate(_A, "Alice", portfolio_raw, fills, ch_state)

    assert row is not None
    assert row.address == _A
    assert row.display_name == "Alice"
    assert row.label == "Alice"
    assert row.windows["month"].ret_pct == 10.0         # (1100/1000 - 1) * 100
    assert row.windows["month"].max_dd_pct == -10.0     # 900/1000 - 1
    assert row.live_days == 64                          # W1：首末點日曆跨距（65 點、
                                                          # 逐日一點 → 首末相差 64 天）
    assert row.fill_count_30d == 2
    assert row.close_win_rate_pct == 100.0
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


def test_enrich_candidate_skipped_when_first_point_is_zero():
    """D2：帳戶淨值首點 <=0 → 該列整筆剔除（不得用非正分母算報酬）。"""
    portfolio_raw = _portfolio_raw([0, 1000], [1000] * 60)
    assert enrich_candidate(_A, None, portfolio_raw, [], _ch_state()) is None


def test_enrich_candidate_skipped_when_zeroed_mid_series():
    """D2：帳戶淨值途中歸零 → 該列整筆剔除。"""
    portfolio_raw = _portfolio_raw([1000, 0, 1000], [1000] * 60)
    assert enrich_candidate(_A, None, portfolio_raw, [], _ch_state()) is None


def test_enrich_candidate_skipped_when_perp_month_missing():
    """讀不到（perpMonth 視窗缺席）→ 跳過該列，不進榜、不編數字。"""
    portfolio_raw = [["perpAllTime", {"accountValueHistory": _av_series(0, [1000] * 60),
                                     "pnlHistory": []}]]
    assert enrich_candidate(_A, None, portfolio_raw, [], _ch_state()) is None


def test_enrich_candidate_skipped_when_perp_all_time_missing():
    portfolio_raw = [["perpMonth", {"accountValueHistory": _av_series(0, [1000, 1100]),
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
    day/week/month/allTime 各自獨立的 ret/dd（同源同基準，各窗各自的序列）。"""
    portfolio_raw = [
        ["perpDay", {"accountValueHistory": _av_series(0, [1000, 1010], step_ms=3_600_000),
                     "pnlHistory": [], "vlm": "0"}],
        ["perpWeek", {"accountValueHistory": _av_series(0, [1000, 950, 1050]),
                     "pnlHistory": [], "vlm": "0"}],
        ["perpMonth", {"accountValueHistory": _av_series(0, [1000, 900, 1100]),
                      "pnlHistory": [], "vlm": "0"}],
        ["perpAllTime", {"accountValueHistory": _av_series(0, [1000] * 65),
                         "pnlHistory": [], "vlm": "0"}],
    ]
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state())
    assert row is not None
    assert set(row.windows.keys()) == {"day", "week", "month", "allTime"}
    assert row.windows["day"].ret_pct == 1.0             # (1010/1000 - 1) * 100
    assert row.windows["week"].ret_pct == 5.0            # (1050/1000 - 1) * 100
    assert row.windows["week"].max_dd_pct == -5.0        # 950/1000 - 1
    assert row.windows["month"].ret_pct == 10.0          # (1100/1000 - 1) * 100
    assert row.windows["allTime"].ret_pct == 0.0         # 全序列恆為 1000


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
    """R4-3：day／week 序列本身無效（首點非正）只讓那一鍵是 `None`——不像
    month／allTime 那樣連坐整列（gating 只在 month／allTime，見模組檔頭）。"""
    portfolio_raw = _portfolio_raw([1000, 1100], [1000] * 60)
    portfolio_raw = [["perpDay", {"accountValueHistory": _av_series(0, [0, 1000]),
                                  "pnlHistory": [], "vlm": "0"}], *portfolio_raw]
    row = enrich_candidate(_A, None, portfolio_raw, [], _ch_state())
    assert row is not None
    assert row.windows["day"] is None


# ============================================================
# 純函式：qualify — 資格過濾邊界（等號行為釘死）
# ============================================================

def test_qualify_live_days_exactly_at_threshold_passes():
    cfg = ExploreConfig(min_trading_days=60, min_fills=0)
    assert qualify(_row(live_days=60, fill_count_30d=0), cfg) is True


def test_qualify_live_days_one_below_threshold_fails():
    cfg = ExploreConfig(min_trading_days=60, min_fills=0)
    assert qualify(_row(live_days=59, fill_count_30d=0), cfg) is False


def test_qualify_fill_count_exactly_at_threshold_passes():
    cfg = ExploreConfig(min_trading_days=0, min_fills=200)
    assert qualify(_row(live_days=0, fill_count_30d=200), cfg) is True


def test_qualify_fill_count_one_below_threshold_fails():
    cfg = ExploreConfig(min_trading_days=0, min_fills=200)
    assert qualify(_row(live_days=0, fill_count_30d=199), cfg) is False


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
    bad_row = _row(live_days=1, fill_count_30d=1, dd_pct=-99.0,
                   concentration_pct=99.0)
    assert qualify(bad_row, cfg, require_sample=False, max_dd_filter=False,
                  exclude_concentrated=False) is True
    assert qualify(bad_row, cfg) is False


# ============================================================
# 純函式：qualify — R4-3 window 參數（回撤過濾看所選窗，不是永遠 month）
# ============================================================

def test_qualify_max_dd_uses_selected_window_not_always_month():
    windows = {
        "day": WindowStats(ret_pct=1.0, max_dd_pct=-50.0, spark=()),
        "week": None,
        "month": WindowStats(ret_pct=10.0, max_dd_pct=-5.0, spark=()),
        "allTime": WindowStats(ret_pct=20.0, max_dd_pct=-5.0, spark=()),
    }
    row = _row(windows=windows)
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(row, cfg, window="month") is True    # month dd=-5 <= 30
    assert qualify(row, cfg, window="day") is False      # day dd=-50 > 30


def test_qualify_missing_window_stats_passes_max_dd_no_evidence_no_penalty():
    windows = {"day": None, "week": None,
              "month": WindowStats(10.0, -5.0, ()), "allTime": WindowStats(10.0, -5.0, ())}
    row = _row(windows=windows)
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(row, cfg, window="week") is True


# ============================================================
# 純函式：sort_key（風險調整排序鍵，D2）
# ============================================================

def test_sort_key_ranks_higher_return_lower_drawdown_first():
    high = _row(ret_pct=20.0, dd_pct=-5.0)
    low = _row(ret_pct=20.0, dd_pct=-15.0)
    assert sort_key(high) > sort_key(low)


def test_sort_key_zero_drawdown_uses_floor_not_division_by_zero():
    row = _row(ret_pct=1.0, dd_pct=0.0)
    assert sort_key(row) == Decimal("1.0") / Decimal("0.5")


def test_sort_key_uses_selected_window():
    windows = {"day": WindowStats(50.0, -1.0, ()), "week": None,
              "month": WindowStats(10.0, -5.0, ()), "allTime": WindowStats(10.0, -5.0, ())}
    row = _row(windows=windows)
    assert sort_key(row, window="day") == Decimal("50.0") / Decimal("1.0")
    assert sort_key(row, window="month") == Decimal("10.0") / Decimal("5.0")


def test_sort_key_falls_back_to_month_when_selected_window_missing():
    windows = {"day": None, "week": None,
              "month": WindowStats(10.0, -5.0, ()), "allTime": WindowStats(10.0, -5.0, ())}
    row = _row(windows=windows)
    assert sort_key(row, window="week") == sort_key(row, window="month")


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
# ExploreIndex：building 態、fail-open、排除、分頁（endpoint 前的直接測試）
# ============================================================

def _seed_hl(hl: FakeHL, address: str, *, roi_ret_pct=("1000", "1100"),
            alltime_days=60):
    hl.portfolios[address.lower()] = _portfolio_raw(
        [int(v) for v in roi_ret_pct], [1000] * alltime_days)
    hl.fills_detail[address.lower()] = []
    hl.clearinghouse[address.lower()] = _ch_state()


# ============================================================
# ExploreIndex._call_hl：節流間隔 ＋ 429 退避重試 ＋ 中止整輪建置（2026-08-30
# mainnet 整合實跑 burst 429 事故修法）
# ============================================================

_429_MESSAGE = ("Client error '429 Too Many Requests' for url "
               "'https://api.hyperliquid.xyz/info'")


def test_call_hl_sleeps_configured_interval_between_every_single_hl_call():
    """節流間隔套用在**每個 HL 請求之間**（不是地址之間）：一個地址 3 個
    HL 呼叫，呼叫與呼叫之間都要有一次設定值的 sleep。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=60)
    calls: list[str] = []
    orig_portfolio, orig_fills, orig_ch = (hl.portfolio, hl.get_fills_detail,
                                           hl.clearinghouse_state)

    def portfolio(address):
        calls.append("call:portfolio")
        return orig_portfolio(address)

    def fills(address, start, end):
        calls.append("call:fills")
        return orig_fills(address, start, end)

    def clearinghouse(address):
        calls.append("call:clearinghouse")
        return orig_ch(address)

    hl.portfolio, hl.get_fills_detail, hl.clearinghouse_state = (portfolio, fills, clearinghouse)

    def sleep_fn(seconds):
        calls.append(f"sleep:{seconds}")

    cfg = ExploreConfig(min_trading_days=0, min_fills=0, enrich_call_interval_s=0.7)
    index = ExploreIndex(leaderboard_source_fn=lambda: _leaderboard_payload(_lb_row(_A, roi="0.5")),
                         hl=hl, excluded_fn=lambda: set(), cfg=cfg,
                         now_fn=lambda: 1000.0, sleep_fn=sleep_fn)
    index.build_sync()

    assert calls == ["call:portfolio", "sleep:0.7", "call:fills", "sleep:0.7",
                     "call:clearinghouse", "sleep:0.7"]


def test_call_hl_retries_429_with_exponential_backoff_then_succeeds():
    """429 視為 transient（讀操作冪等，工程原則 2）：前兩次 429，第三次成功
    → 該地址仍正常進榜；退避延遲依序為 `RATE_LIMIT_RETRY_DELAYS_S` 的前兩個
    （2s/8s）。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=60)
    real_portfolio = hl.portfolio
    state = {"n": 0}

    def flaky_portfolio(address):
        state["n"] += 1
        if state["n"] <= 2:
            raise RuntimeError(_429_MESSAGE)
        return real_portfolio(address)

    hl.portfolio = flaky_portfolio
    sleeps: list[float] = []
    cfg = ExploreConfig(min_trading_days=0, min_fills=0, enrich_call_interval_s=0.1)
    index = ExploreIndex(leaderboard_source_fn=lambda: _leaderboard_payload(_lb_row(_A, roi="0.5")),
                         hl=hl, excluded_fn=lambda: set(), cfg=cfg,
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: sleeps.append(s))
    index.build_sync()

    result = index.query(require_sample=False)
    assert len(result["rows"]) == 1  # 429 兩次後第三次成功，該地址仍進榜
    assert sleeps[:2] == [2.0, 8.0]  # 429 退避延遲（RATE_LIMIT_RETRY_DELAYS_S 前兩個）


def test_build_aborts_on_persistent_rate_limit_and_keeps_old_snapshot(caplog):
    """退避重試耗盡仍 429 → 中止整輪建置（不繼續燒剩餘候選）、保留舊
    snapshot（fail-open）、`building: False`（有舊值可回）、且大聲留痕
    `build aborted: rate limited`。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=60)
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"))
    cfg = ExploreConfig(min_trading_days=0, min_fills=0, enrich_call_interval_s=0.0)
    # ⭐ enrich_ttl_s=0：固定的 now_fn（1000.0）會讓第二輪 build_sync 命中
    # per-address enrich 快取、完全不再打 `hl.portfolio`——這裡要測的正是
    # 「第二輪重新打上游、遇到持續 429」，把快取關掉才會真的走到重試路徑。
    index = ExploreIndex(leaderboard_source_fn=lambda: payload, hl=hl,
                         excluded_fn=lambda: set(), cfg=cfg,
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None,
                         enrich_ttl_s=0.0)
    index.build_sync()  # 第一輪成功，建立舊 snapshot
    first = index.query(require_sample=False)
    assert len(first["rows"]) == 1

    def always_429(address):
        raise RuntimeError(_429_MESSAGE)

    hl.portfolio = always_429

    with caplog.at_level("ERROR"):
        index.build_sync()  # 第二輪：持續 429，三次退避耗盡

    assert "build aborted: rate limited" in caplog.text
    second = index.query(require_sample=False)
    assert second["rows"] == first["rows"]   # 舊 snapshot 保留，不是空清單
    assert second["building"] is False        # 有舊值 → 不是 building 態


def test_call_hl_non_429_error_still_skips_only_that_address_not_whole_build():
    """非 429 的錯誤維持既有「跳過該地址」語意，不觸發整輪中止——與 429
    中止路徑明確分流。"""
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=60)
    _seed_hl(hl, _B, alltime_days=60)
    real_portfolio = hl.portfolio

    def bad_for_a(address):
        if address.lower() == _A.lower():
            # ⚠️ 訊息刻意不含 "429" 三個字元——`_is_rate_limited` 是字串子字串
            # 比對，混進這串數字會被誤判成 rate limit，這裡要測的正是「非
            # rate limit 錯誤」的分流。
            raise ValueError("資料格式不符（欄位缺失）")
        return real_portfolio(address)

    hl.portfolio = bad_for_a
    cfg = ExploreConfig(min_trading_days=0, min_fills=0, enrich_call_interval_s=0.0)
    payload = _leaderboard_payload(_lb_row(_A, roi="0.9"), _lb_row(_B, roi="0.5"))
    index = ExploreIndex(leaderboard_source_fn=lambda: payload, hl=hl,
                         excluded_fn=lambda: set(), cfg=cfg,
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    index.build_sync()
    result = index.query(require_sample=False)
    addrs = [r["address"] for r in result["rows"]]
    assert _A not in addrs
    assert _B in addrs
    assert result["building"] is False


def test_call_hl_non_429_exception_still_sleeps_the_throttle_interval():
    """C4 殘洞修法：`_call_hl` 的節流不再只掛在成功路徑——上游丟出非 429 的
    錯誤（例如連線重置／5xx，`_is_rate_limited` 判斷為 False，立即上拋、不
    重試）時，也必須先睡滿一次 `enrich_call_interval_s` 才離開這個函式，
    否則地址與地址之間的節流在上游故障時會退化回無節流的 burst（見模組
    檔頭 C4 記錄）。"""
    sleeps: list[float] = []

    def always_fails():
        raise ConnectionError("connection reset by peer")

    index = ExploreIndex(leaderboard_source_fn=lambda: None, hl=FakeHL(),
                         excluded_fn=lambda: set(),
                         cfg=ExploreConfig(enrich_call_interval_s=0.7),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: sleeps.append(s))

    with pytest.raises(ConnectionError):
        index._call_hl(always_fails, what="test address=0xabc")

    assert sleeps == [0.7]


def test_call_hl_rate_limited_abort_path_still_sleeps_the_throttle_interval():
    """同一條 finally 保底也涵蓋 429 退避耗盡的 `_RateLimitedAbort` 路徑。"""
    sleeps: list[float] = []

    def always_429():
        raise RuntimeError(_429_MESSAGE)

    index = ExploreIndex(leaderboard_source_fn=lambda: None, hl=FakeHL(),
                         excluded_fn=lambda: set(),
                         cfg=ExploreConfig(enrich_call_interval_s=0.7),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: sleeps.append(s))

    with pytest.raises(hl_explore._RateLimitedAbort):
        index._call_hl(always_429, what="test address=0xabc")

    # 三次退避延遲（2s/8s/30s）之後，finally 補一次節流間隔（0.7s）。
    assert sleeps == [2.0, 8.0, 30.0, 0.7]


def test_index_query_never_built_returns_building_true_and_empty_rows_without_blocking():
    """建置中或**從未成功過** → `building: True` ＋空 rows，且讀路徑不阻塞
    （呼叫立即返回，不等背景 thread 跑完）。"""
    started = threading.Event()
    release = threading.Event()

    def slow_source():
        started.set()
        assert release.wait(timeout=5), "release 逾時未被觸發"
        return _leaderboard_payload()  # 沒有候選人，building 很快跑完

    index = ExploreIndex(leaderboard_source_fn=slow_source, hl=FakeHL(),
                         excluded_fn=lambda: set(), cfg=ExploreConfig(),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)

    start = time.monotonic()
    result = index.query()
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"query() 被背景建置卡住了（耗時 {elapsed}s）"
    assert result == {"rows": [], "page": 1, "page_size": ExploreConfig().page_size,
                      "total_qualified": 0, "total_scanned": 0,
                      "updated_at": None, "building": True}
    assert started.wait(timeout=5), "背景建置未啟動"
    release.set()


def test_index_build_sync_fails_open_to_previous_snapshot_on_upstream_failure():
    """上游故障（本輪來源回 None）→ 沿用舊版，不清空、不 building。"""
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"))
    hl = FakeHL()
    _seed_hl(hl, _A)
    state = {"call": 0}

    def source_fn():
        state["call"] += 1
        return payload if state["call"] == 1 else None

    index = ExploreIndex(leaderboard_source_fn=source_fn, hl=hl,
                         excluded_fn=lambda: set(),
                         cfg=ExploreConfig(min_trading_days=0, min_fills=0),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    index.build_sync()
    first = index.query()
    assert len(first["rows"]) == 1
    assert first["building"] is False

    index.build_sync()  # 第二輪上游失效
    second = index.query()
    assert second["rows"] == first["rows"]
    assert second["building"] is False


def test_index_excludes_filet_own_address_end_to_end():
    payload = _leaderboard_payload(_lb_row(_A, roi="0.9"), _lb_row(_FILET_OWN, roi="0.99"))
    hl = FakeHL()
    _seed_hl(hl, _A)
    _seed_hl(hl, _FILET_OWN)
    index = ExploreIndex(leaderboard_source_fn=lambda: payload, hl=hl,
                         excluded_fn=lambda: {_FILET_OWN.lower()}, cfg=ExploreConfig(),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    index.build_sync()
    addrs = [r["address"] for r in index.query(require_sample=False)["rows"]]
    assert _A in addrs
    assert _FILET_OWN not in addrs


def test_index_pagination_across_pages():
    rows_payload = _leaderboard_payload(
        *[_lb_row(f"0x{i:040x}", roi=str(i)) for i in range(60)])
    hl = FakeHL()
    for i in range(60):
        _seed_hl(hl, f"0x{i:040x}")
    cfg = ExploreConfig(page_size=25, min_trading_days=0, min_fills=0)
    index = ExploreIndex(leaderboard_source_fn=lambda: rows_payload, hl=hl,
                         excluded_fn=lambda: set(), cfg=cfg,
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    index.build_sync()
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
    """R4-3 端到端：`window` 參數改變回傳列的排序（`sort_key` 依所選窗）。
    地址 A 是 day 窗強、month 窗弱；地址 B 相反——window 切換應讓排名對調。"""
    hl = FakeHL()
    hl.portfolios[_A.lower()] = [
        ["perpDay", {"accountValueHistory": _av_series(0, [1000, 1100], step_ms=3_600_000),
                     "pnlHistory": [], "vlm": "0"}],
        ["perpWeek", {"accountValueHistory": _av_series(0, [1000, 1000]), "pnlHistory": [], "vlm": "0"}],
        ["perpMonth", {"accountValueHistory": _av_series(0, [1000, 1010]), "pnlHistory": [], "vlm": "0"}],
        ["perpAllTime", {"accountValueHistory": _av_series(0, [1000] * 60), "pnlHistory": [], "vlm": "0"}],
    ]
    hl.fills_detail[_A.lower()] = []
    hl.clearinghouse[_A.lower()] = _ch_state()
    hl.portfolios[_B.lower()] = [
        ["perpDay", {"accountValueHistory": _av_series(0, [1000, 1000]), "pnlHistory": [], "vlm": "0"}],
        ["perpWeek", {"accountValueHistory": _av_series(0, [1000, 1000]), "pnlHistory": [], "vlm": "0"}],
        ["perpMonth", {"accountValueHistory": _av_series(0, [1000, 1200]), "pnlHistory": [], "vlm": "0"}],
        ["perpAllTime", {"accountValueHistory": _av_series(0, [1000] * 60), "pnlHistory": [], "vlm": "0"}],
    ]
    hl.fills_detail[_B.lower()] = []
    hl.clearinghouse[_B.lower()] = _ch_state()
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"), _lb_row(_B, roi="0.4"))
    cfg = ExploreConfig(min_trading_days=0, min_fills=0)
    index = ExploreIndex(leaderboard_source_fn=lambda: payload, hl=hl,
                         excluded_fn=lambda: set(), cfg=cfg,
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    index.build_sync()

    by_day = index.query(window="day")
    assert [r["address"] for r in by_day["rows"]] == [_A, _B]   # A 的 day 報酬較高

    by_month = index.query(window="month")
    assert [r["address"] for r in by_month["rows"]] == [_B, _A]  # B 的 month 報酬較高


# ============================================================
# ExploreIndex：R4-3 index 結構版本——不相容快照視同未建置，強制重建
# ============================================================

def test_index_version_mismatch_forces_rebuild_even_within_ttl():
    """把記憶體內快照的版本標記竄改成舊版後，即使 TTL 未過期，`query()` 也
    必須回 `building: True`（不得把不相容形狀的舊列序列化給前端），並忽略
    TTL 觸發背景重建。"""
    hl = FakeHL()
    _seed_hl(hl, _A)
    payload = _leaderboard_payload(_lb_row(_A, roi="0.5"))
    index = ExploreIndex(leaderboard_source_fn=lambda: payload, hl=hl,
                         excluded_fn=lambda: set(),
                         cfg=ExploreConfig(min_trading_days=0, min_fills=0),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    index.build_sync()
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
    index = ExploreIndex(leaderboard_source_fn=lambda: None, hl=FakeHL(),
                         excluded_fn=lambda: set(), cfg=ExploreConfig(),
                         now_fn=lambda: 1000.0, sleep_fn=lambda s: None)
    assert index._rows_version is None


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


def test_endpoint_full_flow_after_build_completes(tmp_path, monkeypatch):
    """走完整條管線：上游 leaderboard → enrich → 過濾 → 排序 → 分頁，一路到
    HTTP 回應；並驗證 Filet 自營地址在端點層也被排除（讀精選白名單）。

    `app.py` 接線的 `ExploreIndex` 不注入假 `sleep_fn`（正式行為就是真睡，
    見 `_call_hl` 節流）——這裡把節流間隔歸零，測試才不會真的睡
    `EXPLORE_ENRICH_CALL_INTERVAL_S` 秒（`ExploreConfig.from_env` 讀這個環境
    變數，見 hl_explore.py）。"""
    monkeypatch.setenv("EXPLORE_ENRICH_CALL_INTERVAL_S", "0")
    payload = _leaderboard_payload(_lb_row(_A, display_name="Alice", roi="0.5"),
                                   _lb_row(_FILET_OWN, roi="0.99"))
    hl = FakeHL()
    _seed_hl(hl, _A, alltime_days=65)
    _seed_hl(hl, _FILET_OWN, alltime_days=65)
    app = _app(tmp_path, hl=hl, leaderboard_get_fn=lambda url: payload,
              leaders=[{"address": _FILET_OWN, "name": "Filet 自營", "enabled": True}])
    client = _client(app)

    index = app.state.explore_index
    index.build_sync()  # 測試直接同步建置一次，不等背景 thread（見 hl_explore 檔頭）

    # R4-3：`qualified=0` chip 已移除——改送 min_live_days=0/min_fills=0
    # 停用樣本門檻（`_seed_hl` 的假地址 fills_detail 是空清單，預設
    # min_fills=200 門檻進不了榜）。
    r = client.get("/api/public/explore", params={"min_live_days": 0, "min_fills": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["building"] is False
    addrs = [row["address"] for row in body["rows"]]
    assert _A in addrs
    assert _FILET_OWN not in addrs
    row = next(row for row in body["rows"] if row["address"] == _A)
    assert row["display_name"] == "Alice"
    assert row["label"] == "Alice"
    assert row["windows"]["month"]["ret_pct"] == 10.0
