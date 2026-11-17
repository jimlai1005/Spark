"""tests/test_equity_curve_ts.py — plan 2026-11-17-perf-followups-and-build-swap §1 A1。

曲線時間戳（E1）與區間報酬樣本數 N（E2）的後端守護測試。期望值由 plan 錨例 B1 手算釘死：
A2 序列（plan 2026-11-16）AV=[1000,1500,3000,2200]、P=[0,100,300,500]，每 10 天一點 →
`sample_ts_ms == (0, 864000000, 1728000000, 2592000000)`、取樣點 4、區間數 3。
"""
import json
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from spark.filet.leader_perf import compute_window_performance, jsonable_performance
from spark.filet.strategies import (build_equity_index, build_equity_index_ts,
                                    build_metrics)
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket

DAY_MS = 86_400_000
_A = "0x" + "a1" * 20


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _rows(points, period="allTime"):
    av = [[d * DAY_MS, str(a)] for d, a, _ in points]
    pnl = [[d * DAY_MS, str(p)] for d, _, p in points]
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl, "vlm": "1"}]]


def _a2_rows():
    return _rows([(0, "1000", "0"), (10, "1500", "100"),
                  (20, "3000", "300"), (30, "2200", "500")])


# --------------------------------------------------------------------------
# 錨例 B1
# --------------------------------------------------------------------------
def test_b1_sample_ts_and_return_sample_count_anchor():
    perf = compute_window_performance(_a2_rows(), "allTime")
    assert perf["status"] == "ok"
    assert perf["sample_ts_ms"] == (0, 864000000, 1728000000, 2592000000)
    assert isinstance(perf["sample_ts_ms"], tuple)
    assert perf["return_sample_count"] == 3
    assert perf["sample_count"] == 4
    assert len(perf["sample_ts_ms"]) == len(perf["roi_index"]) == perf["sample_count"]


def test_insufficient_perf_has_no_ts_keys():
    perf = compute_window_performance(_rows([(0, "1000", "0")]), "allTime")
    assert perf["status"] == "insufficient"
    assert "sample_ts_ms" not in perf
    assert "return_sample_count" not in perf


def test_jsonable_performance_does_not_dump_sample_ts_by_default():
    perf = compute_window_performance(_a2_rows(), "allTime")
    slim = jsonable_performance(perf)
    assert "sample_ts_ms" not in slim
    assert slim["sample_ts_ms_len"] == 4
    assert slim["return_sample_count"] == 3
    json.dumps(slim)      # 不得有 tuple of Decimal 之類無法序列化的東西
    full = jsonable_performance(perf, include_equity_index=True)
    assert full["sample_ts_ms"] == [0, 864000000, 1728000000, 2592000000]
    json.dumps(full)


# --------------------------------------------------------------------------
# strategies 純函式
# --------------------------------------------------------------------------
def test_build_metrics_return_sample_count():
    perf = compute_window_performance(_a2_rows(), "allTime")
    m = build_metrics(perf)
    assert m["return_sample_count"] == 3
    assert m["sample_count"] == 4          # 既有鍵不變


@pytest.mark.parametrize("perf", [None, {"status": "insufficient", "sample_count": 1}])
def test_build_metrics_return_sample_count_zero_when_not_ok(perf):
    assert build_metrics(perf)["return_sample_count"] == 0


def test_build_equity_index_ts():
    perf = compute_window_performance(_a2_rows(), "allTime")
    assert build_equity_index_ts(perf) == [0, 864000000, 1728000000, 2592000000]
    assert len(build_equity_index_ts(perf)) == len(build_equity_index(perf))


@pytest.mark.parametrize("perf", [
    None, {"status": "insufficient"}, {"status": "ok"},   # 非 ok／缺鍵
])
def test_build_equity_index_ts_none_when_unavailable(perf):
    assert build_equity_index_ts(perf) is None


# --------------------------------------------------------------------------
# 端點：equity_index 與 equity_index_ts 同一個 perf、等長
# --------------------------------------------------------------------------
def _write_leaders(tmp_path) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": [{"address": _A, "name": "Alpha", "slug": "alpha"}]}))
    return str(p)


def test_strategy_detail_equity_index_ts_same_length_and_values(tmp_path):
    cfg = make_cfg(tmp_path, leaders_path=_write_leaders(tmp_path))
    app, _cfg, _store, _keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = _a2_rows()
    body = TestClient(app, base_url="https://testserver").get(
        "/api/public/strategies/alpha").json()
    assert len(body["equity_index"]) == 4
    assert body["equity_index_ts"] == [0, 864000000, 1728000000, 2592000000]
    assert len(body["equity_index_ts"]) == len(body["equity_index"])
    assert body["metrics"]["return_sample_count"] == 3
    assert body["metrics"]["sample_count"] == 4
    assert Decimal(body["equity_index"][-1]) > 1


def test_strategy_detail_no_perf_equity_index_ts_is_null(tmp_path):
    cfg = make_cfg(tmp_path, leaders_path=_write_leaders(tmp_path))
    app, *_ = make_app(tmp_path, cfg=cfg)
    body = TestClient(app, base_url="https://testserver").get(
        "/api/public/strategies/alpha").json()
    assert body["equity_index"] == []
    assert body["equity_index_ts"] is None
    assert body["metrics"]["return_sample_count"] == 0


def test_trader_detail_metrics_carry_return_sample_count_without_equity_index(tmp_path):
    """交易員詳情端點本就不輸出 `equity_index`（由 `windows[w].spark` 取代），
    故不新增 `equity_index_ts`；`metrics[window].return_sample_count` 由 build_metrics 供給。"""
    cfg = make_cfg(tmp_path)
    app, _cfg, _store, _keysvc, hl = make_app(tmp_path, cfg=cfg)
    hl.portfolios[_A] = _a2_rows()
    body = TestClient(app, base_url="https://testserver").get(
        f"/api/public/traders/{_A}").json()
    assert "equity_index" not in body
    assert "equity_index_ts" not in body
    assert body["metrics"]["allTime"]["return_sample_count"] == 3
    assert body["metrics"]["allTime"]["sample_count"] == 4
