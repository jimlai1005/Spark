"""tests/test_public_stats.py — /api/public/stats、/api/public/status（策略平台 Task 6）。

無需登入的公開端點：`stats` 是首頁證據列（累計路由量/實盤天數/費率），`status`
是 footer／`/status` 頁的系統狀態燈。兩者共用同一種 60s in-process cache 機制。
純函式部分（`compute_routed_volume_usd_total`／`compute_live_days`／
`engine_component_status`／`overall_status`／`TTLCache`）直接單元測試；端點測試
只盯：形狀、cache 命中、heartbeat 過期/讀不到、資料源丟例外時不 500、不外流
follower 個資。全離線（HL/manifest 皆為假資料/tmp 檔）。
"""
import json
import os
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from spark.filet.leaders import LeaderRef
from spark.publicapi import public_stats
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
_DAY_MS = 86400000


def write_leaders(tmp_path, entries) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def _portfolio_rows(av, pnl, period="allTime"):
    # ⚠️ 2026-08-31 issue log I-15 使用者裁決：`live_days` 走 `_strategy_perf_for`
    # →「allTime」（spot+perp 合併窗，原 `perpAllTime`），見 app.py 同函式註解。
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "0"}]]


def sixty_day_rows(start_av="1000", end_av="1200"):
    """跨 60 整天、雙點序列 → covered_days == 60（沿 test_public_strategies.py 慣例）。"""
    t = 60 * _DAY_MS
    delta = str(Decimal(end_av) - Decimal(start_av))
    return _portfolio_rows([[0, start_av], [t, end_av]], [[0, "0"], [t, delta]])


def write_accrued_history(tmp_path, points) -> str:
    """`points`：`[(date_iso, accrued_str, captured_at_iso), ...]`（新格式，含
    `captured_at`）。回傳寫入的檔案路徑。"""
    p = tmp_path / "accrued_history.jsonl"
    lines = [json.dumps({"date": d, "accrued": a, "captured_at": cap})
            for d, a, cap in points]
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def write_heartbeat_file(exchange_dir, account_id: str, *, mtime: float | None = None):
    """在 heartbeat 目錄直接落一個空 `.json` 檔（本模組只讀 mtime，不解析內容）。"""
    hb_dir = exchange_dir / "engine" / "health"
    hb_dir.mkdir(parents=True, exist_ok=True)
    p = hb_dir / f"{account_id}.json"
    p.write_text("{}")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ============================================================
# 純函式：compute_routed_volume_usd_total
# ============================================================

def test_routed_volume_derives_from_latest_accrued_point_via_fixed_fee_rate(tmp_path):
    """volume = accrued_total / (BUILDER_FEE_BPS/10000)。BUILDER_FEE_BPS=2 → rate
    0.0002 → accrued 100 對應路由量 500000（整除的錨例，驗證公式本身）。"""
    path = write_accrued_history(tmp_path, [
        ("2026-08-01", "40", "2026-08-01T00:00:00+00:00"),
        ("2026-08-02", "100", "2026-08-02T00:00:00+00:00")])
    v = public_stats.compute_routed_volume_usd_total(path)
    assert v == Decimal("500000")


def test_routed_volume_uses_only_latest_point_not_delta(tmp_path):
    """⭐ 只取總量（最新一點），不是今昨差——與 /api/ops/revenue 的用法分家。"""
    path = write_accrued_history(tmp_path, [
        ("2026-08-01", "9999999", "2026-08-01T00:00:00+00:00"),
        ("2026-08-02", "40", "2026-08-02T00:00:00+00:00")])
    v = public_stats.compute_routed_volume_usd_total(path)
    assert v == Decimal("200000")   # 40 / 0.0002，不是 (40-9999999)/0.0002


def test_routed_volume_none_when_no_history(tmp_path):
    v = public_stats.compute_routed_volume_usd_total(str(tmp_path / "nope.jsonl"))
    assert v is None


def test_routed_volume_none_when_source_raises(tmp_path, monkeypatch):
    def _boom(_path):
        raise OSError("disk gone")
    monkeypatch.setattr(public_stats, "load_accrued_series", _boom)
    v = public_stats.compute_routed_volume_usd_total(str(tmp_path / "x.jsonl"))
    assert v is None


# ============================================================
# 純函式：compute_live_days
# ============================================================

def _entry(**over):
    base = dict(address=_A, name="Alpha", enabled=True, accepting_new=True)
    base.update(over)
    return LeaderRef(**base)


def test_live_days_from_featured_entry_covered_days():
    entries = [_entry(featured=False), _entry(address="0x" + "b2" * 20, featured=True)]

    def perf_for(addr):
        assert addr == "0x" + "b2" * 20   # 只查 featured 的位址
        return {"status": "ok", "covered_days": Decimal("72.0000")}

    assert public_stats.compute_live_days(entries, perf_for) == 72


def test_live_days_none_when_no_featured_entry():
    entries = [_entry(featured=False)]
    assert public_stats.compute_live_days(entries, lambda a: None) is None


def test_live_days_none_when_perf_missing_or_not_ok():
    entries = [_entry(featured=True)]
    assert public_stats.compute_live_days(entries, lambda a: None) is None
    assert public_stats.compute_live_days(
        entries, lambda a: {"status": "insufficient"}) is None


def test_live_days_none_when_perf_for_raises():
    entries = [_entry(featured=True)]

    def _boom(addr):
        raise ConnectionError("hl 5xx")
    assert public_stats.compute_live_days(entries, _boom) is None


# ============================================================
# 純函式：engine_component_status / overall_status
# ============================================================

def test_engine_status_ok_when_fresh(tmp_path):
    write_heartbeat_file(tmp_path, "acct-1", mtime=1_000_000.0)
    status = public_stats.engine_component_status(
        tmp_path / "engine" / "health", now_fn=lambda: 1_000_000.0 + 10)
    assert status == "ok"


def test_engine_status_degraded_when_stale(tmp_path):
    write_heartbeat_file(tmp_path, "acct-1", mtime=1_000_000.0)
    status = public_stats.engine_component_status(
        tmp_path / "engine" / "health",
        now_fn=lambda: 1_000_000.0 + public_stats.ENGINE_HEARTBEAT_STALE_S + 1)
    assert status == "degraded"


def test_engine_status_unknown_when_directory_missing(tmp_path):
    status = public_stats.engine_component_status(
        tmp_path / "engine" / "health", now_fn=lambda: 1_000_000.0)
    assert status == "unknown"


def test_engine_status_takes_newest_of_multiple_heartbeats(tmp_path):
    """多 follower 引擎：取最新一個 heartbeat 代表整體。"""
    write_heartbeat_file(tmp_path, "acct-old", mtime=1_000_000.0)
    write_heartbeat_file(tmp_path, "acct-new", mtime=1_000_500.0)
    now = 1_000_500.0 + 10
    status = public_stats.engine_component_status(
        tmp_path / "engine" / "health", now_fn=lambda: now)
    assert status == "ok"   # 最新的一個（+10s）仍新鮮，即使最舊的一個早已過期


def test_engine_status_unknown_when_source_raises(tmp_path, monkeypatch):
    def _boom(_dir):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(public_stats, "_newest_heartbeat_mtime", _boom)
    status = public_stats.engine_component_status(
        tmp_path / "engine" / "health", now_fn=lambda: 1_000_000.0)
    assert status == "unknown"


def test_overall_status_is_worst_component():
    assert public_stats.overall_status(
        [{"name": "api", "status": "ok"}, {"name": "engine", "status": "ok"}]) == "ok"
    assert public_stats.overall_status(
        [{"name": "api", "status": "ok"},
         {"name": "engine", "status": "degraded"}]) == "degraded"
    assert public_stats.overall_status(
        [{"name": "api", "status": "ok"},
         {"name": "engine", "status": "unknown"}]) == "unknown"


def test_overall_status_unknown_when_no_components():
    assert public_stats.overall_status([]) == "unknown"


# ============================================================
# 純函式：TTLCache
# ============================================================

def test_ttl_cache_hits_within_window_and_recomputes_after():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"n": calls["n"]}

    cache = public_stats.TTLCache(now_fn=lambda: clock["t"], ttl_s=60.0)
    assert cache.get(compute) == {"n": 1}
    clock["t"] += 30.0
    assert cache.get(compute) == {"n": 1}      # 仍在窗內，快取命中
    clock["t"] += 31.0
    assert cache.get(compute) == {"n": 2}      # 累計 61s，重新計算


# ============================================================
# 端點：GET /api/public/stats
# ============================================================

def test_stats_shape_no_auth_required(tmp_path):
    hist = write_accrued_history(tmp_path, [
        ("2026-08-27", "40", "2026-08-27T00:00:00+00:00"),
        ("2026-08-28", "856", "2026-08-28T00:00:00+00:00")])
    cfg = make_cfg(tmp_path, accrued_history_path=hist,
                   leaders_path=write_leaders(tmp_path, [
                       {"address": _A, "name": "Alpha", "featured": True}]))
    app, cfg2, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = sixty_day_rows()
    r = _client(app).get("/api/public/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"routed_volume_usd_total", "builder_fee_bps",
                         "live_days", "updated_at"}
    assert body["routed_volume_usd_total"] == "4280000"
    assert body["builder_fee_bps"] == 2
    assert body["live_days"] == 60
    assert r.cookies.get("filet_session") is None      # 無 cookie 副作用


def test_stats_all_fields_null_when_no_sources_available(tmp_path):
    cfg = make_cfg(tmp_path,
                   accrued_history_path=str(tmp_path / "nope.jsonl"),
                   leaders_path=write_leaders(tmp_path, []))
    app, *_ = make_app(tmp_path, cfg=cfg)
    r = _client(app).get("/api/public/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["routed_volume_usd_total"] is None
    assert body["live_days"] is None
    assert body["builder_fee_bps"] == 2   # 固定常數，不受資料源缺席影響


def test_stats_source_exception_degrades_to_null_not_500(tmp_path, monkeypatch):
    hist = write_accrued_history(tmp_path, [
        ("2026-08-28", "856", "2026-08-28T00:00:00+00:00")])
    cfg = make_cfg(tmp_path, accrued_history_path=hist,
                   leaders_path=write_leaders(tmp_path, []))
    app, *_ = make_app(tmp_path, cfg=cfg)

    def _boom(_path):
        raise OSError("disk gone")
    monkeypatch.setattr(public_stats, "load_accrued_series", _boom)
    r = _client(app).get("/api/public/stats")
    assert r.status_code == 200, r.text
    assert r.json()["routed_volume_usd_total"] is None


def test_stats_source_called_once_within_60s_cache_window(tmp_path, monkeypatch):
    hist = write_accrued_history(tmp_path, [
        ("2026-08-28", "40", "2026-08-28T00:00:00+00:00")])
    cfg = make_cfg(tmp_path, accrued_history_path=hist,
                   leaders_path=write_leaders(tmp_path, []))
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    clock = {"t": 1_000_000.0}
    calls = {"n": 0}
    real_load = public_stats.load_accrued_series

    def _counting_load(path):
        calls["n"] += 1
        return real_load(path)
    monkeypatch.setattr(public_stats, "load_accrued_series", _counting_load)
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    assert c.get("/api/public/stats").status_code == 200
    assert calls["n"] == 1

    clock["t"] += 30.0
    assert c.get("/api/public/stats").status_code == 200
    assert calls["n"] == 1      # 仍在 60s 窗內，快取命中

    clock["t"] += 31.0
    assert c.get("/api/public/stats").status_code == 200
    assert calls["n"] == 2      # 累計 61s，重新查詢


# ============================================================
# 端點：GET /api/public/status
# ============================================================

def test_status_shape_ok_when_heartbeat_fresh(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = {"t": 1_000_000.0}
    write_heartbeat_file(tmp_path / "exchange", "acct-1", mtime=clock["t"] - 5)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    r = _client(app).get("/api/public/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"status", "components", "updated_at"}
    assert body["status"] == "ok"
    assert body["components"] == [{"name": "api", "status": "ok"},
                                  {"name": "engine", "status": "ok"}]
    assert r.cookies.get("filet_session") is None


def test_status_degraded_when_heartbeat_stale(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = {"t": 1_000_000.0}
    stale_mtime = clock["t"] - public_stats.ENGINE_HEARTBEAT_STALE_S - 1
    write_heartbeat_file(tmp_path / "exchange", "acct-1", mtime=stale_mtime)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    body = _client(app).get("/api/public/status").json()
    assert body["status"] == "degraded"
    engine = next(c for c in body["components"] if c["name"] == "engine")
    assert engine["status"] == "degraded"


def test_status_unknown_when_heartbeat_dir_absent(tmp_path):
    cfg = make_cfg(tmp_path)   # exchange_dir 底下沒有任何 engine/health 目錄
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = _client(app).get("/api/public/status").json()
    assert body["status"] == "unknown"
    engine = next(c for c in body["components"] if c["name"] == "engine")
    assert engine["status"] == "unknown"


def test_status_source_exception_degrades_to_unknown_not_500(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    write_heartbeat_file(tmp_path / "exchange", "acct-1", mtime=1_000_000.0)
    app, *_ = make_app(tmp_path, cfg=cfg)

    def _boom(_dir):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(public_stats, "_newest_heartbeat_mtime", _boom)
    r = _client(app).get("/api/public/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "unknown"
    engine = next(c for c in body["components"] if c["name"] == "engine")
    assert engine["status"] == "unknown"


def test_status_source_called_once_within_60s_cache_window(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    write_heartbeat_file(tmp_path / "exchange", "acct-1", mtime=1_000_000.0)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    clock = {"t": 1_000_000.0}
    calls = {"n": 0}
    real_mtime = public_stats._newest_heartbeat_mtime

    def _counting_mtime(hb_dir):
        calls["n"] += 1
        return real_mtime(hb_dir)
    monkeypatch.setattr(public_stats, "_newest_heartbeat_mtime", _counting_mtime)
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    assert c.get("/api/public/status").status_code == 200
    assert calls["n"] == 1

    clock["t"] += 30.0
    assert c.get("/api/public/status").status_code == 200
    assert calls["n"] == 1      # 仍在 60s 窗內，快取命中

    clock["t"] += 31.0
    assert c.get("/api/public/status").status_code == 200
    assert calls["n"] == 2      # 累計 61s，重新查詢


# ============================================================
# 不變量 0.3.4：/api/public/* 不得洩漏 follower 個資
# ============================================================

def test_status_response_never_contains_heartbeat_account_id(tmp_path):
    """heartbeat 檔名就是 account_id——本端點只讀 mtime，回應裡不得出現檔名／
    account_id 字面值（不變量 4）。"""
    cfg = make_cfg(tmp_path)
    write_heartbeat_file(tmp_path / "exchange", "secret-follower-42", mtime=1_000_000.0)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: 1_000_005.0)
    r = _client(app).get("/api/public/status")
    assert r.status_code == 200, r.text
    assert "secret-follower-42" not in r.text
    assert "account_id" not in r.text


def test_stats_response_never_contains_follower_fields(tmp_path):
    hist = write_accrued_history(tmp_path, [
        ("2026-08-28", "40", "2026-08-28T00:00:00+00:00")])
    cfg = make_cfg(tmp_path, accrued_history_path=hist,
                   leaders_path=write_leaders(tmp_path, []))
    app, *_ = make_app(tmp_path, cfg=cfg)
    r = _client(app).get("/api/public/stats")
    assert "account_id" not in r.text and "user_address" not in r.text
