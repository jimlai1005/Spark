"""tests/test_api_verify_admin.py
verify（READY → 寫 pending）＋ pending.json 讀寫 ＋ admin 白名單。"""
import socket
from decimal import Decimal

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.publicapi.pending import load_pending, remove_pending_entry, write_pending_entry
from tests.publicapi_helpers import BUILDER, login, make_app, make_cfg

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


# --- pending.py 單元測試 ---

def _entry(acct="f" + "ab" * 20):
    return dict(account_id=acct, user_address="0x" + "ab" * 20,
                builder_address=BUILDER, network="testnet",
                agent_address="0x" + "cd" * 20)


def test_write_pending_idempotent(tmp_path):
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry())
    write_pending_entry(p, **_entry())  # 同 account 再寫 → no-op
    assert len(load_pending(p)) == 1


def test_write_pending_validates(tmp_path):
    p = tmp_path / "pending.json"
    with pytest.raises(ValueError):
        write_pending_entry(p, **{**_entry(), "account_id": "../evil"})
    with pytest.raises(ValueError):
        write_pending_entry(p, **{**_entry(), "network": "devnet"})
    assert load_pending(p) == []


def test_remove_pending(tmp_path):
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry())
    remove_pending_entry(p, _entry()["account_id"])
    assert load_pending(p) == []


# --- pending 條目攜帶客戶選的 leader（P3.1b）---

_LEADER = "0x" + "d4" * 20


def test_write_pending_carries_leader(tmp_path):
    """客戶在 web 層選的 leader 要能寫進佇列——activate CLI 讀的就是這個鍵。"""
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry(), leader_address=_LEADER)
    assert load_pending(p)[0]["leader_address"] == _LEADER


def test_write_pending_normalizes_leader_case(tmp_path):
    """位址大小寫不敏感；寫入即正規化小寫（同基準比較，工程原則 1）。"""
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry(), leader_address=_LEADER.upper().replace("0X", "0x"))
    assert load_pending(p)[0]["leader_address"] == _LEADER


@pytest.mark.parametrize("absent", [None, ""])
def test_write_pending_without_leader_omits_key(tmp_path, absent):
    """未指定 → **不長出該鍵**（向後相容：舊條目缺鍵＝引擎沿用 env 預設）。
    空字串與 None 同義，與 followers._parse_leader 的「空即未指定」慣例一致。"""
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry(), leader_address=absent)
    assert "leader_address" not in load_pending(p)[0]


@pytest.mark.parametrize("bad", ["0xshort", "0x" + "z" * 40, "not-an-address"])
def test_write_pending_rejects_malformed_leader(tmp_path, bad):
    """只驗格式（白名單硬閘在 activate CLI 與引擎側，見 write_pending_entry
    docstring 的分工論證）。壞格式一律拒絕，且佇列不被碰。"""
    p = tmp_path / "pending.json"
    with pytest.raises(ValueError):
        write_pending_entry(p, **_entry(), leader_address=bad)
    assert load_pending(p) == []


# --- verify 端點 ---

def _make_ready(client, hl, wallet):
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    hl.max_fees[(wallet.address.lower(), BUILDER.lower())] = 100
    hl.agents[wallet.address.lower()] = [agent]
    hl.account_values[wallet.address.lower()] = Decimal("150")
    return agent


def test_verify_not_ready_no_pending(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    login(client)
    r = client.post("/api/onboard/verify")
    assert r.status_code == 200
    assert r.json()["state"] == "IN_PROGRESS"  # 斷點續走：回哪些檢查沒過
    assert load_pending(cfg.pending_path) == []


def test_verify_ready_writes_pending_bound_to_session(tmp_path):
    """⭐ 紅線 6：user_address 綁 session、builder_address 是伺服器常數。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = _make_ready(client, hl, wallet)
    r = client.post("/api/onboard/verify")
    assert r.status_code == 200 and r.json()["state"] == "READY"
    entries = load_pending(cfg.pending_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["account_id"] == "f" + wallet.address.lower()[2:]
    assert e["user_address"] == wallet.address.lower()   # 出自 session，非請求輸入
    assert e["builder_address"] == BUILDER               # 伺服器常數
    assert e["network"] == "testnet"
    assert e["agent_address"] == agent
    # 重呼冪等：仍只有一條
    client.post("/api/onboard/verify")
    assert len(load_pending(cfg.pending_path)) == 1


def test_admin_pending_403_for_non_admin(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.get("/api/admin/pending").status_code == 403


def test_admin_pending_ok_for_whitelisted(tmp_path):
    admin_wallet = Account.create()
    cfg = make_cfg(tmp_path,
                   admin_addresses=frozenset({admin_wallet.address.lower()}))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=admin_wallet)
    r = client.get("/api/admin/pending")
    assert r.status_code == 200
    assert r.json() == {"pending": []}


def test_admin_pending_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).get("/api/admin/pending").status_code == 401
