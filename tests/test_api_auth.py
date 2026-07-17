"""tests/test_api_auth.py
SIWE 登入流程：nonce → 簽 → verify → session cookie。真密碼學、fake 外部依賴。"""
import socket

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from tests.publicapi_helpers import login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（keysvc 慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair（self-pipe，不出網）。
    HL/keysvc 全為注入 fake——測試內無任何可觸網的程式路徑。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")  # secure cookie 需 https scheme


def test_login_then_me(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["address"] == wallet.address.lower()
    assert r.json()["account_id"] == "f" + wallet.address.lower()[2:]


def test_me_without_session_401(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).get("/api/me").status_code == 401


def test_verify_wrong_signer_401(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    a, b = Account.create(), Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": a.address, "chain_id": 1}).json()
    sig = b.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 401
    assert client.get("/api/me").status_code == 401


def test_garbage_signature_401(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    a = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": a.address, "chain_id": 1}).json()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": "0xdeadbeef"})
    assert r.status_code == 401


def test_nonce_single_use_replay_401(tmp_path):
    """⭐ nonce 單次使用：同一 nonce+有效簽名重放 → 401（防有效期內重放）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    wallet = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": wallet.address, "chain_id": 1}).json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    first = client.post("/api/auth/verify",
                        json={"nonce": body["nonce"], "signature": sig})
    assert first.status_code == 200
    replay = client.post("/api/auth/verify",
                         json={"nonce": body["nonce"], "signature": sig})
    assert replay.status_code == 401


def test_expired_nonce_401(tmp_path):
    cfg = make_cfg(tmp_path, nonce_ttl_s=0)  # 立即過期
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    wallet = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": wallet.address, "chain_id": 1}).json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 401


def test_session_cookie_attributes(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    # login 內的 verify 回應設 cookie；重打一次取原始 header 驗屬性
    wallet = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": wallet.address, "chain_id": 1}).json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie


def test_logout_clears_session(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.get("/api/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401


def test_nonce_bad_address_400(tmp_path):
    app, *_ = make_app(tmp_path)
    r = _client(app).get("/api/auth/nonce", params={"address": "nope", "chain_id": 1})
    assert r.status_code == 400
