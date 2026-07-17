"""src/spark/publicapi/billing.py
M3 計費骨幹（**全程 Stripe 測試模式**；sk_test_ 強制在 ApiConfig）。
- StripeGateway：publicapi 對 Stripe 的唯一出口（單一 resilience boundary，工程原則 5）。
  checkout session 建立是**非冪等寫入** → 單次嘗試、絕不盲重試（工程原則 2）；
  transient 轉譯內建 ConnectionError（沿 hl.py 慣例，app 統一 502「稍後重試」——
  由前端使用者重按，人肉重試天然去重）；semantic → BillingError（502 專屬 handler）。
- verify_webhook_event：⭐ 驗簽必過才回 Event（偽造 webhook = 免費開通）。本地 HMAC。
- apply_webhook_event：已驗簽事件 → billing 表 upsert。帳號歸屬由 metadata/DB 雙保險；
  重放冪等靠 event.id；順序守衛靠 event.created 嚴格比較；同秒平手偏向低權益
  （opus 總審 F1——Stripe event.created 秒級精度、不保證投遞順序，`>=` 同時服務
  去重與排序會在同秒留下縫：canceled 已套用後同秒晚到的 active 不得覆寫回去）。
- has_active_subscription：entitlement **只查不動**——不接任何自動停用跟單邏輯
  （停用是政策決策，留使用者人工裁決；紅線 6）。"""
import logging

from spark.filet.followers import validate_account_id
from spark.publicapi.store import ApiStore

logger = logging.getLogger(__name__)

# stripe 訂閱狀態 → 本地 status（設計定案 5：白名單映射，未知值歸 canceled——保守不給權益）
_STRIPE_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
}


def map_stripe_status(stripe_status: str) -> str:
    return _STRIPE_STATUS_MAP.get(stripe_status, "canceled")


class BillingError(RuntimeError):
    """Stripe 語意失敗（semantic，不重試）：請求被拒、設定錯、回應缺欄位。"""


class BillingSignatureError(BillingError):
    """webhook 驗簽失敗（⭐ 紅線 2）——呼叫端一律 400、不碰 DB。"""


class StripeGateway:
    """create_fn 可注入（測試給 fake）；預設走 stripe SDK、per-call api_key
    （無全域 stripe.api_key 狀態）。失敗分類集中在 create_checkout_session 一處，
    注入 fake 也繞不開（結構性）。"""

    def __init__(self, secret_key: str, create_fn=None):
        self._secret_key = secret_key
        self._create = create_fn or self._default_create

    def __repr__(self) -> str:  # key 不進 repr/log（紅線 1）
        return "<StripeGateway test-mode>"

    def _default_create(self, **params):
        import stripe
        return stripe.checkout.Session.create(api_key=self._secret_key, **params)

    def create_checkout_session(self, *, account_id: str, price_id: str,
                                success_url: str, cancel_url: str,
                                customer_id: str | None = None) -> str:
        """建 Checkout Session（mode=subscription），回 checkout URL。
        client_reference_id 與 subscription metadata 雙塞 account_id（設計定案 4）。"""
        import stripe
        params = dict(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=account_id,
            subscription_data={"metadata": {"account_id": account_id}},
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if customer_id:
            params["customer"] = customer_id
        try:
            session = self._create(**params)
        except (stripe.APIConnectionError, stripe.RateLimitError) as e:
            # transient；但 checkout 建立非冪等 → 不在邊界重試（工程原則 2），
            # 轉譯 ConnectionError → 502「稍後重試」由前端使用者重按（人肉重試天然去重）
            raise ConnectionError(f"stripe 連線失敗: {type(e).__name__}") from e
        except stripe.StripeError as e:
            # 注意：APIError（Stripe 端 5xx）語意上屬 transient，但落在這個分支——
            # 刻意的：非冪等寫入無論 transient/semantic 都統一單次嘗試不重試，
            # 分支差異只在 log 語氣與例外型別，不在重試行為（opus Finding 5）
            raise BillingError(f"stripe checkout 建立被拒: {type(e).__name__}: "
                               f"{getattr(e, 'user_message', None) or e}") from e
        url = session["url"] if isinstance(session, dict) else getattr(session, "url", None)
        if not url:
            raise BillingError("stripe 回應缺 checkout url")
        return url


def verify_webhook_event(payload: bytes, sig_header: str, webhook_secret: str):
    """⭐ 驗簽必過才回 Event（本地 HMAC-SHA256 + 時戳容忍，construct_event 不觸網）。
    格式壞/簽錯/過期一律 BillingSignatureError——呼叫端 400、不碰 DB。"""
    import stripe
    try:
        return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (stripe.SignatureVerificationError, ValueError) as e:
        raise BillingSignatureError(f"webhook 驗簽失敗: {type(e).__name__}") from e


def apply_webhook_event(store: ApiStore, event, *, event_created: int,
                        now_s: float) -> str:
    """處理**已驗簽**事件 → billing 表。回傳結果標籤（log/測試用；標籤代表
    「已處理該類事件」，較舊事件或同秒被拒經守衛 no-op 時仍回 "updated"——DB 才是真相）。
    - 帳號歸屬：metadata/DB 雙保險（設計定案 4）。
    - 重放冪等：靠 event.id（`upsert_billing(event_id=...)`），同一事件重送整筆 no-op。
    - 順序守衛：event.created 嚴格比較（opus 總審 F1）——較舊事件整筆 no-op，
      晚到的舊 active 不得復活已取消訂閱。
    - 同秒平手：entitlement rank 較高不得覆寫較低（opus 總審 F1）——Stripe
      event.created 只有秒級精度、不保證投遞順序，同一秒的 canceled 與 active
      誰先到無法保證，平手時偏向低權益（active 不得覆寫同秒 canceled/past_due）。
    - 未知事件類型 → "ignored"（回 200 ack，不累積 Stripe 重送佇列）。
    event_created 由呼叫端從 event["created"]（epoch 秒）取。"""
    etype = event["type"]
    obj = event["data"]["object"]
    event_id = event.get("id", "")
    if etype == "checkout.session.completed":
        account_id = obj.get("client_reference_id")
        try:
            validate_account_id(account_id or "")
        except Exception:  # noqa: BLE001 — 縱深防禦：格式不對就拒寫，大聲留痕
            logger.error("checkout.session.completed 的 client_reference_id 不合法，"
                         "拒絕入帳: session=%s", obj.get("id"))
            return "bad_account"
        # test-mode 刻意接受未檢 payment_status（收到已驗簽 completed 即開通）；
        # 正式收費前與 reconcile 計畫一併收（opus Finding 3；見「刻意不做」12）
        store.upsert_billing(account_id, status="active",
                             stripe_customer_id=obj.get("customer"),
                             stripe_subscription_id=obj.get("subscription"),
                             now_s=now_s, event_created=event_created, event_id=event_id)
        logger.info("billing 開通 account=%s", account_id)
        return "activated"
    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        status = ("canceled" if etype.endswith("deleted")
                  else map_stripe_status(obj.get("status", "")))
        account_id = (obj.get("metadata") or {}).get("account_id")
        if not account_id:
            rec = store.get_billing_by_subscription(obj.get("id", ""))
            account_id = rec.account_id if rec else None
        if not account_id:
            logger.warning("subscription 事件對不到 account（sub=%s）——"
                           "可能是外部手建訂閱，忽略", obj.get("id"))
            return "unmatched"
        try:
            validate_account_id(account_id)
        except ValueError as e:
            # metadata.account_id 可被 Stripe dashboard 手動塞任意值——縱深防禦，
            # 在呼叫 upsert 前單獨驗證（收窄 except 範圍，reviewer 觀察：避免寬 except
            # 連同 upsert_billing 內部真程式錯誤一起吞掉）。與歸屬失敗同路徑：
            # log 留痕（含例外訊息以利區分成因）、回 200 ack，不炸 webhook 流。
            logger.warning("subscription 事件的 account_id 不合法，拒絕入帳"
                           "（sub=%s）：%s", obj.get("id"), e)
            return "unmatched"
        store.upsert_billing(account_id, status=status,
                             stripe_customer_id=obj.get("customer"),
                             stripe_subscription_id=obj.get("id"),
                             now_s=now_s, event_created=event_created, event_id=event_id)
        logger.info("billing 狀態更新 account=%s status=%s", account_id, status)
        return "updated"
    return "ignored"


def has_active_subscription(store: ApiStore, account_id: str) -> bool:
    """entitlement 查詢（唯讀）。⭐ 刻意只有查詢：跟單停用是政策決策（人工），
    本函式不得接任何自動停用邏輯（紅線 6）。"""
    rec = store.get_billing(account_id)
    return rec is not None and rec.status == "active"
