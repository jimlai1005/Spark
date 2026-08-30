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
                                        candidate_addresses, enrich_candidate,
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
    呼叫端逐一覆寫想測的欄位）。"""
    base = dict(address=_A, display_name=None, label="0xaaaa…aaaa", coins=(),
               account_bucket="<$10K", spark=(), ret_30d_pct=10.0,
               max_dd_30d_pct=-5.0, trading_days=60, fill_count_30d=200,
               close_win_rate_pct=50.0, concentration_pct=10.0,
               exposure_dir=None, exposure_pct=None, tags=())
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
# 純函式：enrich_candidate（含共用 fills fixture）
# ============================================================

def test_enrich_candidate_computes_return_drawdown_trading_days_and_win_rate():
    portfolio_raw = _portfolio_raw(
        month_values=[1000, 900, 1100],       # 首900跌至900（-10%）後回升至1100（+10%）
        alltime_values=[1000] * 65,             # 65 個 distinct UTC 天
    )
    fills = _sample_fills()                     # 2 筆，closedPnl 皆 >0 → 勝率 100%
    ch_state = _ch_state(account_value="50000", positions=[_position("BTC", "1.5")])

    row = enrich_candidate(_A, "Alice", portfolio_raw, fills, ch_state)

    assert row is not None
    assert row.address == _A
    assert row.display_name == "Alice"
    assert row.label == "Alice"
    assert row.ret_30d_pct == 10.0                     # (1100/1000 - 1) * 100
    assert row.max_dd_30d_pct == -10.0                  # 900/1000 - 1
    assert row.trading_days == 65
    assert row.fill_count_30d == 2
    assert row.close_win_rate_pct == 100.0
    assert row.account_bucket == "$10K–$100K"
    assert row.exposure_dir == "long"
    assert len(row.spark) == 3


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
# 純函式：qualify — 資格過濾邊界（等號行為釘死）
# ============================================================

def test_qualify_trading_days_exactly_at_threshold_passes():
    cfg = ExploreConfig(min_trading_days=60, min_fills=0)
    assert qualify(_row(trading_days=60, fill_count_30d=0), cfg) is True


def test_qualify_trading_days_one_below_threshold_fails():
    cfg = ExploreConfig(min_trading_days=60, min_fills=0)
    assert qualify(_row(trading_days=59, fill_count_30d=0), cfg) is False


def test_qualify_fill_count_exactly_at_threshold_passes():
    cfg = ExploreConfig(min_trading_days=0, min_fills=200)
    assert qualify(_row(trading_days=0, fill_count_30d=200), cfg) is True


def test_qualify_fill_count_one_below_threshold_fails():
    cfg = ExploreConfig(min_trading_days=0, min_fills=200)
    assert qualify(_row(trading_days=0, fill_count_30d=199), cfg) is False


def test_qualify_drawdown_exactly_at_cap_passes():
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(_row(max_dd_30d_pct=-30.0), cfg) is True


def test_qualify_drawdown_just_over_cap_fails():
    cfg = ExploreConfig(max_drawdown_pct=Decimal("30"))
    assert qualify(_row(max_dd_30d_pct=-30.01), cfg) is False


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
    bad_row = _row(trading_days=1, fill_count_30d=1, max_dd_30d_pct=-99.0,
                   concentration_pct=99.0)
    assert qualify(bad_row, cfg, require_sample=False, max_dd_filter=False,
                  exclude_concentrated=False) is True
    assert qualify(bad_row, cfg) is False


# ============================================================
# 純函式：sort_key（風險調整排序鍵，D2）
# ============================================================

def test_sort_key_ranks_higher_return_lower_drawdown_first():
    high = _row(ret_30d_pct=20.0, max_dd_30d_pct=-5.0)
    low = _row(ret_30d_pct=20.0, max_dd_30d_pct=-15.0)
    assert sort_key(high) > sort_key(low)


def test_sort_key_zero_drawdown_uses_floor_not_division_by_zero():
    row = _row(ret_30d_pct=1.0, max_dd_30d_pct=0.0)
    assert sort_key(row) == Decimal("1.0") / Decimal("0.5")


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


@pytest.mark.parametrize("param", ["qualified", "max_dd", "exclude_concentrated"])
def test_endpoint_rejects_non_boolean_toggle_params(tmp_path, param):
    app = _app(tmp_path, leaderboard_get_fn=lambda url: _leaderboard_payload())
    r = _client(app).get("/api/public/explore", params={param: 2})
    assert r.status_code == 422


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

    r = client.get("/api/public/explore", params={"qualified": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["building"] is False
    addrs = [row["address"] for row in body["rows"]]
    assert _A in addrs
    assert _FILET_OWN not in addrs
    row = next(row for row in body["rows"] if row["address"] == _A)
    assert row["display_name"] == "Alice"
    assert row["label"] == "Alice"
    assert row["ret_30d_pct"] == 10.0
