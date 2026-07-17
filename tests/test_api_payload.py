"""tests/test_api_payload.py
payload 端點：動態 chainId、agentName、builder 門檻擋。後端無 submit 端點
（前端直送 HL，見計畫設計定案 1）。"""
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


def _setup(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.account_values[BUILDER.lower()] = Decimal("150")  # builder 門檻達標
    return client, wallet, store, hl


def test_payload_agent_requires_generated_agent(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    r = client.post("/api/onboard/payload/approve-agent", json={"chain_id": 42161})
    assert r.status_code == 409


def test_payload_agent_typed_data(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    r = client.post("/api/onboard/payload/approve-agent", json={"chain_id": 42161})
    assert r.status_code == 200
    td = r.json()["typed_data"]
    assert td["domain"]["chainId"] == 42161      # 動態 chainId（research 風險 1）
    assert td["primaryType"] == "HyperliquidTransaction:ApproveAgent"
    assert td["message"]["agentAddress"] == agent
    assert td["message"]["agentName"] == "filet"
    assert td["message"]["hyperliquidChain"] == "Testnet"
    assert td["message"]["signatureChainId"] == "0xa4b1"  # 動態取自前端錢包


def test_payload_builder_fee_typed_data(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    r = client.post("/api/onboard/payload/approve-builder-fee", json={"chain_id": 1})
    assert r.status_code == 200
    td = r.json()["typed_data"]
    assert td["domain"]["chainId"] == 1
    assert td["primaryType"] == "HyperliquidTransaction:ApproveBuilderFee"
    assert td["message"]["builder"] == BUILDER
    assert td["message"]["maxFeeRate"] == "0.1%"


def test_payload_builder_fee_blocked_when_builder_underfunded(tmp_path):
    """spec 錯誤處理：builder 地址 < 100 USDC → builder code 不生效（症狀：成交但
    fee 不累計）——產 payload 時就大聲擋下（沿 M1 BuilderNotEligible 語意）。"""
    client, wallet, store, hl = _setup(tmp_path)
    hl.account_values[BUILDER.lower()] = Decimal("50")
    r = client.post("/api/onboard/payload/approve-builder-fee", json={"chain_id": 1})
    assert r.status_code == 503


def test_payload_bad_chain_id_400(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    client.post("/api/onboard/agent")
    r = client.post("/api/onboard/payload/approve-agent", json={"chain_id": 0})
    assert r.status_code == 400


def test_payload_fresh_nonce_each_call(tmp_path):
    """每次呼叫產新 nonce（now_ms）——斷點續走重取 payload 拿到新鮮 nonce，
    舊 typed data 作廢即可（nonce 窗口寬，未簽的舊 payload 無風險）。"""
    client, wallet, store, hl = _setup(tmp_path)
    client.post("/api/onboard/agent")
    n1 = client.post("/api/onboard/payload/approve-agent",
                     json={"chain_id": 1}).json()["typed_data"]["message"]["nonce"]
    import time
    time.sleep(0.002)
    n2 = client.post("/api/onboard/payload/approve-agent",
                     json={"chain_id": 1}).json()["typed_data"]["message"]["nonce"]
    assert n2 >= n1  # 毫秒 timestamp 單調不減


def test_payload_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    for path in ("/api/onboard/payload/approve-agent",
                 "/api/onboard/payload/approve-builder-fee"):
        assert client.post(path, json={"chain_id": 1}).status_code == 401


def test_no_submit_endpoints_exist(tmp_path):
    """紅線 5：後端沒有任何收簽名的 submit 端點（前端直送 HL）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    for path in ("/api/onboard/submit/approve-agent",
                 "/api/onboard/submit/approve-builder-fee"):
        assert client.post(path, json={"r": "0x1", "s": "0x2", "v": 27}).status_code == 404
