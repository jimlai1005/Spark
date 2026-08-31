"""tests/test_public_benchmarks.py — `GET /api/public/benchmarks`（issue log I-19：
EquityCurve 疊加對照）。

四個外部標的（BTC/ETH/S&P500/黃金）日線收盤序列，無需登入。純函式部分
（`clamp_days`／`_fetch_series`／`BenchmarksCache`）直接單元測試；端點測試只盯：
形狀、cache 命中（依 days 分桶）、單一標的失敗降級為 null 不拖累其餘/整頁、
`days` 夾取。全離線（FakeHL 假 K 線資料，見 tests/publicapi_helpers.py）。
"""
import socket

import pytest
from fastapi.testclient import TestClient

from spark.publicapi import benchmarks
from spark.publicapi.app import create_app
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import FakeHL, FakeKeysvc, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（沿既有慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def _candles(pairs):
    """`pairs`：`[(epoch_ms, close_str), ...]` → 真實 candleSnapshot 回應形狀的
    最小子集（本模組只讀 `t`／`c`，其餘欄位塞佔位值）。"""
    return [{"t": t, "T": t + 86_400_000 - 1, "s": "X", "i": "1d",
             "o": c, "c": c, "h": c, "l": c, "v": "0", "n": 0}
            for t, c in pairs]


# ============================================================
# 純函式：clamp_days
# ============================================================

def test_clamp_days_within_range_unchanged():
    assert benchmarks.clamp_days(30) == 30


def test_clamp_days_clamps_below_min():
    assert benchmarks.clamp_days(0) == benchmarks.MIN_DAYS
    assert benchmarks.clamp_days(-5) == benchmarks.MIN_DAYS


def test_clamp_days_clamps_above_max():
    assert benchmarks.clamp_days(9999) == benchmarks.MAX_DAYS


def test_clamp_days_default_when_none_or_non_int():
    assert benchmarks.clamp_days(None) == benchmarks.DEFAULT_DAYS


# ============================================================
# 純函式：BenchmarksCache（依 days 分桶）
# ============================================================

def test_cache_hits_within_ttl_and_recomputes_after_expiry():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"n": calls["n"]}

    cache = benchmarks.BenchmarksCache(now_fn=lambda: clock["t"], ttl_s=600.0)
    assert cache.get(90, compute) == {"n": 1}
    clock["t"] += 300.0
    assert cache.get(90, compute) == {"n": 1}   # 仍在窗內，快取命中
    clock["t"] += 301.0
    assert cache.get(90, compute) == {"n": 2}   # 累計 601s，重新計算


def test_cache_buckets_are_independent_per_days_value():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"n": calls["n"]}

    cache = benchmarks.BenchmarksCache(now_fn=lambda: clock["t"], ttl_s=600.0)
    assert cache.get(30, compute) == {"n": 1}
    assert cache.get(90, compute) == {"n": 2}   # 不同 days → 各自獨立計算一次
    assert cache.get(30, compute) == {"n": 1}   # 30 的桶仍在窗內


# ============================================================
# 端點：GET /api/public/benchmarks
# ============================================================

def test_shape_and_all_four_series_populated(tmp_path):
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    hl.candles["BTC"] = _candles([(1_000_000_000_000, "50000")])
    hl.candles["ETH"] = _candles([(1_000_000_000_000, "3000")])
    hl.candles["xyz:SP500"] = _candles([(1_000_000_000_000, "5000")])
    hl.candles["xyz:GOLD"] = _candles([(1_000_000_000_000, "2000")])
    r = _client(app).get("/api/public/benchmarks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"series", "updated_at"}
    assert set(body["series"]) == {"btc", "eth", "sp500", "gold"}
    assert body["series"]["btc"] == [[1_000_000_000_000, "50000"]]
    assert body["series"]["eth"] == [[1_000_000_000_000, "3000"]]
    assert body["series"]["sp500"] == [[1_000_000_000_000, "5000"]]
    assert body["series"]["gold"] == [[1_000_000_000_000, "2000"]]
    assert r.cookies.get("filet_session") is None   # 無需登入，無 cookie 副作用


def test_single_instrument_failure_degrades_to_null_others_unaffected(tmp_path):
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    hl.candles["BTC"] = _candles([(1_000_000_000_000, "50000")])
    hl.candle_error["ETH"] = ConnectionError("hl 5xx")
    r = _client(app).get("/api/public/benchmarks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["series"]["btc"] == [[1_000_000_000_000, "50000"]]
    assert body["series"]["eth"] is None
    assert body["series"]["sp500"] == []   # 未塞 fixture → FakeHL 預設空清單（非失敗）
    assert body["series"]["gold"] == []


def test_all_sources_fail_endpoint_still_200_not_500(tmp_path):
    app, cfg2, store, keysvc, hl = make_app(tmp_path)
    for coin in benchmarks.BENCHMARK_COINS.values():
        hl.candle_error[coin] = RuntimeError("unexpected")
    r = _client(app).get("/api/public/benchmarks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(v is None for v in body["series"].values())


def test_days_query_param_out_of_range_clamped_not_422(tmp_path):
    app, *_ = make_app(tmp_path)
    r = _client(app).get("/api/public/benchmarks?days=99999")
    assert r.status_code == 200, r.text   # 夾取，不是驗證錯誤


def test_upstream_called_once_within_600s_cache_window_for_same_days(tmp_path):
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()

    class _CountingHL(FakeHL):
        def __init__(self):
            super().__init__()
            self.candle_calls = 0

        def candle_snapshot(self, coin, interval, start_ms, end_ms):
            self.candle_calls += 1
            return super().candle_snapshot(coin, interval, start_ms, end_ms)

    hl = _CountingHL()
    hl.candles["BTC"] = _candles([(1_000_000_000_000, "50000")])
    clock = {"t": 1_000_000.0}
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: clock["t"])
    c = _client(app)

    assert c.get("/api/public/benchmarks").status_code == 200
    assert hl.candle_calls == 4   # 四個標的各查一次

    clock["t"] += 300.0
    assert c.get("/api/public/benchmarks").status_code == 200
    assert hl.candle_calls == 4   # 仍在 600s 窗內，快取命中，不重打上游

    clock["t"] += 301.0
    assert c.get("/api/public/benchmarks").status_code == 200
    assert hl.candle_calls == 8   # 累計 601s，重新查詢
