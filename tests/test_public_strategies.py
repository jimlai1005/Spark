"""tests/test_public_strategies.py — /api/public/strategies*（策略平台 Task 5）。

無需登入的公開端點：策略＝精選白名單條目＋展示欄位＋由 leader_perf 算出的統計指標。
純函式部分（`build_metrics`／`build_strategy_view`）直接單元測試計算與門檻邏輯；
端點測試只盯：形狀、enabled:false 隱藏、404、follower_count 聚合與 null 降級、
不外流 follower 個資、60s 快取命中。全離線（HL/manifest 皆為假資料/tmp 檔）。
"""
import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from spark.filet.leaders import LeaderRef
from spark.filet.strategies import build_metrics, build_strategy_view
from spark.publicapi.app import create_app
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import FakeHL, FakeKeysvc, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（沿既有慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair；HL/manifest 全為假資料。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


_A = "0x" + "a1" * 20
_B = "0x" + "b2" * 20

_DAY_MS = 86400000


def write_leaders(tmp_path, entries) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def write_followers(tmp_path, entries) -> str:
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": entries}))
    return str(p)


def follower(account_id, leader_address):
    return {"account_id": account_id,
           "user_address": "0x" + format(hash(account_id) & 0xff, "02x") * 20,
           "builder_address": "0x" + "b1" * 20, "network": "testnet",
           "leader_address": leader_address}


def _portfolio_rows(av, pnl, period="perpAllTime"):
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "0"}]]


def sixty_day_rows(start_av="1000", end_av="1200"):
    """跨 60 整天、雙點序列：twr=0.2、equity_index=(1, 1.2)、無回撤。"""
    t = 60 * _DAY_MS
    delta = str(Decimal(end_av) - Decimal(start_av))
    return _portfolio_rows([[0, start_av], [t, end_av]], [[0, "0"], [t, delta]])


# ============================================================
# 純函式：build_metrics（perf → 策略卡 metrics 子物件）
# ============================================================

def _ok_perf(**over):
    base = {
        "status": "ok", "sample_count": 38, "covered_days": Decimal("72.0000"),
        "first_ts_ms": 0, "last_ts_ms": 72 * _DAY_MS,
        "twr": Decimal("0.2035"), "twr_insufficient_data": False,
        "max_drawdown": Decimal("0.008"), "max_drawdown_insufficient_data": False,
        "sharpe": Decimal("10.24"), "sharpe_se": Decimal("3.36"),
        "sharpe_insufficient_data": False,
        "win_rate": Decimal("0.6486"),
        "annualized_vol": Decimal("0.1805"), "annualized_vol_insufficient_data": False,
        "sortino": Decimal("43.42"), "sortino_insufficient_data": False,
        "best_day_return": Decimal("0.0301"), "worst_day_return": Decimal("-0.008"),
        "equity_index": (Decimal("1"), Decimal("1.2")),
    }
    base.update(over)
    return base


def test_build_metrics_shape_and_values():
    m = build_metrics(_ok_perf())
    assert m["total_return_pct"] == "20.35"
    assert m["total_return_pct_insufficient"] is False
    assert m["max_drawdown_pct"] == "-0.80"          # 取負號呈現回撤方向
    assert m["max_drawdown_pct_insufficient"] is False
    assert m["sharpe"] == "10.24"
    assert m["sharpe_se"] == "3.36"
    assert m["win_rate_pct"] == "64.86"
    assert m["annualized_vol_pct"] == "18.05"
    assert m["sortino"] == "43.42"
    assert m["best_day_pct"] == "3.01"
    assert m["worst_day_pct"] == "-0.80"
    assert m["sample_count"] == 38


def test_build_metrics_insufficient_flag_nulls_value_and_shares_across_sharpe_se():
    """`sharpe_insufficient_data` 同時管住 `sharpe` 與 `sharpe_se`（leader_perf 的
    既有契約：兩者共用同一個不足旗標，不是各自獨立）。"""
    m = build_metrics(_ok_perf(sharpe_insufficient_data=True))
    assert m["sharpe"] is None and m["sharpe_insufficient"] is True
    assert m["sharpe_se"] is None and m["sharpe_se_insufficient"] is True


def test_build_metrics_missing_value_key_is_insufficient():
    """數學上算不出來（perf 字典裡整個沒有這個鍵）＝insufficient，不是 500 或 KeyError。"""
    perf = _ok_perf()
    del perf["sortino"]
    m = build_metrics(perf)
    assert m["sortino"] is None and m["sortino_insufficient"] is True


def test_build_metrics_win_rate_best_worst_never_gated():
    """勝率／最佳最差日不設任何充足度閘（plan 明載）：只要 perf ok 且鍵存在就給值。"""
    m = build_metrics(_ok_perf(covered_days=Decimal("1.0000")))
    assert m["win_rate_pct"] == "64.86" and m["win_rate_pct_insufficient"] is False
    assert m["best_day_pct"] == "3.01" and m["best_day_pct_insufficient"] is False


def test_build_metrics_no_perf_is_all_insufficient():
    m = build_metrics(None)
    assert m["sample_count"] == 0
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe", "sharpe_se",
               "win_rate_pct", "annualized_vol_pct", "sortino",
               "best_day_pct", "worst_day_pct"):
        assert m[key] is None
        assert m[f"{key}_insufficient"] is True


def test_build_metrics_status_insufficient_is_all_insufficient():
    m = build_metrics({"status": "insufficient", "sample_count": 1})
    assert m["total_return_pct"] is None
    assert m["sample_count"] == 1


# ============================================================
# 純函式：build_strategy_view（listable＝enabled∧accepting_new、slug 回退、
# status 投影）——2026-08-29 裁決移除 60 天涵蓋天數閘門，見模組檔頭。
# ============================================================

def _entry(**over):
    base = dict(address=_A, name="Alpha", enabled=True, accepting_new=True)
    base.update(over)
    return LeaderRef(**base)


def test_listable_true_at_58_days_when_accepting_new():
    """曾經卡在 60 天閘門下的 58 天樣本：裁決後 accepting_new 就 listable。"""
    view = build_strategy_view(_entry(), _ok_perf(covered_days=Decimal("58.0000")))
    assert view["listable"] is True
    assert view["live_days"] == 58


def test_listable_false_when_not_accepting_new_even_with_enough_days():
    view = build_strategy_view(_entry(accepting_new=False),
                               _ok_perf(covered_days=Decimal("365.0000")))
    assert view["listable"] is False
    assert view["status"] == "paused"


def test_status_running_when_accepting_new():
    view = build_strategy_view(_entry(), _ok_perf())
    assert view["status"] == "running"


def test_slug_falls_back_to_address_when_absent():
    view = build_strategy_view(_entry(slug=None), _ok_perf())
    assert view["slug"] == _A


def test_slug_used_when_present():
    view = build_strategy_view(_entry(slug="core"), _ok_perf())
    assert view["slug"] == "core"


def test_no_perf_at_all_gives_zero_live_days_but_still_listable():
    """沒有 perf 資料不影響 listable（只受 enabled/accepting_new 控制）；
    live_days 純展示，缺資料時降級為 0。"""
    view = build_strategy_view(_entry(), None)
    assert view["live_days"] == 0
    assert view["listable"] is True


def test_view_never_includes_follower_count_key():
    """純函式結構性保證：follower_count 需要跨客戶 IO，不歸這裡管
    （由呼叫端在拿到這個 dict 之後才併入）。"""
    view = build_strategy_view(_entry(), _ok_perf())
    assert "follower_count" not in view


# ============================================================
# 端點：GET /api/public/strategies（無需登入）
# ============================================================

def test_list_no_auth_required_and_shape(tmp_path):
    cfg = make_cfg(
        tmp_path,
        leaders_path=write_leaders(tmp_path, [
            {"address": _A, "name": "Alpha", "slug": "alpha", "tagline": "動能策略",
             "featured": True, "min_notional_usd": "500", "max_leverage": "3"}]),
        followers_path=str(tmp_path / "nope.json"))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = sixty_day_rows()
    r = _client(app).get("/api/public/strategies")   # 未登入、無 cookie
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"strategies", "updated_at"}
    row = body["strategies"][0]
    assert row["slug"] == "alpha"
    assert row["name"] == "Alpha"
    assert row["tagline"] == "動能策略"
    assert row["featured"] is True
    assert row["leader_address"] == _A
    assert row["status"] == "running"
    assert row["listable"] is True
    assert row["live_days"] == 60
    assert row["min_notional_usd"] == "500"
    assert row["max_leverage"] == "3"
    assert row["follower_count"] is None   # manifest 指向不存在的檔
    metrics_keys = {"total_return_pct", "total_return_pct_insufficient",
                    "max_drawdown_pct", "max_drawdown_pct_insufficient",
                    "sharpe", "sharpe_insufficient",
                    "sharpe_se", "sharpe_se_insufficient",
                    "win_rate_pct", "win_rate_pct_insufficient",
                    "annualized_vol_pct", "annualized_vol_pct_insufficient",
                    "sortino", "sortino_insufficient",
                    "best_day_pct", "best_day_pct_insufficient",
                    "worst_day_pct", "worst_day_pct_insufficient",
                    "sample_count"}
    assert set(row["metrics"]) == metrics_keys
    assert r.cookies.get("filet_session") is None      # 無 cookie 副作用


def test_listable_true_below_60_days_when_accepting_new(tmp_path):
    """2026-08-29 裁決移除 60 天閘門：涵蓋天數不足不再擋 listable，只要
    enabled 且 accepting_new。live_days 純展示，照樣如實反映樣本天數。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    t = 10 * _DAY_MS
    hl.portfolios[_A] = _portfolio_rows([[0, "1000"], [t, "1050"]],
                                        [[0, "0"], [t, "50"]])
    body = _client(app).get("/api/public/strategies").json()
    row = body["strategies"][0]
    assert row["slug"] == "alpha"
    assert row["listable"] is True
    assert row["live_days"] == 10


def test_listable_false_when_accepting_new_false(tmp_path):
    """accepting_new=False 仍然是唯一能讓 listable 翻假的旗標。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha", "accepting_new": False}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = sixty_day_rows()
    body = _client(app).get("/api/public/strategies").json()
    row = body["strategies"][0]
    assert row["listable"] is False
    assert row["status"] == "paused"


def test_enabled_false_hidden_from_list(tmp_path):
    """⭐ enabled=false（安全撤銷）：連 slug／address 都不得出現在回應任何角落。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"},
        {"address": _B, "name": "Beta", "slug": "beta", "enabled": False}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    r = _client(app).get("/api/public/strategies")
    assert [s["slug"] for s in r.json()["strategies"]] == ["alpha"]
    assert _B not in r.text and "Beta" not in r.text and "beta" not in r.text


# ============================================================
# 端點：GET /api/public/strategies/{slug}
# ============================================================

def test_detail_404_for_unknown_slug(tmp_path):
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    r = _client(app).get("/api/public/strategies/does-not-exist")
    assert r.status_code == 404


def test_detail_404_for_disabled_slug(tmp_path):
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _B, "name": "Beta", "slug": "beta", "enabled": False}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    r = _client(app).get("/api/public/strategies/beta")
    assert r.status_code == 404


def test_detail_shape_includes_equity_index_and_methodology(tmp_path):
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = sixty_day_rows()
    r = _client(app).get("/api/public/strategies/alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "alpha"
    assert body["equity_index"] == ["1", "1.2"]
    meth = body["methodology"]
    assert set(meth) == {"start_date", "end_date", "initial_deposit_usd",
                         "sample_count", "annualization_days", "risk_free_rate",
                         "basis", "updated_at"}
    assert meth["annualization_days"] == 365
    assert meth["risk_free_rate"] == "0"
    assert meth["basis"] == "perp"
    assert meth["initial_deposit_usd"] == "1000"     # av[0]（同一次 portfolio 回應）
    assert meth["start_date"] == "1970-01-01"
    expected_end = datetime.fromtimestamp(60 * 86400, tz=timezone.utc).date().isoformat()
    assert meth["end_date"] == expected_end


def test_detail_no_perf_still_200_with_empty_equity_index(tmp_path):
    """上游沒有這個位址的資料（FakeHL 預設回空清單）→ 不 500，equity_index 空陣列。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    r = _client(app).get("/api/public/strategies/alpha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["equity_index"] == []
    assert body["methodology"]["initial_deposit_usd"] is None
    assert body["listable"] is True   # 缺 perf 不再擋 listable（僅 enabled/accepting_new）


# ============================================================
# follower_count：聚合與 null 降級
# ============================================================

def test_follower_count_aggregated_per_leader(tmp_path):
    cfg = make_cfg(
        tmp_path,
        leaders_path=write_leaders(tmp_path, [
            {"address": _A, "name": "Alpha", "slug": "alpha"},
            {"address": _B, "name": "Beta", "slug": "beta"}]),
        followers_path=write_followers(tmp_path, [
            follower("u1", _A), follower("u2", _A), follower("u3", _B)]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = _client(app).get("/api/public/strategies").json()
    counts = {s["slug"]: s["follower_count"] for s in body["strategies"]}
    assert counts == {"alpha": 2, "beta": 1}


def test_follower_count_null_when_manifest_missing(tmp_path):
    """資料源不可用 → null（不是 0——0 是「有資料、剛好零個」的不同陳述）。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]),
        followers_path=str(tmp_path / "nope.json"))
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = _client(app).get("/api/public/strategies").json()
    assert body["strategies"][0]["follower_count"] is None


def test_follower_count_zero_when_manifest_has_no_matching_follower(tmp_path):
    cfg = make_cfg(
        tmp_path,
        leaders_path=write_leaders(tmp_path, [
            {"address": _A, "name": "Alpha", "slug": "alpha"}]),
        followers_path=write_followers(tmp_path, []))
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = _client(app).get("/api/public/strategies").json()
    assert body["strategies"][0]["follower_count"] == 0


# ============================================================
# 不變量 0.3.4：/api/public/* 不得洩漏 follower 個資
# ============================================================

def test_response_never_contains_follower_identifying_fields(tmp_path):
    """結構斷言：list／detail 回應鍵集裡不得出現任何 follower 識別欄位，
    且 follower 的位址／account_id 字面值不得出現在回應文字裡（不變量 4）。"""
    follower_user_addr = "0x" + "99" * 20
    cfg = make_cfg(
        tmp_path,
        leaders_path=write_leaders(tmp_path, [
            {"address": _A, "name": "Alpha", "slug": "alpha"}]),
        followers_path=write_followers(tmp_path, [
            {"account_id": "secret-follower-1", "user_address": follower_user_addr,
             "builder_address": "0x" + "b1" * 20, "network": "testnet",
             "leader_address": _A}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    list_r = c.get("/api/public/strategies")
    detail_r = c.get("/api/public/strategies/alpha")
    for r in (list_r, detail_r):
        assert r.status_code == 200, r.text
        assert follower_user_addr not in r.text
        assert "secret-follower-1" not in r.text
        assert "user_address" not in r.text
        assert "account_id" not in r.text
    # follower_count 是唯一允許外流的聚合數字，且是整數
    row = list_r.json()["strategies"][0]
    assert row["follower_count"] == 1
    assert isinstance(row["follower_count"], int)


# ============================================================
# 60s in-process cache：上游只在 TTL 外被重新呼叫
# ============================================================

class _CountingHL(FakeHL):
    """鏡像 FakeHL，額外記錄 portfolio() 被呼叫幾次（快取命中測試用）。"""

    def __init__(self):
        super().__init__()
        self.portfolio_calls = 0

    def portfolio(self, address: str) -> list:
        self.portfolio_calls += 1
        return super().portfolio(address)


def test_upstream_portfolio_called_once_within_60s_cache_window(tmp_path):
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = _CountingHL()
    hl.portfolios[_A] = sixty_day_rows()
    clock = {"t": 1_000_000.0}
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    assert c.get("/api/public/strategies").status_code == 200
    assert hl.portfolio_calls == 1

    clock["t"] += 30.0                      # 仍在 60s 窗內
    assert c.get("/api/public/strategies").status_code == 200
    assert hl.portfolio_calls == 1          # 快取命中，未再打上游

    clock["t"] += 31.0                      # 累計 61s，超過 TTL
    assert c.get("/api/public/strategies").status_code == 200
    assert hl.portfolio_calls == 2          # 重新查詢


def test_list_and_detail_share_same_metrics_and_as_of(tmp_path):
    """D5 數字一致性：同一個 60s 快取窗內，列表卡與詳情頁的 `metrics`／`as_of`
    必須逐欄相等——兩端點共用同一支 `_strategy_perf_with_as_of` 餵的同一份
    perf 快照，不是各自重算或各自重新觸網。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    hl = FakeHL()
    hl.portfolios[_A] = sixty_day_rows()
    clock = {"t": 1_000_000.0}
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    row = c.get("/api/public/strategies").json()["strategies"][0]
    detail = c.get("/api/public/strategies/alpha").json()

    assert row["as_of"] is not None
    assert row["as_of"] == detail["as_of"] == int(clock["t"])
    assert row["metrics"] == detail["metrics"]


def test_list_as_of_null_when_upstream_fetch_fails(tmp_path):
    """上游 portfolio 查詢直接失敗（連 rows 都拿不到）→ 沒有任何快照可言，
    `as_of` 誠實回 null——與「查到了、但資料不足以算出 window」（as_of 仍有效，
    我們確實在那個時間點查過）是不同的處境，不可疊成同一個 null。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolio_error[_A] = ConnectionError("hl 5xx")
    body = _client(app).get("/api/public/strategies").json()
    assert body["strategies"][0]["as_of"] is None


def test_list_as_of_present_even_when_upstream_returns_empty(tmp_path):
    """上游查詢**成功**但回空清單（真實情境：位址存在、尚無 perp 活動）→
    perf 仍為 insufficient，但 `as_of` 照樣回真正的查詢時間戳——我們確實在
    那個時間點查過，只是沒查到足以算出 window 的資料。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = _client(app).get("/api/public/strategies").json()
    assert body["strategies"][0]["as_of"] is not None


# ============================================================
# 詳情頁：sample_days／sample_threshold／cagr_pct 結構性 gating（Task 3）
# ============================================================

def test_detail_sample_days_and_cagr_present_at_threshold(tmp_path):
    """恰好 60 天（`CAGR_SAMPLE_THRESHOLD_DAYS`）：`sample_days`／
    `sample_threshold` 恆回傳，`cagr_pct` 因為門檻已達而出現。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = sixty_day_rows()
    body = _client(app).get("/api/public/strategies/alpha").json()
    assert body["sample_days"] == 60
    assert body["sample_threshold"] == 60
    assert "cagr_pct" in body
    assert Decimal(body["cagr_pct"]) > 0


def test_detail_cagr_key_absent_below_threshold(tmp_path):
    """10 天樣本（< 60）：`cagr_pct` 鍵**整個不存在**（結構性防呆，不是
    存在但值為 null）——前端因此不必自己判斷門檻。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    t = 10 * _DAY_MS
    hl.portfolios[_A] = _portfolio_rows([[0, "1000"], [t, "1050"]],
                                        [[0, "0"], [t, "50"]])
    body = _client(app).get("/api/public/strategies/alpha").json()
    assert body["sample_days"] == 10
    assert body["sample_threshold"] == 60
    assert "cagr_pct" not in body


def test_detail_cagr_key_absent_when_no_perf(tmp_path):
    """沒有 perf（上游無資料）：`sample_days` 隨 `live_days` 降級為 0，
    `cagr_pct` 一樣不存在。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"}]))
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = _client(app).get("/api/public/strategies/alpha").json()
    assert body["sample_days"] == 0
    assert body["sample_threshold"] == 60
    assert "cagr_pct" not in body


# ============================================================
# 純函式：build_cagr_pct（filet.strategies，Task 3）
# ============================================================

def test_build_cagr_pct_uses_annualized_return_directly():
    """CAGR 直接取 leader_perf 算好的 annualized_return，不重算（同一支計算）。"""
    from spark.filet.strategies import build_cagr_pct
    m = build_cagr_pct(_ok_perf(annualized_return=Decimal("0.4523")))
    assert m == "45.23"


def test_build_cagr_pct_none_when_annualized_return_absent():
    """`annualized_return` 鍵不存在（帳戶歸零，數學上無定義）→ None。"""
    from spark.filet.strategies import build_cagr_pct
    assert build_cagr_pct(_ok_perf()) is None


def test_build_cagr_pct_none_when_perf_not_ok():
    from spark.filet.strategies import build_cagr_pct
    assert build_cagr_pct({"status": "insufficient"}) is None
    assert build_cagr_pct(None) is None


def test_upstream_failure_degrades_that_leader_not_whole_list(tmp_path):
    """上游查詢失敗（transient）→ 該策略的指標全 insufficient，其他策略／整個
    端點不受影響（不得 500/502——公開清單本身要比被監控的上游更可靠）。
    listable 不受 perf 缺席影響（2026-08-29 裁決僅看 enabled/accepting_new）。"""
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, [
        {"address": _A, "name": "Alpha", "slug": "alpha"},
        {"address": _B, "name": "Beta", "slug": "beta"}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolio_error[_A] = ConnectionError("hl 5xx")
    hl.portfolios[_B] = sixty_day_rows()
    r = _client(app).get("/api/public/strategies")
    assert r.status_code == 200, r.text
    by_slug = {s["slug"]: s for s in r.json()["strategies"]}
    assert by_slug["alpha"]["metrics"]["sharpe_insufficient"] is True
    assert by_slug["alpha"]["listable"] is True
    assert by_slug["beta"]["listable"] is True
