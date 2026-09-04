"""tests/test_public_traders.py — `GET /api/public/traders/{address}`
（M3 round2 Task 6：交易員詳情頁，不受精選白名單管轄）。

計算重用 `filet.strategies` 的既有純函式（`build_metrics`／`build_cagr_fields`）
＋（2026-09-05 起）`spark.filet.trader_stats`（`window_stats`／`fills_stats`／
`live_days_from_av`，與 `/api/public/explore` 共用，見
docs/superpowers/plans/2026-09-04-explore-trader-pnl-metrics.md Task 4）——
本檔驗證端點層的組裝：位址驗證、快取、四個獨立來源（portfolio／
clearinghouseState／ledger／fills）各自的失敗降級、上限 256 個地址的淘汰，
以及與 `hl_explore.enrich_candidate` 的同源數字逐位相等。全離線
（autouse socket-ban，見 conftest.py；FakeHL 全假資料）。
"""
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark.publicapi import hl_explore
from spark.publicapi.app import create_app
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import FakeHL, FakeKeysvc, make_app, make_cfg

_FIXTURES = Path(__file__).parent / "fixtures"
_TRADER_ADDR = "0x6648f8dd041ed689de7bf501efb3b827cf15b1f3"

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（沿既有慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


_A = "0x" + "a1" * 20
_DAY_MS = 86400000


def _portfolio_rows(av, pnl, period="allTime"):
    # ⚠️ 2026-08-31 issue log I-15 使用者裁決：交易員詳情頁改吃 `allTime`
    # （spot+perp 合併窗，原 `perpAllTime`）——見 app.py `public_trader_detail`。
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "0"}]]


def sixty_day_rows(start_av="1000", end_av="1200"):
    t = 60 * _DAY_MS
    delta = str(Decimal(end_av) - Decimal(start_av))
    return _portfolio_rows([[0, start_av], [t, end_av]], [[0, "0"], [t, delta]])


def test_bad_address_format_422(tmp_path):
    app, *_ = make_app(tmp_path)
    r = _client(app).get("/api/public/traders/not-an-address")
    assert r.status_code == 422


def test_upstream_portfolio_failure_is_503(tmp_path):
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    hl.portfolio_error[_A] = ConnectionError("hl 5xx")
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 503


def test_not_whitelisted_address_still_returns_200(tmp_path):
    """核心行為：leaderboard 任意地址（不在精選白名單、也不在 user registry）
    照樣能看到詳情頁——本端點結構上不 import `leaders.py`。

    ⭐ M3 round4 Task R4-2：`initial_deposit_usd` 改由
    `hl.non_funding_ledger_updates()`（真實 deposit 加總）供給，不再是
    `accountValueHistory` 首點；`start_equity_usd`／`end_equity_usd` 為新增欄位。"""
    cfg = make_cfg(tmp_path)  # 空白名單
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = sixty_day_rows()
    hl.clearinghouse[_A] = {"marginSummary": {"accountValue": "5000.00"},
                            "assetPositions": []}
    hl.ledger_updates[_A] = [{"time": 0, "hash": "0x1",
                              "delta": {"type": "deposit", "usdc": "1000.0"}}]
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["address"] == _A
    assert body["account_value"] == "5000.00"
    assert "equity_index" not in body
    assert body["metrics"]["allTime"]["total_return_pct"] == "20.00"
    assert body["metrics"]["allTime"]["total_return_pct_insufficient"] is False
    assert set(body["metrics"]) == {"day", "week", "month", "allTime"}
    assert set(body["windows"]) == {"day", "week", "month", "allTime"}
    assert body["windows"]["allTime"]["pnl_usd"] == 200.0
    meth = body["methodology"]
    assert meth["initial_deposit_usd"] == "1000.0"   # 真實 ledger deposit 加總
    assert meth["start_equity_usd"] == "1000"        # av[0]（同一次 portfolio 回應）
    assert meth["end_equity_usd"] == "1200"          # av[-1]
    assert set(meth) == {"initial_deposit_usd", "start_equity_usd", "end_equity_usd",
                         "basis", "updated_at", "mdd_note"}


def test_ledger_deposit_failure_degrades_to_null_not_503(tmp_path):
    """真實入金查詢（`hl.non_funding_ledger_updates`）上游失敗，是與 portfolio／
    account_value 都不同的第三個來源（工程原則 1）——它失敗只降級
    `initial_deposit_usd`，equity/metrics/account_value 照樣可用。"""
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    hl.portfolios[_A] = sixty_day_rows()
    hl.ledger_updates_error[_A] = ConnectionError("hl 5xx")
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["methodology"]["initial_deposit_usd"] is None
    assert body["methodology"]["start_equity_usd"] == "1000"
    assert body["windows"]["allTime"]["pnl_usd"] == 200.0


def test_account_value_failure_degrades_to_null_not_503(tmp_path):
    """account_value 來自 clearinghouseState，是與 portfolio 不同的來源
    （工程原則 1：不得混進同一個對比）——它失敗只降級該欄位，equity/metrics
    照樣可用。"""
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    hl.portfolios[_A] = sixty_day_rows()
    hl.clearinghouse_error[_A] = ConnectionError("hl 5xx")
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_value"] is None
    assert body["windows"]["allTime"]["pnl_usd"] == 200.0


def test_no_perf_still_200_with_all_windows_none(tmp_path):
    """FakeHL 預設對未塞資料的地址回空 portfolio 清單（不是失敗）——不 503，
    四窗全 None、metrics 全 insufficient（沿 `/api/public/strategies/{slug}`
    的既有降級精神）。"""
    app, *_ = make_app(tmp_path)
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["windows"] == {"day": None, "week": None, "month": None, "allTime": None}
    assert body["metrics"]["allTime"]["total_return_pct_insufficient"] is True
    assert body["live_days"] == 0
    assert body["exposure"] is None
    assert body["fills_30d"]["order_count"] == 0


# ============================================================
# R4-11：sample_days／sample_threshold／cagr_pct（與策略詳情頁共用
# `strategies.build_cagr_fields`，見 tests/test_public_strategies.py 同款）
# ============================================================

def test_sample_days_and_cagr_present_when_sample_days_at_threshold(tmp_path):
    """`sixty_day_rows()` → covered_days=60 ≥ 30 門檻：`sample_days`／
    `sample_threshold` 恆回傳，`cagr_pct` 鍵存在。"""
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    hl.portfolios[_A] = sixty_day_rows()
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_days"] == 60
    assert body["sample_threshold"] == 30
    assert "cagr_pct" in body
    assert Decimal(body["cagr_pct"]) > 0


def test_cagr_pct_absent_when_sample_days_below_threshold(tmp_path):
    """涵蓋天數不足 30 天：`sample_days`／`sample_threshold` 照常回傳，
    `cagr_pct` 鍵整個不存在（結構性防呆，與策略詳情頁同一份組裝規則）。"""
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    ten_day_rows = _portfolio_rows(
        [[0, "1000"], [10 * _DAY_MS, "1050"]],
        [[0, "0"], [10 * _DAY_MS, "50"]])
    hl.portfolios[_A] = ten_day_rows
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_days"] == 10
    assert body["sample_threshold"] == 30
    assert "cagr_pct" not in body


def test_sample_days_zero_and_cagr_absent_when_no_perf(tmp_path):
    """查無 portfolio 資料（FakeHL 預設回空清單）：`sample_days` 降級為 0，
    `cagr_pct` 一樣不存在。"""
    app, *_ = make_app(tmp_path)
    r = _client(app).get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_days"] == 0
    assert body["sample_threshold"] == 30
    assert "cagr_pct" not in body


def test_case_insensitive_address_normalized_to_lowercase(tmp_path):
    app, *_ = make_app(tmp_path)
    mixed_case = "0x" + "A1" * 20
    r = _client(app).get(f"/api/public/traders/{mixed_case}")
    assert r.status_code == 200, r.text
    assert r.json()["address"] == mixed_case.lower()


class _CountingHL(FakeHL):
    def __init__(self):
        super().__init__()
        self.portfolio_calls = 0

    def portfolio(self, address: str) -> list:
        self.portfolio_calls += 1
        return super().portfolio(address)


def test_upstream_portfolio_called_once_within_5min_cache_window(tmp_path):
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = _CountingHL()
    hl.portfolios[_A] = sixty_day_rows()
    clock = {"t": 1_000_000.0}
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    assert c.get(f"/api/public/traders/{_A}").status_code == 200
    assert hl.portfolio_calls == 1

    clock["t"] += 200.0  # 仍在 300s 窗內
    assert c.get(f"/api/public/traders/{_A}").status_code == 200
    assert hl.portfolio_calls == 1

    clock["t"] += 101.0  # 累計 301s，超過 TTL
    assert c.get(f"/api/public/traders/{_A}").status_code == 200
    assert hl.portfolio_calls == 2


def test_cache_evicts_oldest_when_over_256_address_cap(tmp_path):
    """上限 256 個地址（防濫用）：第 257 個新地址進來時，淘汰最舊一筆而不是
    無上限成長。

    ⭐ [8b-3] 2026-08-29 二輪複審 Warning：舊版本靠「再查一次、斷言是否重新打
    上游」間接推論淘汰是否發生——複審 mutation 實測證明這是空斷言：查了 257
    個 distinct 位址、時鐘總共前進超過 15,000s（遠超過 300s 的 TTL），所以
    `addrs[0]` 再查一次本來就會因為 **TTL 過期**而重打上游，跟「有沒有守住
    256 上限」完全無關——把 `TRADER_PORTFOLIO_CACHE_MAX` 改成 999999 這個測試
    照樣通過。改法：直接 introspect `app.state.trader_portfolio_cache`（沿
    `probe_ratelimit_hits` 的既有唯讀 seam 模式）斷言 dict 大小與內容，
    不靠時鐘去間接推論。

    （時鐘仍需要前進，但只是為了跳出 [C1] 的 per-client rate limit 視窗，
    讓 257 個 distinct 位址都能被查到——與本測試要驗證的淘汰行為無關。）"""
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = _CountingHL()
    clock = {"t": 1_000_000.0}
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    addrs = ["0x" + format(i, "040x") for i in range(257)]
    for a in addrs:
        hl.portfolios[a] = sixty_day_rows()
        r = c.get(f"/api/public/traders/{a}")
        assert r.status_code == 200, r.text
        clock["t"] += 61.0  # 跳出限流視窗，與快取淘汰無關

    cache = app.state.trader_portfolio_cache
    assert len(cache) == 256, "快取 dict 大小必須守住 256 上限，不得無界成長"
    assert addrs[0] not in cache, "最舊的位址必須被淘汰"
    assert addrs[-1] in cache, "最新的位址必須還在快取裡"
    # 253 個之前的、緊接著 addrs[0] 被淘汰的那個也理應在（LRU＝依插入時間淘汰
    # 最舊的一筆，不是隨機淘汰或全清空）。
    assert addrs[1] in cache


# ============================================================
# [C1] per-client rate limit ＋ portfolio 失敗負面快取
# ============================================================

def test_rate_limit_blocks_after_max_requests_from_same_client(tmp_path):
    """同一個 client（同一個 TestClient＝同一個 host）在窗內查超過上限個
    distinct 位址 → 第 11 次 429，不再放行去打上游。"""
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = _CountingHL()
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: 1_000_000.0)
    c = _client(app)

    addrs = ["0x" + format(i, "040x") for i in range(11)]
    for a in addrs[:10]:
        hl.portfolios[a] = sixty_day_rows()
        assert c.get(f"/api/public/traders/{a}").status_code == 200
    assert hl.portfolio_calls == 10

    hl.portfolios[addrs[10]] = sixty_day_rows()
    r = c.get(f"/api/public/traders/{addrs[10]}")
    assert r.status_code == 429
    assert hl.portfolio_calls == 10  # 第 11 次被擋在打上游之前


def test_rate_limit_does_not_consume_quota_on_cache_hit(tmp_path):
    """快取命中（同一位址、TTL 內）不消耗限流額度——反覆查同一個已快取的位址
    不會把額度用光。"""
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = _CountingHL()
    hl.portfolios[_A] = sixty_day_rows()
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: 1_000_000.0)
    c = _client(app)

    for _ in range(20):
        assert c.get(f"/api/public/traders/{_A}").status_code == 200
    assert hl.portfolio_calls == 1  # 只有第一次真的打了上游，其餘全是快取命中


def test_portfolio_failure_negative_cache_short_circuits_repeat_upstream_hits(tmp_path):
    """[C1] 同一個壞地址（上游持續失敗）在負面快取 TTL（60s）內重查，不應
    重新打一次上游——直接短路回 503。"""
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = _CountingHL()
    hl.portfolio_error[_A] = ConnectionError("hl 5xx")
    clock = {"t": 1_000_000.0}
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    assert c.get(f"/api/public/traders/{_A}").status_code == 503
    assert hl.portfolio_calls == 1

    clock["t"] += 30.0  # 仍在 60s 負面快取窗內
    assert c.get(f"/api/public/traders/{_A}").status_code == 503
    assert hl.portfolio_calls == 1  # 短路，沒有再打一次上游

    clock["t"] += 31.0  # 累計 61s，超過負面快取 TTL
    del hl.portfolio_error[_A]
    hl.portfolios[_A] = sixty_day_rows()
    assert c.get(f"/api/public/traders/{_A}").status_code == 200
    assert hl.portfolio_calls == 2


# ============================================================
# [W4] follow_blocked：已撤銷 leader 不該在交易員頁看到跟單 CTA
# ============================================================

def _make_with_leaders(tmp_path, entries):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    return _client(app), hl


def test_follow_blocked_true_when_curated_entry_disabled(tmp_path):
    """精選白名單裡把該位址標成 enabled=false（安全撤銷）→ follow_blocked=true。"""
    c, hl = _make_with_leaders(
        tmp_path, [{"address": _A, "name": "Alpha", "enabled": False}])
    hl.portfolios[_A] = sixty_day_rows()
    r = c.get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    assert r.json()["follow_blocked"] is True


def test_follow_blocked_false_when_curated_entry_enabled(tmp_path):
    c, hl = _make_with_leaders(
        tmp_path, [{"address": _A, "name": "Alpha", "enabled": True}])
    hl.portfolios[_A] = sixty_day_rows()
    r = c.get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    assert r.json()["follow_blocked"] is False


def test_follow_blocked_false_when_address_not_in_any_whitelist(tmp_path):
    """不在精選白名單也不在 user registry 的全新位址：不受管轄，不算「被撤銷」，
    follow_blocked=false（能不能被自訂路徑准入是另一件事，由 select 端點把關）。"""
    c, hl = _make_with_leaders(tmp_path, [])
    hl.portfolios[_A] = sixty_day_rows()
    r = c.get(f"/api/public/traders/{_A}")
    assert r.status_code == 200, r.text
    assert r.json()["follow_blocked"] is False


# ============================================================
# 2026-09-05（Task 4，trader_stats 指標統一）：詳情端點與 explore 同源
# ============================================================

def _load_0x6648_portfolio():
    return json.loads((_FIXTURES / "trader_stats_0x6648_portfolio.json").read_text())


def _load_0x6648_fills():
    return json.loads((_FIXTURES / "trader_stats_0x6648_fills30d.json").read_text())["fills"]


def _seed_0x6648(hl):
    """假 HL 回真實 0x6648 fixture（portfolio／fills）＋一個 accountValue=0
    無持倉的 clearinghouseState（plan Task 4 Step 1 明訂的 fixture 建法）。"""
    hl.portfolios[_TRADER_ADDR] = _load_0x6648_portfolio()
    hl.fills_raw[_TRADER_ADDR] = _load_0x6648_fills()
    hl.clearinghouse[_TRADER_ADDR] = {"marginSummary": {"accountValue": "0.0"},
                                      "assetPositions": []}


def test_trader_detail_shape_matches_explore_windows(tmp_path):
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    _seed_0x6648(hl)
    r = _client(app).get(f"/api/public/traders/{_TRADER_ADDR}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"address", "account_value", "follow_blocked", "live_days", "exposure",
                         "windows", "fills_30d", "methodology", "metrics",
                         "sample_days", "sample_threshold"}
    assert set(body["windows"]) == {"day", "week", "month", "allTime"}
    assert set(body["metrics"]) == {"day", "week", "month", "allTime"}
    # month/allTime 兩窗在 0x6648 上都被閘門判無效 → 該窗 metrics 全部 insufficient
    assert body["metrics"]["month"]["sharpe_insufficient"] is True
    assert body["metrics"]["month"]["sharpe"] is None
    assert body["metrics"]["allTime"]["total_return_pct_insufficient"] is True
    # day 窗 perf ok，但 covered_days < 30 → 比率型指標仍標不足（RATIO_MIN_DAYS）
    assert body["metrics"]["day"]["sharpe_insufficient"] is True
    assert body["metrics"]["day"]["win_rate_pct"] is not None      # N>=1 即存在，不設閘
    assert body["sample_days"] == 0 and "cagr_pct" not in body      # allTime 無效 → 無 CAGR
    m = body["windows"]["month"]
    assert m["pnl_usd"] == 33055.26 and m["max_dd_pct"] is None \
        and m["max_dd_reason"] == "too_many_skipped_intervals" and len(m["spark"]) == 30
    assert body["windows"]["day"]["max_dd_pct"] == pytest.approx(-74.07, abs=0.01)
    assert body["live_days"] == 1003
    f = body["fills_30d"]
    assert (f["order_count"], f["closed_positions"], f["wins"], f["win_rate_pct"],
            f["realized_pnl_usd"], f["truncated"]) == (221, 27, 15, 55.56, 40225.79, False)
    assert set(body["methodology"]) == {"basis", "updated_at", "start_equity_usd",
                                        "end_equity_usd", "initial_deposit_usd", "mdd_note"}
    assert body["methodology"]["basis"] == "combined"
    assert "equity_index" not in body


def test_trader_detail_and_explore_row_agree_on_same_address(tmp_path):
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    _seed_0x6648(hl)
    detail = _client(app).get(f"/api/public/traders/{_TRADER_ADDR}").json()

    portfolio_raw = _load_0x6648_portfolio()
    fills = _load_0x6648_fills()
    ch_state = hl.clearinghouse[_TRADER_ADDR]
    row = hl_explore.enrich_candidate(_TRADER_ADDR, None, portfolio_raw, fills, ch_state,
                                      fills_truncated=False).to_dict()

    for w in ("day", "week", "month", "allTime"):
        assert detail["windows"][w] == row["windows"][w]
    assert detail["live_days"] == row["live_days"]
    assert detail["fills_30d"]["order_count"] == row["order_count_30d"]
    assert detail["fills_30d"]["closed_positions"] == row["closed_positions_30d"]
    assert detail["fills_30d"]["win_rate_pct"] == row["close_win_rate_pct"]
    assert detail["fills_30d"]["realized_pnl_usd"] == row["realized_pnl_30d_usd"]
