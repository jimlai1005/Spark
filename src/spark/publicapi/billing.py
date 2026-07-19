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
  （停用是政策決策，留使用者人工裁決；紅線 6）。
- plan_catalog：方案目錄。功能清單版控於程式碼、價格與 price_id 走設定；
  `shipped=False` 誠實揭露「已規劃但未推出」——不賣不存在的功能。"""
import logging
from dataclasses import dataclass

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


# 對帳單次拉取的硬上限：避免 auto-pagination 在異常帳戶（或 API 迴圈）下無限翻頁。
# 達上限時**不靜默截斷**——list_subscriptions 回 truncated=True，端點原樣上呈，
# 因為截斷過的清單會讓「本地有、Stripe 沒有」的判斷產生假漂移（工程原則 3）。
MAX_RECONCILE_SUBSCRIPTIONS = 1000


class BillingError(RuntimeError):
    """Stripe 語意失敗（semantic，不重試）：請求被拒、設定錯、回應缺欄位。"""


class BillingSignatureError(BillingError):
    """webhook 驗簽失敗（⭐ 紅線 2）——呼叫端一律 400、不碰 DB。"""


class StripeGateway:
    """create_fn 可注入（測試給 fake）；預設走 stripe SDK、per-call api_key
    （無全域 stripe.api_key 狀態）。失敗分類集中在 create_checkout_session 一處，
    注入 fake 也繞不開（結構性）。"""

    def __init__(self, secret_key: str, create_fn=None, portal_fn=None, list_fn=None):
        self._secret_key = secret_key
        self._create = create_fn or self._default_create
        self._portal = portal_fn or self._default_portal
        self._list = list_fn or self._default_list

    def __repr__(self) -> str:  # key 不進 repr/log（紅線 1）
        return "<StripeGateway test-mode>"

    def _default_create(self, **params):
        import stripe
        return stripe.checkout.Session.create(api_key=self._secret_key, **params)

    def _default_portal(self, **params):
        import stripe
        return stripe.billing_portal.Session.create(api_key=self._secret_key, **params)

    def _default_list(self, **params):
        import stripe
        return stripe.Subscription.list(api_key=self._secret_key, **params)

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

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        """建 Billing Portal Session（改付款方式／取消訂閱），回 portal URL。
        失敗分類與重試政策**刻意與 create_checkout_session 完全一致**（同一邊界、
        同一套規則）：transient（連線／限流）→ ConnectionError → app 統一 502
        「稍後重試」，由使用者重按；semantic → BillingError。
        單次嘗試不重試：portal session 建立同屬非冪等寫入（工程原則 2）——雖然
        重複建立只是多產生一條短效連結（無金流副作用），但重試決策留在同一處，
        不讓「這個大概沒差」在邊界內開特例。"""
        import stripe
        try:
            session = self._portal(customer=customer_id, return_url=return_url)
        except (stripe.APIConnectionError, stripe.RateLimitError) as e:
            raise ConnectionError(f"stripe 連線失敗: {type(e).__name__}") from e
        except stripe.StripeError as e:
            raise BillingError(f"stripe portal 建立被拒: {type(e).__name__}: "
                               f"{getattr(e, 'user_message', None) or e}") from e
        url = session["url"] if isinstance(session, dict) else getattr(session, "url", None)
        if not url:
            raise BillingError("stripe 回應缺 portal url")
        return url


    def list_subscriptions(self, *, limit: int = 100) -> dict:
        """列出 Stripe 上的訂閱（對帳用，**唯讀**）。回
        `{"subscriptions": [...], "truncated": bool}`。

        ⭐ 與 checkout/portal 的關鍵差異——**這是讀取，冪等，重試安全**：
        重跑一次只是多讀一次，不會多開一張訂閱、不會多扣一次錢。checkout/portal
        是非冪等寫入，回應遺失時重試會產生重複副作用，故那兩處刻意單次嘗試
        （工程原則 2）。本方法沒有那個約束，日後要在邊界內加退避重試是安全的；
        目前**刻意先不加**：這是 admin 手動開啟的營運檢視，失敗即 502、由管理員重新
        整理頁面即可，人肉重試比隱藏的自動重試更容易看見 Stripe 端的異常。

        `status="all"`：Stripe 預設只回未取消者，但對帳最想抓的正是「本地 active
        而 Stripe 已取消」（漏財），漏掉已取消者等於漏掉主要訊號。

        分頁：`limit` 是**每頁**筆數（Stripe 上限 100），總量靠 SDK 的
        `auto_paging_iter()` 翻完；再套 `MAX_RECONCILE_SUBSCRIPTIONS` 硬上限防無限迴圈。
        達上限時回 `truncated=True` 而不是靜默截斷——截斷的清單會讓「本地有、Stripe
        沒有」變成假漂移，呼叫端必須知道自己拿到的是不完整的樣本。

        欄位白名單投影（沿 `_plan_public` 慣例）：只取對帳需要的 id/status/customer/
        metadata.account_id，其餘 Stripe 欄位（含金額、卡片資訊）不進本服務。
        """
        import stripe
        subs: list[dict] = []
        truncated = False
        try:
            page = self._list(status="all", limit=limit)
            # auto_paging_iter 是惰性的：翻頁的網路呼叫發生在迭代當中，
            # 故整個迴圈都必須在 try 內，否則第二頁的失敗會逃出失敗分類。
            for obj in page.auto_paging_iter():
                if len(subs) >= MAX_RECONCILE_SUBSCRIPTIONS:
                    truncated = True
                    break
                subs.append(_subscription_public(obj))
        except (stripe.APIConnectionError, stripe.RateLimitError) as e:
            raise ConnectionError(f"stripe 連線失敗: {type(e).__name__}") from e
        except stripe.StripeError as e:
            raise BillingError(f"stripe 訂閱列表被拒: {type(e).__name__}: "
                               f"{getattr(e, 'user_message', None) or e}") from e
        if truncated:
            # 大聲留痕（工程原則 3）：對帳樣本不完整是「結論不可信」而非「沒事」
            logger.warning("stripe 訂閱列表達上限 %d 筆，對帳樣本不完整",
                           MAX_RECONCILE_SUBSCRIPTIONS)
        return {"subscriptions": subs, "truncated": truncated}


def _subscription_public(obj) -> dict:
    """Stripe Subscription → 對帳用 dict（白名單欄位）。obj 可為 StripeObject
    或純 dict（測試 fake），兩者皆支援 `.get`。"""
    md = obj.get("metadata") or {}
    return {
        "id": obj.get("id"),
        "status": obj.get("status"),
        "customer": obj.get("customer"),
        "metadata": {"account_id": md.get("account_id") if hasattr(md, "get") else None},
    }


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


# ---------- 方案目錄（定價頁資料源） ----------

@dataclass(frozen=True)
class PlanFeature:
    """text_key：前端 copy.ts 的鍵——⭐ 後端不寫死使用者可見文案（紅線 1）。
    included：該方案是否包含此功能。
    shipped：該功能是否**已推出**（False = 已規劃、開發中）。included 與 shipped
    刻意分成兩個軸：付費層可以「包含但尚未推出」，前端據此標「開發中」而不是
    假裝可用——不賣不存在的功能。"""
    text_key: str
    included: bool
    shipped: bool


@dataclass(frozen=True)
class Plan:
    id: str
    name_key: str
    price_display: str | None   # None = 免費（或價格未設定 → 前端顯示「價格待定」）
    stripe_price_id: str | None  # ⭐ 內部欄位，不外流（見 _plan_public 白名單）
    features: tuple[PlanFeature, ...]


# 功能鍵（單一真相：前端 copy.ts 用同名鍵渲染）
_F_COPYTRADE = "plans.feature.copytrade"
_F_KILLSWITCH = "plans.feature.killswitch"
_F_MULTI_LEADER = "plans.feature.multiLeader"
_F_RATIO_SLIDER = "plans.feature.ratioSlider"

# ⭐ 方案 B（免費層＋月費），使用者拍板：**免費層不受限制**——跟單不限本金、
# 回撤保護照給；付費解鎖的是多 leader 與比例 slider，而這兩個功能都**尚未開發**
# （Phase 3/4）。Phase 3/4 完成時把對應 shipped 改 True（tests/test_publicapi_billing.py
# 有測試釘住 False，屆時會紅燈提醒——這是刻意的提醒機制，不是壞測試）。
_FREE_FEATURES = (
    PlanFeature(_F_COPYTRADE, included=True, shipped=True),
    PlanFeature(_F_KILLSWITCH, included=True, shipped=True),
    PlanFeature(_F_MULTI_LEADER, included=False, shipped=False),
    PlanFeature(_F_RATIO_SLIDER, included=False, shipped=False),
)
_PRO_FEATURES = (
    PlanFeature(_F_COPYTRADE, included=True, shipped=True),
    PlanFeature(_F_KILLSWITCH, included=True, shipped=True),
    PlanFeature(_F_MULTI_LEADER, included=True, shipped=False),
    PlanFeature(_F_RATIO_SLIDER, included=True, shipped=False),
)


def _plan_public(plan: Plan, *, billing_enabled: bool) -> dict:
    """Plan → 對外 dict。⭐ 白名單列欄位（不是 asdict 再 pop）：stripe_price_id
    要外流必須有人**主動加一行**，結構性防洩（工程原則 5 的精神）。"""
    return {
        "id": plan.id,
        "name_key": plan.name_key,
        "price_display": plan.price_display,
        "features": [{"text_key": f.text_key, "included": f.included,
                      "shipped": f.shipped} for f in plan.features],
        # 可購買 = 有 price_id **且** billing 已啟用。免費方案無 price_id → 恆 False。
        "purchasable": bool(plan.stripe_price_id) and billing_enabled,
    }


def plan_catalog(cfg) -> dict:
    """回方案目錄。

    設計：**功能清單版本控管於程式碼**（結構穩定、可測試），**價格與 Stripe price_id
    走設定**（使用者尚未拍板價格，改設定不改碼）。`shipped=False` 表示該功能已規劃但
    尚未推出——前端據此標示「開發中」而非假裝可用（誠實揭露：不賣不存在的功能）。

    billing 未設定時仍回完整目錄（billing_enabled=False、purchasable=False）：
    定價頁在未接金流時要顯示「即將開放」，而不是整頁消失。"""
    plans = (
        Plan(id="free", name_key="plans.free.name", price_display=None,
             stripe_price_id=None, features=_FREE_FEATURES),
        Plan(id="pro", name_key="plans.pro.name",
             price_display=cfg.stripe_price_display,
             stripe_price_id=cfg.stripe_price_id, features=_PRO_FEATURES),
    )
    return {"billing_enabled": cfg.billing_enabled,
            "plans": [_plan_public(p, billing_enabled=cfg.billing_enabled)
                      for p in plans]}
