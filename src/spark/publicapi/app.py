"""src/spark/publicapi/app.py
FastAPI app factory。所有外部依賴（store / keysvc client / HL gateway / 時鐘）由
create_app 注入——測試全離線。onboarding 端點一律綁 session 地址：account_id 由
session 衍生，端點無 account 參數（紅線 3：別人不能替你 onboard 是結構保證）。"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from spark.filet.capital_settings import (CapitalSettingsError,
                                          build_capital_settings_message,
                                          build_capital_settings_record,
                                          canonical_capital_values,
                                          load_capital_settings,
                                          require_bool_flag,
                                          validate_capital_bounds,
                                          verify_capital_settings,
                                          write_capital_settings)
from spark.filet.capital_settings_apply import capital_fingerprint
from spark.filet.followers import load_followers_tolerant
from spark.filet.leader_change import (LeaderChangeError, build_leader_change_message,
                                       build_leader_change_record,
                                       load_leader_changes, verify_leader_change,
                                       write_leader_change)
from spark.filet.leader_perf import (BASIS_NOTE, INSUFFICIENCY_MARKERS,
                                     MDD_SAMPLING_NOTE, UPPER_BOUND_NOTE)
from spark.filet.leaderboard import load_latest_snapshot, snapshot_rows_by_address
from spark.filet.leaders import LeaderRef, is_selectable, load_leaders
from spark.filet.user_leaders import load_user_leaders, record_user_leader
from spark.keysvc.client import KeysvcError
from spark.publicapi.approvals import build_approve_agent, build_approve_builder_fee
from spark.publicapi.billing import (PENDING_CHECKOUT_TTL_S, BillingError,
                                     BillingSignatureError, apply_webhook_event,
                                     has_active_subscription, plan_catalog,
                                     verify_webhook_event)
from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address
# 健康面板讀的是**引擎自己寫的**狀態檔——路徑常數與判定一律引用引擎的定義，
# 不在 API 這側重新宣告（兩份定義漂移的症狀是面板永遠顯示健康）。
from spark.copytrade.equity import sample_coverage
from spark.copytrade.killswitch import ALERTS_LOG_RELPATH, is_tripped
from spark.filet.engine_health import (HEARTBEAT_STALE_S, HeartbeatRead,
                                       heartbeat_path_for, read_heartbeat)
from spark.filet.leader_change_apply import LEDGER_RELPATH as LC_LEDGER_RELPATH
from spark.filet.leader_change_apply import scan_unapplied_leader_changes
from spark.publicapi.ops import ENGINE_STALE_S
from spark.publicapi.ops import (accrued_window, accrued_window_note, customer_pnl,
                                 follower_health, health_summary, jsonable,
                                 load_accrued_series, load_skipped_notional,
                                 revenue_reconciliation, skipped_path_for,
                                 subscription_drift, trade_quality_rows,
                                 trade_quality_summary, utc_days_in_window)
from spark.filet.risk_prefs import (RiskPrefsError, canonical_prefs, prefs_summary,
                                    safe_fallback_prefs)
from spark.publicapi.pending import (load_pending, set_pending_risk,
                                     write_pending_entry)
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer
from spark.publicapi.store import ApiStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "filet_session"

# ⭐ 自訂 leader 探測面的 per-session rate limit（2026-07-27）。preview 與 message
# 兩個端點是「非精選位址的探測／上游放大」面：有 session 者可反覆查不同位址枚舉
# 平台的停用清單（治理資訊），且每次查詢會打一次 HL /info（對交易所是流量放大）。
# sliding-window log：每個 session 位址在 PROBE_RATELIMIT_WINDOW_S 內最多
# PROBE_RATELIMIT_MAX 次，超過回 429。設成模組常數方便調參。對正常用戶零感知
# （沒有人一分鐘查十個位址）。POST select 不套（簽章 gated、濫用成本高）、
# /api/leaders 目錄不套（走離線快照、不打 HL）。
PROBE_RATELIMIT_WINDOW_S = 60.0
PROBE_RATELIMIT_MAX = 10

# 換 leader 驗簽失敗 → 回給客戶的**分類化**訊息。key 是 LeaderChangeError.reason
# （伺服器產生的機器可讀碼），value 是可以安全外顯的固定字串。
# ⭐ 刻意不是 `str(e)`（opus 審查 Minor 2）：例外訊息為了除錯內嵌了請求原值
# （nonce／issued_at／位址），回顯它與本端點自陳的「不記 signature／message 原文」
# 政策直接矛盾。放在模組層是為了讓測試能把它當**單一來源**做白名單式斷言
# （見 test_api_leader_select.test_error_detail_never_echoes_client_input）——
# 表格與斷言各抄一份字串就會漂移，而漂移的那一天沒有人會發現。
LEADER_CHANGE_DETAIL_DEFAULT = "簽章驗證失敗，請重新取得待簽原文並重簽"
LEADER_CHANGE_DETAIL = {
    "malformed": "請求欄位格式不正確，請重新取得待簽原文並重簽",
    "account_mismatch": "請求的帳號與登入身分不符",
    "expired": "簽章已過期，請重新取得待簽原文並重簽",
    "bad_signature": "簽章無法驗證，請重新簽署",
    "signer_mismatch": "簽章者不是本帳號的持有人",
    "nonce_unusable": "這份授權已被使用或已過期，請重新取得待簽原文並重簽",
}

# 資金設定驗簽失敗 → 回給客戶的分類化訊息。與換 leader 分成兩張表（不是共用一張）：
# 兩者的 reason 集合不同（資金設定多了 action_mismatch／out_of_range），共用一張表
# 會讓其中一邊的缺鍵靜默落到 DEFAULT，而客戶看到的是一句無法據以行動的通用訊息。
# ⭐ 同樣刻意不是 `str(e)`：例外訊息為了除錯內嵌了請求原值（nonce、金額），
# 回顯它與「不記 signature／message 原文」的政策矛盾。
CAPITAL_SETTINGS_DETAIL_DEFAULT = "資金設定驗證失敗，請重新取得待簽原文並重簽"
CAPITAL_SETTINGS_DETAIL = {
    "malformed": "請求欄位格式不正確，請重新取得待簽原文並重簽",
    "out_of_range": "數值超出允許範圍：投入本金必須大於 0，"
                    "使用比例必須落在 0（不含）到 1（含）之間",
    "account_mismatch": "請求的帳號與登入身分不符",
    "action_mismatch": "這份簽章不是資金設定授權，請重新取得待簽原文並重簽",
    "expired": "簽章已過期，請重新取得待簽原文並重簽",
    "bad_signature": "簽章無法驗證，請重新簽署",
    "signer_mismatch": "簽章者不是本帳號的持有人",
    "nonce_unusable": "這份授權已被使用或已過期，請重新取得待簽原文並重簽",
}


class VerifyBody(BaseModel):
    nonce: str
    signature: str


class ChainIdBody(BaseModel):
    chain_id: int


class LeaderSelectBody(BaseModel):
    """客戶簽章的換 leader 請求（欄位＝filet/leader_change.py 的記錄格式）。

    ⭐ `account_id` 是全 app 少數**顯式收 account 參數**的端點（其餘一律由 session
    衍生，見檔頭）。這不是破例，是被簽章本身逼出來的：account_id 是待簽訊息的一部分，
    客戶簽的是「把 **fxxx** 這個帳號換到某 leader」。若伺服器改成自己從 session 推導，
    就會出現「客戶簽的是 A、伺服器套用到 B」的縫；收下來再與 session 衍生值比對
    （不符 403），客戶簽了什麼就只能被套用到什麼。
    """

    account_id: str
    leader_address: str
    nonce: str
    issued_at: str
    signature: str
    # 客戶端實際簽的原文。**驗證完全不看它**（伺服器重建自己的版本，見
    # verify_leader_change）——僅原樣留存，供事後比對「客戶當初到底簽了什麼」。
    message: str = ""


class CapitalSettingsBody(BaseModel):
    """客戶簽章的資金設定請求（欄位＝filet/capital_settings.py 的記錄格式減去 action）。

    ⭐ `action` 刻意**不收**：它由 `build_capital_settings_record` 寫死。讓客戶端
    指定動作類型，等於把域分隔的一半交還給請求內容——而請求內容整份都在攻擊者的
    控制範圍內。

    ⭐ 兩個數值收 `str` 而不是 `float`：float 進不了 Decimal 的精確世界（0.1 在
    float 裡不是 0.1），而這兩個值直接乘進部位大小。收字串讓「客戶簽的字串」與
    「伺服器驗的字串」是同一個東西，不經過任何二進位浮點的中轉。

    `account_id` 顯式收下再與 session 衍生值比對（不符 403），理由同
    LeaderSelectBody：它是待簽訊息的一部分，伺服器代推會出現「客戶簽 A、
    伺服器套用到 B」的縫。
    """

    account_id: str
    allocated_capital: str
    capital_utilization: str
    nonce: str
    issued_at: str
    signature: str
    message: str = ""
    # ⭐ 顯式的本金模式旗標（見 filet/capital_settings.py 檔頭）。收 bool 而非字串：
    # Pydantic 會把 "true"/"1" 之類的寬鬆真值轉成 True，但**記錄層**（require_bool_flag）
    # 只收真正的 bool——這裡先收窄成 bool，落檔時就一定是合法型別。
    # 預設 False＝固定本金模式，也就是**限制較嚴**的那一邊：舊版客戶端不送這個欄位
    # 時行為與改動前完全相同，且漏送不可能放行一筆「用全部權益」的授權。
    use_full_equity: bool = False


# leader 目錄要外流的**快照統計欄位白名單**。watchlist 快照存的是一日一點的資產負債
# 切面（見 filet/leaderboard.py 檔頭），**不是**報酬率／Sharpe——欄位命名刻意沿用
# 快照原名，避免在 API 層改名成看起來像績效指標的東西。
# 刻意不外流的兩個快照欄位：`withdrawable`／`total_margin_used`——對「該不該選這個
# leader」沒有增量資訊，卻極易被讀成與客戶自己資金有關的數字。要外流請主動加一行。
_LEADER_STAT_FIELDS = ("account_value", "total_ntl_pos", "unrealized_pnl",
                       "position_count")

# ⭐ 績效欄位與上面的**規模**欄位刻意分開兩張表、在回應裡也分開兩個物件
# （`performance` vs 平鋪的規模欄位）。理由：`account_value` 之類是資產負債切面，
# `twr`／`max_drawdown` 是報酬率——把兩者混在同一層，遲早有人把規模欄位改名成
# 看起來像績效的名字（或反過來讀），而那個誤讀在畫面上完全看不出來。
_LEADER_PERF_WINDOWS = ("perpMonth", "perpAllTime")
_LEADER_PERF_FIELDS = ("period", "basis", "status", "reason", "disclosure_tier",
                       "sample_count", "covered_days", "first_ts_ms", "last_ts_ms",
                       "skipped_intervals", "cum_pnl", "twr", "max_drawdown",
                       "annualized_return") + INSUFFICIENCY_MARKERS
# ⭐ 不足標記由 `leader_perf.INSUFFICIENCY_MARKERS` **拼進來**，不在這裡重抄一份字串
# （2026-07-19 改版）：改版後績效指標即使資料很薄也照樣外流，唯一的警示載體就是那些
# 標記。白名單漏掉任何一個 → 前端拿到一個沒有任何警示的外推數字，而畫面上完全看不
# 出來。用拼接而非複製，讓「新增了標記卻忘了外流」這個錯誤寫不出來（工程原則 5）。


def _leader_perf_public(stats: dict | None) -> dict | None:
    """快照列的 `perf` → 對外的績效投影；沒有績效資料 → None。

    ⭐⭐ 投影用 `if k in row`（**不是** `row.get(k)`）：舊快照沒有新欄位、`leader_perf`
    在年化數學上無定義時也不回 `annualized_return`。改用 `.get()` 會把這些缺席的鍵
    補成 `null` 送給前端，而前端的 `?? 0`／`|| "—"` 之類寫法會把 null 悄悄變成一個
    數字或一個看起來正常的欄位。這是本函式唯一真正重要的一行。

    ⚠️ 2026-07-19 揭露模型改版後，「資料不足」**不再**由缺鍵承載（見
    `filet/leader_perf.py` 檔頭「揭露模型改版」）：薄資料的 `twr`／`annualized_return`
    照樣外流，警示改由 `INSUFFICIENCY_MARKERS` 那組指標層級標記承載。所以本投影的
    白名單**必須**含那組標記——漏掉等於外流一個無警示的外推數字。

    形狀不符（舊快照、schema 漂移）→ None，不 raise：目錄頁不該因為績效缺席而 500
    （沿本模組既有的兩種降級，見 leaders_directory）。
    """
    if not isinstance(stats, dict):
        return None
    perf = stats.get("perf")
    if not isinstance(perf, dict):
        return None
    windows = perf.get("windows")
    if not isinstance(windows, dict):
        return None
    out = {}
    for w in _LEADER_PERF_WINDOWS:
        row = windows.get(w)
        if isinstance(row, dict):
            out[w] = {k: row[k] for k in _LEADER_PERF_FIELDS if k in row}
    return out or None


def _leader_public(ref: LeaderRef, stats: dict | None) -> dict:
    """LeaderRef ＋ 快照列 → 對外 dict。⭐ 白名單列欄位（不是 asdict 再 pop，沿
    `_plan_public` 慣例）：`enabled`／`accepting_new` 是**內部治理狀態**，不外流——
    客戶不需要知道某個 leader 是「例行下架」還是「安全撤銷」，而後者外流等同於
    公告「這個 leader 出事了」。不可選的 leader 根本不會走到這個函式（見端點）。

    stats 為 None（該 leader 不在 watchlist／快照不可用）→ 統計欄位全 null，
    不填 0：0 會被讀成「這個 leader 沒有部位」，是有意義且錯誤的訊息。
    """
    out = {"address": ref.address, "name": ref.name, "description": ref.description}
    for f in _LEADER_STAT_FIELDS:
        out[f] = stats.get(f) if stats else None
    # 績效**獨立一個子物件**（見 _LEADER_PERF_WINDOWS 上方）。None = 這個 leader
    # 沒有績效資料，與「規模欄位為 null」是同一種誠實：不補 0、不補空物件。
    out["performance"] = _leader_perf_public(stats)
    return out


def _chain_activity(state) -> tuple[Decimal, int]:
    """clearinghouseState 原始回應 → （帳戶權益, 持倉數）。

    自訂 leader 預覽的 `exists` 旗標吃這兩個值（權益 > 0 **或** 有持倉＝有 perp
    活動痕跡；精確錨例見 tests/test_api_leader_preview.py）。形狀不符 → (0, 0)＝
    exists=false（誠實回報「讀不到活動」，前端顯示警示但放行）。⚠️ 自 2026-07-27
    裁決後 exists 不再是准入閘門，只是預覽資訊；真正的 transient 讀取失敗
    （clearinghouse_error）仍會上拋 → 502，不會被誤當成 exists=false。
    """
    if not isinstance(state, dict):
        return Decimal("0"), 0
    summary = state.get("marginSummary")
    try:
        value = Decimal(str(summary.get("accountValue", "0"))) \
            if isinstance(summary, dict) else Decimal("0")
    except (ValueError, ArithmeticError):
        value = Decimal("0")
    positions = state.get("assetPositions")
    return value, (len(positions) if isinstance(positions, list) else 0)


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

    # ⭐ per-session 探測面 rate limit（sliding-window log；狀態 per-app）。
    #   資料結構：address → 該 session 在窗內的請求時間戳列表（逐出過期後計數）。
    #   時鐘走 now_fn（注入的假時鐘可測，不直接 call time.time）。FastAPI sync 端點
    #   跑在 threadpool，read-modify-write 用 threading.Lock 包住（否則兩個 worker
    #   同時通過門檻）。落在此處（create_app 內）→ 狀態隨 app 生死，測試互不汙染。
    _probe_hits: dict[str, list[float]] = {}
    _probe_lock = threading.Lock()
    # 唯讀 introspection seam：讓 reaper 的 dict 大小可被測試斷言（Finding 2 無界
    # 成長回歸）。同一個 dict 物件，不改變任何行為。
    app.state.probe_ratelimit_hits = _probe_hits

    def _enforce_probe_ratelimit(address: str) -> None:
        now = now_fn()
        cutoff = now - PROBE_RATELIMIT_WINDOW_S
        with _probe_lock:
            # ⭐ 每次呼叫順手 reap **所有** key（Finding 2）：把每個位址的時間戳修剪
            #   掉過期的，列表變空的 key 直接刪除。這讓 dict 大小以「近
            #   PROBE_RATELIMIT_WINDOW_S 內 probe 過的位址數」為界，堵住「輪替 session
            #   位址各 probe 一次 → dict 無界成長」（單純修剪單一位址的列表長度不夠，
            #   key 數才是成長維度）。O(keys) 每次呼叫，本規模零/極少用戶可接受。
            for k in list(_probe_hits.keys()):
                kept = [t for t in _probe_hits[k] if t > cutoff]
                if kept:
                    _probe_hits[k] = kept
                else:
                    del _probe_hits[k]
            hits = _probe_hits.get(address, [])   # reap 後：本位址的存活窗（或空）
            if len(hits) >= PROBE_RATELIMIT_MAX:
                raise HTTPException(status_code=429,
                                    detail="查詢過於頻繁，請稍後再試")
            hits.append(now)
            _probe_hits[address] = hits

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

    @app.get("/api/me/leader")
    def me_leader(address: str = Depends(_require_session)):
        """客戶查**自己目前跟隨的 leader**。

        ⭐ 為什麼需要這個端點：跟隨關係的唯一真相在 followers manifest，而 manifest
        原本只有 admin 端點讀得到——客戶因此無從知道自己在跟誰，`/leaders` 頁只能
        顯示「本頁無法標示你目前跟隨的 leader」。這對一個「換 leader 要簽章」的產品
        是個洞：客戶在不知道現況的情況下被要求簽署變更。

        ⭐ **只回自己的**，且結構上不可能回別人的：account_id 由 session 衍生，
        本端點**沒有任何 account 參數**（沿檔頭的既有慣例——「別人不能替你 onboard」
        是結構保證而不是檢查）。想查別人只能先拿到別人的 session。

        ⭐ 四種狀態各有明確語意，**不用 null 讓前端猜**（`leader_address` 為 null
        時，前端必須靠 `status` 才知道是「還沒活化」還是「用引擎預設」）：
        - `following`：manifest 明確指定了 leader。
        - `engine_default`：已活化但未指定 leader，引擎沿用進程 env 的
          `COPY_LEADER_ADDRESS`（leader_resolve 的回退路徑）。這是**真的在跟單**，
          只是跟的對象由部署決定——與「沒在跟單」是完全不同的處境。
        - `not_activated`：manifest 裡沒有這個帳號（活化是人工 CLI 動作，見 pending.py）。
        - `indeterminate`：帳號不在 manifest **且** manifest 有無法解析的條目——
          壞掉的那筆可能就是他自己的。回 `not_activated` 會讓一個正在跟單的客戶
          以為自己沒在跟單（危險方向的誤讀，工程原則 3）。

        manifest 讀不到 → 503（沿營運後台讀 manifest 的同一種失敗處理）：回
        「你沒在跟單」比回錯誤危險，客戶會因此以為資金沒在動而不去看它。

        ⚠️ 本端點刻意**不直接**取用跨客戶的 manifest 載入器，只透過
        `_load_own_follower` 拿自己那一筆——理由見該函式 docstring
        （tests/test_api_ops.py 的跨客戶 admin 閘會結構性檢查這件事）。
        """
        account_id = derive_account_id(address)
        mine, manifest_degraded = _load_own_follower(account_id)

        if mine is None:
            indeterminate = manifest_degraded
            return {
                "account_id": account_id,
                "status": "indeterminate" if indeterminate else "not_activated",
                "leader_address": None, "leader_name": None,
                "pending_change": None,
                "note": ("目前無法確認你的跟隨狀態（帳號清單有無法解析的條目）；"
                         "請聯絡管理員，不要當作「未在跟單」處理。") if indeterminate else
                        "你的帳號尚未啟用跟單（啟用是人工作業）；完成入金與授權後，"
                        "管理員會為你啟用，屆時這裡會顯示你跟隨的 leader。",
            }

        leader = mine.leader_address
        # 名稱只從白名單查（客戶在目錄頁看過的同一份資料）。⚠️ 治理旗標
        # enabled／accepting_new **不外流**（沿 _leader_public 的既有理由）——
        # 查無名稱只代表「不在目前的可選清單裡」，不告訴他是哪一種下架。
        name = None
        if leader is not None:
            try:
                name = next((r.name for r in load_leaders(cfg.leaders_path)
                             if r.address == leader), None)
            except ValueError:
                # 白名單壞掉不該讓客戶查不到自己的 leader：位址本身出自 manifest，
                # 是獨立於白名單的真相。少一個顯示名稱而已，大聲留痕即可。
                logger.error("leader 白名單載入失敗（僅影響顯示名稱） %s", cfg.leaders_path)

        return {
            "account_id": account_id,
            "status": "following" if leader else "engine_default",
            "leader_address": leader,
            "leader_name": name,
            "pending_change": _pending_leader_change(account_id, leader),
            "note": ("這是引擎目前為你跟隨的 leader。" if leader else
                     "你已啟用跟單，但尚未指定 leader，引擎沿用部署的預設設定。"
                     "你可以到 leader 目錄選擇一位——在那之前，跟單仍在進行中。"),
        }

    def _load_own_follower(account_id: str):
        """manifest → **只**這一個帳號的 FollowerRef，回 `(ref | None, 有壞條目)`。

        ⭐ 為什麼不是在端點裡拿 `_load_followers()` 再自己 filter（2026-07-19）：
        `_load_followers` 是登記在案的**跨客戶讀取入口**（tests/test_api_ops.py 的
        `CROSS_CUSTOMER_SOURCES`），凡是直接用它的路由都必須掛 admin 閘——那條結構性
        檢查抓到了本端點，而且抓得對：一個 session-gated 的客戶端點手上握著全體客戶
        的清單，只靠一行 filter 把別人濾掉，是「記得寫對」而不是「寫不錯」。
        這裡把窄化收進單一函式，端點結構上就拿不到別人的資料——多客戶清單的生命週期
        完全不離開這個函式。（工程原則 5 的同型：邊界強制，而非呼叫點自律。）

        回傳的第二個值 = manifest 有無法解析的條目。呼叫端**必須**用它區分
        「確定沒有這個帳號」與「可能有但那筆壞了」——見 me_leader 的 indeterminate。
        """
        refs, manifest_errors = _load_followers()
        return (next((r for r in refs if r.account_id == account_id), None),
                bool(manifest_errors))

    def _pending_leader_change(account_id: str, current_leader: str | None) -> dict | None:
        """客戶已簽署、但**尚未反映在 manifest** 的換 leader 記錄。

        ⭐ 只在「已提交的 leader ≠ manifest 目前的 leader」時才回報為 pending：
        引擎套用之後記錄仍留在檔案裡（write_leader_change 是同 account 覆蓋，不是
        流水帳），若照單全收，客戶會永遠看到一個早就生效的「處理中」。比較的兩側
        （記錄裡的位址、manifest 裡的位址）都已正規化成小寫，同基準（工程原則 1）。

        ⚠️ 只投影 `leader_address` 與 `issued_at`——**signature 絕不外流**
        （沿 leaders_select 「不記 signature／message 原文」的政策）。
        """
        try:
            changes = load_leader_changes(cfg.leader_changes_path)
        except (OSError, ValueError) as e:
            # 交換目錄讀不到只影響「處理中」提示，不影響主要答案 → 降級不中斷。
            logger.error("換 leader 記錄讀取失敗 %s: %s", cfg.leader_changes_path, e)
            return None
        rec = next((c for c in changes if isinstance(c, dict)
                    and c.get("account_id") == account_id), None)
        if rec is None:
            return None
        target = rec.get("leader_address")
        if not isinstance(target, str) or target == current_leader:
            return None
        return {
            "leader_address": target,
            "issued_at": rec.get("issued_at"),
            "effective": "next_engine_cycle",
            "note": "你已簽署換 leader，尚未生效：引擎會在下一個 cycle 重新驗證你的"
                    "簽章與白名單後套用。",
        }

    # ---------- leader 目錄（客戶自選 leader 的資料來源） ----------
    def _load_leaders_or_503() -> list[LeaderRef]:
        """白名單載入的單一入口（工程原則 5）：目錄與選擇兩個端點必須看**同一份**
        清單、以**同一種**方式失敗。壞掉一律 503、**不得**降級成空清單——空清單在
        目錄端看起來像「目前沒有 leader」，在選擇端則會讓所有 leader 都變成不可選，
        兩邊都是把一個手滑的編輯偽裝成正常狀態。"""
        try:
            return load_leaders(cfg.leaders_path)
        except ValueError as e:
            logger.error("leader 白名單載入失敗 %s: %s", cfg.leaders_path, e)
            raise HTTPException(
                status_code=503, detail="leader 名單暫時不可用，請稍後重試") from e

    def _load_user_leaders_or_503() -> list[LeaderRef]:
        """user registry 載入的單一入口（准入閘用，review F4）。壞掉一律 503、
        **不得**降級成空清單——空清單會讓 operator 手編的停用位（enabled=false）
        暫時隱形，把一個讀取失敗偽裝成「可准入」。檔案不存在＝合法空 registry
        （load_user_leaders 既有語意），不在此列。"""
        try:
            return load_user_leaders(cfg.user_leaders_path)
        except (OSError, ValueError) as e:
            logger.error("user registry 載入失敗 %s: %s", cfg.user_leaders_path, e)
            raise HTTPException(
                status_code=503, detail="leader 名單暫時不可用，請稍後重試") from e

    # ---------- 自訂 leader 准入（2026-07-27 spec：用戶輸入任意位址） ----------

    def _admission_reject(status: int, reason: str, message: str) -> HTTPException:
        """准入拒絕 → 4xx，detail 是 `{"reason", "message"}` 物件。

        reason 是**機器可判的分類碼**（spec 的 API 契約：invalid_format /
        self_follow / leader_disabled——not_found 自 2026-07-27 裁決後不再是拒絕碼，
        鏈上無活動改為放行帶 exists=false），由伺服器決定、絕不含請求輸入原文
        （沿 LEADER_CHANGE_DETAIL 分類化訊息的既有政策）。
        """
        return HTTPException(status_code=status,
                             detail={"reason": reason, "message": message})

    def _admit_custom_leader(leader_address: str, session_address: str,
                             refs: list[LeaderRef]) -> dict:
        """自訂 leader 的**准入檢查**（格式／禁自跟／operator kill-switch）＋精選條目
        優先權（單一定義），並附鏈上預覽（權益／持倉／exists）。

        ⚠️ 鏈上活動自 2026-07-27 裁決後**不是閘門**：查無活動的位址照樣放行、
        exists 誠實回報 false（前端顯示警示但放行）——leader 尚未進場時客戶可先完成
        配置，進場後引擎自動開始跟。仍會擋下的只有格式錯、自跟、operator 停用/停收。

        preview 與 select 流程（訊息端點、POST 提交）共用本函式：POST 在驗簽後
        **重新執行全部檢查**，不信任客戶端曾呼叫 preview（防 TOCTOU）。
        通過 → 回 preview 資料；不過 → raise（reason-coded 4xx，見 _admission_reject）。
        """
        # (1) 格式：0x + 40 hex，小寫正規化（沿全 app 的單一位址基準）。
        try:
            addr = normalize_address(leader_address)
        except ValueError:
            # 不回顯輸入原文（分類碼政策）；格式細節在訊息裡講規則，不講他送了什麼。
            raise _admission_reject(
                400, "invalid_format",
                "位址格式不正確：須為 0x 開頭＋40 個十六進位字元") from None
        # (2) 禁止自跟：兩側皆 normalize（session 位址在 auth_verify 已正規化）。
        #     自我跟單無意義，且會形成回饋迴圈（本方下的單被當成 leader 目標再放大）。
        if addr == session_address:
            raise _admission_reject(400, "self_follow",
                                    "不能跟單自己的登入位址")
        # ⭐ 兩個下架旗標分兩支處理（2026-07-27 使用者裁決）——語意本就不同，
        #   對「自訂路徑准入」的後果也該不同（filet/leaders.py 檔頭的兩旗標語意）：
        #   - `enabled=false`＝**安全撤銷**（leader 出事）：引擎每輪 is_still_permitted
        #     只看 enabled，會主動收尾正在跟的人。**硬擋**，reason=leader_disabled。
        #   - `accepting_new=false`＝**例行下架**（只是暫不收新客）：引擎照跟。
        #     客戶堅持要跟就**放行**、回 accepting_new=false 讓前端畫警示（不擋）。
        #   精選條目**一律優先**於 registry（合併語義）：先看精選，命中就以它為準；
        #   未列才回落 registry。少了 enabled 這一擋，自訂輸入就是繞過安全撤銷的後門。
        listed = next((r for r in refs if r.address == addr), None)
        if listed is not None:
            if not listed.enabled:
                raise _admission_reject(
                    400, "leader_disabled",
                    "該位址已被平台安全撤銷（leader 出事）——無法跟隨")
            accepting_new = listed.accepting_new
        else:
            # user registry 的 operator kill-switch（review F4）：僅在非精選時查。
            # enabled=false（安全撤銷）→ 硬擋；accepting_new=false（停收，enabled=true）
            # → 放行帶警示。冪等寫入不覆寫既有條目，故 accepting_new=false 的殘留
            # 條目被重選也不會改回可收新客——但引擎照跟（is_still_permitted 只看
            # enabled），與精選側一致。未列於 registry → 預設 accepting_new=true。
            mine = next((r for r in _load_user_leaders_or_503()
                         if r.address == addr), None)
            if mine is not None and not mine.enabled:
                raise _admission_reject(
                    400, "leader_disabled",
                    "該位址已被平台安全撤銷（leader 出事）——無法跟隨")
            accepting_new = mine.accepting_new if mine is not None else True
        # (3) 鏈上存在：走既有 HL gateway（唯讀、冪等 → transient 重試與 502 轉譯
        #     自動繼承，工程原則 5），不另開 HTTP 呼叫路徑。
        account_value, position_count = _chain_activity(hl.clearinghouse_state(addr))
        exists = account_value > 0 or position_count > 0
        # 「鏈上無活動」**不再擋下**自訂位址（2026-07-27 使用者裁決）：leader 可能
        # 宣告即將開始交易，客戶想**提前**完成跟單配置，等 leader 進場後引擎自動
        # 開始跟——不該因為此刻鏈上沒活動就擋下配置。exists 誠實回報（false 時前端
        # 顯示警示但放行），account_value／position_count 照實。無安全風險：select
        # 會把該位址寫進 registry，引擎每輪讀該 leader、無部位時不跟、leader 進場後
        # 自動開始；格式／禁自跟／operator kill-switch 三道閘門與簽章保護一律照舊。
        return {"address": addr, "exists": exists,
                "account_value": str(account_value),   # Decimal → str（落地慣例）
                "position_count": position_count,
                # ⭐ 「在精選白名單裡」，**不是**「可選」：paused（accepting_new=false）
                # 也算 true。語意與理由見 leaders_preview 的 docstring。
                "already_listed": listed is not None,
                # accepting_new=false（例行下架、enabled=true）→ 放行帶此旗標，
                # 前端據此畫警示但不擋（撤銷是 enabled，已在上面硬擋掉）。
                "accepting_new": accepting_new}

    @app.get("/api/leaders/preview")
    def leaders_preview(leader_address: str,
                        address: str = Depends(_require_session)):
        """自訂 leader 的准入檢查 ＋ 鏈上預覽（session 驗證，唯讀、無副作用）。

        回 `{address, exists, account_value, position_count, already_listed}`；
        檢查不過 → 4xx，detail 帶機器可判的 reason code（見 _admit_custom_leader）。

        ⭐ `already_listed=true` ⇒ 位址**在精選白名單裡**（`enabled=true`），
        **不保證可選**——`accepting_new=false` 的 paused leader 也是 true，而
        `/api/leaders` 用 `is_selectable` 過濾（兩旗標都要過），所以它不在那份目錄裡。
        兩者刻意不同源：這個旗標回答的是「該位址由精選檔管轄嗎」，不是「它出現在
        目錄裡嗎」。理由是 Finding 1 的 kill-switch 修復——POST select 對
        `already_listed=true` 的位址**不寫 user registry**（寫了會留下一筆
        `enabled:true` 影子條目，日後撤銷精選條目時反而讓引擎繼續跟）。paused 位址
        本就走自訂分支（is_selectable=false），所以這個旗標必須連 paused 一起涵蓋，
        否則影子條目照樣被寫出來。`accepting_new` 另以獨立欄位回報，前端據它畫警示
        （行為錨定：tests/test_api_leader_preview.py::
        test_paused_curated_leader_is_admitted_with_accepting_new_false）。

        ⭐ per-session rate limit（60s 內 10 次）：本端點是非精選位址的探測面，
        且每次會打一次 HL /info——超限回 429（見 _enforce_probe_ratelimit）。
        """
        _enforce_probe_ratelimit(address)
        refs = _load_leaders_or_503()
        return _admit_custom_leader(leader_address, address, refs)

    @app.get("/api/leaders")
    def leaders_directory(address: str = Depends(_require_session)):
        """客戶**現在可以選**的 leader 清單 ＋ 每個 leader 的快照統計。

        ⭐ 過濾一律走 `leaders.is_selectable`（＝`enabled` **且** `accepting_new`），
        不在這裡自己寫旗標判斷。兩個旗標語意不同、且**任一為假都不該出現在目錄**：
        - `enabled=False` ＝ 安全撤銷（這個 leader 出事了）；
        - `accepting_new=False` ＝ 例行下架（名額滿／準備退場，只擋新客戶）。
        不可選的 leader **連 address 都不外流**——「白名單裡有這筆但你選不到」本身
        就是治理資訊（哪個 leader 剛被撤銷），沒有理由讓客戶端推得出來。

        統計來源＝**已存在的 watchlist 每日快照**（cron 00:10 UTC），不打 HL 即時查詢：
        目錄頁會被頻繁瀏覽，逐次請求轉成上游查詢等於把使用者流量放大成對交易所的
        突發流量（而目錄頁本來就不需要秒級新鮮度）。

        兩種降級，都**不讓整個目錄 500**——拿不到統計只是少幾個數字，回不出清單則是
        客戶完全無法選 leader（後果嚴重得多）：
        - 快照缺失／讀取失敗 → 照回清單，統計欄位全 null，`stats_available=false`＋`note`。
        - 個別 leader 不在 watchlist（或該列是失敗列）→ 只有他的統計為 null。

        `stats_as_of`／`stats_day` 必回：沒有時間戳的話，一份三天前的數字會被當成
        即時數字讀（工程原則 1 的變形——比較的兩端連時點都不同源）。

        白名單載入失敗（JSON 壞／格式錯）→ 503，**不回空清單**：空清單看起來像
        「目前沒有 leader」的正常狀態，會讓一個手滑的編輯靜默變成全站無 leader。
        """
        refs = _load_leaders_or_503()
        selectable = [r for r in refs if is_selectable(r.address, refs)]
        snapshot = load_latest_snapshot(cfg.watchlist_dir)
        rows = snapshot_rows_by_address(snapshot)
        return {
            "leaders": [_leader_public(r, rows.get(r.address)) for r in selectable],
            "stats_available": snapshot is not None,
            "stats_day": snapshot.get("day") if snapshot else None,
            "stats_as_of": snapshot.get("generated_at") if snapshot else None,
            "note": None if snapshot else
                    "績效統計暫時不可用（每日快照尚未產生或讀取失敗）；"
                    "leader 清單不受影響，仍可正常選擇。",
            # ⭐ 績效可用性與 `stats_available` 是**兩個獨立**的旗標，不可合併：
            # 快照可能存在（規模欄位有值）卻沒有績效（舊格式的快照、或 cron 尚未
            # 啟用 portfolio 抓取）。合併成一個會讓前端在「有規模沒績效」時整片
            # 顯示「統計不可用」，把有效資料也一起藏起來。
            "performance_available": bool(
                snapshot and snapshot.get("perf_source") is not None),
            "performance_basis": "perp",
            "performance_windows": list(_LEADER_PERF_WINDOWS),
            # 三段揭露文案由 leader_perf 的常數供給（單一來源）：計算的極限與
            # 呈現的警語必須出自同一處，否則改了公式而文案還停在舊說法。
            "performance_notes": {
                "basis": BASIS_NOTE,
                "upper_bound": UPPER_BOUND_NOTE,
                "max_drawdown": MDD_SAMPLING_NOTE,
                "sufficiency": "每個窗都附 `covered_days`（涵蓋天數）與 `sample_count`"
                               "（樣本點數），並以 `disclosure_tier` 標示這段資料"
                               "**誠實可顯示到什麼程度**：insufficient（無數字）／"
                               "pnl_only（僅 $ 金額）／window_return（＋窗口報酬率與"
                               "回撤）／annualizable（＋年化）。不足 90 天的資料"
                               "**不會**有 `annualized_return` 這個欄位。",
            },
        }

    # 換 leader 的待簽原文所用的 nonce 與 SIWE 登入**共用同一張表**（同一個 nonce
    # 空間，見 leaders_select 的 _consume）——刻意不另開一套機具：兩套一次性表格
    # 意味著兩套過期、兩套消耗語意，而其中一套遲早會漏掉原子性。
    # chain_id 對「換 leader」沒有意義，統一發 0，並順帶得到一個防禦性質：
    # auth_verify 會拿 chain_id=0 重建 SIWE 訊息，客戶從來不會簽那一份，recover 必然
    # 對不上 → 本端點發出的 nonce **無法**被挪去完成一次登入（auth_nonce 端點自己
    # 拒收 chain_id <= 0，所以 0 是登入路徑產生不出來的值）。
    _LEADER_CHANGE_CHAIN_ID = 0

    @app.get("/api/leaders/select/message")
    def leaders_select_message(leader_address: str,
                               address: str = Depends(_require_session)):
        """回傳換 leader 的 **canonical 待簽原文** ＋ 配套的一次性 nonce。

        ⭐ 為什麼原文必須由伺服器產生（沿 SIWE 的既有理由，見 auth_nonce）：
        驗證端是**重建**訊息再 recover（verify_leader_change 刻意不看客戶送來的
        `message`），所以客戶端組出的字串必須與伺服器**逐位元組相同**。少一個換行、
        位址大小寫不同、欄位順序換一下，症狀都是「我本人簽的卻一直被拒」——而那個
        症狀在客戶端與伺服器兩邊看起來都完全正常，是最難診斷的一類 bug。讓伺服器
        回傳原文，客戶端**原樣**丟進錢包簽名，兩邊結構上不可能組出不同的字串
        （工程原則 1：被比較的兩個值同源、同處計算）。

        本端點**只產生原文，不改任何狀態**——唯一的副作用是簽發 nonce（沿
        auth_nonce 的既有慣例；nonce 要能被 select 端點原子消耗，就必須先存在）。
        真正的變更寫入只發生在 POST /api/leaders/select，且在**全部驗證通過之後**。

        ⚠️ 這裡用 `is_selectable`（enabled **且** accepting_new），與 select 端點
        同一個述詞：不可選的 leader 連待簽原文都不該給。若這裡放寬成
        `is_still_permitted`（引擎的述詞），客戶會拿到一份能簽、簽了卻必定被
        select 端點拒絕的原文——把一個閘門變成一個只會浪費客戶一次簽名的陷阱。

        ⭐ per-session rate limit（60s 內 10 次，與 preview 同一計數器）：非精選／
        fresh 位址在此也會打一次 HL /info——超限回 429（見 _enforce_probe_ratelimit）。
        """
        _enforce_probe_ratelimit(address)
        account_id = derive_account_id(address)
        refs = _load_leaders_or_503()
        if is_selectable(leader_address, refs):
            # 精選可選 → 既有流程。走到這裡代表 is_selectable 已在白名單裡找到它
            # → 位址必然合法可正規化。
            leader = normalize_address(leader_address)
        else:
            # 非精選位址 → 准入前置檢查（2026-07-27 spec：取代原本的一律拒絕）。
            # 不過即 reason-coded 4xx（invalid_format／self_follow／leader_disabled
            # ——已列但不可選的位址在這裡被 leader_disabled 擋下，自訂路徑不得繞過
            # operator 的撤銷或停收）。鏈上無活動**不再擋下**（2026-07-27 裁決）：
            # 照樣回原文，客戶可先完成配置，leader 進場後引擎自動開始跟。通過的位址
            # 之後在 POST select 會**重新**全查一次（本端點的通過不被信任，防 TOCTOU）。
            leader = _admit_custom_leader(leader_address, address, refs)["address"]
        # issued_at 版型沿 auth_nonce（帶 Z 的 UTC）——leader_change.parse_issued_at
        # 要求帶時區，naive 時間會被直接拒絕。
        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(address, _LEADER_CHANGE_CHAIN_ID, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        message = build_leader_change_message(account_id=account_id,
                                              leader_address=leader,
                                              nonce=nonce, issued_at=issued_at)
        # 四個欄位全回：客戶端把 message 原樣拿去簽，其餘三個原樣回填進 select 的
        # request body。任何一個由客戶端自己重算，就等於把「兩邊必須同源」的保證
        # 交還給客戶端的記性。
        return {"message": message, "nonce": nonce, "issued_at": issued_at,
                "leader_address": leader, "account_id": account_id}

    @app.post("/api/leaders/select")
    def leaders_select(body: LeaderSelectBody,
                       address: str = Depends(_require_session)):
        """客戶**自己簽章**要求換 leader → 寫一筆簽章變更記錄。

        ⭐ 為什麼是簽章而不是「登入後按個鈕就改」（改本端點前先讀
        filet/leader_change.py 檔頭的完整威脅模型）：營運方從客戶換 leader 的收斂
        交易中賺 builder fee，從 churn 獲利的一方不該同時是守住 churn 的一方。而
        白名單只回答「這個 leader 一般而言可不可接受」，回答不了「這位客戶真的要求
        換到他嗎」——能寫入變更的人可以把保守客戶指向白名單內、但對他完全不適合的
        高槓桿策略，白名單全程放行，一次切換就是實質損失。客戶的簽章是唯一能回答
        後面那個問題的東西，而本進程被打穿也偽造不出它。

        ⭐ 本端點**不改 manifest**，只落一筆記錄。兩個理由缺一不可：
        (1) filet-api 對引擎 manifest 本就沒有寫權（權限拓撲見 pending.py 檔頭）；
        (2) 就算有，直接改也會繞過引擎套用前的**二次驗章**——那道驗證的價值正在於
        它獨立於本進程，繞過它等於把整套簽章設計降級成裝飾。

        失敗分類（紅線 4／工程原則 2）：驗簽失敗、leader 不可選、session 不符
        **全是 semantic**——重試同一份請求必定再次失敗，客戶端不得自動重試。
        寫檔失敗才是 transient（5xx，可重試；寫入冪等：同 account 覆蓋）。

        ⭐ 驗簽失敗一律 **400 而非 401**：401 在本 app 的既有語意是「session 沒了」，
        前端據此把使用者踢回登入頁（web 的 session-expiry redirect）。簽章壞掉時
        session 好端端的，回 401 會讓客戶莫名其妙被登出，而真正的問題（他簽錯了／
        簽章過期）反而不會被顯示出來。
        """
        # 1) 客戶只能改自己的：body 的 account_id 必須等於 session 衍生值。
        #    account_id 是待簽訊息的一部分（見 LeaderSelectBody），所以必須顯式收下
        #    再比對，不能由伺服器代推——代推會讓「客戶簽 A、伺服器套用到 B」成為可能。
        account_id = derive_account_id(address)
        if body.account_id != account_id:
            # 403 而非 404：對方確實通過了身分驗證，只是無權變更這個帳號。
            # 不回洩該 account 是否存在（列舉防禦）。
            raise HTTPException(status_code=403,
                                detail="只能變更自己帳號的 leader")

        # 2) leader 閘門。精選可選（is_selectable ＝ enabled 且 accepting_new）→
        #    既有精選流程（不寫 registry）。非精選 → **重新執行全部准入檢查**
        #    （2026-07-27 spec）：不信任客戶端曾呼叫 preview 或訊息端點——那兩次
        #    通過與這次提交之間，位址可能已被 operator 停用、帳戶可能已清空
        #    （TOCTOU）。custom=None ⇒ 精選路徑。
        #    ⚠️ 精選側刻意不是 is_still_permitted：那是引擎對「已經在跟的人可否
        #    繼續跟」的述詞（只看 enabled），用在這裡會讓客戶選中一個已停止接客
        #    的 leader。兩個旗標的語意差異見 filet/leaders.py 檔頭。
        #    ⭐ per-session probe rate limit **只掛在自訂分支**（Finding 1）：走到
        #    _admit_custom_leader 就是一次 HL /info ＋一組 reason-coded 4xx，與
        #    preview／message 完全同一個探測面——不掛的話，一個帶垃圾簽章的迴圈就能
        #    無限放大上游流量、並枚舉哪些位址回 leader_disabled（＝平台撤銷清單，
        #    治理資訊）。驗簽在後面，擋不住這個迴圈：它根本不打算通過驗簽。
        #    精選分支不限流（既有保證不變）：那條路不打 HL、也不外洩任何治理資訊。
        #    ⚠️ 位置刻意在 _admit_custom_leader **之前、驗簽之前**：429 與准入失敗
        #    一樣都不得消耗 nonce（見 test_admission_failure_does_not_burn_the_nonce
        #    ——一次限流打嗝作廢客戶手上的授權，等於自我 DoS）。正常流程每次換手
        #    只花 2 次額度（message ＋ POST），離 10 次上限很遠。
        refs = _load_leaders_or_503()
        custom = None
        if not is_selectable(body.leader_address, refs):
            _enforce_probe_ratelimit(address)
            custom = _admit_custom_leader(body.leader_address, address, refs)

        # 3) 驗章。⭐ user_address 出自 **session**（可信來源），不是請求內容；
        #    訊息由 verify_leader_change 自己重建，body.message 只是稽核留存。
        def _consume(nonce: str) -> bool:
            """一次性 nonce：沿 SIWE 的同一張表與同一個原子 UPDATE。

            兩道額外要求，缺一不可：
            1. nonce 是**發給本人**的——否則 A 能拿 B 的 nonce 去湊，雖不足以偽造
               簽章，卻能無成本地作廢別人手上的授權。
            2. ⭐ nonce 是**本端點發的**（`chain_id == _LEADER_CHANGE_CHAIN_ID`，即 0）。
               沒有這一條，一顆 SIWE **登入** nonce 就能被挪來換 leader（opus 審查
               Minor 3）。反方向的防禦本來就成立——auth_verify 拿 chain_id=0 重建
               SIWE 訊息必然 recover 不符，且 auth_nonce 拒收 chain_id <= 0——但
               「域分隔」要成立必須**兩個方向都是結構性的**，只擋一邊的分隔符不是
               分隔符。兩張表合一是刻意的（見 _LEADER_CHANGE_CHAIN_ID 的註解），
               代價就是必須在消耗點顯式宣告「我只收我這個域的 nonce」。
            """
            rec = store.consume_nonce(nonce, now_s=now_fn())
            return (rec is not None and rec.address == address
                    and rec.chain_id == _LEADER_CHANGE_CHAIN_ID)

        record = build_leader_change_record(
            account_id=body.account_id, leader_address=body.leader_address,
            nonce=body.nonce, issued_at=body.issued_at, signature=body.signature,
            message=body.message)
        try:
            verified = verify_leader_change(record, account_id=account_id,
                                            user_address=address, now_s=now_fn(),
                                            consume_nonce=_consume)
        except LeaderChangeError as e:
            # 稽核痕跡（偽造探測）：記 reason 與帳號，**不記** signature／message 原文
            # ——來路不明的內容不進 log（沿 billing webhook 驗簽失敗的既有作法）。
            logger.warning("換 leader 驗簽失敗 account=%s reason=%s", account_id, e.reason)
            # ⭐ 回**分類化的訊息**，不是 str(e)（opus 審查 Minor 2）：例外訊息內嵌
            # 客戶送來的 nonce／issued_at／signature 原值（例如「nonce 格式不合法:
            # '...'」），回顯它等於把「不記 signature／message 原文」的政策在 HTTP
            # 回應這一側破功。分類碼由伺服器決定，內容不含任何請求輸入。
            raise HTTPException(
                status_code=400,
                detail=LEADER_CHANGE_DETAIL.get(e.reason, LEADER_CHANGE_DETAIL_DEFAULT)
            ) from None

        # 4a) 自訂 leader → 先**冪等**寫入 user registry，再落換 leader 記錄。
        #     順序是刻意的：registry 是引擎驗證的來源，先記錄後寫 registry 的話，
        #     「寫入失敗」會留下一筆引擎永遠拒絕套用的簽章意圖。反向（registry
        #     成功、記錄失敗）是安全的：客戶重送同一個 POST，registry 寫入冪等
        #     跳過，記錄補上。
        # ⭐ 但**精選位址（already_listed=true）不寫 registry**（Finding 1 kill-switch
        #    破口）：精選 paused 位址（is_selectable=false → 走 custom 分支）本就經精選
        #    檔 enabled=true 被引擎放行（is_still_permitted 只看 enabled），寫進 registry
        #    只是製造一筆 enabled:true 影子條目——日後 operator 用「移除精選條目」撤銷
        #    （leaders.py 明載＝enabled:false）時，merge 沒有精選條目可壓過影子 → 引擎
        #    繼續跟一個已撤銷的 leader。只有真正未列的自訂位址（already_listed=false）
        #    才需要、也才寫 registry。
        if custom is not None and not custom["already_listed"]:
            try:
                wrote = record_user_leader(cfg.user_leaders_path,
                                           address=verified.leader_address,
                                           added_by=account_id)
            except (OSError, ValueError) as e:
                # ⭐ 安全動作 fail loudly（工程原則 3）：registry 進不去就**不得**
                # 記錄換 leader——記了，引擎會拿一筆白名單驗不過的意圖每輪告警；
                # 吞了，客戶以為換成了。5xx（transient，可重試；寫入冪等）。
                logger.error("user registry 寫入失敗 account=%s leader=%s path=%s: %s",
                             account_id, verified.leader_address,
                             cfg.user_leaders_path, e)
                raise HTTPException(
                    status_code=500,
                    detail="自訂 leader 註冊寫入失敗，變更未生效，請稍後重試") from e
            if wrote:
                logger.info("自訂 leader 已寫入 user registry account=%s leader=%s",
                            account_id, verified.leader_address)

        # 4b) 落檔。此後這是唯一的寫入，且必須在**全部驗證通過之後**——驗簽失敗
        #    卻留下記錄，等於把「被拒絕的請求」偽裝成待套用的意圖。
        # 落地的是**驗證後的正規化值**（verified.*），不是請求的原樣字串——位址大小寫
        # 在此收斂成單一基準，引擎端不必再猜（工程原則 1）。signature 原樣保留，
        # 引擎重驗時會自己重建訊息，正規化是冪等的，重驗結果相同。
        record = build_leader_change_record(
            account_id=verified.account_id, leader_address=verified.leader_address,
            nonce=verified.nonce, issued_at=verified.issued_at,
            signature=body.signature, message=body.message)
        try:
            write_leader_change(cfg.leader_changes_path, record)
        except OSError as e:
            # transient：磁碟／權限問題，重試可能成功（寫入冪等：同 account 覆蓋）。
            # 大聲留痕（工程原則 3）：客戶的意圖已驗證通過卻沒能落地，不能靜靜吞掉。
            logger.error("換 leader 記錄落檔失敗 account=%s path=%s: %s",
                         account_id, cfg.leader_changes_path, e)
            raise HTTPException(status_code=500,
                                detail="變更記錄寫入失敗，請稍後重試") from e
        logger.info("換 leader 記錄已落地 account=%s leader=%s",
                    account_id, verified.leader_address)

        # ⭐ 回應必須明講後果與生效時機，不讓前端自己猜（`effective` 是機器可讀的
        #    語意欄位，後面兩個字串是給人看的）。換 leader 不是換一個設定值：引擎會
        #    收斂到新 leader 的部位，平掉舊部位、開新部位，有實際的 taker 成本。
        return {
            "ok": True,
            "account_id": account_id,
            "leader_address": verified.leader_address,
            "effective": "next_engine_cycle",
            "effective_note": "已記錄，於引擎的下一個 cycle 生效——不是立即生效；"
                              "引擎會在套用前**自己重新驗證你的簽章與白名單**，"
                              "驗證不過則不會套用。",
            "consequences": "生效時引擎會把你的部位收斂到新 leader："
                            "平掉目前的部位、依新 leader 開新部位。"
                            "這是真實成交，會產生實際的交易成本（taker 費用與滑價）。",
        }

    # ---------- 資金設定（per-follower 本金與使用比例） ----------
    # ⭐ nonce 與換 leader、SIWE 登入**共用同一張表與同一個 chain_id 域（0）**。
    # 刻意不另開第三套機具：三套一次性表格意味著三套過期與三套消耗語意，而其中
    # 一套遲早會漏掉原子性。共用的代價是消耗點必須顯式宣告「我只收這個域的 nonce」
    # （見 _consume），而**動作之間**的分隔靠的不是 nonce 域，是待簽訊息的模板
    # ——兩個模板的第一行是不同的固定字面量，任何輸入都到不了第一行，所以不存在
    # 一組輸入能讓它們產生同一字串（完整論證見 filet/capital_settings.py 檔頭）。

    def _load_own_capital_record(account_id: str) -> dict | None:
        """交換目錄 → **只**這一個帳號的資金設定記錄，且**只投影安全欄位**。

        ⭐⭐ 兩層窄化，都是結構性的（沿 `_load_own_follower` 的同一個決定）：
        1. 多帳號清單的生命週期完全不離開本函式 ⇒ 端點結構上拿不到別人的記錄，
           不是靠端點裡記得寫一行 filter。
        2. **回傳值只含安全欄位**（金額、比例、模式、提交時刻與指紋）——`signature`
           與 `message` 原文結構上到不了回應，不是靠端點記得別把它們塞進去。
           這是紅線 3 的形狀：讓它寫不出來，而不是提醒別寫（工程原則 5 的精神）。

        記錄壞掉／讀不到一律回 None（＝「查不到已提交的設定」），**不 raise**：
        這一格只影響「處理中」提示，不該讓客戶連自己目前生效的設定都查不到。
        """
        try:
            records = load_capital_settings(cfg.capital_settings_path)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.error("資金設定記錄讀取失敗 %s: %r", cfg.capital_settings_path, e)
            return None
        mine = [r for r in records if isinstance(r, dict)
                and r.get("account_id") == account_id]
        if not mine:
            return None
        # write_capital_settings 是「同 account 覆蓋」，正常至多一筆；取最後一筆
        # ＝取最新意圖（沿引擎 `_my_record` 的同一個選法，兩邊看的是同一筆）。
        rec = mine[-1]
        try:
            _, alloc, _, util = canonical_capital_values(
                rec.get("allocated_capital"), rec.get("capital_utilization"))
            full = require_bool_flag(rec, "use_full_equity")
        except (CapitalSettingsError, ValueError, TypeError):
            # 記錄格式壞 ⇒ 引擎也會拒絕它。這裡當成「沒有待套用的記錄」，
            # 不對客戶宣稱有一筆處理中的變更（那會讓他一直等一個不會來的生效）。
            return None
        issued_at = rec.get("issued_at")
        return {"allocated_capital": alloc, "capital_utilization": util,
                "use_full_equity": full,
                "submitted_at": issued_at if isinstance(issued_at, str) else None,
                # 指紋與引擎的竄改偵測共用 `capital_fingerprint`（單一定義）：
                # 「已提交 vs 已生效」的判定式與引擎眼中的「同一組設定」必須同義。
                "fingerprint": capital_fingerprint(alloc, util, full)}

    @app.get("/api/me/capital")
    def me_capital(address: str = Depends(_require_session)):
        """客戶查**自己目前生效的資金設定**，以及已提交但尚未套用的那一筆。

        ⭐ 為什麼需要這個端點：`/capital` 頁要客戶簽署一次改變曝險倍數的授權，卻
        沒有任何地方能告訴他**現在的值是多少**、**上次簽的到底生效了沒**。前後對照
        永遠缺左半邊，客戶只能在不知道現況的情況下按下簽名。

        ⭐ **只回自己的**，且結構上不可能回別人的：account_id 由 session 衍生，本端點
        **沒有任何 account 參數**，兩個資料來源都是「只吃單一 account」的載入函式
        （`_read_heartbeat` 的路徑由 account_id 推導、`_load_own_capital_record`
        在函式內窄化並只投影安全欄位）。想查別人只能先拿到別人的 session。

        ⭐⭐ **「已提交」與「已生效」必須分得開**（本端點最重要的性質）。
        生效值的唯一來源是**引擎發布的健康心跳**——那是引擎真正拿去乘部位大小的那組
        值。交換目錄裡的記錄只是「客戶簽了、API 收了」，引擎可能還沒套用、也可能因為
        白名單／邊界檢查而永遠不會套用。把記錄當成生效值回傳，會讓一個把使用比例從
        1.0 調到 0.2 的客戶以為曝險已經降下來了，而實際上一點都沒變。
        所以：記錄與心跳的指紋不同（或心跳讀不到）⇒ 記錄一律歸入 `pending`。

        `pending.state` 兩態，語意不同：
        - `not_yet_applied`——生效值已知且與提交值不同 ⇒ **確定**還沒套用。
        - `unconfirmed`——生效值不可知（心跳缺席／過期）⇒ **無從得知**套用了沒。
          這一態刻意不折疊進上一態：前者可以安心等下一個 cycle，後者代表引擎那邊
          可能出了事，處置完全不同。

        `status` 四態，**不用 null 讓前端猜**：
        - `effective`——心跳新鮮，生效值可知。
        - `unknown`——已活化但心跳缺席／過期／引擎本輪無法判定資金設定。
        - `not_activated`——帳號尚未活化（活化是人工 CLI 動作）。
        - `indeterminate`——帳號不在 manifest **且** manifest 有壞條目（壞的那筆可能
          就是他自己的）。回 `not_activated` 會讓一個正在跟單的客戶以為自己沒在跟單。

        ⚠️ **不外流** signature／message 原文：`_load_own_capital_record` 只投影安全
        欄位，所以那些東西結構上到不了這裡（沿 `_pending_leader_change` 的同一政策）。
        """
        account_id = derive_account_id(address)
        mine, manifest_degraded = _load_own_follower(account_id)
        submitted = _load_own_capital_record(account_id)

        if mine is None:
            # 尚未活化：他仍可能已經簽過一筆（POST 不要求活化）。照實說「已提交、
            # 活化後才會生效」，比假裝沒有這筆記錄誠實。
            return jsonable({
                "account_id": account_id,
                "status": "indeterminate" if manifest_degraded else "not_activated",
                "effective": None,
                "pending": _capital_pending(submitted, None),
                "heartbeat": None,
                "note": ("目前無法確認你的資金設定（帳號清單有無法解析的條目）；"
                         "請聯絡管理員，不要當作「未設定」處理。") if manifest_degraded
                        else "你的帳號尚未啟用跟單，因此還沒有生效中的資金設定。"
                             "啟用後，這裡會顯示引擎實際採用的本金與使用比例。",
            })

        hb = _read_heartbeat(account_id, now_fn())
        # ⭐ `hb.data` 只有在心跳新鮮時才非 None（engine_health 的結構性保證）——
        # 過期的心跳到不了這裡，也就不可能被當成生效值回傳。
        cap = (hb.data or {}).get("capital") or {}
        effective = None
        if cap.get("source") in ("customer_signed", "env_default"):
            effective = {
                "allocated_capital": cap.get("allocated_capital"),
                "capital_utilization": cap.get("capital_utilization"),
                "use_full_equity": cap.get("use_full_equity"),
                "source": cap.get("source"),
                # 上次變更時刻＝引擎**實際套用**的時刻（不是客戶簽署的時刻）：
                # 客戶問的是「我改的東西什麼時候開始作用」。env 預設 → null。
                "changed_at": cap.get("changed_at"),
                # 這組值是引擎在哪一刻回報的——沒有這個時間戳，一份接近過期的心跳
                # 會被當成即時查詢讀（工程原則 1 的變形：連時點都不同源）。
                "as_of": hb.at,
            }

        effective_fp = None
        if effective is not None and all(
                effective[k] is not None
                for k in ("allocated_capital", "capital_utilization")):
            effective_fp = capital_fingerprint(effective["allocated_capital"],
                                               effective["capital_utilization"],
                                               bool(effective["use_full_equity"]))
        return jsonable({
            "account_id": account_id,
            "status": "effective" if effective is not None else "unknown",
            "effective": effective,
            "pending": _capital_pending(submitted, effective_fp),
            "heartbeat": {"status": hb.status, "at": hb.at, "age_s": hb.age_s,
                          "stale_after_s": HEARTBEAT_STALE_S},
            "note": ("這是引擎目前實際採用的本金與使用比例。"
                     if effective is not None else
                     "目前無法確認生效中的資金設定（引擎的健康心跳缺席或已過期）。"
                     "**請不要**把下方「已提交」的數值當成生效值——它可能還沒被套用。"
                     "若這個狀態持續，請聯絡管理員。"),
        })

    def _capital_pending(submitted: dict | None, effective_fp: str | None) -> dict | None:
        """「已提交但尚未生效」的那一筆（**已生效的一律不回報為 pending**）。

        ⭐⭐ 本函式是「已提交 vs 已生效」這個區分的**單一實作點**。拿掉指紋比對，
        兩種災難二選一：
        - 恆回 pending ⇒ 客戶永遠看到一個早就生效的「處理中」（記錄檔沒有人負責清，
          `write_capital_settings` 是同 account 覆蓋而非流水帳），久了他會學會忽略它。
        - 恆回 None ⇒ 一筆還沒套用的變更被當成已生效，客戶以為自己的曝險已經降下來了
          ——這個方向會讓他在錯誤的安全感下加碼，是兩者中危險得多的那個。

        比較的兩側同基準（工程原則 1）：兩邊都是 `capital_fingerprint` 算出的指紋，
        而輸入都是 `canonical_capital_values` 產出的 canonical 字串。拿 `"1000"` 與
        `"1000.00"` 直接比是同一類 bug 的另一個版本。
        """
        if submitted is None:
            return None
        if effective_fp is not None and submitted["fingerprint"] == effective_fp:
            return None      # 已生效——記錄留在檔案裡是正常的，不是「處理中」
        unconfirmed = effective_fp is None
        return {
            "allocated_capital": submitted["allocated_capital"],
            "capital_utilization": submitted["capital_utilization"],
            "use_full_equity": submitted["use_full_equity"],
            "submitted_at": submitted["submitted_at"],
            "state": "unconfirmed" if unconfirmed else "not_yet_applied",
            "effective_when": "next_engine_cycle",
            "note": ("你已簽署這組設定，但目前**無法確認**它生效了沒（引擎的健康心跳"
                     "缺席或已過期）。在確認之前，請以你原本的設定評估曝險。"
                     if unconfirmed else
                     "你已簽署這組設定，**尚未生效**：引擎會在下一個 cycle 重新驗證你的"
                     "簽章與數值範圍後套用。套用不會立即強制再平衡，部位隨 leader 的"
                     "後續動作自然收斂。"),
        }

    @app.get("/api/me/capital/message")
    def capital_settings_message(allocated_capital: str, capital_utilization: str,
                                 use_full_equity: bool = False,
                                 address: str = Depends(_require_session)):
        """回傳資金設定的 **canonical 待簽原文** ＋ 配套的一次性 nonce。

        形狀沿 `/api/leaders/select/message`：伺服器產生原文，客戶端**原樣**丟進
        錢包簽名。理由見該端點——驗證端是重建訊息再 recover，客戶端自己組字串會
        因為一個小數位或一個換行而得到「本人簽的卻一直被拒」，且兩邊 log 都正常。

        ⭐ 邊界在**發原文之前**就檢查（超界 → 400，不發 nonce、不給原文）：
        讓客戶簽一份必定被 POST 拒絕的原文，是把閘門變成一個只會浪費他一次錢包
        簽名的陷阱（沿 leaders_select_message 用 is_selectable 的同一個決定）。

        ⭐ `use_full_equity=true` 時 `allocated_capital` 必須送 0（不是「隨便送、
        反正會被忽略」）——邊界檢查會擋下矛盾組合，理由見 validate_capital_bounds：
        客戶不該簽下一份同時寫著「本金 1000」與「用全部權益」的授權。
        """
        account_id = derive_account_id(address)
        try:
            # canonical 化（格式）＋ 邊界（政策），兩者都是 semantic 失敗（400）。
            alloc, alloc_str, util, util_str = canonical_capital_values(
                allocated_capital, capital_utilization)
            validate_capital_bounds(alloc, util, use_full_equity=use_full_equity)
        except CapitalSettingsError as e:
            raise HTTPException(
                status_code=400,
                detail=CAPITAL_SETTINGS_DETAIL.get(e.reason,
                                                   CAPITAL_SETTINGS_DETAIL_DEFAULT)
            ) from None

        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(address, _LEADER_CHANGE_CHAIN_ID, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        # 回 canonical 字串（不是客戶送來的原樣字串）：客戶端把這兩個值原樣回填進
        # POST 的 body，兩邊結構上不可能組出不同的字串（工程原則 1）。
        message = build_capital_settings_message(
            account_id=account_id, allocated_capital=alloc_str,
            capital_utilization=util_str, nonce=nonce, issued_at=issued_at,
            use_full_equity=use_full_equity)
        # 旗標原樣回給客戶端，讓它原封不動回填進 POST body——與兩個金額字串同一個
        # 理由（工程原則 1）：伺服器重建訊息時用的是哪個值，客戶端就該回哪個值。
        return {"message": message, "nonce": nonce, "issued_at": issued_at,
                "account_id": account_id, "allocated_capital": alloc_str,
                "capital_utilization": util_str,
                "use_full_equity": use_full_equity}

    @app.post("/api/me/capital")
    def capital_settings_submit(body: CapitalSettingsBody,
                                address: str = Depends(_require_session)):
        """客戶**自己簽章**調整投入本金與使用比例 → 寫一筆簽章記錄。

        ⭐ 為什麼這裡也要簽章（威脅模型全文見 filet/capital_settings.py 檔頭）：
        這兩個值直接乘進部位大小。能改它們的人就能把使用比例拉滿，讓客戶的曝險
        瞬間變成數倍、清算距離縮到數分之一——而白名單全程放行（它管的是「跟誰」，
        不是「押多大」），事後看紀錄一切合規。危害與換 leader 同級，所以用同一套
        信任錨；簽章機制既然已經存在，邊際成本近乎零。

        ⭐ 本端點**不改任何引擎設定**，只落一筆記錄。引擎在套用前自己重新驗章，
        **並自己重新檢查邊界**——繞過那道驗證等於把整套簽章設計降級成裝飾。

        失敗分類（工程原則 2）：驗簽失敗、動作類型不符、超界、session 不符
        **全是 semantic**（4xx，不得自動重試）；寫檔失敗才是 transient（5xx）。
        """
        # 1) 只能改自己的（同 leaders_select：403 而非 404，不洩漏帳號是否存在）。
        account_id = derive_account_id(address)
        if body.account_id != account_id:
            raise HTTPException(status_code=403, detail="只能變更自己帳號的資金設定")

        # 2) 邊界（政策）。⭐ 超界一律 4xx，**不夾取**：夾取會讓流程順利跑完，
        #    代價是客戶簽了 A、系統執行了 B，而且沒有人會知道。
        try:
            alloc, _alloc_str, util, _util_str = canonical_capital_values(
                body.allocated_capital, body.capital_utilization)
            validate_capital_bounds(alloc, util,
                                    use_full_equity=body.use_full_equity)
        except CapitalSettingsError as e:
            raise HTTPException(
                status_code=400,
                detail=CAPITAL_SETTINGS_DETAIL.get(e.reason,
                                                   CAPITAL_SETTINGS_DETAIL_DEFAULT)
            ) from None

        # 3) 驗章。user_address 出自 **session**（可信來源），不是請求內容。
        def _consume(nonce: str) -> bool:
            """一次性 nonce：與換 leader 同一張表、同一個原子 UPDATE、同一個域。

            兩道要求同 leaders_select 的 _consume：nonce 必須是**發給本人**的，
            且必須是**這個 chain_id 域**發的（擋 SIWE 登入 nonce 被挪用）。
            ⚠️ 這裡**不**分辨「是換 leader 端點發的還是本端點發的」——兩者同域是
            刻意的，因為分辨它們的是**簽章本身**：客戶簽的原文寫死了動作類型，
            拿換 leader 的 nonce 配資金設定的簽章，重建出來的訊息對不上任何一邊。
            """
            rec = store.consume_nonce(nonce, now_s=now_fn())
            return (rec is not None and rec.address == address
                    and rec.chain_id == _LEADER_CHANGE_CHAIN_ID)

        record = build_capital_settings_record(
            account_id=body.account_id, allocated_capital=body.allocated_capital,
            capital_utilization=body.capital_utilization, nonce=body.nonce,
            issued_at=body.issued_at, signature=body.signature,
            message=body.message, use_full_equity=body.use_full_equity)
        try:
            verified = verify_capital_settings(record, account_id=account_id,
                                               user_address=address,
                                               now_s=now_fn(),
                                               consume_nonce=_consume)
        except CapitalSettingsError as e:
            # 稽核痕跡（偽造探測）：記 reason 與帳號，**不記** signature／message
            # 原文，也不記金額（來路不明的內容不進 log）。
            logger.warning("資金設定驗簽失敗 account=%s reason=%s", account_id, e.reason)
            raise HTTPException(
                status_code=400,
                detail=CAPITAL_SETTINGS_DETAIL.get(e.reason,
                                                   CAPITAL_SETTINGS_DETAIL_DEFAULT)
            ) from None

        # 4) 落檔（唯一的寫入，且在**全部驗證通過之後**）。落地的是驗證後的
        #    canonical 值，不是請求的原樣字串——引擎重建訊息時不必再猜格式。
        record = build_capital_settings_record(
            account_id=verified.account_id,
            allocated_capital=verified.allocated_capital_str,
            capital_utilization=verified.capital_utilization_str,
            nonce=verified.nonce, issued_at=verified.issued_at,
            signature=body.signature, message=body.message,
            # 旗標取自 **verified**（驗章通過的值），不是 body——落檔的每一個欄位
            # 都必須是通過驗證的那一份，否則落地的記錄與客戶簽的原文可以不一致。
            use_full_equity=verified.use_full_equity)
        try:
            write_capital_settings(cfg.capital_settings_path, record)
        except OSError as e:
            logger.error("資金設定記錄落檔失敗 account=%s path=%s: %s",
                         account_id, cfg.capital_settings_path, e)
            raise HTTPException(status_code=500,
                                detail="設定記錄寫入失敗，請稍後重試") from e
        logger.info("資金設定記錄已落地 account=%s", account_id)

        # ⭐ 回應必須明講生效時機與「不做即時強制再平衡」。後者不是實作細節而是
        #    客戶會直接感受到的行為：調低比例之後部位不會立刻縮小，而是隨 leader
        #    的下一次動作自然收斂。不講清楚，客戶會以為系統沒反應而重複提交
        #    （每次都是一次真實的簽章與一顆 nonce）。
        return {
            "ok": True,
            "account_id": account_id,
            "allocated_capital": verified.allocated_capital_str,
            "capital_utilization": verified.capital_utilization_str,
            "use_full_equity": verified.use_full_equity,
            "effective": "next_engine_cycle",
            "effective_note": "已記錄，於引擎的下一個 cycle 生效——不是立即生效；"
                              "引擎會在套用前**自己重新驗證你的簽章與數值範圍**，"
                              "驗證不過則不會套用。",
            "consequences": "新的部位大小會在下一個 cycle 起套用，但**不會立即強制"
                            "再平衡**現有部位——引擎讓部位隨 leader 的後續動作自然"
                            "收斂，避免一次無謂的 taker 成本。調高比例會放大曝險與"
                            "清算風險。",
        }

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

    def _spot_stranded(address: str, *, funded: bool) -> dict | None:
        """客戶的錢是不是卡在 **spot** 錢包？回提示資料或 None（不提示）。

        ⭐ 為什麼這個提示存在：我方只鏡像 perp。客戶從 CEX 提幣或走第三方橋入金時，
        錢會落在 spot，perp 仍是 0 → `funded` 判 False。客戶在交易所頁面上看得到錢，
        卻不知道還差一步劃轉，而畫面上只寫「尚未入金」——這是入金漏斗上最貴的一種
        沉默。本欄位存在的目的就是把那句話補上。

        ⭐ `funded=True` ⇒ 一律不提示（前置閘，排在餘額查詢之前）。上一段就是本提示
        的全部適用範圍——「perp 還沒錢，而錢在 spot」。perp 已達 `min_user_deposit`
        的客戶正在正常跟單，對他顯示「你的錢卡在 spot」是純粹的困惑與客服成本，
        而下一段的 fail-silent 早已把「假警報比沒提示糟」定為本函式的失效方向。
        觸發面不是邊角：門檻僅 1 USDC，客戶劃轉時留下零頭、或同時持有 spot USDC
        做別的用途都會中——而產品本來就預期客戶有 spot 餘額與操作
        （見 `copytrade/costbreaker.py` 的 `_in_perp_scope`）。

        為什麼是 `funded` 閘而不是「把門檻改成 min_user_deposit 的相對量」：
        (a) 相對門檻只是把假警報的邊界往上挪，已入金又持有大額 spot 的客戶照樣中；
        (b) `config.py:89` 已明文記載門檻**刻意不**與 `min_user_deposit` 綁定
            （那是「夠不夠開始跟單」，這是「值不值得提醒」，兩個問題）——改成相對量
            等於推翻一個有依據的決定；
        (c) `funded` 由呼叫端傳入而非在此重算：`_progress` 已用
            `get_account_value(address) >= cfg.min_user_deposit` 算過一次，重算會變成
            「同一輪內兩次讀取」的混基準（工程原則 1），且多一次 HL 查詢與失敗面。

        ⚠️ **只偵測，不代勞**：spot → perp 的劃轉是 user-signed action，需要主鑰才
        簽得動；我方結構上不持有主鑰（非託管不變量，紅線 3）。本函式與它的回傳結構
        因此刻意**不含**任何劃轉入口、代簽 payload 或「幫我轉」的旗標——那會是一個
        我們無法兌現、且不該讓任何人以為存在的承諾。前端只能顯示說明與外部連結。

        ⭐ 查詢失敗 → **None（不提示）**，而不是「未知」或悲觀提示。這裡的失效方向
        與 builder 合規監控**刻意相反**：對一個已經劃轉好、正在正常跟單的客戶顯示
        「你的錢卡在 spot」是純粹的困惑與客服成本，而漏掉一次提示的代價只是他晚一點
        知道（下次載入就會看到）。假警報比沒提示糟——所以這裡 fail-silent。
        ConnectionError／TimeoutError 必須在此攔下：本 app 的全域 handler 會把它們
        變成 502，讓一個純輔助欄位打掉整個 onboarding 狀態頁。
        """
        if funded:
            return None  # 見 docstring：已入金 ⇒ 本提示不適用，且假警報比沒提示糟
        try:
            spot_usdc = hl.spot_usdc_balance(address)
        except Exception:  # noqa: BLE001 — 見 docstring：假警報比沒提示糟
            logger.warning("spot 餘額查詢失敗（僅影響提示，不影響 onboarding 判定）")
            return None
        if spot_usdc < cfg.spot_stranded_min_usdc:
            return None
        return {
            "usdc": str(spot_usdc),                       # Decimal → str（落地慣例）
            "threshold": str(cfg.spot_stranded_min_usdc),
            # ⭐ 顯式說明「為什麼只能你自己動手」：不寫的話，下一個讀這份回應的人
            # 很自然會問「那你們幫我轉一下」，而答案是結構上不行。
            "action_required": "manual_transfer_spot_to_perp",
            "note": (f"你有 {spot_usdc} USDC 在 **spot** 錢包。跟單只使用 perp 帳戶，"
                     "所以這筆錢要由你自己在 Hyperliquid 介面劃轉到 perp 才會開始"
                     "跟單。劃轉需要你的主錢包簽章，我們**無法**代為操作"
                     "（我們不持有你的主鑰）。"),
        }

    def _progress(address: str) -> dict:
        """onboarding 進度：狀態靠鏈上查詢判定（冪等、斷點續走以此為準，沿 M1 精神）。

        ⭐ 為什麼 spot 提示放在這裡而不是 `/api/me/leader`（2026-07-19）：
        1. 這裡是 `funded=False` 的**產生地**，而「錢卡在 spot」正是它最常見的原因。
           把診斷放在結論旁邊，前端不必自己把兩個端點的資料拼起來推理。
        2. `/api/me/leader` 目前**完全不觸網**（只讀 manifest）。為了一個輔助提示給它
           加上一條 HL 查詢，等於給一個純本地端點引進新的延遲與失敗模式。
        3. session 隔離沿用既有結構：`address` 來自 `Depends(_require_session)`，
           本函式**沒有** account 參數，結構上查不到別人的 spot 餘額。
        """
        account_id = derive_account_id(address)
        agent_address = store.get_agent_address(account_id)
        builder_fee_approved = hl.max_builder_fee(address, cfg.builder_address) != 0
        agent_approved = bool(agent_address) and agent_address in hl.agent_addresses(address)
        # ⭐ 一次讀取、兩處使用（工程原則 1）：擋下客戶的那個數字與顯示給他看的
        # 那個數字**必須是同一個**。為顯示另讀一次（或改用 withdrawable 之類的
        # 別的欄位）會產生「畫面寫 105、系統仍說不足」這種無法自我診斷的客服問題。
        perp_account_value = hl.get_account_value(address)
        funded = perp_account_value >= cfg.min_user_deposit  # 常數單一來源（M4）
        ready = bool(agent_address) and builder_fee_approved and agent_approved and funded
        return {
            "address": address, "account_id": account_id,
            "agent_address": agent_address,
            "agent_generated": agent_address is not None,
            "builder_fee_approved": builder_fee_approved,
            "agent_approved": agent_approved,
            "funded": funded,
            # ⭐⭐ 判定用的**同一個**數字原樣外流（2026-07-30）。存在的理由與
            # `spot_stranded` 同一類：`funded=False` 單獨出現時客戶無法自我診斷，
            # 而入金被擋最常見的原因就是「錢在 spot，perp 是 0」。把判定值與門檻
            # 並排顯示，客戶自己就看得出差在哪，不必開客服單。
            #
            # ⭐ 這兩個欄位**沒有** null 的情形：`get_account_value` 讀取失敗即拋
            # （經 resilience 邊界重試後仍失敗 → 全域 handler → 502），本函式刻意
            # 不攔。攔下來吞成 0 會讓「讀不到」偽裝成「沒錢」而使 funded=False，
            # 那是本專案明令禁止的方向（讀不到錢 ≠ 錢不存在）；整頁 502 才是誠實的
            # 失敗。前端因此不需要處理「未知餘額」狀態。
            "perp_account_value": str(perp_account_value),   # Decimal → str（落地慣例）
            "min_deposit": str(cfg.min_user_deposit),
            # 「還差多少」在**後端**用 Decimal 算完才外流（專案慣例：內部一律 Decimal）。
            # 交給前端做 `Number(a) - Number(b)` 會把兩個無損字串轉成 float 再相減，
            # 畫面上遲早出現 1.9999999997 這種數字。已達標時為 "0"。
            "deposit_shortfall": str(max(Decimal("0"),
                                         cfg.min_user_deposit - perp_account_value)),
            # None ＝ 已入金、沒有卡住的錢、或查不到（對前端是同一件事：不顯示提示）。
            # ⭐ funded 傳入而非重算：與上面同一次讀取的結果，同基準（工程原則 1）。
            "spot_stranded": _spot_stranded(address, funded=funded),
            "state": "READY" if ready else "IN_PROGRESS",
        }

    @app.get("/api/onboard/status")
    def onboard_status(address: str = Depends(_require_session)):
        return _progress(address)  # 純讀；副作用（寫 pending）只在 POST /api/onboard/verify

    @app.post("/api/onboard/verify")
    def onboard_verify(address: str = Depends(_require_session)):
        """檢查全過 → 寫 pending 條目（由 auto-activate watcher 於用戶簽章選定
        leader 後自動啟用；人工後備 scripts/filet_activate.py。spec 的「activate
        不做成 API 端點」仍成立——本端點只寫佇列，無 systemd/寫 manifest 權）。
        未全過 → 回進度供斷點續走（冪等，可重跑）。"""
        p = _progress(address)
        if p["state"] == "READY":
            # ⭐ user_address 出自 session、builder_address 出自伺服器設定（紅線 6）
            write_pending_entry(cfg.pending_path, account_id=p["account_id"],
                                user_address=address,
                                builder_address=cfg.builder_address,
                                network=cfg.network,
                                agent_address=p["agent_address"])
        return p

    # ---------- 風控偏好（錢包主人自選，2026-07-30）----------
    # ⚠️ 本路徑**沒有客戶簽章**，與換 leader／資金設定不同。取捨、邊界與已知缺口
    # 全文見 `filet/risk_prefs.py` 檔頭；一句話版本：這裡只調保護門檻（不是部位
    # 大小），且偏好只在啟用那一刻被烙進 env，之後 filet-api 影響不了執行中的引擎。

    def _my_pending_entry(account_id: str) -> dict | None:
        return next((e for e in load_pending(cfg.pending_path)
                     if e.get("account_id") == account_id), None)

    @app.get("/api/me/risk")
    def me_risk(address: str = Depends(_require_session)):
        """我的風控偏好 ＋ 可調參數規格（前端據此畫表單，不硬編任何數字）。

        ⭐ `editable` 是本回應最重要的欄位：偏好只在啟用那一刻寫進引擎的 env，
        而 watcher 啟用後會移除 pending 條目、且**不覆寫**既有 env。所以已啟用的
        帳號在這裡改設定不會有任何效果——前端必須據此把表單關掉並說明原因，
        不能讓客戶對著一個按了沒用的開關按第二次（沉默的無效比不給選更糟）。
        """
        entry = _my_pending_entry(derive_account_id(address))
        stored = entry.get("risk") if entry else None
        # ⭐ 存著的偏好驗不過（手改／舊格式）**不得** 500：那會讓客戶連改回來的
        # 介面都打不開，而 500 又長得像「稍後重試就好」的暫時故障（工程原則 2）。
        # 顯示的是 watcher 遇到同一份壞資料時會採用的那一份（風控開啟的安全側），
        # 並明講「這不是你存的值」——兩邊對同一個壞資料的解讀必須一致，
        # 否則畫面說關、引擎跑開，客戶無從得知哪個是真的。
        unreadable = False
        try:
            summary = prefs_summary(stored)
        except RiskPrefsError:
            logger.warning("風控偏好無法解析，改以安全側顯示 account=%s",
                           derive_account_id(address))
            unreadable = True
            summary = prefs_summary(safe_fallback_prefs())
        return {**summary,
                "stored_unreadable": unreadable,
                "stored_unreadable_note": (
                    "你先前儲存的風控設定讀取異常，畫面顯示的是系統採用的安全預設"
                    "（風控開啟）。請重新確認並儲存一次。" if unreadable else None),
                "editable": entry is not None,
                "not_editable_reason": None if entry is not None else "not_pending",
                "not_editable_note": (
                    None if entry is not None else
                    "這個帳號不在待啟用佇列中（尚未完成綁定，或引擎已經啟用）。"
                    "風控設定在引擎啟用的那一刻寫入並固定；已啟用的帳號要調整，"
                    "請聯絡我們（改動需要在伺服器端進行，不經由本頁）。")}

    @app.post("/api/me/risk")
    def me_risk_submit(body: dict, address: str = Depends(_require_session)):
        """設定我的風控偏好（寫進**我自己的** pending 條目）。

        失敗分類（工程原則 2）：值超界／欄位不認得／不在佇列中**全是 semantic**
        （4xx，客戶端不得自動重試）；寫檔失敗才是 transient（5xx）。

        ⭐ 不在佇列中一律 409（不是靜默 200）：見 `me_risk` 的 `editable` 說明。
        """
        account_id = derive_account_id(address)
        # ⭐⭐ 缺鍵補值的來源是「這個帳號目前存的值」，不是產品預設，且 `enabled`
        # 必須明確指定（審查 F1）：否則一個 body 為 `{}` 的請求會把客戶已開啟的
        # 風控靜默關掉並回 200——「沒設定」絕不等於「不要保護」。
        entry = _my_pending_entry(account_id)
        try:
            # 目前存的值本身可能是壞的；壞的就退到安全側當基底，不拿它當合法基準。
            try:
                base = canonical_prefs(entry.get("risk") if entry else None)
            except RiskPrefsError:
                base = safe_fallback_prefs()
            prefs = canonical_prefs(body.get("prefs") if isinstance(body, dict) else None,
                                    base=base, require_enabled=True)
        except RiskPrefsError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        try:
            written = set_pending_risk(cfg.pending_path, account_id, prefs)
        except OSError as e:
            logger.error("風控偏好落檔失敗 account=%s: %s", account_id, e)
            raise HTTPException(status_code=500,
                                detail="設定寫入失敗，請稍後重試") from e
        if not written:
            raise HTTPException(
                status_code=409,
                detail="這個帳號不在待啟用佇列中（尚未完成綁定，或引擎已經啟用）。"
                       "已啟用的帳號要調整風控設定請聯絡我們。")
        logger.info("風控偏好已更新 account=%s enabled=%s",
                    account_id, prefs["enabled"])
        return {"ok": True, "account_id": account_id, "prefs": prefs,
                "effective": "at_activation",
                "effective_note": "這份設定會在引擎啟用（約一分鐘內）時寫入並生效；"
                                  "啟用之後就固定下來，之後要調整請聯絡我們。"}

    @app.get("/api/admin/pending")
    def admin_pending(admin: str = Depends(_require_admin)):
        """管理端唯讀：檢視 pending 佇列。啟用由 auto-activate watcher（root timer，
        scripts/filet_auto_activate.py）自動處理；人工後備為 scripts/filet_activate.py。
        web 層無任何 systemd/寫 manifest 權——這一點在自動化後**仍然為真**。"""
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
    def ops_customers(days: int | None = None, window: str | None = None,
                      admin: str = Depends(_require_admin)):
        """每客戶損益（跨客戶聚合，admin only）。

        兩種時間窗，**互斥**：
        - 預設（未給 `window`）：now 往回 `days` 天（預設 1）。自由檢視用。
        - `window=accrued`：⭐ 與 /api/ops/revenue **同一個窗口**——兩者都呼叫
          `ops.accrued_window()`，不各自推導。這是本端點與收入對帳表可以並排相減的
          唯一模式；預設的 days 窗與 accrued 快照窗會錯開（accrued 是查詢當下的
          累積量，不對齊日曆日），兩張表的 builder fee 在該模式下**不可相減**。

        `days` 與 `window=accrued` 同時給 → 400。不靜默忽略其中一個：靜默的那一半
        會讓人以為自己看的是另一個基準（工程原則 1 的失敗正是「看起來同基準」）。

        單一 follower 查詢失敗只影響該列的 error 欄，不影響其他客戶（ops.customer_pnl）。
        """
        if window is not None and window != "accrued":
            raise HTTPException(status_code=400,
                                detail="window 僅支援 'accrued'（省略則用 days 窗）")
        if window is not None and days is not None:
            raise HTTPException(
                status_code=400,
                detail="days 與 window=accrued 互斥：accrued 窗由快照時刻決定，"
                       "不接受天數；請擇一")
        refs, manifest_errors = _load_followers()

        if window == "accrued":
            series = load_accrued_series(cfg.accrued_history_path)
            win = accrued_window(series)
            if win is None:
                # 不退化成日曆日／now 往回 N 天：那會產生一個「看起來對齊」的窗口，
                # 正是本修復要消滅的東西（同 ops_revenue 的 basis_unknown 分支）。
                return jsonable({
                    "window": "accrued", "basis_unknown": True,
                    "window_start": None, "window_end": None,
                    "note": f"{accrued_window_note(series)}；"
                            f"本次客戶損益無法與收入對帳同基準，故不計算。",
                    "manifest_errors": manifest_errors,
                })
            start, end = win
            rows = customer_pnl(refs, hl, start, end, store=store)
            return jsonable({"window": "accrued", "basis_unknown": False,
                             "window_start": start.isoformat(),
                             "window_end": end.isoformat(),
                             "start": start.isoformat(), "end": end.isoformat(),
                             "customers": rows,
                             "manifest_errors": manifest_errors})

        days = 1 if days is None else days
        if not 1 <= days <= 90:
            raise HTTPException(status_code=400, detail="days 須介於 1 到 90")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        rows = customer_pnl(refs, hl, start, end, store=store)
        return jsonable({"days": days, "start": start.isoformat(),
                         "end": end.isoformat(),
                         # window_start/window_end 兩個端點同名同義，讓人肉核對兩張表
                         # 的窗口是否真的相同（days 窗與 accrued 窗必定不同）
                         "window_start": start.isoformat(),
                         "window_end": end.isoformat(),
                         "window": "days",
                         "customers": rows,
                         "manifest_errors": manifest_errors})

    def _read_heartbeat(account_id: str, now_s: float) -> HeartbeatRead:
        """讀一個 follower 的引擎健康心跳（**只吃單一 account**，結構上拿不到別人的）。

        路徑由 `account_id` 推導 ⇒ 沒有「先載入全部再 filter」這一步，也就沒有
        「忘記 filter」這個 bug（沿 `_load_own_follower` 的同一個決定）。
        任何失敗都降級成 `unreadable`，**不 raise**：健康面板不得被單一 follower
        的心跳問題打成 500——那會讓其餘 follower 的狀態也一起看不到。
        """
        try:
            return read_heartbeat(
                heartbeat_path_for(cfg.exchange_dir, account_id), now_s)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("心跳讀取失敗 account=%s: %r", account_id, e)
            return HeartbeatRead("unreadable")

    @app.get("/api/ops/health")
    def ops_health(admin: str = Depends(_require_admin)):
        """系統健康面板（admin only）：引擎存活、kill switch、樣本覆蓋度、上次快照
        時間、告警數，以及**換 leader 已寫入但未套用的積壓**。

        ⭐⭐ 本端點的設計原則只有一條：**讀不到就說讀不到**。每一格都有一個
        「未知」狀態，且未知**絕不**被折疊成看起來健康的值——
        - kill switch 讀不到 → `null` ＋ `killswitch_known=false`（不是「沒觸發」）
        - 無 equity 樣本 → `engine_alive=null`（不是 false，更不是 true）
        - 告警檔讀不到 → `alerts=null`（不是 0）
        - 換 leader 鏈路查不下去 → `unapplied_leader_changes=null`（不是 0）
        健康面板的讀者用它決定「要不要現在去看」；一個謊報健康的格子會讓他**不去
        看**，而那正是他最該去看的時刻。謊報健康比沒有面板更危險。

        ⚠️ `engine_alive` 的基準（誠實標註，`liveness_basis` 欄位一併上呈）：
        這**不是** process 檢查，而是「引擎最近有沒有寫 equity 樣本」的代理
        （引擎每 cycle 寫一筆）。一個還在寫檔卻不下單的進程，這一格看起來會健康。
        真正的 process 存活由 systemd 管（RUNBOOK 的 `systemctl status`）。

        ⭐⭐ 資料來源：**引擎主動發布的健康心跳**（`filet.engine_health`），不是把
        引擎的狀態根權限放寬給 API。狀態根是 `0700 filet-engine`，面板跑在 filet-api
        ——放寬到 0750 會讓 API 讀得到引擎的**全部**狀態（權益樣本、ARM 檔、兩份已
        兌現帳本、告警流水），而面板只需要幾個摘要值。改由引擎每 cycle 往交換目錄的
        engine→api 子通道寫一份窄的摘要：**發布一份窄的產物，優於開放廣泛的讀取權**
        （與換 leader 的設計同構，只是通道反向）。每列的 `basis` 說明它的 kill switch
        與覆蓋度出自直讀（`state_root`）還是心跳（`heartbeat`）。

        ⭐⭐ **過期的心跳不會被當成目前狀態顯示**：`heartbeat_status="stale"` 時，
        面板只多出「最後心跳時刻」與「心跳年齡」兩格，心跳裡的值一個都不會被填進去
        （`read_heartbeat` 在過期時結構性地不回傳 payload）。心跳讀不到／過期時整列
        維持既有的「未知」語意——一份 40 分鐘前的「kill switch 未觸發」，在客戶的引擎
        已經熔斷的當下顯示成現況，正是本面板最不能犯的錯。
        `heartbeat_status` 三態不折疊：`missing`（引擎從未寫過／子目錄沒建）、
        `stale`（引擎沒跑，或引擎在跑但寫不進交換目錄）、`unreadable`（檔案壞了）
        的處置完全不同，合成一個「未知」等於把 admin 該做的第一步藏起來。

        ⭐ 積壓的判定與每日對帳報告**同源**：兩者都呼叫
        `leader_change_apply.scan_unapplied_leader_changes`，不各自寫一次判定式
        ——本端點是那份日報的即時視圖，兩邊漂移會讓其中一邊變成狼來了。
        """
        refs, manifest_errors = _load_followers()
        now_s = now_fn()

        def _state_root(ref):
            return Path(cfg.state_base) / ref.account_id

        rows = [follower_health(
            ref, _state_root(ref), now_fn=lambda: now_s,
            coverage_fn=lambda root: sample_coverage(root, now_fn=lambda: now_s),
            killswitch_fn=is_tripped,
            alerts_path_fn=lambda root: root / ALERTS_LOG_RELPATH,
            heartbeat_fn=lambda acct: _read_heartbeat(acct, now_s),
        ) for ref in refs]

        # 換 leader 積壓：查不下去（交換目錄沒設好、記錄檔讀不到）→ None ＋ 錯誤原文，
        # **不是 0**。0 會被讀成「沒有積壓」，實際是「無從得知有沒有積壓」。
        try:
            findings, lc_errors = scan_unapplied_leader_changes(
                refs, now_s,
                changes_path=cfg.leader_changes_path,
                ledger_for=lambda acct: (Path(cfg.state_base) / acct
                                         / LC_LEDGER_RELPATH))
        except Exception as e:  # noqa: BLE001 — 健康面板不得被單一項目打成 500
            logger.warning("換 leader 積壓掃描失敗: %r", e)
            findings, lc_errors = None, [f"換 leader 積壓掃描失敗：{e!r}"]

        backlog = None if (findings is None or lc_errors) else len(findings)
        return jsonable({
            "checked_at": datetime.fromtimestamp(now_s, timezone.utc).isoformat(),
            "engine_stale_after_s": ENGINE_STALE_S,
            "followers": rows,
            "unapplied_leader_changes": [
                {"account_id": f.account_id, "nonce": f.nonce,
                 "age_s": f.age_s, "reason": f.reason}
                for f in (findings or [])],
            "summary": health_summary(rows, backlog, lc_errors),
            "manifest_errors": manifest_errors,
        })

    @app.get("/api/ops/trade-quality")
    def ops_trade_quality(days: int | None = None, window: str | None = None,
                          admin: str = Depends(_require_admin)):
        """跨 follower 的成交品質（admin only）：TE（配對延遲）／滑價／taker 佔比／
        skipped 小額。

        ⭐ 這幾個量與 `scripts/copytrade_daily_report.py` 的日報**同源**：兩邊都呼叫
        `copytrade.report.compute_trade_quality`，不各自複製公式。日報是單一帳戶的
        每日檢視，本端點是同一組指標的跨客戶橫切——兩份算式會漂移，而兩張表並排
        顯示時看不出它們已經不同基準（工程原則 1）。

        時間窗與 /api/ops/customers **完全同一套規則**（互斥、同一個
        `ops.accrued_window()` 推導）：品質面板要能與損益、收入對帳並排讀，
        三者的窗口就必須出自同一個來源，不得各自推導。

        誠實呈現（本端點最重要的性質）：
        - 不知道 follower 跟哪個 leader（manifest 無 `leader_address`）→ TE 與滑價
          回 `null` ＋ `te_available=false`，**不回 0**（0 會被讀成「零延遲」）。
        - skipped 檔讀不到 → `skipped_available=false` ＋ `null`，同樣不回 0。
        - skipped 以日曆日落檔而窗口非整日 → 只回名目、**不回比例**（分子分母不同
          基準）。附 `skipped_note` 說明為什麼那一格是空的。
        """
        if window is not None and window != "accrued":
            raise HTTPException(status_code=400,
                                detail="window 僅支援 'accrued'（省略則用 days 窗）")
        if window is not None and days is not None:
            raise HTTPException(
                status_code=400,
                detail="days 與 window=accrued 互斥：accrued 窗由快照時刻決定，"
                       "不接受天數；請擇一")
        refs, manifest_errors = _load_followers()

        if window == "accrued":
            series = load_accrued_series(cfg.accrued_history_path)
            win = accrued_window(series)
            if win is None:
                return jsonable({
                    "window": "accrued", "basis_unknown": True,
                    "window_start": None, "window_end": None,
                    "note": f"{accrued_window_note(series)}；"
                            f"本次成交品質無法與收入對帳同基準，故不計算。",
                    "manifest_errors": manifest_errors,
                })
            start, end = win
        else:
            days = 1 if days is None else days
            if not 1 <= days <= 90:
                raise HTTPException(status_code=400, detail="days 須介於 1 到 90")
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)

        # ⭐ leader 成交每個相異 leader **查一次**（多個 follower 跟同一個 leader
        # 是常態）。快取的 key 是正規化小寫位址，否則同一位址的兩種大小寫會各查一次。
        leader_cache: dict[str, list] = {}

        def _leader_fills_for(ref):
            addr = (ref.leader_address or "").lower()
            if not addr:
                return None       # manifest 未記錄 → TE 不可算（見端點 docstring）
            if addr not in leader_cache:
                leader_cache[addr] = hl.get_user_fills(addr, start, end)
            return leader_cache[addr]

        days_in_window = utc_days_in_window(start, end)

        def _skipped_for(ref):
            """該 follower 在窗口涵蓋日的 skipped 名目；**任一天讀不到就回 None**。

            ⭐ 「部分天數有檔」不得當成完整合計：少一天的檔會讓名目偏低，而偏低的
            skipped 讀起來就是「引擎沒在跳過客戶的單」——正好是這個指標要抓的問題。
            寧可回「不知道」，不回一個偏低卻看不出偏低的數。
            """
            total = Decimal("0")
            for day_iso in days_in_window:
                v = load_skipped_notional(
                    skipped_path_for(cfg.state_base, ref.account_id, day_iso))
                if v is None:
                    return None
                total += v
            return total

        rows = trade_quality_rows(refs, hl, start, end,
                                  leader_fills_for=_leader_fills_for,
                                  skipped_for=_skipped_for)
        return jsonable({
            "window": window or "days",
            "basis_unknown": False,
            **({"days": days} if window != "accrued" else {}),
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "skipped_days": days_in_window,
            "followers": rows,
            "summary": trade_quality_summary(rows),
            "manifest_errors": manifest_errors,
        })

    @app.get("/api/ops/revenue")
    def ops_revenue(threshold_pct: float = 0.01, admin: str = Depends(_require_admin)):
        """收入對帳（admin only）：應收（Σ 各客戶歸屬 builder_fee）vs 實收（北極星
        accrued 今昨差）。

        ⚠️ 同基準（工程原則 1）：accrued 是**查詢當下**的鏈上累積量，故相鄰兩筆的差
        涵蓋 `(前次 captured_at, 本次 captured_at]`——fills 窗口一律取**這兩個快照時刻**，
        不是日曆日。曾經用日曆日取 fills（opus 對抗審查 Critical）：日報 cron 排在
        00:10 時，accrued 增量其實是「昨天一整天」，fills 卻只有「今天 0 點到現在」
        的十幾分鐘，健康帳戶會被判成巨大差異並誤報漏財。

        兩種**拒絕計算**的情形（回結構化旗標而非硬算——算錯的數字比沒有數字危險）：
        - `insufficient_accrued_history`：歷史不足兩點（缺 accrued_prev 會把整段累積量
          當成單日增量，產生天文數字的假 delta）。
        - `basis_unknown`：相鄰兩筆任一缺 `captured_at`（舊格式資料），或兩個時刻非
          嚴格遞增（快照被回填／時鐘倒退）——窗口無從對齊，不算 discrepancy、
          不告警（`over_threshold` 恆 False），附 `note` 說明原因。
        兩種情形都不回數值欄（型別上就讀不到，避免顯示層把「無資料」畫成 0）。"""
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
        prev_pt, now_pt = series[-2], series[-1]
        # ⭐ 窗口只從 ops.accrued_window 取（與 /api/ops/customers?window=accrued 同源，
        # 工程原則 1）。不硬算：窗口界只能來自快照時刻，缺了就沒有正確答案。用日期猜
        # 會整整錯開一天，把健康帳戶判成漏財——錯的數字會叫醒人去查不存在的問題。
        win = accrued_window(series)
        if win is None:
            note = accrued_window_note(series)
            return jsonable({
                "insufficient_accrued_history": False, "basis_unknown": True,
                "over_threshold": False,          # 不告警：算不出來 ≠ 有異常
                "day": now_pt.date, "prev_day": prev_pt.date,
                "window_start": None, "window_end": None,
                "note": f"{note}；本日對帳跳過（下一次日報落檔後即自動恢復）。",
                "manifest_errors": manifest_errors,
            })
        start, end = win
        rows = customer_pnl(refs, hl, start, end, store=store)
        result = revenue_reconciliation(rows, now_pt.accrued, prev_pt.accrued,
                                        threshold_pct=threshold_pct)
        if result["over_threshold"]:
            # 對帳超標＝收入歸屬與鏈上實收對不上，大聲留痕（工程原則 3）
            logger.warning("收入對帳超標 day=%s attributed=%s accrued_delta=%s pct=%s",
                           now_pt.date, result["attributed"], result["accrued_delta"],
                           result["discrepancy_pct"])
        return jsonable({**result, "insufficient_accrued_history": False,
                         "basis_unknown": False,
                         "day": now_pt.date, "prev_day": prev_pt.date,
                         "window_start": start.isoformat(), "window_end": end.isoformat(),
                         "customers": rows, "manifest_errors": manifest_errors})

    @app.get("/api/ops/subscriptions")
    def ops_subscriptions(admin: str = Depends(_require_admin)):
        """訂閱對帳（admin only）：本地 billing 表 vs Stripe 真實狀態。

        存在理由：webhook 是本地 billing 表的唯一寫入者，掉一包就永久漂移，
        原本沒有任何察覺途徑。兩個漂移方向的危害不同（見 ops.subscription_drift）：
        本地 active／Stripe 已取消 = 漏財；Stripe active／本地沒有 = 客戶付了錢沒權益。

        ⭐ **刻意只偵測、不修正**：本端點不做任何寫入（不改本地 billing、不碰 Stripe）。
        「一鍵以 Stripe 為準同步本地」會直接改變計費與 entitlement 狀態——那是碰錢的
        操作，必須是人工決策（紅線 5/6 的精神）。修正動作留待後續，且需使用者明確授權。
        本輪的正確用法：看到漂移 → 人工確認 Stripe 端真相 → 決定怎麼處理。

        ⚠️ `truncated=True` 時清單不完整（達 MAX_RECONCILE_SUBSCRIPTIONS 上限），
        `local_active_stripe_not` 內「Stripe 查無此訂閱」的項目可能是假漂移——
        原樣上呈而非靜默吞掉，讓管理員知道結論不可信（工程原則 3）。

        Stripe 失敗分類（紅線 4）：transient=ConnectionError→502；semantic=BillingError→502。
        列表查詢是冪等讀取（與 checkout 不同），重試安全——見 list_subscriptions docstring。
        """
        _require_billing()
        listing = billing.list_subscriptions()
        result = subscription_drift(store.list_billing(), listing["subscriptions"])
        if result["drift_count"]:
            # 漂移＝計費與服務對不上，大聲留痕（工程原則 3），不只靜靜回 200
            logger.warning("訂閱對帳發現漂移 total=%d 漏財=%d 付錢沒權益=%d "
                           "狀態不符=%d 孤兒=%d truncated=%s",
                           result["drift_count"], len(result["local_active_stripe_not"]),
                           len(result["stripe_active_local_not"]),
                           len(result["status_mismatch"]), len(result["orphan_stripe"]),
                           listing["truncated"])
        return jsonable({**result, "truncated": listing["truncated"]})

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
        端點無輸入參數。

        **兩道擋板，語意不同，前端要能分辨**（都是 409，detail 不同）：
        1. 已 active → 「已有生效訂閱」。資料源是本地 billing 表。
        2. 有未逾時的 pending checkout → 「已有進行中的結帳」。⭐ 這道是必要的：
           本地 billing 表的**唯一寫入者是 webhook**，使用者付完款導回時 webhook
           可能還沒送達（秒級延遲，Stripe 不保證即時），第 1 道此時查無記錄 →
           建出第二個 Checkout Session → 兩張訂閱、兩次扣款。首購路徑連 customer_id
           都不共用，Stripe 端不會自行去重（工程原則 2：非冪等寫入不能只靠事後狀態）。

        ⭐ 佔位在呼叫 Stripe **之前**（claim → call）：反過來就等於沒擋板，
        重複請求會在 Stripe 往返的那幾百毫秒內全部通過。
        ⭐ 建 session 失敗必須把位子還回去（工程原則 3 的補償版）：一次網路抖動
        不該讓客戶 15 分鐘不能結帳。用 except-clear-raise，不吞例外——原本的失敗
        分類（ConnectionError/BillingError → 502）照原樣往上走。
        Stripe 失敗分類（紅線 4）：transient=ConnectionError→502 稍後重試（人肉重試，
        非冪等寫入不在後端盲重試）；semantic=BillingError→502 專屬 handler。"""
        _require_billing()
        account_id = derive_account_id(address)
        rec = store.get_billing(account_id)
        if rec is not None and rec.status == "active":
            raise HTTPException(status_code=409, detail="已有生效訂閱")
        if not store.claim_pending_checkout(account_id, now_s=now_fn(),
                                            ttl_s=PENDING_CHECKOUT_TTL_S):
            raise HTTPException(status_code=409,
                                detail="已有進行中的結帳，請完成付款或稍候再試")
        try:
            url = billing.create_checkout_session(
                account_id=account_id, price_id=cfg.stripe_price_id,
                success_url=f"{cfg.siwe_uri}/billing?checkout=success",
                cancel_url=f"{cfg.siwe_uri}/billing?checkout=cancel",
                customer_id=rec.stripe_customer_id if rec else None)
        except Exception:
            # 沒有建成 session ⇒ 沒有任何 Stripe 副作用 ⇒ 位子必須立刻還回去
            store.clear_pending_checkout(account_id)
            raise
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
