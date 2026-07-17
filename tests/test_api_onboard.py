"""tests/test_api_onboard.py"""
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.publicapi_helpers import BUILDER, login, make_app

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def test_generate_agent_returns_address(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    agent = r.json()["agent_address"]
    assert agent.startswith("0x") and len(agent) == 42 and agent == agent.lower()
    account_id = "f" + wallet.address.lower()[2:]
    assert keysvc.generated[account_id].lower() == agent
    assert store.get_agent_address(account_id) == agent


def test_generate_agent_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).post("/api/onboard/agent").status_code == 401


def test_generate_agent_twice_409(tmp_path):
    """防重生：已有 agent 拒絕 rotate（避免作廢既有鏈上授權，沿 M1 語意）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 200
    r = client.post("/api/onboard/agent")
    assert r.status_code == 409
    assert "不重生" in r.json()["detail"]


def test_keysvc_down_502(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    keysvc.fail = ConnectionRefusedError("keysvc down")
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 502


def test_desync_self_heals_via_address_op(tmp_path):
    """keysvc 有 key 但 DB 無地址（DB 遺失/回應遺失殘局）→ 唯讀 address op 自癒回填
    （設計定案 12），照常 200，回應帶 recovered=true 供觀測。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = "f" + wallet.address.lower()[2:]
    keysvc.generated[account_id] = "0x" + "EE" * 20  # 預塞：keystore 有、DB 無
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    assert r.json()["recovered"] is True
    assert r.json()["agent_address"] == "0x" + "ee" * 20   # normalize 後回填
    assert store.get_agent_address(account_id) == "0x" + "ee" * 20  # DB 已回填


def test_desync_and_address_also_fails_409(tmp_path):
    """自癒也失敗（address op 打不通）才 409，訊息明確要求人工介入。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = "f" + wallet.address.lower()[2:]
    keysvc.generated[account_id] = "0x" + "ee" * 20
    keysvc.address_fail = ConnectionRefusedError("keysvc down")
    r = client.post("/api/onboard/agent")
    assert r.status_code == 409
    assert "無法自動復原" in r.json()["detail"]
    assert store.get_agent_address(account_id) is None  # 未寫入半套狀態


def _make_ready(hl, wallet_addr: str, agent: str):
    hl.max_fees[(wallet_addr.lower(), BUILDER.lower())] = 100
    hl.agents[wallet_addr.lower()] = [agent]
    hl.account_values[wallet_addr.lower()] = Decimal("150")


def test_status_progresses_to_ready(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    s0 = client.get("/api/onboard/status").json()
    assert s0["agent_generated"] is False and s0["state"] == "IN_PROGRESS"
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    s1 = client.get("/api/onboard/status").json()
    assert s1["agent_generated"] and s1["builder_fee_approved"]
    assert s1["agent_approved"] and s1["funded"]
    assert s1["state"] == "READY"
    assert s1["agent_address"] == agent


def test_status_funding_below_floor_not_ready(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    hl.account_values[wallet.address.lower()] = Decimal("99")  # < 100 USDC 門檻
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is False and s["state"] == "IN_PROGRESS"


def test_status_isolated_between_users(tmp_path):
    """紅線 3：account 由 session 衍生——另一個使用者看不到、也影響不了你的進度。"""
    app, *_ = make_app(tmp_path)
    c1, c2 = _client(app), _client(app)
    login(c1)
    login(c2)
    c1.post("/api/onboard/agent")
    assert c2.get("/api/onboard/status").json()["agent_generated"] is False
