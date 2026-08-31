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
    assert body["truncated"] is False
    # I-18：fills 非空 → 不觸發空態的「最近一筆」回溯查詢，維持 None。
    assert body["last_fill_time"] is None


def test_fills_ignores_days_param_uses_fixed_30d_window(tmp_path):
    """I-18 使用者裁決：`/api/me/fills` 改固定 30 天窗，`days` 參數保留但被
    忽略——任何 `days` 值（含超出舊版 1~90 合法範圍的值）都不再 422，且上游
    查詢視窗恆為 30 天。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()

    windows = []
    orig = hl.get_fills_detail_paged

    def counting(address, start, end, **kw):
        windows.append((end - start).days)
        return orig(address, start, end, **kw)
    hl.get_fills_detail_paged = counting
    hl.fills_detail[addr] = FILLS_SAMPLE

    assert c.get("/api/me/fills", params={"days": 0}).status_code == 200
    assert c.get("/api/me/fills", params={"days": 91}).status_code == 200
    assert windows == [30]  # 第二次命中同一格 60s TTL 快取（key 已收斂回純 addr），未重打上游


def test_fills_upstream_failure_is_503_not_db_fallback(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    hl.fills_detail_error[wallet.address.lower()] = ConnectionError("hl 5xx")
    r = c.get("/api/me/fills")
    assert r.status_code == 503


def test_fills_cache_key_is_address_only_now_days_is_ignored(tmp_path):
    """I-18：視窗固定後快取鍵收斂回純 `addr`——同一地址不論 `days` 傳什麼值，
    TTL 內都命中同一格快取，只打一次上游（取代舊版 `(addr, days)` 快取鍵測試，
    見 issue log I-18「`days` 參數收斂或忽略」裁決）。"""
    clock = {"t": 1_000_000.0}
    app, cfg, store, keysvc, hl = make_app(tmp_path, now_fn=lambda: clock["t"])
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()

    calls = {"n": 0}
    orig = hl.get_fills_detail_paged

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    hl.get_fills_detail_paged = counting
    hl.fills_detail[addr] = FILLS_SAMPLE

    assert c.get("/api/me/fills", params={"days": 7}).status_code == 200
    assert calls["n"] == 1
    assert c.get("/api/me/fills", params={"days": 30}).status_code == 200
    assert calls["n"] == 1  # 不同 days 值，仍撞同一格快取（days 已被忽略）


def test_fills_cached_within_60s_ttl(tmp_path):
    clock = {"t": 1_000_000.0}
    app, cfg, store, keysvc, hl = make_app(tmp_path, now_fn=lambda: clock["t"])
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()

    calls = {"n": 0}
    orig = hl.get_fills_detail_paged

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    hl.get_fills_detail_paged = counting
    hl.fills_detail[addr] = FILLS_SAMPLE

    assert c.get("/api/me/fills").status_code == 200
    assert calls["n"] == 1
    clock["t"] += 30.0
    assert c.get("/api/me/fills").status_code == 200
    assert calls["n"] == 1  # 仍在 60s 窗內，未重打上游
    clock["t"] += 31.0
    assert c.get("/api/me/fills").status_code == 200
    assert calls["n"] == 2  # 超過 TTL，重打


def test_fills_truncated_flag_propagates_from_gateway(tmp_path):
    """I-18：`hl.get_fills_detail_paged` 回報 `truncated=True`（迴圈防炸上限
    仍在時）要如實透到 API 回應，不得被端點層吞掉或改寫。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    wallet = login(c)
    addr = wallet.address.lower()
    hl.fills_detail[addr] = FILLS_SAMPLE
    hl.get_fills_detail_paged = lambda address, start, end, **kw: (FILLS_SAMPLE, True)
    r = c.get("/api/me/fills")
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] is True


def test_fills_empty_window_looks_back_for_last_fill_time(tmp_path):
    """I-18：30 天窗零筆時，額外查一次有界回溯窗取最近一筆成交時間
    （`last_fill_time`），供前端空態文案分辨「近 30 天沒有」與「完全沒有」。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    login(c)
    # 主查詢（近 30 天）維持空清單（hl.fills_detail 預設 []）；回溯窗查詢注入一筆。

    def fallback(address, start, end):
        if (end - start).days <= 30:
            return []
        return [{"time": 1_700_000_000_000, "coin": "ETH", "side": "B", "px": "1",
                 "sz": "1", "fee": "0", "closed_pnl": "0", "hash": "0xabc"}]
    hl.get_fills_detail = fallback
    r = c.get("/api/me/fills")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fills"] == []
    assert body["last_fill_time"] == 1_700_000_000_000


def test_fills_empty_window_no_history_at_all_last_fill_time_is_none(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c = _client(app)
    login(c)
    r = c.get("/api/me/fills")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fills"] == []
    assert body["last_fill_time"] is None


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
