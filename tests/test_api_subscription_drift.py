"""tests/test_api_subscription_drift.py
訂閱對帳（本地 billing 表 vs Stripe 真實狀態），全離線。

⭐ 這裡守的是兩個方向不同危害的漂移：
- local_active_stripe_not = **漏財**（還在服務、收不到錢）
- stripe_active_local_not = **客戶付了錢沒拿到權益**（信任問題）
兩者互斥且都不得被吞掉；orphan_stripe 只收非 active 的孤兒（危害優先於分類整齊）。

另守：狀態比較必須先過 map_stripe_status 正規化（同基準，工程原則 1）——
trialing 在本地映射就是 active，不得被誤判成 mismatch。
"""
import socket

import pytest
import stripe
from eth_account import Account
from fastapi.testclient import TestClient

from spark.publicapi.billing import (MAX_RECONCILE_SUBSCRIPTIONS, BillingError,
                                     StripeGateway)
from spark.publicapi.ops import subscription_drift
from tests.publicapi_helpers import billing_cfg, login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 需本機 socketpair；Stripe/HL/keysvc 全為注入 fake，無觸網路徑。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


ACCT_A = "f" + "a1" * 20
ACCT_B = "f" + "b2" * 20
ACCT_C = "f" + "c3" * 20


def _local(account_id, status, sub_id=None):
    return {"account_id": account_id, "status": status,
            "stripe_subscription_id": sub_id}


def _sub(sub_id, status, account_id=None):
    return {"id": sub_id, "status": status, "customer": "cus_x",
            "metadata": {"account_id": account_id}}


# ---------- 純函式：四種漂移 ----------

def test_local_active_stripe_canceled_is_revenue_leak():
    """⭐ 漏財：本地還在給權益，Stripe 早已取消。"""
    d = subscription_drift([_local(ACCT_A, "active", "sub_1")],
                           [_sub("sub_1", "canceled", ACCT_A)])
    assert [e["account_id"] for e in d["local_active_stripe_not"]] == [ACCT_A]
    assert d["local_active_stripe_not"][0]["stripe_status"] == "canceled"
    assert d["stripe_active_local_not"] == [] and d["status_mismatch"] == []
    assert d["in_sync_count"] == 0 and d["drift_count"] == 1


def test_local_active_but_stripe_has_no_such_subscription():
    """Stripe 端整筆查無（status=all 已涵蓋已取消者 → 真的沒有）也算漏財。"""
    d = subscription_drift([_local(ACCT_A, "active", "sub_gone")], [])
    assert len(d["local_active_stripe_not"]) == 1
    assert d["local_active_stripe_not"][0]["stripe_status"] is None


def test_stripe_active_local_missing_is_paid_without_entitlement():
    """⭐ 客戶付了錢沒權益：Stripe active，本地完全沒記錄（webhook 掉包的典型症狀）。"""
    d = subscription_drift([], [_sub("sub_9", "active", ACCT_B)])
    assert len(d["stripe_active_local_not"]) == 1
    entry = d["stripe_active_local_not"][0]
    assert entry["account_id"] == ACCT_B and entry["local_status"] is None
    assert d["orphan_stripe"] == []  # 危害優先：active 孤兒不歸 orphan


def test_stripe_active_local_canceled():
    """本地已標 canceled 但 Stripe 仍在收費 → 同樣是「付了錢沒權益」。"""
    d = subscription_drift([_local(ACCT_A, "canceled", "sub_1")],
                           [_sub("sub_1", "active", ACCT_A)])
    assert len(d["stripe_active_local_not"]) == 1
    assert d["local_active_stripe_not"] == []


def test_status_mismatch_neither_side_active():
    """兩邊都有、都非 active、狀態不同 → status_mismatch（不誤入前兩類）。"""
    d = subscription_drift([_local(ACCT_A, "canceled", "sub_1")],
                           [_sub("sub_1", "past_due", ACCT_A)])
    assert [e["account_id"] for e in d["status_mismatch"]] == [ACCT_A]
    assert d["local_active_stripe_not"] == [] and d["stripe_active_local_not"] == []


def test_orphan_stripe_only_collects_non_active():
    """對不到任何本地 account 且非 active → orphan（外部手建／測試殘留）。"""
    d = subscription_drift([], [_sub("sub_orphan", "canceled", None)])
    assert len(d["orphan_stripe"]) == 1
    assert d["orphan_stripe"][0]["stripe_subscription_id"] == "sub_orphan"
    assert d["stripe_active_local_not"] == []


def test_orphan_with_unknown_account_id_still_flagged_when_active():
    """metadata 帶了本地不存在的 account_id 且 active → 仍進「付了錢沒權益」。"""
    d = subscription_drift([_local(ACCT_A, "active", "sub_1")],
                           [_sub("sub_1", "active", ACCT_A),
                            _sub("sub_2", "active", "f" + "de" * 20)])
    assert d["in_sync_count"] == 1
    assert len(d["stripe_active_local_not"]) == 1
    assert d["stripe_active_local_not"][0]["stripe_subscription_id"] == "sub_2"


# ---------- 一致 ----------

def test_in_sync_counts_and_empty_drift_lists():
    d = subscription_drift(
        [_local(ACCT_A, "active", "sub_1"), _local(ACCT_B, "past_due", "sub_2"),
         _local(ACCT_C, "canceled", None)],
        [_sub("sub_1", "active", ACCT_A), _sub("sub_2", "past_due", ACCT_B)])
    assert d["in_sync_count"] == 3     # 含本地 canceled 且 Stripe 無此筆（兩邊都無權益）
    assert d["drift_count"] == 0
    for key in ("local_active_stripe_not", "stripe_active_local_not",
                "status_mismatch", "orphan_stripe"):
        assert d[key] == [], key
    assert d["local_count"] == 3 and d["stripe_count"] == 2


# ---------- ⭐ 狀態正規化（同基準比較，工程原則 1） ----------

def test_trialing_normalized_to_active_is_not_mismatch():
    """⭐ map_stripe_status: trialing → active。本地 active + Stripe trialing 是**一致**，
    比較前不正規化就會產生整批假漂移。"""
    d = subscription_drift([_local(ACCT_A, "active", "sub_1")],
                           [_sub("sub_1", "trialing", ACCT_A)])
    assert d["in_sync_count"] == 1 and d["drift_count"] == 0
    assert d["local_active_stripe_not"] == []


def test_unpaid_normalized_to_past_due_is_not_mismatch():
    """同上另一半：unpaid → past_due。"""
    d = subscription_drift([_local(ACCT_A, "past_due", "sub_1")],
                           [_sub("sub_1", "unpaid", ACCT_A)])
    assert d["in_sync_count"] == 1
    assert d["stripe_active_local_not"] == []


def test_unknown_stripe_status_maps_to_canceled():
    """白名單外（如 incomplete_expired）→ canceled，保守不給權益。"""
    d = subscription_drift([_local(ACCT_A, "active", "sub_1")],
                           [_sub("sub_1", "incomplete_expired", ACCT_A)])
    assert len(d["local_active_stripe_not"]) == 1
    assert d["local_active_stripe_not"][0]["stripe_status"] == "canceled"
    assert d["local_active_stripe_not"][0]["stripe_status_raw"] == "incomplete_expired"


# ---------- 對應鍵：subscription_id 優先，metadata 次之 ----------

def test_matches_by_metadata_when_subscription_id_absent_locally():
    """本地尚未存到 sub id（webhook 掉了首包）→ 靠 metadata.account_id 對上。"""
    d = subscription_drift([_local(ACCT_A, "none", None)],
                           [_sub("sub_1", "active", ACCT_A)])
    assert len(d["stripe_active_local_not"]) == 1
    assert d["stripe_active_local_not"][0]["matched_by"] == "metadata"


def test_subscription_id_takes_precedence_over_metadata():
    d = subscription_drift([_local(ACCT_A, "active", "sub_1")],
                           [_sub("sub_1", "active", ACCT_B)])
    assert d["in_sync_count"] == 1
    assert d["stripe_active_local_not"] == []


def test_accepts_billing_record_dataclass_rows(tmp_path):
    """local_rows 可直接吃 store.list_billing() 的 BillingRecord（非 dict）。"""
    from spark.publicapi.store import ApiStore
    store = ApiStore(str(tmp_path / "api.db"))
    store.upsert_billing(ACCT_A, status="active", stripe_subscription_id="sub_1",
                         now_s=1.0)
    d = subscription_drift(store.list_billing(), [_sub("sub_1", "canceled", ACCT_A)])
    assert [e["account_id"] for e in d["local_active_stripe_not"]] == [ACCT_A]


# ---------- StripeGateway.list_subscriptions（唯讀、分頁、失敗分類） ----------

class _FakePage:
    """模擬 stripe ListObject：auto_paging_iter 惰性跨頁（真 SDK 的翻頁網路呼叫
    發生在迭代當中，故失敗也可能在迭代中才拋——用 generator 忠實重現）。"""

    def __init__(self, pages, raise_on_page=None):
        self.pages = pages
        self.raise_on_page = raise_on_page

    def auto_paging_iter(self):
        for i, page in enumerate(self.pages):
            if self.raise_on_page == i:
                raise self.raise_on_page_exc
            yield from page


def _gw(pages, raise_on_page=None, exc=None, seen=None):
    page = _FakePage(pages, raise_on_page)
    page.raise_on_page_exc = exc

    def list_fn(**params):
        if seen is not None:
            seen.update(params)
        return page

    return StripeGateway("sk_test_abc", list_fn=list_fn)


def test_list_subscriptions_uses_status_all_and_paginates():
    """⭐ status=all（不然漏掉「本地 active／Stripe 已取消」這個主要訊號）；
    超過一頁時 auto_paging_iter 全部取到。"""
    seen = {}
    pages = [[_sub(f"sub_{i}", "active", None) for i in range(100)],
             [_sub(f"sub_{i}", "canceled", None) for i in range(100, 150)]]
    out = _gw(pages, seen=seen).list_subscriptions()
    assert seen["status"] == "all" and seen["limit"] == 100
    assert len(out["subscriptions"]) == 150
    assert out["truncated"] is False
    assert out["subscriptions"][-1]["id"] == "sub_149"


def test_list_subscriptions_truncates_loudly_at_cap():
    """達硬上限 → truncated=True（不靜默截斷：截斷清單會造出假漂移）。"""
    pages = [[_sub(f"sub_{i}", "active", None) for i in range(600)] for _ in range(3)]
    out = _gw(pages).list_subscriptions()
    assert out["truncated"] is True
    assert len(out["subscriptions"]) == MAX_RECONCILE_SUBSCRIPTIONS


def test_list_subscriptions_whitelists_fields():
    """白名單投影：金額等其他 Stripe 欄位不進本服務。"""
    raw = {"id": "sub_1", "status": "active", "customer": "cus_1",
           "metadata": {"account_id": ACCT_A, "secret_note": "x"},
           "latest_invoice": {"amount_paid": 999}}
    out = _gw([[raw]]).list_subscriptions()
    assert out["subscriptions"] == [{
        "id": "sub_1", "status": "active", "customer": "cus_1",
        "metadata": {"account_id": ACCT_A}}]


def test_list_subscriptions_transient_error_is_connection_error():
    gw = _gw([[]], raise_on_page=0, exc=stripe.APIConnectionError("conn reset"))
    with pytest.raises(ConnectionError):
        gw.list_subscriptions()


def test_list_subscriptions_rate_limit_is_transient():
    gw = _gw([[]], raise_on_page=0, exc=stripe.RateLimitError("slow down"))
    with pytest.raises(ConnectionError):
        gw.list_subscriptions()


def test_list_subscriptions_semantic_error_is_billing_error():
    gw = _gw([[]], raise_on_page=0, exc=stripe.StripeError("invalid api key"))
    with pytest.raises(BillingError):
        gw.list_subscriptions()


def test_failure_on_second_page_is_still_classified():
    """翻頁失敗發生在迭代中——整個迴圈在 try 內，不得逃出失敗分類。"""
    gw = _gw([[_sub("sub_1", "active", None)], []], raise_on_page=1,
             exc=stripe.APIConnectionError("reset mid-pagination"))
    with pytest.raises(ConnectionError):
        gw.list_subscriptions()


def test_secret_key_not_leaked_in_list_errors():
    gw = _gw([[]], raise_on_page=0, exc=stripe.StripeError("bad request"))
    gw._secret_key = "sk_test_supersecret"
    with pytest.raises(BillingError) as ei:
        gw.list_subscriptions()
    assert "sk_test_supersecret" not in str(ei.value)


# ---------- 端點 /api/ops/subscriptions（admin only + 需 billing） ----------

def _app(tmp_path, pages=None, billing=True):
    """已登入的 admin client。billing=False → 無 stripe 設定（cfg.billing_enabled False）。"""
    wallet = Account.create()
    over = {"admin_addresses": frozenset({wallet.address.lower()})}
    cfg = billing_cfg(tmp_path, **over) if billing else make_cfg(tmp_path, **over)
    gw = _gw(pages if pages is not None else [[]]) if billing else None
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, billing=gw)
    client = TestClient(app, base_url="https://testserver")
    login(client, wallet=wallet)
    return client, store


def test_endpoint_reports_both_drift_directions(tmp_path):
    client, store = _app(tmp_path, pages=[[_sub("sub_1", "canceled", ACCT_A),
                                           _sub("sub_2", "active", ACCT_B)]])
    store.upsert_billing(ACCT_A, status="active", stripe_subscription_id="sub_1",
                         now_s=1.0)
    r = client.get("/api/ops/subscriptions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["account_id"] for e in body["local_active_stripe_not"]] == [ACCT_A]
    assert [e["account_id"] for e in body["stripe_active_local_not"]] == [ACCT_B]
    assert body["drift_count"] == 2 and body["truncated"] is False


def test_endpoint_in_sync(tmp_path):
    client, store = _app(tmp_path, pages=[[_sub("sub_1", "trialing", ACCT_A)]])
    store.upsert_billing(ACCT_A, status="active", stripe_subscription_id="sub_1",
                         now_s=1.0)
    body = client.get("/api/ops/subscriptions").json()
    assert body["in_sync_count"] == 1 and body["drift_count"] == 0


def test_endpoint_surfaces_truncated(tmp_path):
    """截斷旗標原樣上呈：管理員必須知道這份對帳結論不完整。"""
    pages = [[_sub(f"sub_{i}", "canceled", None) for i in range(600)] for _ in range(3)]
    client, store = _app(tmp_path, pages=pages)
    body = client.get("/api/ops/subscriptions").json()
    assert body["truncated"] is True


def test_endpoint_does_not_write_anything(tmp_path):
    """⭐ 唯讀：偵測不修正。跑完對帳後本地 billing 狀態一個字都不能變。"""
    client, store = _app(tmp_path, pages=[[_sub("sub_1", "canceled", ACCT_A)]])
    store.upsert_billing(ACCT_A, status="active", stripe_subscription_id="sub_1",
                         now_s=1.0)
    before = store.get_billing(ACCT_A)
    assert client.get("/api/ops/subscriptions").status_code == 200
    assert store.get_billing(ACCT_A) == before   # 含 updated_at 都不動


def test_endpoint_501_when_billing_disabled(tmp_path):
    client, store = _app(tmp_path, billing=False)
    assert client.get("/api/ops/subscriptions").status_code == 501


def test_endpoint_502_on_transient_stripe_failure(tmp_path):
    wallet = Account.create()
    cfg = billing_cfg(tmp_path, admin_addresses=frozenset({wallet.address.lower()}))
    gw = _gw([[]], raise_on_page=0, exc=stripe.APIConnectionError("reset"))
    app, *_ = make_app(tmp_path, cfg=cfg, billing=gw)
    client = TestClient(app, base_url="https://testserver")
    login(client, wallet=wallet)
    assert client.get("/api/ops/subscriptions").status_code == 502


def test_endpoint_502_on_semantic_stripe_failure(tmp_path):
    wallet = Account.create()
    cfg = billing_cfg(tmp_path, admin_addresses=frozenset({wallet.address.lower()}))
    gw = _gw([[]], raise_on_page=0, exc=stripe.StripeError("invalid api key"))
    app, *_ = make_app(tmp_path, cfg=cfg, billing=gw)
    client = TestClient(app, base_url="https://testserver")
    login(client, wallet=wallet)
    assert client.get("/api/ops/subscriptions").status_code == 502
