"""tests/test_api_referral.py
客戶簽章的 Hyperliquid 推薦碼 opt-in（2026-09-19，`referral_optin.py` 的 API 端點）：
`GET /api/me/referral`／`POST /api/me/referral/message`／`POST /api/me/referral`。

全離線（FakeKeysvc／FakeHL），SIWE 登入與 opt-in 簽章都用真密碼學。沿
`tests/test_api_risk_settings.py` 的形狀（risk_settings 的姊妹端點、姊妹測試檔）。

⭐ 本檔**不用** `tests/publicapi_helpers.make_app`（它不轉發 `referral_lookup`），
直接呼叫 `create_app` 組裝，才能把鏈上查詢 stub 注入 `GET /api/me/referral`。
"""
import socket

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.referral_optin import load_referral_optins
from spark.publicapi.app import create_app
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import FakeHL, FakeKeysvc, login, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（同 test_api_risk_settings）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 需要 loopback socket；外部網路仍由 conftest 的 autouse 斷網擋住。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _make_app(tmp_path, *, referral_code=None, referral_lookup=None):
    cfg = make_cfg(tmp_path, referral_code=referral_code)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    app = create_app(cfg, store, keysvc, hl, referral_lookup=referral_lookup)
    return app, cfg, store


@pytest.fixture
def client_wallet(tmp_path):
    """推薦功能已設定（`referral_code="FILET"`）的預設 fixture。"""
    app, cfg, _store = _make_app(tmp_path, referral_code="FILET")
    client = TestClient(app, base_url="https://testserver")  # secure cookie 需 https
    wallet = login(client)
    return client, cfg, wallet


def _get_message(client):
    return client.post("/api/me/referral/message")


def _submit(client, wallet, *, account_id=None, signer=None, tamper=None):
    """完整流程：取原文 → 簽 → POST。`tamper` 在簽完之後改 body。"""
    r = _get_message(client)
    assert r.status_code == 200, r.text
    m = r.json()
    sig = (signer or wallet).sign_message(
        encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": account_id or m["account_id"], "code": m["code"],
            "nonce": m["nonce"], "issued_at": m["issued_at"],
            "signature": sig, "message": m["message"]}
    if tamper:
        body.update(tamper)
    return client.post("/api/me/referral", json=body)


# ── 未設定：功能整體關閉 ──────────────────────────────────────────────

def test_message_503_when_not_configured(tmp_path):
    app, _cfg, _store = _make_app(tmp_path, referral_code=None)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    r = _get_message(client)
    assert r.status_code == 503


def test_get_status_disabled_when_not_configured(tmp_path):
    app, _cfg, _store = _make_app(tmp_path, referral_code=None)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    r = client.get("/api/me/referral")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["code"] is None
    assert body["signed"] is False


# ── 已設定：待簽原文 canonical 化 ─────────────────────────────────────

def test_message_returns_canonical_uppercase_code(tmp_path):
    """cfg 存的推薦碼即使不是大寫，端點回的 `code` 與原文一律 canonical 化
    （唯一定義在 `referral_optin.normalize_referral_code`）。"""
    app, _cfg, _store = _make_app(tmp_path, referral_code="filet")
    client = TestClient(app, base_url="https://testserver")
    login(client)
    r = _get_message(client)
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "FILET"
    assert "Referral Code: FILET" in body["message"]
    assert body["message"].splitlines()[0] == "Filet: set Hyperliquid referral code"


def test_get_status_enabled_shows_configured_code(client_wallet):
    client, _cfg, _wallet = client_wallet
    r = client.get("/api/me/referral")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["code"] == "FILET"
    assert body["signed"] is False
    assert body["signed_at"] is None


# ── 提交成功：落檔 + GET 反映 signed ─────────────────────────────────

def test_submit_success_persists_and_get_reflects_signed(client_wallet):
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["code"] == "FILET"
    assert body["effective"] == "next_engine_cycle"

    recs = load_referral_optins(cfg.referral_optin_path)
    assert len(recs) == 1
    assert recs[0]["account_id"] == body["account_id"]
    assert recs[0]["code"] == "FILET"

    status = client.get("/api/me/referral").json()
    assert status["signed"] is True
    assert status["signed_at"] == recs[0]["issued_at"]


# ── 驗簽失敗：nonce 重放 / 簽章者不符 / 帳號不符 ─────────────────────

def test_nonce_cannot_be_reused(client_wallet):
    """⭐ 同一份簽章只能兌現一次：原樣重送 → 400，且記錄不重複。"""
    client, cfg, wallet = client_wallet
    m = _get_message(client).json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": m["account_id"], "code": m["code"], "nonce": m["nonce"],
            "issued_at": m["issued_at"], "signature": sig, "message": m["message"]}
    assert client.post("/api/me/referral", json=body).status_code == 200
    second = client.post("/api/me/referral", json=body)
    assert second.status_code == 400
    assert second.json()["detail"] == "這份授權已被使用或已過期，請重新取得待簽原文並重簽"
    assert len(load_referral_optins(cfg.referral_optin_path)) == 1


def test_wrong_signer_is_rejected_with_400_and_not_persisted(client_wallet):
    """⭐⭐ 400 而非 401：session 有效，壞掉的是這一份請求內容；且不落檔。"""
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, signer=Account.create())
    assert r.status_code == 400
    assert r.json()["detail"] == "簽章者不是本帳號的持有人"
    assert load_referral_optins(cfg.referral_optin_path) == []


def test_account_mismatch_is_rejected_with_403(client_wallet):
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, account_id="f" + "11" * 20)
    assert r.status_code == 403
    assert load_referral_optins(cfg.referral_optin_path) == []


# ── onchain_code：注入 stub ──────────────────────────────────────────

def test_onchain_code_present_via_stub(tmp_path):
    app, _cfg, _store = _make_app(
        tmp_path, referral_code="FILET",
        referral_lookup=lambda addr: "FILET")
    client = TestClient(app, base_url="https://testserver")
    login(client)
    body = client.get("/api/me/referral").json()
    assert body["onchain_code"] == "FILET"
    assert body["onchain_error"] is False


def test_onchain_code_null_via_stub(tmp_path):
    app, _cfg, _store = _make_app(
        tmp_path, referral_code="FILET",
        referral_lookup=lambda addr: None)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    body = client.get("/api/me/referral").json()
    assert body["onchain_code"] is None
    assert body["onchain_error"] is False


def test_onchain_lookup_exception_sets_onchain_error(tmp_path):
    def _boom(addr):
        raise ConnectionError("上游逾時")

    app, _cfg, _store = _make_app(
        tmp_path, referral_code="FILET", referral_lookup=_boom)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    r = client.get("/api/me/referral")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["onchain_code"] is None
    assert body["onchain_error"] is True


def test_onchain_lookup_not_injected_sets_onchain_error(client_wallet):
    """未注入 `referral_lookup`（例如尚未接上真實查詢的部署）→ 降級成
    `onchain_error: true`，不 500。"""
    client, _cfg, _wallet = client_wallet
    body = client.get("/api/me/referral").json()
    assert body["onchain_code"] is None
    assert body["onchain_error"] is True


# ── W3（2026-09-19 審查修正）：鏈上查詢加進程內快取 ──────────────────

def test_onchain_lookup_cached_for_non_null_code(tmp_path):
    """非 None 的碼永久快取——兩次 GET 只打 stub 一次。"""
    calls = []

    def _lookup(addr):
        calls.append(addr)
        return "FILET"

    app, _cfg, _store = _make_app(
        tmp_path, referral_code="FILET", referral_lookup=_lookup)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    client.get("/api/me/referral")
    client.get("/api/me/referral")
    assert len(calls) == 1


def test_onchain_lookup_null_result_cached_then_reexpires(tmp_path):
    """None 結果只快取 `REFERRAL_ONCHAIN_NEG_TTL_S` 秒：TTL 內不重打，過了重打。"""
    calls = []
    now = {"t": 1_000_000.0}

    def _lookup(addr):
        calls.append(addr)
        return None

    cfg = make_cfg(tmp_path, referral_code="FILET")
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    app = create_app(cfg, store, keysvc, hl, now_fn=lambda: now["t"],
                     referral_lookup=_lookup)
    client = TestClient(app, base_url="https://testserver")
    login(client)

    client.get("/api/me/referral")
    assert len(calls) == 1
    client.get("/api/me/referral")
    assert len(calls) == 1  # 仍在 60s TTL 內，不重打

    now["t"] += 61
    client.get("/api/me/referral")
    assert len(calls) == 2  # TTL 過期，重打
