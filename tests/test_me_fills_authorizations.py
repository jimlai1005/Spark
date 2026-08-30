"""tests/test_me_fills_authorizations.py — GET /api/me/fills、GET /api/me/authorizations
（M3 round2 Task 7：Dashboard「成交記錄・授權歷程」tab，資料直取 Hyperliquid，
不讀自家 DB）。

fixture 用 2026-08-29 curl 對真實 HL info/explorer API 實測後裁剪的樣本
（tests/fixtures/hl_user_fills_sample.json、hl_explorer_user_details_sample.json）。
全離線（autouse socket-ban，見 conftest.py；FakeHL 全假資料，見 publicapi_helpers.py）。
"""
import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark.publicapi.app import filter_authorizations
from tests.publicapi_helpers import login, make_app

_REAL_SOCKET = socket.socket
_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def _load(name):
    return json.loads((_FIXTURES / name).read_text())


FILLS_SAMPLE = _load("hl_user_fills_sample.json")
EXPLORER_SAMPLE = _load("hl_explorer_user_details_sample.json")


# ---------- /api/me/fills ----------

def test_fills_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    r = _client(app).get("/api/me/fills")
    assert r.status_code == 401


def test_fills_returns_hl_detail_fields(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()
    hl.fills_detail[addr] = [{
        "time": 1774926504932, "coin": "ETH", "side": "B", "px": "2074.9",
        "sz": "41.4803", "fee": "-2.582024", "closed_pnl": "217.356772",
        "hash": "0x317e78012add56b532f80438128ac402033900e6c5d07587d5472353e9d1309f",
    }]
    r = c.get("/api/me/fills")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fills"] == hl.fills_detail[addr]


def test_fills_days_out_of_range_422(tmp_path):
    app, *_ = make_app(tmp_path)
    c = _client(app)
    login(c)
    assert c.get("/api/me/fills", params={"days": 0}).status_code == 422
    assert c.get("/api/me/fills", params={"days": 91}).status_code == 422
    assert c.get("/api/me/fills", params={"days": 90}).status_code == 200


def test_fills_upstream_failure_is_503_not_db_fallback(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    hl.fills_detail_error[wallet.address.lower()] = ConnectionError("hl 5xx")
    r = c.get("/api/me/fills")
    assert r.status_code == 503


def test_fills_cache_key_includes_days(tmp_path):
    """[W1] 2026-08-29 opus 審查：快取 key 若漏 `days`，切換天數會撞到同一格
    快取、回傳錯誤天數範圍的成交明細卻不報錯。同一地址切換 days 必須各自
    觸發一次上游查詢，回到同一個 days 才吃 TTL 內的快取。"""
    clock = {"t": 1_000_000.0}
    app, cfg, store, keysvc, hl = make_app(tmp_path, now_fn=lambda: clock["t"])
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()

    calls = {"n": 0, "days": []}
    orig = hl.get_fills_detail

    def counting(address, start, end):
        calls["n"] += 1
        calls["days"].append((end - start).days)
        return orig(address, start, end)
    hl.get_fills_detail = counting
    hl.fills_detail[addr] = FILLS_SAMPLE

    assert c.get("/api/me/fills", params={"days": 7}).status_code == 200
    assert calls["n"] == 1
    assert c.get("/api/me/fills", params={"days": 30}).status_code == 200
    assert calls["n"] == 2  # 不同 days，不得撞到同一格快取
    assert c.get("/api/me/fills", params={"days": 7}).status_code == 200
    assert calls["n"] == 2  # 回到 days=7，仍在 60s TTL 內，命中快取
    assert calls["days"] == [7, 30]


def test_fills_cached_within_60s_ttl(tmp_path):
    clock = {"t": 1_000_000.0}
    app, cfg, store, keysvc, hl = make_app(tmp_path, now_fn=lambda: clock["t"])
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()

    calls = {"n": 0}
    orig = hl.get_fills_detail

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    hl.get_fills_detail = counting
    hl.fills_detail[addr] = FILLS_SAMPLE

    assert c.get("/api/me/fills").status_code == 200
    assert calls["n"] == 1
    clock["t"] += 30.0
    assert c.get("/api/me/fills").status_code == 200
    assert calls["n"] == 1  # 仍在 60s 窗內，未重打上游
    clock["t"] += 31.0
    assert c.get("/api/me/fills").status_code == 200
    assert calls["n"] == 2  # 超過 TTL，重打


# ---------- /api/me/authorizations ----------

def test_authorizations_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    r = _client(app).get("/api/me/authorizations")
    assert r.status_code == 401


def test_authorizations_filters_and_sorts_real_sample(tmp_path):
    """真實 explorer 樣本裡混了 approveAgent／approveBuilderFee／order 三種
    action——只有前兩種留下，且按時間降冪（樣本裡 approveAgent 時間戳較晚）。

    ⭐ [W2] 2026-08-29 opus 審查修正：後端不再組中文摘要字串（`summary`），
    改回結構化欄位（`agent_address`／`builder`／`max_fee_rate`），組字移到
    前端 `copy.ts`（ZH/EN 對稱）。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    hl.user_details_payload[wallet.address.lower()] = EXPLORER_SAMPLE
    r = c.get("/api/me/authorizations")
    assert r.status_code == 200, r.text
    rows = r.json()["authorizations"]
    assert [row["action_type"] for row in rows] == ["approveAgent", "approveBuilderFee"]
    assert rows[0]["time"] == 1787752386163
    assert rows[0]["hash"] == ("0x78421cd43cf39c2079bb04430552e5020c4600b9d7"
                               "f6baf21c0ac826fbf7760b")
    assert rows[0]["agent_address"] == "0xaf2292a19d2b144f17115be0775851cd878ef72c"
    assert rows[0]["builder"] is None
    assert rows[0]["max_fee_rate"] is None
    assert rows[1]["agent_address"] is None
    assert rows[1]["builder"] == "0x5af1b5f44207784dcb850bbb4143c5dcd1885f71"
    assert rows[1]["max_fee_rate"] == "0.095%"


def test_authorizations_upstream_failure_is_503_not_db_fallback(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    hl.user_details_error[wallet.address.lower()] = ConnectionError("explorer 5xx")
    r = c.get("/api/me/authorizations")
    assert r.status_code == 503


def test_authorizations_cached_within_60s_ttl(tmp_path):
    clock = {"t": 1_000_000.0}
    app, cfg, store, keysvc, hl = make_app(tmp_path, now_fn=lambda: clock["t"])
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()

    calls = {"n": 0}
    orig = hl.user_details

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    hl.user_details = counting
    hl.user_details_payload[addr] = EXPLORER_SAMPLE

    assert c.get("/api/me/authorizations").status_code == 200
    assert calls["n"] == 1
    clock["t"] += 59.0
    assert c.get("/api/me/authorizations").status_code == 200
    assert calls["n"] == 1
    clock["t"] += 2.0
    assert c.get("/api/me/authorizations").status_code == 200
    assert calls["n"] == 2


def test_authorizations_limited_to_100_rows():
    """`filter_authorizations` 純函式錨例：超過 100 筆授權動作只留最新 100。"""
    txs = [{"time": i, "action": {"type": "approveAgent", "agentAddress": "0xabc"},
           "hash": f"0x{i:064x}"} for i in range(150)]
    out = filter_authorizations(txs, limit=100)
    assert len(out) == 100
    assert out[0]["time"] == 149  # 降冪排序，最新在前
    assert out[-1]["time"] == 50


def test_filter_authorizations_skips_malformed_entries():
    txs = [
        {"time": 5, "action": {"type": "approveAgent", "agentAddress": "0xabc"},
         "hash": "0x1"},
        {"time": 4, "action": {"type": "order"}, "hash": "0x2"},  # 非授權動作，過濾掉
        "not-a-dict",  # 形狀不符，跳過
        {"action": {"type": "approveAgent"}, "hash": "0x3"},  # 缺 time，跳過
    ]
    out = filter_authorizations(txs, limit=100)
    assert len(out) == 1
    assert out[0]["time"] == 5
