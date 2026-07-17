"""src/spark/publicapi/app.py
FastAPI app factory。所有外部依賴（store / keysvc client / HL gateway / 時鐘）由
create_app 注入——測試全離線。onboarding 端點一律綁 session 地址：account_id 由
session 衍生，端點無 account 參數（紅線 3：別人不能替你 onboard 是結構保證）。"""
import logging
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from spark.keysvc.client import KeysvcError
from spark.publicapi.approvals import build_approve_agent, build_approve_builder_fee
from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address
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


def create_app(cfg: ApiConfig, store: ApiStore, keysvc, hl, now_fn=time.time) -> FastAPI:
    app = FastAPI(title="filet public api",
                  docs_url=None, redoc_url=None, openapi_url=None)

    def _require_session(request: Request) -> str:
        sid = request.cookies.get(SESSION_COOKIE)
        addr = store.get_session_address(sid, now_s=now_fn()) if sid else None
        if addr is None:
            raise HTTPException(status_code=401, detail="未登入或 session 已過期")
        return addr

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
    def admin_pending(address: str = Depends(_require_session)):
        """管理端唯讀：檢視 pending 清單（逐筆核對 builder_address 用）。啟用走人工
        CLI scripts/filet_activate.py，web 層無任何 systemd/寫 manifest 權。"""
        if address not in cfg.admin_addresses:  # 兩側皆 normalize 過
            raise HTTPException(status_code=403, detail="非管理員")
        return {"pending": load_pending(cfg.pending_path)}

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

    return app
