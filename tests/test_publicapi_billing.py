"""tests/test_publicapi_billing.py — billing 模組單元測試（全離線）。
StripeGateway 的 SDK 呼叫用 monkeypatch stripe.checkout.Session.create；
驗簽用真 HMAC（stripe.Webhook.construct_event 本地運算）。socket-ban 是 backstop：
漏 mock 的真外呼會直接炸 RuntimeError。"""
import hashlib
import hmac
import json
import time

import pytest
import stripe

from spark.publicapi.billing import (BillingError, BillingSignatureError, StripeGateway,
                                     apply_webhook_event, has_active_subscription,
                                     map_stripe_status, plan_catalog, verify_webhook_event)
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import billing_cfg, make_cfg

ACCT = "f" + "ab" * 20
WEBHOOK_SECRET = "whsec_test_secret"


def _store(tmp_path):
    return ApiStore(tmp_path / "api.db")


def _sig(payload: bytes, secret: str = WEBHOOK_SECRET, t: int | None = None) -> str:
    """照 Stripe 簽名規格手工組合法簽名：v1 = HMAC-SHA256(secret, f"{t}.{payload}")。"""
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _event_payload(etype: str, obj: dict, event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "object": "event", "type": etype,
                       "data": {"object": obj}}).encode()


# ---------- status 映射（設計定案 5：白名單，未知歸 canceled） ----------

@pytest.mark.parametrize("stripe_status,expected", [
    ("active", "active"), ("trialing", "active"),
    ("past_due", "past_due"), ("unpaid", "past_due"),
    ("canceled", "canceled"), ("incomplete", "canceled"),
    ("incomplete_expired", "canceled"), ("paused", "canceled"),
    ("some_future_status", "canceled"),
])
def test_map_stripe_status(stripe_status, expected):
    assert map_stripe_status(stripe_status) == expected


# ---------- 驗簽（⭐ 紅線 2） ----------

def test_verify_accepts_valid_signature():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert event["type"] == "checkout.session.completed"


def test_verify_rejects_bad_signature():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(payload, _sig(payload, secret="whsec_WRONG"), WEBHOOK_SECRET)


def test_verify_rejects_tampered_payload():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    sig = _sig(payload)
    tampered = payload.replace(b"cs_1", b"cs_2")
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(tampered, sig, WEBHOOK_SECRET)


def test_verify_rejects_stale_timestamp():
    """重放防護：construct_event 預設容忍 300s，過期簽名拒收。"""
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    old = _sig(payload, t=int(time.time()) - 3600)
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(payload, old, WEBHOOK_SECRET)


def test_verify_rejects_garbage_header():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(payload, "not-a-signature", WEBHOOK_SECRET)


# ---------- 事件處理（重放冪等、event.created 亂序守衛、對不到帳） ----------

def test_checkout_completed_activates(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("checkout.session.completed",
                             {"id": "cs_1", "client_reference_id": ACCT,
                              "customer": "cus_1", "subscription": "sub_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=100, now_s=1000.0) == "activated"
    rec = store.get_billing(ACCT)
    assert rec.status == "active"
    assert rec.stripe_customer_id == "cus_1"
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.last_event_created == 100
    assert has_active_subscription(store, ACCT) is True


def test_subscription_updated_past_due_via_metadata(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("customer.subscription.updated",
                             {"id": "sub_1", "status": "past_due", "customer": "cus_1",
                              "metadata": {"account_id": ACCT}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=110, now_s=1.0) == "updated"
    assert store.get_billing(ACCT).status == "past_due"
    assert has_active_subscription(store, ACCT) is False


def test_subscription_deleted_via_db_fallback(tmp_path):
    """metadata 缺 account_id → fallback 用 DB 的 subscription_id 對回（設計定案 4）。"""
    store = _store(tmp_path)
    store.upsert_billing(ACCT, status="active", stripe_subscription_id="sub_1",
                         now_s=1.0, event_created=100)
    payload = _event_payload("customer.subscription.deleted",
                             {"id": "sub_1", "status": "canceled", "metadata": {}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=120, now_s=2.0) == "updated"
    assert store.get_billing(ACCT).status == "canceled"


def test_subscription_event_unmatched_is_ignored(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("customer.subscription.updated",
                             {"id": "sub_unknown", "status": "active", "metadata": {}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "unmatched"
    assert store.get_billing(ACCT) is None


def test_subscription_event_bad_metadata_account_id_refused(tmp_path):
    """對稱於 checkout 分支的縱深防禦：metadata.account_id 可被 Stripe dashboard
    手動塞任意值——store 邊界 validate 拒收，回 "unmatched" 語意、不 raise、不寫 DB。"""
    store = _store(tmp_path)
    payload = _event_payload("customer.subscription.updated",
                             {"id": "sub_1", "status": "active", "customer": "cus_1",
                              "metadata": {"account_id": "../etc/passwd"}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "unmatched"
    assert store.get_billing("../etc/passwd") is None
    assert store.get_billing_by_subscription("sub_1") is None


def test_unknown_event_type_ignored(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("invoice.paid", {"id": "in_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "ignored"


def test_checkout_completed_bad_account_id_refused(tmp_path):
    """縱深防禦：client_reference_id 不合法（非本系統 account_id 格式）→ 不寫 DB。"""
    store = _store(tmp_path)
    payload = _event_payload("checkout.session.completed",
                             {"id": "cs_1", "client_reference_id": "../etc/passwd",
                              "customer": "cus_1", "subscription": "sub_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "bad_account"
    assert store.get_billing_by_subscription("sub_1") is None


def test_replayed_event_is_idempotent(tmp_path):
    """⭐ opus 總審 F1：Stripe 可能重送同一事件（同 event.id）——重放冪等靠
    event_id，是**整筆** no-op，第二次 apply 連 updated_at 都不動（不是「同結果」而是
    「原地不動」）。"""
    store = _store(tmp_path)
    payload = _event_payload("checkout.session.completed",
                             {"id": "cs_1", "client_reference_id": ACCT,
                              "customer": "cus_1", "subscription": "sub_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    apply_webhook_event(store, event, event_created=100, now_s=1.0)
    apply_webhook_event(store, event, event_created=100, now_s=2.0)  # 同 event.id 重放
    rec = store.get_billing(ACCT)
    assert rec.status == "active" and rec.updated_at == 1.0  # 整筆 no-op，非「重新套用同值」


def test_out_of_order_stale_active_does_not_resurrect(tmp_path):
    """⭐ opus 必改 1(a)：canceled(created=T2) 已套用後，晚到的 active(created=T1)
    不得復活權益——status 仍 canceled（event.created 單調守衛）。兩個事件是**不同**
    Stripe event（不同 event.id）——同一帳號的真實生命週期裡每個事件 id 都不同，
    用相異 id 才不會誤觸重放冪等短路（opus 總審 F1 修復後 event.id 才是去重鍵）。"""
    store = _store(tmp_path)
    p_cancel = _event_payload("customer.subscription.deleted",
                              {"id": "sub_1", "status": "canceled",
                               "metadata": {"account_id": ACCT}}, event_id="evt_cancel")
    ev = verify_webhook_event(p_cancel, _sig(p_cancel), WEBHOOK_SECRET)
    apply_webhook_event(store, ev, event_created=200, now_s=1.0)
    p_active = _event_payload("customer.subscription.updated",
                              {"id": "sub_1", "status": "active", "customer": "cus_1",
                               "metadata": {"account_id": ACCT}}, event_id="evt_active_stale")
    ev2 = verify_webhook_event(p_active, _sig(p_active), WEBHOOK_SECRET)
    assert apply_webhook_event(store, ev2, event_created=100, now_s=2.0) == "updated"
    assert store.get_billing(ACCT).status == "canceled"          # 舊事件 no-op
    assert has_active_subscription(store, ACCT) is False


def test_in_order_cancel_after_active(tmp_path):
    """opus 必改 1(b)：順序正常 active(T1) → canceled(T2) → 最終 canceled。
    不同 event.id（見上一測試註解）。"""
    store = _store(tmp_path)
    p_active = _event_payload("customer.subscription.updated",
                              {"id": "sub_1", "status": "active", "customer": "cus_1",
                               "metadata": {"account_id": ACCT}}, event_id="evt_active")
    ev1 = verify_webhook_event(p_active, _sig(p_active), WEBHOOK_SECRET)
    apply_webhook_event(store, ev1, event_created=100, now_s=1.0)
    assert store.get_billing(ACCT).status == "active"
    p_cancel = _event_payload("customer.subscription.deleted",
                              {"id": "sub_1", "status": "canceled",
                               "metadata": {"account_id": ACCT}}, event_id="evt_cancel")
    ev2 = verify_webhook_event(p_cancel, _sig(p_cancel), WEBHOOK_SECRET)
    apply_webhook_event(store, ev2, event_created=200, now_s=2.0)
    assert store.get_billing(ACCT).status == "canceled"


def test_same_second_active_after_cancel_does_not_resurrect(tmp_path):
    """⭐ opus 總審 F1：同秒縫——canceled(created=T) 已套用後，**同一秒**（非更舊）
    晚到的 active(created=T) 事件不得覆寫回 active。這正是 F1 指出的縫：舊寫法的
    `WHERE excluded.last_event_created >= billing.last_event_created` 用 `>=` 對同秒
    事件一律放行，等於平手時偏向「後到者」而非「低權益」。"""
    store = _store(tmp_path)
    p_cancel = _event_payload("customer.subscription.deleted",
                              {"id": "sub_1", "status": "canceled",
                               "metadata": {"account_id": ACCT}}, event_id="evt_cancel_ts")
    ev1 = verify_webhook_event(p_cancel, _sig(p_cancel), WEBHOOK_SECRET)
    apply_webhook_event(store, ev1, event_created=200, now_s=1.0)
    p_active = _event_payload("customer.subscription.updated",
                              {"id": "sub_1", "status": "active", "customer": "cus_1",
                               "metadata": {"account_id": ACCT}},
                              event_id="evt_active_same_second")
    ev2 = verify_webhook_event(p_active, _sig(p_active), WEBHOOK_SECRET)
    assert apply_webhook_event(store, ev2, event_created=200, now_s=2.0) == "updated"
    assert store.get_billing(ACCT).status == "canceled"          # 同秒不得覆寫
    assert has_active_subscription(store, ACCT) is False


# ---------- StripeGateway（紅線 4：非冪等不盲重試、失敗分類） ----------

def _gateway_call(gw):
    return gw.create_checkout_session(account_id=ACCT, price_id="price_x",
                                      success_url="https://d/ok", cancel_url="https://d/no")


def test_checkout_session_params_and_url(monkeypatch):
    seen = {}

    def fake_create(api_key=None, **params):
        seen.update(params, api_key=api_key)
        return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)
    gw = StripeGateway("sk_test_abc")
    url = _gateway_call(gw)
    assert url == "https://checkout.stripe.com/c/pay/cs_1"
    assert seen["api_key"] == "sk_test_abc"
    assert seen["mode"] == "subscription"
    assert seen["line_items"] == [{"price": "price_x", "quantity": 1}]
    assert seen["client_reference_id"] == ACCT
    assert seen["subscription_data"] == {"metadata": {"account_id": ACCT}}  # 設計定案 4
    assert seen["success_url"] == "https://d/ok"
    assert seen["cancel_url"] == "https://d/no"
    assert "customer" not in seen  # 無既有 customer 不帶


def test_checkout_reuses_existing_customer(monkeypatch):
    seen = {}
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda api_key=None, **p: (seen.update(p),
                                                   {"id": "cs", "url": "https://u"})[1])
    gw = StripeGateway("sk_test_abc")
    gw.create_checkout_session(account_id=ACCT, price_id="price_x",
                               success_url="https://d/ok", cancel_url="https://d/no",
                               customer_id="cus_1")
    assert seen["customer"] == "cus_1"


def test_transient_error_translated_no_retry(monkeypatch):
    """APIConnectionError → ConnectionError（app 統一 502「稍後重試」）；
    且**只呼叫一次**——checkout 建立非冪等，絕不盲重試（工程原則 2）。"""
    calls = []

    def boom(api_key=None, **p):
        calls.append(1)
        raise stripe.APIConnectionError("conn reset")

    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(ConnectionError):
        _gateway_call(StripeGateway("sk_test_abc"))
    assert len(calls) == 1


def test_rate_limit_is_transient(monkeypatch):
    def boom(api_key=None, **p):
        raise stripe.RateLimitError("slow down")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(ConnectionError):
        _gateway_call(StripeGateway("sk_test_abc"))


def test_semantic_error_is_billing_error(monkeypatch):
    def boom(api_key=None, **p):
        raise stripe.StripeError("no such price")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(BillingError):
        _gateway_call(StripeGateway("sk_test_abc"))


def test_missing_url_is_billing_error(monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda api_key=None, **p: {"id": "cs_1", "url": None})
    with pytest.raises(BillingError):
        _gateway_call(StripeGateway("sk_test_abc"))


def test_secret_key_not_in_gateway_repr_or_errors(monkeypatch):
    """key 不進 repr 與例外訊息（紅線 1 縱深防禦）。"""
    def boom(api_key=None, **p):
        raise stripe.StripeError("bad request")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    gw = StripeGateway("sk_test_supersecret")
    assert "sk_test_supersecret" not in repr(gw)
    with pytest.raises(BillingError) as ei:
        _gateway_call(gw)
    assert "sk_test_supersecret" not in str(ei.value)


# ---------- Customer Portal（失敗分類與 checkout 同一套規則） ----------

def _portal_call(gw):
    return gw.create_portal_session(customer_id="cus_1", return_url="https://d/billing")


def test_portal_session_params_and_url(monkeypatch):
    seen = {}

    def fake_create(api_key=None, **params):
        seen.update(params, api_key=api_key)
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1"}

    monkeypatch.setattr(stripe.billing_portal.Session, "create", fake_create)
    url = _portal_call(StripeGateway("sk_test_abc"))
    assert url == "https://billing.stripe.com/p/session/bps_1"
    assert seen["api_key"] == "sk_test_abc"
    assert seen["customer"] == "cus_1"
    assert seen["return_url"] == "https://d/billing"


def test_portal_transient_error_translated_no_retry(monkeypatch):
    """APIConnectionError → ConnectionError（app 統一 502），且**只呼叫一次**：
    重試政策與 checkout 一致，不在邊界內為 portal 開特例（工程原則 2）。"""
    calls = []

    def boom(api_key=None, **p):
        calls.append(1)
        raise stripe.APIConnectionError("conn reset")

    monkeypatch.setattr(stripe.billing_portal.Session, "create", boom)
    with pytest.raises(ConnectionError):
        _portal_call(StripeGateway("sk_test_abc"))
    assert len(calls) == 1


def test_portal_rate_limit_is_transient(monkeypatch):
    def boom(api_key=None, **p):
        raise stripe.RateLimitError("slow down")
    monkeypatch.setattr(stripe.billing_portal.Session, "create", boom)
    with pytest.raises(ConnectionError):
        _portal_call(StripeGateway("sk_test_abc"))


def test_portal_semantic_error_is_billing_error(monkeypatch):
    """no such customer / portal 未設定 → semantic，不重試。"""
    def boom(api_key=None, **p):
        raise stripe.StripeError("no such customer")
    monkeypatch.setattr(stripe.billing_portal.Session, "create", boom)
    with pytest.raises(BillingError):
        _portal_call(StripeGateway("sk_test_abc"))


def test_portal_missing_url_is_billing_error(monkeypatch):
    monkeypatch.setattr(stripe.billing_portal.Session, "create",
                        lambda api_key=None, **p: {"id": "bps_1", "url": None})
    with pytest.raises(BillingError):
        _portal_call(StripeGateway("sk_test_abc"))


def test_portal_secret_key_not_leaked(monkeypatch):
    def boom(api_key=None, **p):
        raise stripe.StripeError("bad request")
    monkeypatch.setattr(stripe.billing_portal.Session, "create", boom)
    with pytest.raises(BillingError) as ei:
        _portal_call(StripeGateway("sk_test_supersecret"))
    assert "sk_test_supersecret" not in str(ei.value)


# ---------- 方案目錄 ----------

def _features(catalog: dict, plan_id: str) -> dict[str, dict]:
    plan = next(p for p in catalog["plans"] if p["id"] == plan_id)
    return {f["text_key"]: f for f in plan["features"]}


def test_plan_catalog_full_when_billing_disabled(tmp_path):
    """⭐ billing 未設定（無 stripe 三元組）時**仍回完整方案目錄**——定價頁在未接
    金流時要顯示「即將開放」，而不是整頁消失。只有 billing_enabled/purchasable 轉 False。"""
    catalog = plan_catalog(make_cfg(tmp_path))
    assert catalog["billing_enabled"] is False
    assert [p["id"] for p in catalog["plans"]] == ["free", "pro"]
    assert all(p["purchasable"] is False for p in catalog["plans"])
    # 目錄內容不因 billing 停用而縮水：四項功能照列
    assert len(_features(catalog, "pro")) == 4
    assert catalog["plans"][1]["price_display"] is None  # 價格未設 → 前端「價格待定」


def test_plan_catalog_purchasable_only_for_paid_plan_when_enabled(tmp_path):
    catalog = plan_catalog(billing_cfg(tmp_path, stripe_price_display="$29 / 月"))
    assert catalog["billing_enabled"] is True
    free, pro = catalog["plans"]
    assert free["purchasable"] is False   # 免費方案無 price_id → 恆 False
    assert free["price_display"] is None
    assert pro["purchasable"] is True
    assert pro["price_display"] == "$29 / 月"  # 價格走設定（使用者尚未拍板）


def test_plan_catalog_never_exposes_stripe_price_id(tmp_path):
    """⭐ 結構性斷言（紅線 2）：price_id 不得出現在對外目錄的任何角落。
    遞迴掃全部鍵與值——新增欄位若夾帶 price_id 會在這裡紅燈。"""
    cfg = billing_cfg(tmp_path, stripe_price_id="price_SECRET_123")
    catalog = plan_catalog(cfg)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert "price_id" not in k, f"對外欄位夾帶 price_id: {k}"
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        else:
            assert node != "price_SECRET_123"

    walk(catalog)
    assert "price_SECRET_123" not in json.dumps(catalog)


def test_unshipped_paid_features_are_marked_unshipped(tmp_path):
    """⭐ 誠實揭露：多 leader 與比例 slider **尚未開發**（Phase 3/4）——pro 方案
    included=True 但 shipped=False，前端據此標「開發中」而不是假裝可用。
    這個測試是刻意的提醒機制：Phase 3/4 出貨時它會紅燈，提醒把 shipped 改 True。"""
    catalog = plan_catalog(billing_cfg(tmp_path))
    pro = _features(catalog, "pro")
    for key in ("plans.feature.multiLeader", "plans.feature.ratioSlider"):
        assert pro[key]["included"] is True
        assert pro[key]["shipped"] is False, f"{key} 已出貨？請同步更新方案目錄"
    # 已推出的功能：免費層不受限制（跟單不限本金、回撤保護照給）
    free = _features(catalog, "free")
    for key in ("plans.feature.copytrade", "plans.feature.killswitch"):
        assert free[key] == {"text_key": key, "included": True, "shipped": True}
    # 免費層不含付費功能
    assert free["plans.feature.multiLeader"]["included"] is False
    assert free["plans.feature.ratioSlider"]["included"] is False


def test_plan_catalog_has_no_hardcoded_user_copy(tmp_path):
    """⭐ 紅線 1：後端只給 i18n 鍵，使用者可見文案在前端 copy.ts。
    每個 name_key/text_key 都必須長得像鍵（plans.* 命名空間、無空白）。"""
    catalog = plan_catalog(billing_cfg(tmp_path))
    for plan in catalog["plans"]:
        assert plan["name_key"].startswith("plans.") and " " not in plan["name_key"]
        for f in plan["features"]:
            assert f["text_key"].startswith("plans.feature.") and " " not in f["text_key"]


# ---------- entitlement（紅線 6：只查不動） ----------

def test_has_active_subscription_states(tmp_path):
    """遞增 event_created（真實序列的兩個不同事件）——若都留預設 0，同秒平手偏向
    低權益（opus 總審 F1）會把第二次的 active 判成同秒 tie 而拒絕套用，非本測試
    要驗的行為，故顯式給遞增時戳。"""
    store = _store(tmp_path)
    assert has_active_subscription(store, ACCT) is False        # 無紀錄
    store.upsert_billing(ACCT, status="past_due", now_s=1.0, event_created=1)
    assert has_active_subscription(store, ACCT) is False
    store.upsert_billing(ACCT, status="active", now_s=2.0, event_created=2)
    assert has_active_subscription(store, ACCT) is True
