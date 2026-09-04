import json
from pathlib import Path

import pytest

from spark.filet.trader_stats import (FillsStats, WindowStats, downsample, fills_stats,
                                      live_days_from_av, window_stats)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def portfolio():
    return json.load(open(FIX / "trader_stats_0x6648_portfolio.json"))


@pytest.fixture(scope="module")
def fills():
    return json.load(open(FIX / "trader_stats_0x6648_fills30d.json"))["fills"]


# --- window_stats：錨例來自真實 0x6648（2026-09-04 抓取），與 Hyperbot 圖表末值逐位一致 ---
def test_window_stats_month_pnl_usd_and_spark_from_pnl_history(portfolio):
    ws = window_stats(portfolio, "month")
    assert isinstance(ws, WindowStats)
    assert ws.pnl_usd == 33055.26                    # pnlHistory 末值 33055.25879 − 首值 0，quantize 0.01
    assert ws.max_dd_pct is None
    assert ws.max_dd_reason == "too_many_skipped_intervals"   # 31/56 區間淨值 < 100 USDC
    assert len(ws.spark) == 30
    assert ws.spark[0] == 0.0 and ws.spark[-1] == 33055.25879


def test_window_stats_day_has_drawdown(portfolio):
    ws = window_stats(portfolio, "day")
    assert ws.pnl_usd == -2181.94
    assert ws.max_dd_pct == pytest.approx(-74.07, abs=0.01)   # 權益指數 MDD，負值慣例（D1）
    assert ws.max_dd_reason is None
    assert len(ws.spark) == 21                       # 21 點 <= 30，不補點


def test_window_stats_all_time_is_flow_dominated(portfolio):
    ws = window_stats(portfolio, "allTime")
    assert ws.pnl_usd == 27504.48
    assert ws.max_dd_pct is None
    assert ws.max_dd_reason == "flow_dominated_interval"


def test_window_stats_week(portfolio):
    ws = window_stats(portfolio, "week")
    assert ws.pnl_usd == 764.18
    assert ws.max_dd_reason == "too_many_skipped_intervals"


def test_window_stats_missing_window_is_none():
    assert window_stats([["day", {"accountValueHistory": [], "pnlHistory": []}]], "month") is None


def test_window_stats_single_point_is_none():
    rows = [["month", {"accountValueHistory": [[1, "10"]], "pnlHistory": [[1, "0"]]}]]
    assert window_stats(rows, "month") is None


def test_window_stats_to_dict_shape(portfolio):
    d = window_stats(portfolio, "month").to_dict()
    assert set(d) == {"pnl_usd", "max_dd_pct", "max_dd_reason", "spark"}
    assert d["max_dd_pct"] is None and isinstance(d["spark"], list)


# --- live_days ---
def test_live_days_from_all_time_calendar_span(portfolio):
    av = dict(portfolio)["allTime"]["accountValueHistory"]
    pts = [(int(t), v) for t, v in av]
    assert live_days_from_av(pts) == 1003   # 2023-12-07 → 2026-09-04（fixture 末點）


def test_live_days_empty_is_zero():
    assert live_days_from_av([]) == 0


# --- downsample ---
def test_downsample_keeps_short_series_and_caps_long():
    assert downsample([1.0, 2.0, 3.0], n=30) == [1.0, 2.0, 3.0]
    out = downsample([float(i) for i in range(100)], n=30)
    assert len(out) == 30 and out[0] == 0.0 and out[-1] == 99.0


# --- fills_stats：錨例與 Hyperbot query-addr-stat period=30 逐位一致 ---
def test_fills_stats_matches_hyperbot_definitions(fills):
    fs = fills_stats(fills, truncated=False)
    assert isinstance(fs, FillsStats)
    assert fs.order_count == 221           # distinct oid
    assert fs.closed_positions == 27       # 部位歸零生命週期（含翻倉 Short > Long）
    assert fs.wins == 15
    assert fs.win_rate_pct == 55.56        # 15/27，quantize 0.01
    assert fs.realized_pnl_usd == 40225.79 # Σ closedPnl
    assert fs.truncated is False
    assert len(fs.coins) <= 3 and 0 <= fs.concentration_pct <= 100


def test_fills_stats_excludes_spot_fills():
    perp = [{"coin": "BTC", "oid": 1, "dir": "Open Long", "startPosition": "0", "sz": "1", "px": "100",
             "closedPnl": "0", "time": 1},
            {"coin": "BTC", "oid": 2, "dir": "Close Long", "startPosition": "1", "sz": "1", "px": "110",
             "closedPnl": "10", "time": 2}]
    spot = [{"coin": "PURR/USDC", "oid": 3, "dir": "Buy", "startPosition": "0", "sz": "5", "px": "1",
             "closedPnl": "0", "time": 3}]
    fs = fills_stats(perp + spot, truncated=False)
    assert fs.order_count == 2 and fs.closed_positions == 1 and fs.wins == 1
    assert fs.win_rate_pct == 100.0 and fs.realized_pnl_usd == 10.0


def test_fills_stats_flip_counts_as_close_and_partial_close_does_not():
    f = [{"coin": "ETH", "oid": 1, "dir": "Open Long", "startPosition": "0", "sz": "2", "px": "100",
          "closedPnl": "0", "time": 1},
         {"coin": "ETH", "oid": 2, "dir": "Close Long", "startPosition": "2", "sz": "1", "px": "90",
          "closedPnl": "-10", "time": 2},                      # 部分平倉，不算生命週期結束
         {"coin": "ETH", "oid": 3, "dir": "Long > Short", "startPosition": "1", "sz": "3", "px": "120",
          "closedPnl": "20", "time": 3}]                       # 翻倉：關掉剩餘 1 顆，算一次
    fs = fills_stats(f, truncated=False)
    assert fs.closed_positions == 1 and fs.wins == 1          # 累積 -10 + 20 = +10 > 0
    assert fs.order_count == 3


def test_fills_stats_no_closes_win_rate_none():
    f = [{"coin": "ETH", "oid": 1, "dir": "Open Long", "startPosition": "0", "sz": "2", "px": "100",
          "closedPnl": "0", "time": 1}]
    fs = fills_stats(f, truncated=True)
    assert fs.closed_positions == 0 and fs.win_rate_pct is None and fs.truncated is True


def test_fills_stats_empty():
    fs = fills_stats([], truncated=False)
    assert fs.order_count == 0 and fs.realized_pnl_usd == 0.0 and fs.coins == () \
        and fs.concentration_pct is None
