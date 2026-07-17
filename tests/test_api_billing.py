"""tests/test_api_billing.py — billing 端點測試（全離線）。
webhook 驗簽走真 HMAC；stripe checkout 用注入 create_fn 的 StripeGateway。"""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from spark.publicapi.billing import StripeGateway
from tests.publicapi_helpers import billing_cfg, make_app, stripe_sig

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（keysvc 慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair（self-pipe，不出網）。
    HL/keysvc 全為注入 fake——測試內無任何可觸網的程式路徑。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _billing_app(tmp_path, create_fn=None):
    cfg = billing_cfg(tmp_path)
    gw = StripeGateway("sk_test_abc", create_fn=create_fn or
                       (lambda **p: {"id": "cs_1", "url": "https://checkout.example/cs_1"}))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, billing=gw)
    return app, cfg, store


def _event(etype: str, obj: dict, created: int = 1_700_000_000) -> bytes:
    return json.dumps({"id": "evt_1", "object": "event", "type": etype,
                       "created": created, "data": {"object": obj}}).encode()


# ---------- webhook（⭐ 紅線 2：驗簽必過；唯一 session-auth 豁免端點） ----------

def test_webhook_valid_signature_activates(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "ab" * 20
    payload = _event("checkout.session.completed",
                     {"id": "cs_1", "client_reference_id": acct,
                      "customer": "cus_1", "subscription": "sub_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "activated"
    assert store.get_billing(acct).status == "active"


def test_webhook_bad_signature_400_and_no_db_write(tmp_path):
    """⭐ 偽造 webhook = 免費開通：簽錯 → 400，且 DB 一個 byte 都不動。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "ab" * 20
    payload = _event("checkout.session.completed",
                     {"id": "cs_1", "client_reference_id": acct,
                      "customer": "cus_1", "subscription": "sub_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload, secret="whsec_WRONG")})
    assert r.status_code == 400
    assert store.get_billing(acct) is None


def test_webhook_missing_signature_header_400(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    payload = _event("checkout.session.completed", {"id": "cs_1"})
    r = client.post("/api/billing/webhook", content=payload)
    assert r.status_code == 400


def test_webhook_needs_no_session(tmp_path):
    """webhook 是 Stripe 伺服器回呼——**不帶 cookie 也要能進驗簽**（豁免是刻意的，
    授權由 HMAC 取代）。用合法簽名、無 session 驗證通路存在。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)  # 無 login
    payload = _event("invoice.paid", {"id": "in_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200
    assert r.json()["outcome"] == "ignored"


def test_webhook_subscription_lifecycle(tmp_path):
    """completed → updated(past_due) → deleted：DB 狀態逐步跟進。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "cd" * 20

    p1 = _event("checkout.session.completed",
                {"id": "cs_1", "client_reference_id": acct,
                 "customer": "cus_9", "subscription": "sub_9"}, created=1000)
    client.post("/api/billing/webhook", content=p1,
                headers={"stripe-signature": stripe_sig(p1)})
    assert store.get_billing(acct).status == "active"

    p2 = _event("customer.subscription.updated",
                {"id": "sub_9", "status": "past_due", "customer": "cus_9",
                 "metadata": {"account_id": acct}}, created=1001)
    client.post("/api/billing/webhook", content=p2,
                headers={"stripe-signature": stripe_sig(p2)})
    assert store.get_billing(acct).status == "past_due"

    p3 = _event("customer.subscription.deleted",
                {"id": "sub_9", "status": "canceled", "metadata": {}}, created=1002)
    client.post("/api/billing/webhook", content=p3,
                headers={"stripe-signature": stripe_sig(p3)})
    assert store.get_billing(acct).status == "canceled"


def test_webhook_501_when_billing_disabled(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)  # 無 stripe 設定、無 gateway
    client = TestClient(app)
    r = client.post("/api/billing/webhook", content=b"{}",
                    headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 501
