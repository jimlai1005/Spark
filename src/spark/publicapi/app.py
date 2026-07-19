"""src/spark/publicapi/app.py
FastAPI app factory。所有外部依賴（store / keysvc client / HL gateway / 時鐘）由
create_app 注入——測試全離線。onboarding 端點一律綁 session 地址：account_id 由
session 衍生，端點無 account 參數（紅線 3：別人不能替你 onboard 是結構保證）。"""
import logging
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from spark.filet.followers import load_followers_tolerant
from spark.keysvc.client import KeysvcError
from spark.publicapi.approvals import build_approve_agent, build_approve_builder_fee
from spark.publicapi.billing import (BillingError, BillingSignatureError,
                                     apply_webhook_event, has_active_subscription,
                                     plan_catalog, verify_webhook_event)
from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address
from spark.publicapi.ops import (customer_pnl, jsonable, load_accrued_series,
                                 revenue_reconciliation)
from spark.publicapi.pending import load_pending, write_pending_entry
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer
from spark.publicapi.store import ApiStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "filet_session"


class VerifyBody(BaseModel):
    nonce: str
    signature: str


class ChainIdBody(BaseModel):
    chain_id: int


def create_app(cfg: ApiConfig, store: ApiStore, keysvc, hl, now_fn=time.time,
               billing=None) -> FastAPI:
    app = FastAPI(title="filet public api",
                  docs_url=None, redoc_url=None, openapi_url=None)

    # 單一邊界（工程原則 5）：HL resilience 重試耗盡後上拋的 transient 例外，
    # 統一轉譯成 502（而非通用 500），供前端判斷「稍後重試」。逐端點不再各自 try/except。
    @app.exception_handler(ConnectionError)
    async def _hl_conn_error(request, exc):
        return JSONResponse(status_code=502, content={"detail": "上游服務暫時不可用，請稍後重試"})

    @app.exception_handler(TimeoutError)
    async def _hl_timeout(request, exc):
        return JSONResponse(status_code=502, content={"detail": "上游服務逾時，請稍後重試"})

    @app.exception_handler(BillingError)
    async def _billing_error(request, exc):
        # semantic 失敗（設定錯/請求被拒）：不重試、大聲留痕（工程原則 3）
        logger.error("stripe 語意失敗: %s", exc)
        return JSONResponse(status_code=502,
                            content={"detail": "計費服務錯誤，請稍後重試或聯絡管理員"})

    def _require_session(request: Request) -> str:
        sid = request.cookies.get(SESSION_COOKIE)
        addr = store.get_session_address(sid, now_s=now_fn()) if sid else None
        if addr is None:
            raise HTTPException(status_code=401, detail="未登入或 session 已過期")
        return addr

    def _require_admin(address: str = Depends(_require_session)) -> str:
        """⭐ 管理端唯一一道閘（單一定義，工程原則 5 的授權版）：**所有**跨客戶端點
        都必須經過它。無 session → 401（由 _require_session 拋）、非白名單 → 403。
        刻意做成 dependency 而非各端點各寫一次 if——「跨客戶聚合」是全新的存取模式
        （其餘端點都 session-scoped），逐點複製檢查遲早會漏掉一點。"""
        if address not in cfg.admin_addresses:  # 兩側皆 normalize 過
            raise HTTPException(status_code=403, detail="非管理員")
        return address

    def _require_billing() -> None:
        if billing is None or not cfg.billing_enabled:
            raise HTTPException(status_code=501, detail="計費未啟用")

    # ---------- auth ----------
    @app.get("/api/auth/nonce")
    def auth_nonce(address: str, chain_id: int):
        try:
            addr = normalize_address(address)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if chain_id <= 0:
            raise HTTPException(status_code=400, detail="chain_id 不合法")
        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(addr, chain_id, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        message = build_siwe_message(domain=cfg.siwe_domain, uri=cfg.siwe_uri,
                                     address=addr, chain_id=chain_id,
                                     nonce=nonce, issued_at=issued_at)
        return {"nonce": nonce, "message": message}

    @app.post("/api/auth/verify")
    def auth_verify(body: VerifyBody, response: Response):
        rec = store.consume_nonce(body.nonce, now_s=now_fn())  # 原子單次使用（紅線 4）
        if rec is None:
            raise HTTPException(status_code=401, detail="nonce 不存在、已用過或已過期")
        message = build_siwe_message(domain=cfg.siwe_domain, uri=cfg.siwe_uri,
                                     address=rec.address, chain_id=rec.chain_id,
                                     nonce=body.nonce, issued_at=rec.issued_at)
        try:
            signer = normalize_address(recover_siwe_signer(message, body.signature))
        except Exception:  # noqa: BLE001 — 壞簽名格式一律 401，不洩內部
            raise HTTPException(status_code=401, detail="SIWE 簽名無效") from None
        if signer != rec.address:  # 兩側皆 normalize（工程原則 1：同基準比較）
            raise HTTPException(status_code=401, detail="SIWE 簽名無效")
        sid = store.create_session(signer, now_s=now_fn(), ttl_s=cfg.session_ttl_s)
        response.set_cookie(SESSION_COOKIE, sid, max_age=cfg.session_ttl_s,
                            httponly=True, secure=True, samesite="lax", path="/")
        return {"address": signer, "account_id": derive_account_id(signer)}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            store.delete_session(sid)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(address: str = Depends(_require_session)):
        return {"address": address, "account_id": derive_account_id(address)}

    # ---------- onboarding ----------
    @app.post("/api/onboard/agent")
    def onboard_agent(address: str = Depends(_require_session)):
        account_id = derive_account_id(address)
        store.ensure_onboarding(account_id, address)
        if store.get_agent_address(account_id):
            raise HTTPException(
                status_code=409,
                detail="已有 agent，不重生（避免 rotate 作廢既有鏈上授權）")
        try:
            agent_address = normalize_address(keysvc.generate(account_id))
        except KeysvcError as e:  # 結構化 code 分支——不比對訊息字串（opus 審 M3）
            if e.code == "exists":
                # keystore 有 key、DB 無地址（DB 遺失/回應遺失殘局）：
                # 唯讀 address op 自癒回填（設計定案 12），使用者不卡死。
                try:
                    agent_address = normalize_address(keysvc.address(account_id))
                except Exception as e2:  # noqa: BLE001 — 自癒也失敗才放棄，大聲告警
                    logger.error(
                        "keystore 與 DB 狀態不一致且無法自動復原 account=%s: %s",
                        account_id, e2)
                    raise HTTPException(
                        status_code=409,
                        detail="keystore 與 DB 狀態不一致且無法自動復原，"
                               "請聯絡管理員") from e2
                store.set_agent_address(account_id, agent_address)
                logger.warning("agent 地址自癒回填 account=%s", account_id)
                return {"agent_address": agent_address, "recovered": True}
            logger.error("keysvc generate 失敗 account=%s: %s", account_id, e)
            raise HTTPException(status_code=502, detail="金鑰服務暫時不可用") from e
        except OSError as e:  # socket 連不上等——安全關鍵路徑大聲失敗（工程原則 3）
            logger.error("keysvc 不可達 account=%s: %s", account_id, e)
            raise HTTPException(status_code=502, detail="金鑰服務暫時不可用") from e
        store.set_agent_address(account_id, agent_address)
        return {"agent_address": agent_address}

    def _progress(address: str) -> dict:
        """onboarding 進度：狀態靠鏈上查詢判定（冪等、斷點續走以此為準，沿 M1 精神）。"""
        account_id = derive_account_id(address)
        agent_address = store.get_agent_address(account_id)
        builder_fee_approved = hl.max_builder_fee(address, cfg.builder_address) != 0
        agent_approved = bool(agent_address) and agent_address in hl.agent_addresses(address)
        funded = hl.get_account_value(address) >= cfg.min_user_deposit  # 常數單一來源（M4）
        ready = bool(agent_address) and builder_fee_approved and agent_approved and funded
        return {
            "address": address, "account_id": account_id,
            "agent_address": agent_address,
            "agent_generated": agent_address is not None,
            "builder_fee_approved": builder_fee_approved,
            "agent_approved": agent_approved,
            "funded": funded,
            "state": "READY" if ready else "IN_PROGRESS",
        }

    @app.get("/api/onboard/status")
    def onboard_status(address: str = Depends(_require_session)):
        return _progress(address)  # 純讀；副作用（寫 pending）只在 POST /api/onboard/verify

    @app.post("/api/onboard/verify")
    def onboard_verify(address: str = Depends(_require_session)):
        """檢查全過 → 寫 pending 條目（等管理端人工 CLI 核准；spec：activate 不做成
        API 端點）。未全過 → 回進度供斷點續走（冪等，可重跑）。"""
        p = _progress(address)
        if p["state"] == "READY":
            # ⭐ user_address 出自 session、builder_address 出自伺服器設定（紅線 6）
            write_pending_entry(cfg.pending_path, account_id=p["account_id"],
                                user_address=address,
                                builder_address=cfg.builder_address,
                                network=cfg.network,
                                agent_address=p["agent_address"])
        return p

    @app.get("/api/admin/pending")
    def admin_pending(admin: str = Depends(_require_admin)):
        """管理端唯讀：檢視 pending 清單（逐筆核對 builder_address 用）。啟用走人工
        CLI scripts/filet_activate.py，web 層無任何 systemd/寫 manifest 權。"""
        return {"pending": load_pending(cfg.pending_path)}

    # ---------- 營運後台 /ops（admin only；全 repo 唯一的跨客戶聚合） ----------
    def _load_followers():
        """讀 followers manifest（唯讀；寫入只有人工 activate CLI）。
        容錯載入：一個壞條目不該讓整張營運報表變空白，壞條目併同回報。"""
        try:
            return load_followers_tolerant(cfg.followers_path)
        except FileNotFoundError as e:
            # 大聲失敗：manifest 不存在時回空清單會被誤讀成「沒有客戶」（工程原則 3）
            logger.error("followers manifest 不存在: %s", cfg.followers_path)
            raise HTTPException(status_code=503,
                                detail="follower manifest 不存在，請聯絡管理員") from e

    @app.get("/api/ops/customers")
    def ops_customers(days: int = 1, admin: str = Depends(_require_admin)):
        """每客戶損益（跨客戶聚合，admin only）。時間窗＝now 往回 days 天。
        單一 follower 查詢失敗只影響該列的 error 欄，不影響其他客戶（ops.customer_pnl）。"""
        if not 1 <= days <= 90:
            raise HTTPException(status_code=400, detail="days 須介於 1 到 90")
        refs, manifest_errors = _load_followers()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        rows = customer_pnl(refs, hl, start, end, store=store)
        return jsonable({"days": days, "start": start.isoformat(),
                         "end": end.isoformat(), "customers": rows,
                         "manifest_errors": manifest_errors})

    @app.get("/api/ops/revenue")
    def ops_revenue(threshold_pct: float = 0.01, admin: str = Depends(_require_admin)):
        """收入對帳（admin only）：應收（Σ 各客戶歸屬 builder_fee）vs 實收（北極星
        accrued 今昨差）。

        ⚠️ 同基準（工程原則 1）：accrued 的今昨差來自日報腳本每日查一次落下的歷史
        序列，其涵蓋期間是「最新一筆的那個 UTC 日」——故 fills 時間窗一律取**同一個
        UTC 日**，而不是 now 往回 24 小時。兩邊窗口錯開會造出純屬錯配的假 discrepancy。

        歷史序列不足兩點時不硬算（缺 accrued_prev 就把整段累積量當成單日增量，
        會產生天文數字的假 delta）：回 insufficient_accrued_history，數值欄留 null。"""
        if threshold_pct < 0:
            raise HTTPException(status_code=400, detail="threshold_pct 不得為負")
        refs, manifest_errors = _load_followers()
        series = load_accrued_series(cfg.accrued_history_path)
        if len(series) < 2:
            return jsonable({
                "insufficient_accrued_history": True,
                "history_points": len(series),
                "detail": "accrued 歷史不足兩點，無法計算單日實收增量"
                          "（由 scripts/copytrade_daily_report.py 每日累積）",
                "manifest_errors": manifest_errors,
            })
        (prev_day, accrued_prev), (day_iso, accrued_now) = series[-2], series[-1]
        day = date.fromisoformat(day_iso)
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        # 當日仍在進行中 → 取到 now（與日報腳本的 UTC 0 點~now 同慣例）；已過完的日子取整日
        end = now if day >= now.date() else start + timedelta(days=1)
        rows = customer_pnl(refs, hl, start, end, store=store)
        result = revenue_reconciliation(rows, accrued_now, accrued_prev,
                                        threshold_pct=threshold_pct)
        if result["over_threshold"]:
            # 對帳超標＝收入歸屬與鏈上實收對不上，大聲留痕（工程原則 3）
            logger.warning("收入對帳超標 day=%s attributed=%s accrued_delta=%s pct=%s",
                           day_iso, result["attributed"], result["accrued_delta"],
                           result["discrepancy_pct"])
        return jsonable({**result, "insufficient_accrued_history": False,
                         "day": day_iso, "prev_day": prev_day,
                         "window_start": start.isoformat(), "window_end": end.isoformat(),
                         "customers": rows, "manifest_errors": manifest_errors})

    # ---------- 待簽 payload（後端建 typed data，不簽；前端簽完直送 HL /exchange） ----------
    @app.post("/api/onboard/payload/approve-agent")
    def payload_approve_agent(body: ChainIdBody,
                              address: str = Depends(_require_session)):
        account_id = derive_account_id(address)
        agent_address = store.get_agent_address(account_id)
        if not agent_address:
            raise HTTPException(status_code=409,
                                detail="尚未生成 agent，先呼叫 /api/onboard/agent")
        if body.chain_id <= 0:
            raise HTTPException(status_code=400, detail="chain_id 不合法")
        # ⭐ agentAddress/agentName 出自伺服器（keysvc 地址＋設定常數），不收使用者輸入
        typed_data, _action = build_approve_agent(
            agent_address=agent_address, agent_name=cfg.agent_name,
            wallet_chain_id=body.chain_id, is_mainnet=cfg.is_mainnet)
        # action 不落地：前端持有 typed data 簽完直送 HL，提交結果由 status 鏈上查詢確認
        return {"typed_data": typed_data}

    @app.post("/api/onboard/payload/approve-builder-fee")
    def payload_approve_builder_fee(body: ChainIdBody,
                                    address: str = Depends(_require_session)):
        account_id = derive_account_id(address)
        store.ensure_onboarding(account_id, address)
        if body.chain_id <= 0:
            raise HTTPException(status_code=400, detail="chain_id 不合法")
        # builder 啟用門檻（spec 錯誤處理；沿 M1 BuilderNotEligible）：<100 USDC 時
        # builder code 不生效，症狀是「成交但 fee 不累計」——這裡大聲擋下。
        if hl.get_account_value(cfg.builder_address) < cfg.min_builder_balance:
            raise HTTPException(
                status_code=503,
                detail=f"builder 地址餘額低於 {cfg.min_builder_balance} USDC 門檻，"
                       "暫停 onboarding，請聯絡管理員")
        # ⭐ builder/maxFeeRate 出自伺服器設定常數，不收使用者輸入（紅線 6）
        typed_data, _action = build_approve_builder_fee(
            builder=cfg.builder_address, max_fee_rate=cfg.max_fee_rate,
            wallet_chain_id=body.chain_id, is_mainnet=cfg.is_mainnet)
        return {"typed_data": typed_data}

    # ---------- billing（M3 計費骨幹；測試模式 only，sk_test_ 由 ApiConfig 強制） ----------
    @app.post("/api/billing/webhook")
    async def billing_webhook(request: Request):
        # ⭐ 全 app 唯一不走 session auth 的端點（紅線 2）：Stripe 伺服器對伺服器
        # 回呼無 cookie；授權由 Stripe-Signature HMAC 驗簽取代（secret 只有 Stripe
        # 與本服務知道）。驗簽不過一律 400、不碰 DB。async：需先取 raw body 驗簽。
        _require_billing()
        payload = await request.body()
        sig = request.headers.get("stripe-signature", "")
        try:
            event = verify_webhook_event(payload, sig, cfg.stripe_webhook_secret)
        except BillingSignatureError:
            # 不進 BillingError 的 502 handler：簽名壞是呼叫者的錯（400），
            # 且刻意不回洩簽名失敗細節。log 為稽核痕跡（偽造探測）：靜態訊息，
            # 不含 payload/簽名原文，避免可疑內容進 log。
            logger.warning("billing webhook 驗簽失敗")
            raise HTTPException(status_code=400, detail="webhook 驗簽失敗") from None
        outcome = apply_webhook_event(store, event,
                                      event_created=int(event.get("created", 0)),
                                      now_s=now_fn())
        return {"received": True, "outcome": outcome}

    @app.post("/api/billing/checkout")
    def billing_checkout(address: str = Depends(_require_session)):
        """建 Checkout Session、回 URL。session 綁定：account_id 由 session 衍生，
        端點無輸入參數。冪等擋板：已 active → 409。
        Stripe 失敗分類（紅線 4）：transient=ConnectionError→502 稍後重試（人肉重試，
        非冪等寫入不在後端盲重試）；semantic=BillingError→502 專屬 handler。"""
        _require_billing()
        account_id = derive_account_id(address)
        rec = store.get_billing(account_id)
        if rec is not None and rec.status == "active":
            raise HTTPException(status_code=409, detail="已有生效訂閱")
        url = billing.create_checkout_session(
            account_id=account_id, price_id=cfg.stripe_price_id,
            success_url=f"{cfg.siwe_uri}/billing?checkout=success",
            cancel_url=f"{cfg.siwe_uri}/billing?checkout=cancel",
            customer_id=rec.stripe_customer_id if rec else None)
        return {"checkout_url": url}

    @app.get("/api/billing/plans")
    def billing_plans():
        """方案目錄（定價頁資料源）。⭐ 兩個刻意的豁免：
        1. **不需 session**——定價頁要能在登入前瀏覽（全 app 第二個 session 豁免端點，
           但與 webhook 不同，這裡沒有任何授權需求：回的是公開商品資訊、無帳號資料、
           無 DB 讀取、無 Stripe 呼叫）。
        2. **不過 _require_billing**——billing 未設定時仍回完整目錄
           （billing_enabled=false、purchasable=false），前端據此顯示「即將開放」，
           而不是整頁 501 消失。
        回傳不含 stripe_price_id（plan_catalog 白名單欄位，結構性）。"""
        return plan_catalog(cfg)

    @app.post("/api/billing/portal")
    def billing_portal(address: str = Depends(_require_session)):
        """Stripe Customer Portal（自助改付款方式／取消訂閱），回 portal URL。
        session 綁定：customer_id 由 session 衍生的 account 查 DB 得到，端點無輸入
        參數——使用者不可能指定別人的 customer（沿「別人不能替你 onboard」精神）。
        無 customer_id → 409：portal 只能管理既有訂閱，沒有 customer 就無可管理者。
        Stripe 失敗分類（紅線 4）：transient=ConnectionError→502 稍後重試；
        semantic=BillingError→502 專屬 handler。"""
        _require_billing()
        account_id = derive_account_id(address)
        rec = store.get_billing(account_id)
        if rec is None or not rec.stripe_customer_id:
            raise HTTPException(status_code=409, detail="尚無訂閱記錄，請先訂閱")
        url = billing.create_portal_session(customer_id=rec.stripe_customer_id,
                                            return_url=f"{cfg.siwe_uri}/billing")
        return {"url": url}

    @app.get("/api/billing/status")
    def billing_status(address: str = Depends(_require_session)):
        """讀 DB（webhook 是唯一寫入者）。active 欄位 = entitlement 查詢結果——
        僅供前端顯示；不接任何自動停用邏輯（紅線 6）。"""
        _require_billing()
        account_id = derive_account_id(address)
        rec = store.get_billing(account_id)
        return {"account_id": account_id,
                "status": rec.status if rec else "none",
                "active": has_active_subscription(store, account_id)}

    return app
