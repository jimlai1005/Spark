"""tests/test_api_billing.py — billing 端點測試（全離線）。
webhook 驗簽走真 HMAC；stripe checkout 用注入 create_fn 的 StripeGateway。"""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from spark.publicapi.billing import (PENDING_CHECKOUT_TTL_S, BillingError,
                                     StripeGateway)
from spark.publicapi.config import derive_account_id
from tests.publicapi_helpers import billing_cfg, login, make_app, stripe_sig

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（keysvc 慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair（self-pipe，不出網）。
    HL/keysvc 全為注入 fake——測試內無任何可觸網的程式路徑。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _billing_app(tmp_path, create_fn=None, portal_fn=None, cfg=None, now_fn=None):
    cfg = cfg or billing_cfg(tmp_path)
    gw = StripeGateway("sk_test_abc", create_fn=create_fn or
                       (lambda **p: {"id": "cs_1", "url": "https://checkout.example/cs_1"}),
                       portal_fn=portal_fn or
                       (lambda **p: {"id": "bps_1", "url": "https://portal.example/bps_1"}))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, billing=gw, now_fn=now_fn)
    return app, cfg, store


def _event(etype: str, obj: dict, created: int = 1_700_000_000,
          event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "object": "event", "type": etype,
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


def test_webhook_bad_signature_logs_warning_without_payload(tmp_path, caplog):
    """reviewer 順手項：驗簽失敗要留稽核痕跡，但 log 只放靜態訊息，不含 payload
    /簽名原文（不得洩漏使用者可控內容進 log）。"""
    import logging

    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "ab" * 20
    payload = _event("checkout.session.completed",
                     {"id": "cs_1", "client_reference_id": acct,
                      "customer": "cus_1", "subscription": "sub_1"})
    with caplog.at_level(logging.WARNING):
        r = client.post("/api/billing/webhook", content=payload,
                        headers={"stripe-signature": stripe_sig(payload, secret="whsec_WRONG")})
    assert r.status_code == 400
    assert "驗簽失敗" in caplog.text
    blob = " ".join(rec.getMessage() for rec in caplog.records)
    assert acct not in blob
    assert "cs_1" not in blob
    assert payload.decode() not in caplog.text


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
    """completed → updated(past_due) → deleted：DB 狀態逐步跟進。三個事件是**不同**
    Stripe event（不同 event.id）——真實生命週期裡每個事件 id 都不同；用相異 id
    才不會誤觸 event.id 重放冪等短路（opus 總審 F1：重放冪等靠 event.id）。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "cd" * 20

    p1 = _event("checkout.session.completed",
                {"id": "cs_1", "client_reference_id": acct,
                 "customer": "cus_9", "subscription": "sub_9"}, created=1000,
                event_id="evt_completed")
    client.post("/api/billing/webhook", content=p1,
                headers={"stripe-signature": stripe_sig(p1)})
    assert store.get_billing(acct).status == "active"

    p2 = _event("customer.subscription.updated",
                {"id": "sub_9", "status": "past_due", "customer": "cus_9",
                 "metadata": {"account_id": acct}}, created=1001,
                event_id="evt_past_due")
    client.post("/api/billing/webhook", content=p2,
                headers={"stripe-signature": stripe_sig(p2)})
    assert store.get_billing(acct).status == "past_due"

    p3 = _event("customer.subscription.deleted",
                {"id": "sub_9", "status": "canceled", "metadata": {}}, created=1002,
                event_id="evt_deleted")
    client.post("/api/billing/webhook", content=p3,
                headers={"stripe-signature": stripe_sig(p3)})
    assert store.get_billing(acct).status == "canceled"


def test_webhook_501_when_billing_disabled(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)  # 無 stripe 設定、無 gateway
    client = TestClient(app)
    r = client.post("/api/billing/webhook", content=b"{}",
                    headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 501


# ---------- checkout / status（紅線 3：未啟用 501、onboarding 隔離） ----------

def test_checkout_returns_url_bound_to_session(tmp_path):
    seen = {}

    def create_fn(**p):
        seen.update(p)
        return {"id": "cs_1", "url": "https://checkout.example/cs_1"}

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, base_url="https://testserver")  # secure cookie 需 https scheme
    wallet = login(client)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 200, r.text
    assert r.json() == {"checkout_url": "https://checkout.example/cs_1"}
    # ⭐ account 綁 session（沿紅線「別人不能替你 onboard」精神）：無 body 參數，
    # client_reference_id 只能來自 session 衍生
    assert seen["client_reference_id"] == derive_account_id(wallet.address)
    # success/cancel URL 從 siwe_uri 衍生（設計定案 3）
    assert seen["success_url"] == f"{cfg.siwe_uri}/billing?checkout=success"
    assert seen["cancel_url"] == f"{cfg.siwe_uri}/billing?checkout=cancel"


def test_checkout_409_when_already_active(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="active", now_s=1.0)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 409


def test_checkout_allows_retry_after_cancel_and_reuses_customer(tmp_path):
    seen = {}

    def create_fn(**p):
        seen.update(p)
        return {"id": "cs_2", "url": "https://checkout.example/cs_2"}

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="canceled",
                         stripe_customer_id="cus_1", now_s=1.0)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 200
    assert seen["customer"] == "cus_1"  # 既有 customer 重用（設計定案 12）


def test_checkout_requires_session(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    r = TestClient(app).post("/api/billing/checkout")
    assert r.status_code == 401


def test_checkout_transient_stripe_failure_is_502(tmp_path):
    def create_fn(**p):
        raise ConnectionError("stripe 連線失敗")  # gateway 對 transient 轉譯後的形態

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    login(client)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 502  # 既有 ConnectionError handler：前端「稍後重試」


# ---------- ⭐ 重複結帳擋板（opus 對抗審查 Important：工程原則 2 非冪等寫入） ----------

def test_second_checkout_while_pending_is_409(tmp_path):
    """⭐ 核心情境：付款完成 → 導回 → **webhook 尚未送達** → 使用者再按一次訂閱。
    本地 billing 表此時仍查無記錄（唯一寫入者是 webhook），舊實作會建出第二個
    Checkout Session＝兩張訂閱、兩次扣款。首購路徑連 customer_id 都不共用，
    Stripe 端不會自行去重。"""
    calls = []

    def create_fn(**p):
        calls.append(p)
        return {"id": f"cs_{len(calls)}", "url": f"https://checkout.example/{len(calls)}"}

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    assert client.post("/api/billing/checkout").status_code == 200
    r = client.post("/api/billing/checkout")
    assert r.status_code == 409
    assert "進行中" in r.json()["detail"]
    assert len(calls) == 1, "第二次請求不得抵達 Stripe（否則就是第二張訂閱）"


def test_pending_guard_is_per_account(tmp_path):
    """擋板只擋同一個 account——別的客戶照常結帳。"""
    app, cfg, store = _billing_app(tmp_path)
    c1 = TestClient(app, base_url="https://testserver")
    c2 = TestClient(app, base_url="https://testserver")
    login(c1)
    login(c2)
    assert c1.post("/api/billing/checkout").status_code == 200
    assert c2.post("/api/billing/checkout").status_code == 200


def test_webhook_clears_pending_and_active_409_is_distinguishable(tmp_path):
    """webhook 落地後：pending 位子被釋放，且後續 409 走的是**另一道**擋板
    （「已有生效訂閱」）——兩者語意不同，前端要能分辨（detail 不同字串）。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    acct = derive_account_id(wallet.address)

    assert client.post("/api/billing/checkout").status_code == 200
    pending_msg = client.post("/api/billing/checkout").json()["detail"]
    assert store.get_pending_checkout(acct) is not None

    payload = _event("checkout.session.completed",
                     {"id": "cs_1", "client_reference_id": acct,
                      "customer": "cus_1", "subscription": "sub_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200 and r.json()["outcome"] == "activated"
    assert store.get_pending_checkout(acct) is None, "webhook 落地必須釋放位子"

    r = client.post("/api/billing/checkout")
    assert r.status_code == 409
    assert r.json()["detail"] != pending_msg
    assert "生效訂閱" in r.json()["detail"]


def test_pending_expires_after_ttl_allows_retry(tmp_path):
    """⭐ 逾時後允許重試：放棄付款的使用者不該被永久卡住（擋板是防重複扣款，
    不是懲罰）。TTL 內擋、TTL 後放行。"""
    clock = {"t": 1_000_000.0}
    app, cfg, store = _billing_app(tmp_path, now_fn=lambda: clock["t"])
    client = TestClient(app, base_url="https://testserver")
    login(client)
    assert client.post("/api/billing/checkout").status_code == 200

    clock["t"] += PENDING_CHECKOUT_TTL_S - 1
    assert client.post("/api/billing/checkout").status_code == 409, "TTL 內仍要擋"
    clock["t"] += 2
    assert client.post("/api/billing/checkout").status_code == 200, "TTL 後允許重試"


def test_pending_cleared_when_create_session_fails(tmp_path):
    """⭐ 失敗補償：建 session 拋例外 ⇒ 沒有任何 Stripe 副作用 ⇒ 位子必須立刻還回去。
    否則一次網路抖動就把客戶卡滿整個 TTL（15 分鐘不能付錢）。"""
    boom = {"on": True}

    def create_fn(**p):
        if boom["on"]:
            raise ConnectionError("stripe 連線失敗")
        return {"id": "cs_ok", "url": "https://checkout.example/ok"}

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, base_url="https://testserver",
                        raise_server_exceptions=False)
    wallet = login(client)
    acct = derive_account_id(wallet.address)

    assert client.post("/api/billing/checkout").status_code == 502  # 失敗分類不變
    assert store.get_pending_checkout(acct) is None, "失敗後不得留下佔位"
    boom["on"] = False
    assert client.post("/api/billing/checkout").status_code == 200, "下一次立刻可重試"


def test_pending_cleared_on_semantic_failure_too(tmp_path):
    """semantic 失敗（BillingError → 502 專屬 handler）同樣要還位子——
    補償寫在 except Exception，不依賴呼叫端記得為每種例外各寫一次。"""
    def create_fn(**p):
        raise BillingError("stripe checkout 建立被拒: StripeError: no such price")

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, base_url="https://testserver",
                        raise_server_exceptions=False)
    wallet = login(client)
    assert client.post("/api/billing/checkout").status_code == 502
    assert store.get_pending_checkout(derive_account_id(wallet.address)) is None


def test_status_reads_db(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    acct = derive_account_id(wallet.address)
    r = client.get("/api/billing/status")
    assert r.status_code == 200
    assert r.json() == {"account_id": acct, "status": "none", "active": False}
    store.upsert_billing(acct, status="active", now_s=1.0)
    r = client.get("/api/billing/status")
    assert r.json() == {"account_id": acct, "status": "active", "active": True}


def test_status_requires_session(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    assert TestClient(app).get("/api/billing/status").status_code == 401


def test_billing_endpoints_501_when_disabled(tmp_path):
    """紅線 3：三個 stripe env 未設 → billing 端點 501「計費未啟用」。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    assert client.post("/api/billing/checkout").status_code == 501
    assert client.get("/api/billing/status").status_code == 501


# ---------- plans（公開端點：無 session、billing 停用亦可讀） ----------

def test_plans_needs_no_session(tmp_path):
    """⭐ 定價頁要能在登入前瀏覽——**不帶 cookie 也回 200**（刻意的 session 豁免）。"""
    app, cfg, store = _billing_app(tmp_path)
    r = TestClient(app).get("/api/billing/plans")
    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()["plans"]] == ["free", "pro"]


def test_plans_available_when_billing_disabled(tmp_path):
    """⭐ 與其他 billing 端點不同：**不回 501**——未接金流時前端要顯示
    「即將開放」，而不是整頁消失。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)  # 無 stripe 設定、無 gateway
    r = TestClient(app).get("/api/billing/plans")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["billing_enabled"] is False
    assert all(p["purchasable"] is False for p in body["plans"])
    assert len(body["plans"]) == 2  # 目錄不縮水


def test_plans_response_has_no_stripe_price_id(tmp_path):
    """⭐ 紅線 2 的端點層防線：HTTP 回應原文不得含 price_id（結構性）。"""
    cfg = billing_cfg(tmp_path, stripe_price_id="price_SECRET_123",
                      stripe_price_display="$29 / 月")
    app, cfg, store = _billing_app(tmp_path, cfg=cfg)
    r = TestClient(app).get("/api/billing/plans")
    assert r.status_code == 200
    assert "price_SECRET_123" not in r.text
    assert "price_id" not in r.text
    pro = next(p for p in r.json()["plans"] if p["id"] == "pro")
    assert pro["price_display"] == "$29 / 月" and pro["purchasable"] is True


# ---------- portal（session 綁定；無訂閱 409） ----------

def test_portal_returns_url_bound_to_session(tmp_path):
    seen = {}

    def portal_fn(**p):
        seen.update(p)
        return {"id": "bps_1", "url": "https://portal.example/bps_1"}

    app, cfg, store = _billing_app(tmp_path, portal_fn=portal_fn)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="active",
                         stripe_customer_id="cus_7", now_s=1.0)
    r = client.post("/api/billing/portal")
    assert r.status_code == 200, r.text
    assert r.json() == {"url": "https://portal.example/bps_1"}
    # customer 只能來自 session 衍生的 account（端點無輸入參數）
    assert seen["customer"] == "cus_7"
    assert seen["return_url"] == f"{cfg.siwe_uri}/billing"  # 從 siwe_uri 衍生，無新 env


def test_portal_409_when_no_customer(tmp_path):
    """尚無訂閱記錄 → 409（portal 只能管理既有訂閱）。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    r = client.post("/api/billing/portal")
    assert r.status_code == 409
    assert "訂閱" in r.json()["detail"]


def test_portal_409_when_record_exists_without_customer_id(tmp_path):
    """有 billing 列但 customer_id 為空（例如 webhook 只寫了狀態）→ 一樣 409，
    不得拿 None 去呼叫 Stripe。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="canceled", now_s=1.0)
    assert client.post("/api/billing/portal").status_code == 409


def test_portal_requires_session(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    assert TestClient(app).post("/api/billing/portal").status_code == 401


def test_portal_501_when_billing_disabled(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    assert client.post("/api/billing/portal").status_code == 501


def test_portal_transient_stripe_failure_is_502(tmp_path):
    def portal_fn(**p):
        raise ConnectionError("stripe 連線失敗")  # gateway 對 transient 轉譯後的形態

    app, cfg, store = _billing_app(tmp_path, portal_fn=portal_fn)
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="active",
                         stripe_customer_id="cus_7", now_s=1.0)
    r = client.post("/api/billing/portal")
    assert r.status_code == 502  # 既有 ConnectionError handler：前端「稍後重試」


def test_portal_semantic_stripe_failure_is_502(tmp_path):
    """semantic（no such customer / portal 未設定）→ BillingError → 專屬 502 handler。"""
    def portal_fn(**p):
        raise BillingError("stripe portal 建立被拒: StripeError: no such customer")

    app, cfg, store = _billing_app(tmp_path, portal_fn=portal_fn)
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="active",
                         stripe_customer_id="cus_7", now_s=1.0)
    r = client.post("/api/billing/portal")
    assert r.status_code == 502
    assert "計費服務錯誤" in r.json()["detail"]


def test_onboarding_unaffected_when_billing_disabled(tmp_path):
    """紅線 3 的另一半：billing 未設時 onboarding 全流程行為與 M2 相同（隔離）。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    r = client.get("/api/onboard/status")
    assert r.status_code == 200
    assert r.json()["agent_generated"] is True
