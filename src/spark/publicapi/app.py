"""src/spark/publicapi/app.py
FastAPI app factory。所有外部依賴（store / keysvc client / HL gateway / 時鐘）由
create_app 注入——測試全離線。onboarding 端點一律綁 session 地址：account_id 由
session 衍生，端點無 account 參數（紅線 3：別人不能替你 onboard 是結構保證）。"""
import logging
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer
from spark.publicapi.store import ApiStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "filet_session"


class VerifyBody(BaseModel):
    nonce: str
    signature: str


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

    return app
