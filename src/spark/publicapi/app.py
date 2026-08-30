"""src/spark/publicapi/app.py
FastAPI app factory。所有外部依賴（store / keysvc client / HL gateway / 時鐘）由
create_app 注入——測試全離線。onboarding 端點一律綁 session 地址：account_id 由
session 衍生，端點無 account 參數（紅線 3：別人不能替你 onboard 是結構保證）。"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
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
from spark.filet.aggregate import collect_follower_summary, summarize_fills
from spark.filet.followers import FollowerRef, load_followers_tolerant
from spark.filet.leader_change import (LeaderChangeError, build_leader_change_message,
                                       build_leader_change_record,
                                       load_leader_changes, verify_leader_change,
                                       write_leader_change)
from spark.filet.leader_perf import (BASIS_NOTE, INSUFFICIENCY_MARKERS,
                                     MDD_SAMPLING_NOTE, UPPER_BOUND_NOTE,
                                     compute_window_performance, extract_window)
from spark.filet.leaderboard import load_latest_snapshot, snapshot_rows_by_address
from spark.filet.leaders import LeaderRef, find_leader, is_selectable, load_leaders
from spark.filet.strategies import (CAGR_SAMPLE_THRESHOLD_DAYS, build_cagr_pct,
                                    build_equity_index, build_metrics,
                                    build_methodology, build_strategy_view)
from spark.publicapi import hl_explore, hl_leaderboard, public_stats
from spark.filet.user_leaders import load_user_leaders, merge_leaders, record_user_leader
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
                                       heartbeat_dir_for, heartbeat_path_for,
                                       read_heartbeat)
from spark.filet.leader_change_apply import LEDGER_RELPATH as LC_LEDGER_RELPATH
from spark.filet.leader_change_apply import scan_unapplied_leader_changes
from spark.publicapi.ops import ENGINE_STALE_S
from spark.publicapi.ops import (accrued_window, accrued_window_note, customer_pnl,
                                 follower_health, follower_trade_quality,
                                 health_summary, jsonable,
                                 load_accrued_series, load_skipped_notional,
                                 revenue_reconciliation, skipped_path_for,
                                 subscription_drift, trade_quality_rows,
                                 trade_quality_summary, utc_days_in_window)
from spark.filet.risk_prefs import (RiskPrefsError, canonical_prefs, prefs_summary,
                                    safe_fallback_prefs)
from spark.filet.risk_settings import (RISK_SETTINGS_MAX_AGE_S, RiskSettingsError,
                                       build_risk_settings_message,
                                       build_risk_settings_record,
                                       build_risk_unlock_message,
                                       build_risk_unlock_record,
                                       load_risk_settings, verify_risk_settings,
                                       verify_risk_unlock, write_risk_settings,
                                       write_risk_unlock)
from spark.filet.close_all import (CloseAllError, build_close_all_message,
                                   build_close_all_record, close_all_path_for,
                                   close_all_result_path_for,
                                   load_close_all_requests, read_close_all_result,
                                   verify_close_all, write_close_all_request)
from spark.filet.pause_flag import pause_flag_path_for, write_pause_flag
from spark.copytrade.notifier import NullNotifier, TelegramNotifier
from spark.publicapi.pending import load_pending, write_pending_entry
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer
from spark.publicapi.store import ApiStore
# vault advisory 檢查的**單一實作**（2026-07-31 Wave 2）：檢查（run_checks）與資料
# 抓取（fetch_preflight_data）都 import preflight 腳本的定義，app 端不得重寫任何一項
# ——恆等式若在兩處各寫一份，漂移的症狀是「腳本說 PASS、准入說 FAIL」而兩邊都自信。
from scripts.vault_preflight import fetch_preflight_data, run_checks

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


# 風控設定／解除熔斷驗簽失敗 → 回給客戶的分類化訊息。第三張表（不與資金設定共用）：
# reason 集合雖然目前相同，但兩者的**可行動建議**不同（一邊是「重新設定門檻」、
# 一邊是「重新按恢復跟單」），而共用一張表會讓其中一邊的文案改動靜默改掉另一邊。
# ⭐ 同樣刻意不是 `str(e)`：例外訊息為了除錯內嵌了 nonce／issued_at 原值。
# ⚠️ **偏好值的區間錯誤不走這張表**（走 `RiskPrefsError` 的 `str(e)`）：那類訊息必須
# 說得出合法區間才有辦法讓客戶改對，而它只含參數名與數值——不含任何授權材料。
RISK_SETTINGS_DETAIL_DEFAULT = "風控設定驗證失敗，請重新取得待簽原文並重簽"
RISK_SETTINGS_DETAIL = {
    "malformed": "請求欄位格式不正確，請重新取得待簽原文並重簽",
    "account_mismatch": "請求的帳號與登入身分不符",
    "action_mismatch": "這份簽章不是風控設定授權，請重新取得待簽原文並重簽",
    "expired": "簽章已過期，請重新取得待簽原文並重簽",
    "bad_signature": "簽章無法驗證，請重新簽署",
    "signer_mismatch": "簽章者不是本帳號的持有人",
    "nonce_unusable": "這份授權已被使用或已過期，請重新取得待簽原文並重簽",
}
RISK_UNLOCK_DETAIL_DEFAULT = "解除熔斷驗證失敗，請重新取得待簽原文並重簽"
RISK_UNLOCK_DETAIL = {**RISK_SETTINGS_DETAIL,
                      "action_mismatch": "這份簽章不是解除熔斷的授權，"
                                         "請重新取得待簽原文並重簽"}

# 「平倉並撤銷」驗簽失敗 → 回給客戶的分類化訊息（第四張表：獨立的可行動建議，
# 同 RISK_UNLOCK_DETAIL 不與資金/風控共用一張表的理由）。
CLOSE_ALL_DETAIL_DEFAULT = "平倉並撤銷驗證失敗，請重新取得待簽原文並重簽"
CLOSE_ALL_DETAIL = {**RISK_SETTINGS_DETAIL,
                    "action_mismatch": "這份簽章不是平倉並撤銷的授權，"
                                       "請重新取得待簽原文並重簽"}


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


class RiskSettingsMessageBody(BaseModel):
    """`POST /api/me/risk/message` 的 body——只有待簽的偏好本身。"""

    prefs: dict


class RiskSettingsBody(BaseModel):
    """客戶簽章的風控設定請求（欄位＝filet/risk_settings.py 的記錄格式減去 action）。

    `action` 刻意**不收**、`account_id` 顯式收下再與 session 衍生值比對（不符 403），
    兩者的理由與 `CapitalSettingsBody` 逐字相同——先讀那份 docstring。

    `prefs` 收 `dict`（值一律是字串或 bool，不是 float）：比例值直接決定熔斷門檻，
    而 float 進不了 Decimal 的精確世界。正規化與區間的單一定義點在
    `risk_prefs.canonical_prefs`，本模型只負責把它原樣接下來。
    """

    account_id: str
    prefs: dict
    nonce: str
    issued_at: str
    signature: str
    # 客戶端實際簽的原文。**驗證完全不看它**（伺服器重建自己的版本）——僅原樣留存。
    message: str = ""


class RiskUnlockBody(BaseModel):
    """客戶簽章的「立即解除熔斷鎖定」請求（記錄格式減去 action）。

    ⭐ 這是**一次性動作**：與 `RiskSettingsBody` 分成兩個模型、兩個端點、兩個檔，
    因為一份「調整門檻」的授權絕不能被兌換成一次「把熔斷鎖打開」（反向亦然）。
    結構性的分隔在待簽訊息的第一行（見 filet/risk_settings.py 檔頭的域分隔論證）。
    """

    account_id: str
    nonce: str
    issued_at: str
    signature: str
    message: str = ""


class CloseAllBody(BaseModel):
    """客戶簽章的「平倉並撤銷」請求（記錄格式減去 action）。

    ⭐ 一次性、不可逆動作——形狀沿 `RiskUnlockBody`（同一個信任錨），但**獨立**
    一個模型、一個端點、一個檔：一份「立即恢復跟單」的授權絕不能被兌換成一次
    「平倉並撤銷」（反向亦然）。結構性分隔在待簽訊息的第一行（見
    `filet/close_all.py` 檔頭的域分隔論證）。
    """

    account_id: str
    nonce: str
    issued_at: str
    signature: str
    message: str = ""


class PauseBody(BaseModel):
    """暫停/恢復跟單（Task 15 kill switch 第一級）。**無需簽章**——兩個方向都只在
    既有授權範圍內收窄/恢復活動（見專案 CLAUDE.md 紅線 5 對照）。"""

    action: str  # "pause" | "resume"（見 me_pause 的顯式驗證，不用 Literal 以維持
                 # 與其餘 4xx 分類化訊息一致的錯誤處理風格）


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


# ---------- /api/me/dashboard（Task 13：客戶儀表板唯一資料源）純函式層 ----------
# ⭐ 全部模組層、不吃 create_app 的閉包，方便單元測試直接餵固定資料算錨例
# （available_pct 等），也讓 `_safe_block` 能把任何一塊的例外獨立隔離。

def _safe_block(label: str, fn, *args, **kwargs):
    """每一塊獨立 nullable 的**單一實作**：子資料源丟例外 → 該塊回 None，端點
    絕不因此 500（Task 13 規格：六塊各自獨立失敗，其餘照常）。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — 區塊級隔離是本函式存在的唯一理由
        logger.warning("dashboard 區塊 %s 計算失敗: %r", label, e)
        return None


def _dashboard_account_snapshot(state: dict) -> dict | None:
    """clearinghouseState 原始回應 → equity 摘要。**單一次呼叫**取出的四個欄位
    （accountValue／totalMarginUsed／withdrawable／totalNtlPos）互為同源同基準
    （工程原則 1；plan 不變量 2）——本函式不接受第二個資料來源做混算。
    形狀不符 → None（讀不到 ≠ 危險態，不變量 7：顯示「—」，不觸發任何動作）。
    """
    if not isinstance(state, dict):
        return None
    ms = state.get("marginSummary")
    if not isinstance(ms, dict):
        return None
    try:
        return {
            "account_value": Decimal(str(ms["accountValue"])),
            "margin_used": Decimal(str(ms["totalMarginUsed"])),
            "withdrawable": Decimal(str(state["withdrawable"])),
            "total_ntl_pos": Decimal(str(ms["totalNtlPos"])),
        }
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return None


def _available_pct(withdrawable: Decimal, margin_used: Decimal) -> Decimal | None:
    """可用保證金（withdrawable）佔已用保證金（margin_used）之比；分母 ≤0 → None
    （不得除零；沿全 repo「分母 0 → null」的既有慣例）。"""
    if margin_used <= 0:
        return None
    return (withdrawable / margin_used).quantize(Decimal("0.0001"),
                                                  rounding=ROUND_HALF_UP)


def _dashboard_positions_raw(state: dict) -> list[dict] | None:
    """assetPositions → 逐倉位摘要（Decimal 值）。

    ⭐ `value`／`mark` 是**同一次** clearinghouseState 內既有欄位的代數推導，
    不是新引入一個從未在本 repo 驗證過的欄位名（`positionValue`／`markPx`
    全 repo 搜尋皆無出現——letter-to-future-sessions 的教訓：欄位名是假設，
    不是事實，未經驗證的欄位名與未經驗證的公式同樣可疑）：
    - `value = marginUsed × leverage`（HL 對 leverage 的定義即 notional/marginUsed，
      兩個因子同出這一次回應，見 hyperliquid.py／leaderboard.py 對這兩個既有
      欄位的用法）。
    - `mark = entryPx + unrealizedPnl / szi`（HL 對 unrealizedPnl 的定義即
      szi × (markPx − entryPx)，多空同一式，非近似）。
    """
    if not isinstance(state, dict):
        return None
    raw = state.get("assetPositions")
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    try:
        for item in raw:
            pos = item["position"]
            szi = Decimal(str(pos["szi"]))
            if szi == 0:
                continue
            entry_px_raw = pos["entryPx"]
            entry_px = (Decimal(str(entry_px_raw)) if entry_px_raw is not None
                       else Decimal("0"))
            leverage = pos["leverage"]
            lev_val = Decimal(str(leverage["value"]))
            margin_used = Decimal(str(pos["marginUsed"]))
            upnl = Decimal(str(pos["unrealizedPnl"]))
            out.append({
                "coin": pos["coin"],
                "side": "long" if szi > 0 else "short",
                "leverage": lev_val,
                "margin_mode": ("cross" if leverage.get("type") == "cross"
                               else "isolated"),
                "value": margin_used * lev_val,
                "upnl": upnl,
                "entry": entry_px,
                "mark": entry_px + (upnl / szi),
            })
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return None
    return out


def _dashboard_exposure(acct: dict, positions: list[dict] | None) -> dict:
    """曝險摘要：`notional`／`leverage` 取自 acct（同一次 clearinghouseState，
    工程原則 1）；多空占比與最大倉位需要逐倉位 `value`——`positions` 解析失敗
    （None）時這幾格個別回 None，其餘照常（不因持倉明細壞掉而連坐帳戶級數字）。
    """
    av = acct["account_value"]
    leverage = ((acct["total_ntl_pos"] / av).quantize(Decimal("0.01"),
                                                       rounding=ROUND_HALF_UP)
               if av > 0 else None)
    long_pct = short_pct = max_position = None
    if positions is not None:
        total_value = sum((p["value"] for p in positions), Decimal("0"))
        if total_value > 0:
            long_value = sum((p["value"] for p in positions if p["side"] == "long"),
                             Decimal("0"))
            short_value = total_value - long_value
            long_pct = (long_value / total_value * 100).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP)
            short_pct = (short_value / total_value * 100).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP)
            biggest = max(positions, key=lambda p: p["value"])
            max_position = {
                "symbol": biggest["coin"],
                "pct": (biggest["value"] / total_value * 100).quantize(
                    Decimal("0.1"), rounding=ROUND_HALF_UP),
            }
    return {
        "notional": acct["total_ntl_pos"], "leverage": leverage,
        "long_pct": long_pct, "short_pct": short_pct,
        "position_count": len(positions) if positions is not None else None,
        "max_position": max_position,
    }


def _read_pause_flag(exchange_dir: str, user_address: str) -> tuple[bool | None, bool]:
    """暫停旗標（路徑 `pause_flag_path_for`，寫端見 `POST /api/me/pause`）。
    回傳 `(paused, unknown)`：
    - 檔案不存在 → `(False, False)`（讀不到檔案視為未暫停，Task 13 規格明文）。
    - 讀出來但格式不對／IO 失敗 → `(None, True)`——**不**比照引擎側 fail-safe
      當成「視為暫停」（那是 Task 15 引擎動作側的方向，見 `filet/pause_flag.py`
      檔頭；本端點只是顯示層）；呼叫端改用別的訊號判定 `state`，並在
      `signal_source_ok` 反映這裡讀不準。
    """
    p = Path(pause_flag_path_for(exchange_dir, user_address))
    if not p.exists():
        return False, False
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        logger.warning("dashboard: pause 旗標讀取失敗 address=%s: %r", user_address, e)
        return None, True
    if not isinstance(data, dict):
        return None, True
    return bool(data.get("paused")), False


def _dashboard_close_request(exchange_dir: str, account_id: str) -> dict | None:
    """「平倉並撤銷」請求塊（opus 審查 Critical 2b）：`{"state": "pending"|
    "expired"|"completed"}`；本帳號從未提出過請求 → `None`（多數帳號的正常狀態，
    前端不顯示這塊）。

    state 判定（單一定義，`close_all_apply.CloseAllApplier` 是唯一寫端）：
    - 查無本帳號的請求 → `None`。
    - 有請求，但 result 標記不存在，或其 `request_issued_at` 跟這筆請求的
      `issued_at` 不同（客戶重簽過新的一筆，舊標記還沒被蓋掉）→ `pending`：
      引擎還沒處理過**這一筆**（不能拿舊標記冒充新請求已處理）。
    - 標記存在且 `request_issued_at` 相符 → 標記的 `status`。

    讀取/格式失敗一律回 `None`（顯示層失敗方向，同 `_read_pause_flag`——這裡沒有
    安全動作可做，只有「不確定」可以誠實回報）。
    """
    try:
        requests = load_close_all_requests(close_all_path_for(exchange_dir))
        mine = [r for r in requests
               if isinstance(r, dict) and r.get("account_id") == account_id]
    except (OSError, ValueError, TypeError, AttributeError) as e:
        logger.warning("dashboard: 平倉並撤銷請求讀取失敗 account=%s: %r",
                       account_id, e)
        return None
    if not mine:
        return None
    issued_at = mine[-1].get("issued_at")
    result = read_close_all_result(close_all_result_path_for(exchange_dir, account_id))
    if (result is not None and isinstance(issued_at, str)
            and result.get("request_issued_at") == issued_at
            and result.get("status") in ("expired", "completed")):
        return {"state": result["status"]}
    return {"state": "pending"}


def _dashboard_guards(hb: "HeartbeatRead", mine, acct: dict | None,
                      leaders_path: str) -> dict:
    """設定 vs 目前三條護欄。**max** 全部取自各自的權威來源（心跳＝引擎實際套用值、
    leaders.json＝策略層強制槓桿帽），**now** 取自同一次 clearinghouseState（acct）。

    ⭐⭐ `drawdown.now` 刻意恆為 `None`：唯一可信的「目前回撤」基準是引擎自己的
    7 天滾動高水位樣本（`copytrade.equity`），只活在引擎狀態根，filet-api 讀不到。
    改用這裡能拿到的 30D 窗權益指數自算「現在的回撤」，是拿另一個 basis 冒充它
    ——與 equity 事故 #2/#4/#5 同一種陷阱（比較的兩側必須同源同基準）。寧可顯示
    「—」，不自組一個看起來像對的數字（plan 不變量 7）。
    """
    scale_max = lev_max = dd_max = dd_enabled = None
    if hb.fresh:
        data = hb.data or {}
        cap = data.get("capital") or {}
        if cap.get("source") in ("customer_signed", "env_default"):
            util = cap.get("capital_utilization")
            if util is not None:
                try:
                    scale_max = Decimal(str(util))
                except (ValueError, ArithmeticError):
                    scale_max = None
        risk = data.get("risk") or {}
        if risk.get("source") in ("customer_signed", "env_default"):
            dd_enabled = risk.get("controls_enabled")
            mdd = (risk.get("prefs") or {}).get("max_drawdown_pct")
            if mdd is not None:
                try:
                    dd_max = -Decimal(str(mdd))
                except (ValueError, ArithmeticError):
                    dd_max = None
    if mine is not None and mine.leader_address:
        try:
            entry = next((r for r in load_leaders(leaders_path)
                         if r.address == mine.leader_address), None)
        except ValueError:
            entry = None
        if entry is not None and entry.max_leverage is not None:
            try:
                lev_max = Decimal(entry.max_leverage)
            except (ValueError, ArithmeticError):
                lev_max = None
    now_scale = now_lev = None
    if acct is not None and acct["account_value"] > 0:
        now_scale = (acct["margin_used"] / acct["account_value"]).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP)
        now_lev = (acct["total_ntl_pos"] / acct["account_value"]).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "scale": {"now": now_scale, "max": scale_max},
        "leverage": {"now": now_lev, "max": lev_max},
        "drawdown": {"now": None, "max": dd_max, "enabled": dd_enabled},
    }


def _dashboard_status(mine, hb: "HeartbeatRead", acct: dict | None,
                      leaders_path: str, exchange_dir: str) -> dict:
    """狀態塊：state 四態＋護欄。`mine is None`（未活化）→ `inactive`，其餘全
    null——沒有引擎在追蹤這個帳號，任何跟單相關的數字都無從談起。"""
    if mine is None:
        return {
            "strategy_name": None, "state": "inactive", "following_days": None,
            "signal_source_ok": None, "close_request": None,
            "guards": {"scale": {"now": None, "max": None},
                      "leverage": {"now": None, "max": None},
                      "drawdown": {"now": None, "max": None, "enabled": None}},
        }
    strategy_name = None
    if mine.leader_address:
        try:
            strategy_name = next((r.name for r in load_leaders(leaders_path)
                                  if r.address == mine.leader_address), None)
        except ValueError:
            logger.error("dashboard: leader 白名單載入失敗（僅影響顯示名稱） %s",
                        leaders_path)
    tripped = (hb.data or {}).get("killswitch_tripped") if hb.fresh else None
    paused, pause_unknown = _read_pause_flag(exchange_dir, mine.user_address)
    if tripped is True:
        state = "halted"
    elif paused is True:
        state = "paused"
    else:
        state = "following"
    last_cycle_ok = ((hb.data or {}).get("last_cycle") or {}).get("result") == "ok"
    return {
        "strategy_name": strategy_name, "state": state, "following_days": None,
        "signal_source_ok": bool(hb.fresh and not pause_unknown and last_cycle_ok),
        "close_request": _dashboard_close_request(exchange_dir, mine.account_id),
        "guards": _dashboard_guards(hb, mine, acct, leaders_path),
    }


def _dashboard_sync(ref: FollowerRef, hl, mine, hb: "HeartbeatRead",
                    now_s: float) -> dict:
    """同步誤差塊：過濾自 `follower_trade_quality`——與 `/api/ops/trade-quality`
    完全**同一個純函式**，只餵這個帳號自己（不變量 4 的同型：不得跨客戶）。
    窗口固定近 24 小時，對應 `missed_signals_24h` 命名的語意窗。

    latency_p95_ms／unsynced_positions／scale_deviation_pct／missed_signals_24h／
    missed_reason／last_recon_ts：`compute_trade_quality`／`TradeQuality` 未產出
    這些量（全 repo 搜尋確認），沒有既有來源 → 維持 `None`，不新造公式
    （R2·C／M3 round3 Task 3：無樣本一律 `None`，絕不送 `0` 冒充「零誤差」）。

    ⭐ M3 round3 Task 3：`data_state` 三態——
    - `"error"`：這個帳號**自己**的成交查詢失敗（`follower_trade_quality` 內部
      已把 `adapter.get_user_fills` 的例外吞成 `quality_available=False`，不是
      拋出來——這裡把它投影成一個前端看得懂的狀態，而不是假裝「這一輪沒有樣本」）。
    - `"warming"`：這個帳號的引擎**從未**發布過心跳（`hb.status == "missing"`，
      見 `engine_health.HeartbeatRead` 檔頭「剛 activate」）——結構上就是「還沒
      有時間累積」，不是資料源壞了。
    - `"ok"`：其餘情況，即使個別欄位仍是 `None`（例如 manifest 未記
      `leader_address`、或 leader 24h 內沒有成交）——那是「這個來源本來就沒有
      這個量」，與「還在暖機」是不同的處境，不可疊成同一個狀態（工程原則 1）。

    `since_ts`（跟單啟動時間）：全 repo 沒有既有來源（manifest／心跳都不記
    follower 首次被引擎追蹤的時刻——`_dashboard_status` 的 `following_days`
    留 `None` 是同一個既有缺口）。沿用本函式檔頭「沒有既有來源不新造公式」的
    既有慣例，本輪維持 `None`；`data_state="warming"` 已足以讓前端畫出
    「跟單啟動後 24h 內開始累積」這句固定文案，不需要精確的起算時刻。
    """
    end = datetime.fromtimestamp(now_s, timezone.utc)
    start = end - timedelta(hours=24)
    leader_fills = None
    if mine is not None and mine.leader_address:
        try:
            leader_fills = hl.get_user_fills(mine.leader_address, start, end)
        except Exception as e:  # noqa: BLE001 — leader 查詢失敗只讓 TE 未知
                                # （沿 ops_trade_quality 的既有隔離慣例）
            logger.warning("dashboard sync: leader 成交查詢失敗 account=%s: %r",
                          ref.account_id, e)
    row = follower_trade_quality(ref, hl, start, end, leader_fills=leader_fills,
                                 skipped_notional=None, skipped_ratio_comparable=False)
    latency_median_ms = None
    delay_s = row.get("median_delay_s")
    if delay_s is not None:
        latency_median_ms = int((delay_s * 1000).to_integral_value())
    if not row.get("quality_available", True):
        data_state = "error"
    elif hb.status == "missing":
        data_state = "warming"
    else:
        data_state = "ok"
    return {
        "latency_median_ms": latency_median_ms, "latency_p95_ms": None,
        "price_diff_bp": row.get("taker_slippage_bp_median"),
        "unsynced_positions": None, "scale_deviation_pct": None,
        "missed_signals_24h": None, "missed_reason": None, "last_recon_ts": None,
        "data_state": data_state, "since_ts": None,
    }


def _dashboard_risk_controls_enabled(hb: "HeartbeatRead",
                                     signed_prefs: dict | None) -> bool:
    """風控總開關的最佳已知值——**恆回布林，不留 null**（M3 round3 Task 3 D5/R2·C：
    前端要用它決定「未啟用 · 前往設定 →」這個確定的態一，不能是「不知道」）。

    `signed_prefs`＝呼叫端已解出的「這個帳號目前已簽章的偏好」原始 dict
    （`_my_signed_risk_record(account_id)` 的 `["prefs"]`，找不到記錄時傳
    `None`）——本函式不做 IO，維持與其餘 `_dashboard_*` helper 同一種模組層級
    純函式形狀（`_my_signed_risk_record` 因需要 `cfg.risk_settings_path` 而留在
    `create_app` 閉包內，見該函式檔頭的兩層窄化論證）。

    優先權（由新到舊、由確定到不確定）：
    1. 心跳新鮮且來源可信（`customer_signed`／`env_default`）＝引擎**目前實際
       套用**的值——與 `_dashboard_guards` 的 `guards.drawdown.enabled` 同一
       讀法、同源（工程原則 1）。
    2. 心跳讀不到可信值時，退到帳號**已簽章但引擎可能還沒套用**的偏好——仍是
       客戶自己的選擇，只是新鮮度差一截（沿 `/api/me/risk` 的 `prefs` 語意）。
    3. 兩者都沒有（從未簽過、也沒有心跳）→ 產品預設 `False`（新錢包預設不啟用
       任何風控，見 `filet.risk_prefs.default_prefs`／專案 CLAUDE.md 紅線 5）
       ——這不是「不知道」，是這個帳號目前的真實狀態：沒有任何風控在執法。
    """
    if hb.fresh:
        risk = (hb.data or {}).get("risk") or {}
        if risk.get("source") in ("customer_signed", "env_default"):
            enabled = risk.get("controls_enabled")
            if isinstance(enabled, bool):
                return enabled
    if signed_prefs is not None:
        try:
            return canonical_prefs(signed_prefs)["enabled"]
        except RiskPrefsError:
            pass
    return False


# ⭐⭐ R-A（2026-08-30 opus 審查 C2/C3，取代下方舊 Warning 1 註記的緩解手法）：
# 舊版即使有 per-account 快取，TTL 過期後單一 request 仍會「逐日」重呼
# `collect_follower_summary`——`period=all` 對兩年帳戶等於串行打 ~730 次
# `userFillsByTime`，直接打在與實盤引擎共用額度的 Hyperliquid 上游、且無法偵測
# 單頁 2000 筆上限截斷（>2000 筆帳戶的合計被靜默低估，逐日加總與期間合計還可能
# 不等，因為兩者各自獨立查詢各自可能截斷）。修法：整個期間**一次**用
# `hl.get_user_fills_paged`（`spark.publicapi.hl`）分頁抓好 fills，本地用純
# Python 依 UTC 日切片——期間合計與逐日 bar 現在是**同一份已抓資料**的不同切片，
# 結構上不可能兜不起來（同源同基準），且呼叫次數 ∝ 筆數/2000，不再 ∝ 天數。
# `_fee_daily_bars` 因此从「呼叫 HL 的函式」降級成「純 Python 聚合函式」——
# 見下方新簽名（吃 fills 清單，不吃 hl gateway）。
#
# ⭐ 舊 Warning 1（opus 審查 2026-08-29）背景保留：`daily_bars` 逐日重呼
# `collect_follower_summary`——一次 dashboard 請求觸發約「月內天數＋1」次
# `get_user_fills`（月中約 15 次、月底約 31 次）。per-account **in-process**
# 快取（TTL 300s）仍然保留（見下）：即使單一 request 已收斂成一次分頁抓取，
# 5 分鐘內的重複 dashboard 請求還是不必重算。快取鍵是 `ref.account_id`；
# TTL 判定用呼叫端傳入的 `now_s`（與整個 dashboard 端點共用同一個時鐘，可測試
# 注入，不另外硬綁 `time.time()`）。
# ⚠️ 多 worker 部署下每個 process 各自一份快取（in-process，非共享）——這裡刻意
# 不做跨 process 一致性：費用顯示不是安全關鍵路徑，worst case 是不同 worker 在
# TTL 內回應略舊的數字，不是資料損毀或資金風險。
_FEES_MONTH_CACHE_TTL_S = 300.0
_fees_month_cache: dict[str, tuple[float, dict]] = {}
# M3 round3 Task 2：`/api/me/fees?period=` 的 per-(account_id, period) 快取，
# 同一個 5min TTL（沿 `_FEES_MONTH_CACHE_TTL_S`，見 plan「沿用 /api/me/fills 的
# 認證與快取慣例，5min TTL」——注意這比 /api/me/fills 自己的 60s TTL 長，是 plan
# 明文指定的值，不是誤植）。
_fees_period_cache: dict[tuple[str, str], tuple[float, dict]] = {}
# ⭐ W4（2026-08-30 opus 審查）：256 上限＋近似 LRU 淘汰——沿
# `_trader_portfolio_cache`（app.py 上方 `_cached_trader_data`）同一款慣例，
# 防止這個模組層 dict 隨不同帳號數無界成長（同一份設計理由：展示用快取，
# 淘汰掉最舊一筆不影響正確性，只影響下一次是否要重新計算）。
_FEES_PERIOD_CACHE_MAX = 256
# period=all 逐日迴圈的起點取不到帳戶真實交易起點（perpAllTime 首點）時的安全上界
# ——避免從 1970 起跑產生過大的查詢視窗（工程原則：外部呼叫需有界）。這只影響
# 「查詢視窗多寬」，不影響任何費用/PnL 欄位的公式或來源。
_FEES_ALL_FALLBACK_DAYS = 400


def _effective_rate_bps(builder_fee: Decimal, routed_volume: Decimal) -> "Decimal | None":
    if routed_volume <= 0:
        return None
    return ((builder_fee / routed_volume) * 10000).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fetch_period_fills(ref: FollowerRef, hl, start: datetime, end: datetime, *,
                        end_exclusive: bool) -> tuple[list, bool]:
    """整個期間**一次**分頁抓 fills（R-A C2/C3 修法，取代逐日重複呼叫
    `collect_follower_summary`）。回 `(fills, truncated)`：`end_exclusive=True`
    時在抓到的資料上本地過濾 `f.time < end`（與 `collect_follower_summary` 的
    `end_exclusive` 同一個半開區間慣例，Task 2b）。`truncated=True` 時，回傳的
    `fills` 是這個期間視窗內成交的**下限值**（見 `HLGateway.get_user_fills_paged`
    docstring）——呼叫端（期間合計與逐日 bar）都吃同一份 `fills`，所以無論是否
    截斷，兩者永遠互相加總一致（同源同基準）。"""
    fills, truncated = hl.get_user_fills_paged(ref.user_address, start, end)
    if end_exclusive:
        fills = [f for f in fills if f.time < end]
    return fills, truncated


def _fee_daily_bars(ref: FollowerRef, fills: list, start: datetime, end: datetime) -> list[dict]:
    """本地依 UTC 日切片、聚合**已經抓好**的 fills（R-A C2/C3 修法：本函式不再
    自己打 HL，呼叫端負責用 `_fetch_period_fills` 一次抓好整個期間，見上方模組
    註解）。無成交日（`fill_count == 0`）不產生列（「—」列由前端補日曆）；
    `builder_fee == 0` 但有成交的日子照實列出——$0.00 與「當日無成交」語意分開
    （R2·B）。

    `[start, end)`：`start` 對齊到當日 00:00 UTC；`end` 可以是任意時刻（本日尚未
    走完的部分日照算），迴圈用 `day < end`（而非 `day.date() <= end.date()`）
    確保 `end` 剛好落在某天 00:00（例如「上個月」的排他上界＝本月 1 號 00:00）
    時不會多算出下個月第一天。

    ⭐ M3 round3 Task 2b：每個 `[day, day_end)` 都用半開區間過濾（`day <= f.time
    < day_end`）——恰好落在 `day_end`（例如 UTC 午夜整）的成交只歸下一天，不會
    同時被本日與次日兩個切片各記一次。"""
    bars: list[dict] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        day_end = min(day + timedelta(days=1), end)
        day_fills = [f for f in fills if day <= f.time < day_end]
        day_summary = summarize_fills(ref, day_fills)
        if day_summary.fills > 0:
            bars.append({
                "date": day.date().isoformat(),
                "fill_count": day_summary.fills,
                "routed_volume": day_summary.notional,
                "builder_fee": day_summary.builder_fee,
                "effective_rate_bps": _effective_rate_bps(
                    day_summary.builder_fee, day_summary.notional),
            })
        day += timedelta(days=1)
    return bars


def _dashboard_fees_month(ref: FollowerRef, hl, now_s: float, *,
                          cache: dict[str, tuple[float, dict]] | None = None) -> dict:
    """本月路由量與費用：與 `_dashboard_fees_period` 共用 `_fetch_period_fills`／
    `summarize_fills`（同源同函式，不各自複製公式；`/api/ops/revenue`／
    `customer_pnl` 走各自的 `collect_follower_summary` 呼叫，本函式不影響
    那條既有路徑）。`daily_bars`：逐日聚合（`_fee_daily_bars`，見其 docstring）
    ——本函式與 `daily_bars` 現在吃**同一次抓到的 fills**（R-A C2/C3 修法），
    結構上不會兜不起來。

    `cache` 不給 → 用模組層共用字典（正式路徑）；測試可傳一份乾淨字典避免
    跨測試汙染（見上方模組註解的快取語意）。**只快取成功結果**——失敗（拋例外）
    不寫入快取，下一次請求會照常重試，不會把一次暫時性故障釘住 300 秒。
    """
    cache = _fees_month_cache if cache is None else cache
    cached = cache.get(ref.account_id)
    if cached is not None and (now_s - cached[0]) < _FEES_MONTH_CACHE_TTL_S:
        return cached[1]
    now_dt = datetime.fromtimestamp(now_s, timezone.utc)
    month_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    # end_exclusive=True：與 `_fee_daily_bars` 同一個半開區間慣例，月總量才會
    # 恰好等於逐日 bar 加總，不因午夜邊界重複計費而兜不起來（Task 2b）。
    fills, truncated = _fetch_period_fills(ref, hl, month_start, now_dt,
                                           end_exclusive=True)
    summary = summarize_fills(ref, fills)
    fill_count, routed_volume, builder_fees = (
        summary.fills, summary.notional, summary.builder_fee)
    avg_fee = ((builder_fees / fill_count).quantize(Decimal("0.01"),
                                                     rounding=ROUND_HALF_UP)
              if fill_count > 0 else None)
    effective_rate_bps = _effective_rate_bps(builder_fees, routed_volume)
    daily_bars = _fee_daily_bars(ref, fills, month_start, now_dt)
    result = {
        "routed_volume": routed_volume, "builder_fees": builder_fees,
        "fill_count": fill_count, "avg_fee": avg_fee,
        "effective_rate_bps": effective_rate_bps, "daily_bars": daily_bars,
        # R-A C2/C3：達分頁上限仍滿頁 → True，本結果所有欄位皆是已抓到部分的
        # 下限值（前端本輪先回欄位，note 顯示留待前端 task）。
        "truncated": truncated,
    }
    cache[ref.account_id] = (now_s, result)
    return result


def _month_bounds(now_dt: datetime, *, months_back: int) -> tuple[datetime, datetime]:
    """回傳 `[start, end)`：`months_back=0` 是本月（`start`＝本月 1 號 00:00，
    `end`＝`now_dt`）；`months_back=1` 是上個月整月（`start`＝上月 1 號 00:00，
    `end`＝本月 1 號 00:00，排他上界）。本函式只支援 0／1（本模組唯二用法），
    不做通用 N 個月前的迴圈——留白比一段沒測過的通用迴圈誠實。"""
    this_month_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    if months_back == 0:
        return this_month_start, now_dt
    if months_back == 1:
        year, month = this_month_start.year, this_month_start.month - 1
        if month == 0:
            month, year = 12, year - 1
        return this_month_start.replace(year=year, month=month), this_month_start
    raise ValueError(f"_month_bounds 只支援 months_back 0 或 1: {months_back!r}")


def _fees_all_time_start(ref: FollowerRef, hl, now_dt: datetime) -> datetime:
    """`period=all` 的起點：`perpAllTime` accountValueHistory 首點時間戳（帳戶
    實際交易起點，同源自 `hl.portfolio()`——與 explore/策略頁沿用同一個 perp
    all-time 窗，不另拼新視窗定義）。取不到（`portfolio()` 失敗、或無
    `perpAllTime` 視窗資料）→ 退回 `_FEES_ALL_FALLBACK_DAYS` 安全上界，只是
    「迴圈跑多遠」的保護，不影響任何費用公式。"""
    try:
        rows = hl.portfolio(ref.user_address)
    except Exception:  # noqa: BLE001 — 純粹用來界定迴圈起點，查不到就退回安全上界
        rows = None
    if rows is not None:
        window = extract_window(rows, "perpAllTime")
        if window is not None:
            av, _pnl = window
            if av:
                return datetime.fromtimestamp(av[0][0] / 1000, timezone.utc)
    return now_dt - timedelta(days=_FEES_ALL_FALLBACK_DAYS)


_FEES_PERIODS = ("this_month", "last_month", "all")


def _pnl_share_pct(builder_fees: Decimal, realized_pnl: "Decimal | None",
                   total_fee: Decimal) -> "Decimal | None":
    """佔已實現淨 PnL 的百分比（M3 round3 Task 2b，主線程裁決 D12）。分母＝
    期間已實現淨 PnL＝Σ closedPnl − Σ fee（同一批 fills、同一次
    `summarize_fills` 呼叫算出來的兩個欄位，同窗同源；未實現 PnL明確不進分母）。
    `realized_pnl is None`（這批 fills 裡有任一筆缺 closedPnl 資料——W5
    all-or-nothing，見 `summarize_fills` docstring）或分母 ≤0 → `None`（前端
    顯示「—」）。

    數值錨例（plan Task 2b）：builder_fees=2.00、closedPnl 合計=10.00、
    fee 合計=3.50 → 淨 6.50 → 2.00/6.50*100 ≈ 30.77%。

    ⚠️ TODO（W5，2026-08-30 opus 審查，上線後補做一次）：`closedPnl 不含手續費、
    fee 含 builder fee` 是**未經真實 payload 對帳驗證**的假設——現有 fixture
    （`tests/fixtures/hl_user_fills_sample.json`）只證明兩個欄位個別存在，
    沒有證明 `closedPnl` 的計算基礎排除了 `fee`（HL 官方文件對此未逐欄位交代）。
    若上線後對帳發現 `closedPnl` 其實已經淨過手續費，本函式的 `net = realized_pnl
    - total_fee` 會把手續費算兩次（分母虛低 ⇒ `pnl_share_pct` 虛高），須回來修
    這一行，不是重新設計整條管線。"""
    if realized_pnl is None:
        return None
    net = realized_pnl - total_fee
    if net <= 0:
        return None
    return ((builder_fees / net) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dashboard_fees_period(ref: FollowerRef, hl, now_s: float, period: str, *,
                           cache: dict[tuple[str, str], tuple[float, dict]] | None = None
                           ) -> dict:
    """`/api/me/fees?period=` 的計算層。`period` 決定 `[start, end)`：
    `this_month`／`last_month` 為日曆月（`_month_bounds`）；`all` 為帳戶實際
    交易起點至今（`_fees_all_time_start`）。彙總與逐日明細**同一份已抓資料**
    （`_fetch_period_fills` 一次抓、`summarize_fills`／`_fee_daily_bars` 各自
    切片計算，不另拼第二來源，R-A C2/C3 修法）；`end_exclusive=True`
    （Task 2b）與 `_fee_daily_bars` 同一個半開區間慣例，期間總量才會恰好等於
    逐日 bar 加總（截斷時兩者也仍然相等，因為都是同一份已截斷資料的切片）。

    `pnl_share_pct`：見 `_pnl_share_pct`（Task 2b 主線程裁決 D12，已改用同一條
    fills 流的已實現淨 PnL 當分母，不再永遠 `None`）。"""
    cache = _fees_period_cache if cache is None else cache
    key = (ref.account_id, period)
    cached = cache.get(key)
    if cached is not None and (now_s - cached[0]) < _FEES_MONTH_CACHE_TTL_S:
        return cached[1]
    now_dt = datetime.fromtimestamp(now_s, timezone.utc)
    if period == "this_month":
        start, end = _month_bounds(now_dt, months_back=0)
    elif period == "last_month":
        start, end = _month_bounds(now_dt, months_back=1)
    elif period == "all":
        start, end = _fees_all_time_start(ref, hl, now_dt), now_dt
    else:
        raise ValueError(f"period 僅支援 {_FEES_PERIODS}: {period!r}")

    fills, truncated = _fetch_period_fills(ref, hl, start, end, end_exclusive=True)
    summary = summarize_fills(ref, fills)
    fill_count, routed_volume, builder_fees = (
        summary.fills, summary.notional, summary.builder_fee)
    pnl_share_pct = _pnl_share_pct(builder_fees, summary.realized_pnl, summary.total_fee)
    daily = _fee_daily_bars(ref, fills, start, end)
    result = {
        "summary": {
            "builder_fees": builder_fees, "routed_volume": routed_volume,
            "fill_count": fill_count, "pnl_share_pct": pnl_share_pct,
            # R-A C2/C3：達分頁上限仍滿頁 → True，本結果所有欄位皆是已抓到
            # 部分的下限值（前端顯示「僅涵蓋最近 N 筆」note 留待前端 task）。
            "truncated": truncated,
        },
        "daily": daily,
    }
    # ⭐ W4：256 上限＋近似 LRU 淘汰（沿 `_cached_trader_data` 同款慣例，見上方
    # `_FEES_PERIOD_CACHE_MAX` 註解）——只在真的要新增一筆、且已達上限時淘汰，
    # 命中既有 key（同帳號同 period 的更新）不觸發淘汰。
    if key not in cache and len(cache) >= _FEES_PERIOD_CACHE_MAX:
        oldest = min(cache, key=lambda k: cache[k][0])
        del cache[oldest]
    cache[key] = (now_s, result)
    return result


def _dashboard_pnl_and_return(ref: FollowerRef, hl, positions: list[dict] | None
                              ) -> tuple[dict, "Decimal | None"]:
    """淨 PnL 區塊 ＋ 30D 報酬（equity 塊用）。**同一個 `portfolio()` 回應**餵出
    兩邊，不重複打點（Task 13 規格：30D 報酬與 PnL series 走同一條 leader_perf
    管線、餵 follower 位址）。

    ⭐ `net`／`fees_paid`／`fee_share_of_pnl_pct` 三者同窗口（perpMonth 的
    `[first_ts_ms, last_ts_ms]`）：dollar PnL 出自 HL `pnlHistory`（`cum_pnl`），
    fee 出自**同窗口**的 `collect_follower_summary`（與 billing／ops 同一個函式），
    `net = cum_pnl − fees_paid`。

    ⭐⭐ `realized` 恆為 `None`（2026-08-29 opus 審查 Warning 5，工程原則 1）：
    過去算成 `cum_pnl − unrealized`，但 `cum_pnl` 是 30 天窗（`perpMonth`）內的
    累積值，`unrealized` 卻是**目前所有持倉自各自開倉以來**的未實現損益快照
    ——兩者基準不同源（一個有窗口起點、一個沒有），對長期持倉可以錯到反號
    （例如一筆 40 天前開倉、近期才轉盈的部位：30 天窗的 cum_pnl 可能是負值，
    減去它「開倉以來」的正 unrealized，會算出一個更負的假「已實現虧損」）。
    找不到同窗口的已實現數字就是找不到，不拼湊一個異基準的近似值冒充——前端
    對 `null` 顯示「—」。`closed_positions`：無既有來源，同理維持 `None`。
    """
    rows = hl.portfolio(ref.user_address)
    series = None
    window = extract_window(rows, "perpMonth")
    if window is not None:
        av, _pnl = window
        series = [[ts, v] for ts, v in av]

    perf = compute_window_performance(rows, "perpMonth")
    net = realized = fees_paid = fee_share = win_rate = mdd = None
    ret_30d = None
    unrealized = (sum((p["upnl"] for p in positions), Decimal("0"))
                 if positions is not None else None)
    if perf.get("status") == "ok":
        cum_pnl = perf["cum_pnl"]
        ret_30d = (perf["twr"] * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        mdd = (-(perf["max_drawdown"] * 100)).quantize(Decimal("0.01"),
                                                        rounding=ROUND_HALF_UP)
        if "win_rate" in perf:
            win_rate = (perf["win_rate"] * 100).quantize(Decimal("0.01"),
                                                          rounding=ROUND_HALF_UP)
        start = datetime.fromtimestamp(perf["first_ts_ms"] / 1000, timezone.utc)
        end = datetime.fromtimestamp(perf["last_ts_ms"] / 1000, timezone.utc)
        summary = collect_follower_summary(ref, hl, start, end)
        if summary.error is None:
            fees_paid = summary.builder_fee
            net = cum_pnl - fees_paid
            denom = abs(net + fees_paid)
            if denom != 0:
                fee_share = ((fees_paid / denom) * 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)

    pnl_block = {
        "net": net, "realized": realized, "unrealized": unrealized,
        "fees_paid": fees_paid, "fee_share_of_pnl_pct": fee_share,
        "win_rate_pct": win_rate, "closed_positions": None,
        "max_drawdown_pct": mdd, "series": series,
    }
    return pnl_block, ret_30d


# ---------- /api/me/authorizations（M3 round2 Task 7）純函式層 ----------
# ⭐ action.type 的確切字串是 2026-08-29 curl 實測（見 hl.py user_details 檔頭
# 與 tests/fixtures/hl_explorer_user_details_sample.json），不是憑印象/文件猜的
# camelCase（工程原則：欄位名是假設不是事實）。

_AUTHORIZATION_ACTION_TYPES = frozenset({"approveAgent", "approveBuilderFee"})


def _authorization_detail(action: dict) -> dict:
    """單一動作 → 結構化欄位（[W2] 2026-08-29 opus 審查修正：後端曾直接組出
    中文摘要字串——語系一律出自 `web/src/lib/copy.ts`，後端寫死中文等於繞過
    那條紅線且無法雙語化。改回只給機器可讀的原始欄位，組字留給前端。未知
    欄位缺漏一律降級為 `None`，不猜造內容。"""
    action_type = action.get("type")
    if action_type == "approveAgent":
        return {"agent_address": action.get("agentAddress"), "builder": None,
                "max_fee_rate": None}
    if action_type == "approveBuilderFee":
        return {"agent_address": None, "builder": action.get("builder"),
                "max_fee_rate": action.get("maxFeeRate")}
    return {"agent_address": None, "builder": None, "max_fee_rate": None}


def filter_authorizations(txs: list, limit: int) -> list[dict]:
    """explorer `userDetails` 原始 txs → 只留 approveAgent／approveBuilderFee，
    按時間降冪排序後裁切前 `limit` 筆。形狀不符的條目直接跳過（不得讓一筆壞
    資料炸掉整份清單）。"""
    out = []
    for tx in txs or []:
        if not isinstance(tx, dict):
            continue
        action = tx.get("action")
        if not isinstance(action, dict) or action.get("type") not in _AUTHORIZATION_ACTION_TYPES:
            continue
        try:
            time_ms = int(tx["time"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "time": time_ms,
            "action_type": action["type"],
            **_authorization_detail(action),
            "hash": tx.get("hash", ""),
        })
    out.sort(key=lambda r: r["time"], reverse=True)
    return out[:limit]


def create_app(cfg: ApiConfig, store: ApiStore, keysvc, hl, now_fn=time.time,
               billing=None, notifier=None, leaderboard_get_fn=None) -> FastAPI:
    app = FastAPI(title="filet public api",
                  docs_url=None, redoc_url=None, openapi_url=None)

    # 營運告警通道（vault 准入 advisory FAIL 用；CLAUDE.md：通知一律走 Notifier 注入）。
    # 未注入 → 由 cfg 建預設：TG 兩鍵齊 → TelegramNotifier；否則 NullNotifier
    # （log-only——告警點本就同步 logger.warning，缺 TG 只是少一條外送通道，不得炸）。
    if notifier is None:
        notifier = (TelegramNotifier(token=cfg.tg_bot_token, chat_id=cfg.tg_chat_id)
                    if cfg.tg_bot_token and cfg.tg_chat_id else NullNotifier())
    app.state.notifier = notifier   # 唯讀 introspection seam（沿 probe_ratelimit_hits）

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
        state = hl.clearinghouse_state(addr)
        account_value, position_count = _chain_activity(state)
        exists = account_value > 0 or position_count > 0
        # (4) vault 自動偵測＋advisory 檢查（2026-07-31 owner 裁決：vault 也要無痛
        #     低門檻上線）。transient 例外照 _chain_activity 同語意上拋（502）：
        #     「讀不到鏈」≠「不是 vault」，靜默當 standard 會讓 vault 保護整條缺席。
        kind, vault_checks = _detect_vault(addr, state)
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
                "accepting_new": accepting_new,
                # 跨 wave 契約（2026-07-31）：kind＝"standard"｜"vault"；
                # vault_checks 僅 kind=="vault" 時非 null，failures 只列 FAIL 項。
                "kind": kind, "vault_checks": vault_checks}

    def _detect_vault(addr: str, state) -> tuple[str, dict | None]:
        """vaultDetails 驗身＋advisory 六項檢查 → (kind, vault_checks)。

        ⭐ **advisory，不是閘門**（2026-07-31 owner 裁決）：任一 FAIL 都**不擋**
        用戶——preview 顯示警語、工程判斷交給用戶；同時 notifier.critical＋
        logger.warning 讓營運人工介入（告警不影響回應）。檢查與資料抓取一律
        import vault_preflight 的單一實作（見檔頭 import 的同源理由）。

        transient 例外（vault_details／portfolio／ledger 讀取失敗）原樣上拋 → 502
        （工程原則 2：讀不到 ≠ 不是 vault）；檢查層自身的形狀炸裂（KeyError 等
        semantic）→ 記一筆 check-error FAIL＋告警，仍不擋用戶——檢查掛掉不等於
        vault 有問題，但一定要有人看（工程原則 3：不靜默）。
        """
        vd = hl.vault_details(addr)
        if not isinstance(vd, dict) or not vd.get("name"):
            return "standard", None    # 非 vault（真實 API 回 JSON null → None）
        # check-error 的 detail 淨化：只含例外型別名、**不含** str(e)——底層例外
        # 訊息常帶內部 URL（httpx 的 "for url 'http://…'"），不得回顯給客戶端。
        def _check_error_failures(e: Exception) -> list[dict]:
            return [{"name": "check-error",
                     "detail": f"檢查執行失敗（{type(e).__name__}）——"
                               f"資料形狀不符 preflight 假設，需人工重跑 preflight"}]

        try:
            data = fetch_preflight_data(hl, addr, clearinghouse_state=state,
                                        vault_details=vd)
            results = run_checks(data)
            failures = [{"name": r.name, "detail": r.detail}
                        for r in results if not r.passed]
        except httpx.HTTPStatusError as e:
            # HL 5xx＝上游暫時性故障（resilience 邊界按訊息分類重試、耗盡後把
            # HTTPStatusError 原樣上拋到這裡）——是 transient，不是「vault 檢查
            # FAIL」；誤入 check-error 會產生假 FAIL＋假 critical（工程原則 2）。
            if e.response.status_code >= 500:
                raise ConnectionError(f"HL {e.response.status_code}") from e
            failures = _check_error_failures(e)  # 4xx＝semantic → advisory check-error
        except (ConnectionError, TimeoutError):
            raise                      # transient → 上層轉 502（同 _chain_activity）
        except Exception as e:  # noqa: BLE001 — semantic 形狀炸裂不得擋用戶（advisory）
            failures = _check_error_failures(e)
        if failures:
            fail_txt = "；".join(f"{f['name']}: {f['detail']}" for f in failures)
            logger.warning("自訂 vault leader 准入檢查 FAIL leader=%s failures=%s",
                           addr, fail_txt)
            notifier.critical(
                "vault_admission",
                f"自訂 vault leader 准入檢查 FAIL：{addr}｜{fail_txt}｜"
                f"advisory 模式未擋下——用戶可能已繼續跟單，請人工核對此 vault",
                dedup_key=f"vault_admission_fail:{addr}")
        return "vault", {"passed": not failures, "failures": failures}

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

    # ---------- /api/public/strategies*（策略平台，2026-08-28 改版 Task 5）----------
    # ⭐ 無需登入、無 cookie 副作用（不變量 0.3.5）：首頁／策略頁在客戶連錢包之前
    # 就要能渲染。上游（HL portfolio）走 60s in-process cache——這是公開端點，
    # 流量放大到交易所的風險與 /api/leaders/preview 同型，但這裡連 session 探測面
    # rate limit 都沒有（真的不需要登入），快取是唯一的節流手段。
    _strategy_portfolio_cache: dict[str, tuple[float, list]] = {}
    _strategy_portfolio_lock = threading.Lock()
    STRATEGY_PORTFOLIO_CACHE_TTL_S = 60.0

    def _cached_strategy_portfolio_with_ts(address: str) -> tuple[list, float] | None:
        """`hl.portfolio()` 的 60s 快取，回傳 `(rows, fetched_at)`。上游任何失敗
        （transient 或 schema 漂移）→ `None`，**不快取失敗**（下次請求照樣重試，
        不必等 TTL 過期）——呼叫端把它降級成該策略的 perf 缺席，不得讓一個 leader
        的上游故障拖垮整份清單（沿 /api/leaders 目錄「個別 leader 統計為 null」
        的既有降級精神）。

        ⭐ M3 round3 Task 3（D5 數字一致性）：`fetched_at`＝這批 `rows`（進而算出
        的 perf）實際落地這份快取的時間戳，是列表卡與詳情頁回應裡 `as_of` 的
        **唯一來源**——兩個端點在同一個 60s 快取窗內命中同一格快取時，回傳的
        `fetched_at` 是同一個數字，值也因此保證同源同基準（工程原則 1）；快取過
        期後兩者各自重新觸網才會各自前進，`as_of` 會誠實反映這一點，不假裝兩次
        不同時間點的抓取是「同一份快照」。
        """
        now = now_fn()
        with _strategy_portfolio_lock:
            cached = _strategy_portfolio_cache.get(address)
        if cached is not None and now - cached[0] < STRATEGY_PORTFOLIO_CACHE_TTL_S:
            return cached[1], cached[0]
        try:
            rows = hl.portfolio(address)
        except Exception as e:  # noqa: BLE001 — 公開端點：上游任何失敗都不得 500
            logger.error("策略績效上游查詢失敗 leader=%s: %s", address, e)
            return None
        with _strategy_portfolio_lock:
            _strategy_portfolio_cache[address] = (now, rows)
        return rows, now

    def _cached_strategy_portfolio(address: str) -> list | None:
        """`_cached_strategy_portfolio_with_ts` 的薄包裝，只要 rows 不要時間戳
        （既有呼叫點——`initial_deposit_usd` 推導——不需要 as_of）。"""
        result = _cached_strategy_portfolio_with_ts(address)
        return result[0] if result is not None else None

    def _strategy_perf_with_as_of(address: str) -> tuple[dict | None, int | None]:
        """位址 → `(perf, as_of)`。`perf`＝`perpAllTime` 窗的績效（策略卡固定只用
        這一窗：首頁/詳情頁要的是「這個策略整體值不值得跟」，不是逐窗比較）；
        `as_of`＝算出這份 perf 所用 `rows` 的快取時間戳（epoch 秒）。上游或計算
        失敗 → `(None, None)`，`strategies.build_strategy_view` 對 None 的處理＝
        該策略全部指標 insufficient。
        """
        result = _cached_strategy_portfolio_with_ts(address)
        if result is None:
            return None, None
        rows, fetched_at = result
        try:
            perf = compute_window_performance(rows, "perpAllTime")
        except Exception as e:  # noqa: BLE001 — schema 漂移不得炸掉整份清單
            logger.error("策略績效計算失敗 leader=%s: %s", address, e)
            return None, None
        return perf, int(fetched_at)

    def _strategy_perf_for(address: str) -> dict | None:
        """`_strategy_perf_with_as_of` 的薄包裝，只要 perf 不要 as_of（既有呼叫點
        `/api/public/stats` 的 `perf_for=_strategy_perf_for` 不需要時間戳）。"""
        return _strategy_perf_with_as_of(address)[0]

    def _strategy_follower_counts() -> dict[str, int] | None:
        """位址（小寫）→ 目前 active 跟隨的 follower 數。

        「active」＝ followers manifest 條目**明確指定**該位址為 `leader_address`
        （不含落回引擎預設 `COPY_LEADER_ADDRESS` 的隱含跟隨——manifest 本身看不出
        那份對應關係，見 `filet/followers.py` 的 `leader_address` 語意；`me_leader`
        端點的 `engine_default` 狀態就是這個缺口的既有先例）。

        manifest 讀不到／JSON 壞掉 → `None`（**全域**降級：呼叫端把每個策略的
        `follower_count` 一起設為 null，不得因為一個聚合失敗就讓整份策略清單
        500——不變量 4 只禁止外流 follower 個資，沒禁止在聚合失敗時誠實說「不知道」）。
        """
        try:
            refs, _errors = load_followers_tolerant(cfg.followers_path)
        except (OSError, ValueError) as e:
            logger.error("follower 聚合失敗（策略 follower_count 全數降級為 null）"
                         " %s: %s", cfg.followers_path, e)
            return None
        counts: dict[str, int] = {}
        for r in refs:
            if r.leader_address:
                counts[r.leader_address] = counts.get(r.leader_address, 0) + 1
        return counts

    def _public_strategy_entries() -> list[LeaderRef]:
        """精選白名單中 `enabled=True` 的條目（`enabled=False` 的安全撤銷條目
        連 slug／address 都不得外流，沿 `_leader_public` 的既有理由）。"""
        return [r for r in _load_leaders_or_503() if r.enabled]

    @app.get("/api/public/strategies")
    def public_strategies_list():
        """策略平台首頁／列表頁餵資料的公開端點。無需登入。

        `listable=False` 的條目（`accepting_new=False`——2026-08-29 裁決移除 60 天
        涵蓋天數閘門，見 `filet.strategies` 檔頭）**仍然出現**在清單裡——前端據
        `listable` 畫「暫不開放新跟單」的 disabled 態，不是用這個端點過濾（見
        plan §0.2）。只有 `enabled=False` 的安全撤銷條目才整筆不出現。
        """
        counts = _strategy_follower_counts()
        strategies = []
        for entry in _public_strategy_entries():
            # ⭐ M3 round3 Task 3（D5 數字一致性）：列表卡與 `/{slug}` 詳情頁**同一支
            # `build_strategy_view`／`build_metrics`計算**餵同一次 perf 快照——
            # 兩端點唯一的差異是「這次請求命中快取的哪一格」，不是計算路徑本身。
            # `as_of` 把這份 perf 快照的實際時間戳（快取寫入時刻）誠實回吐，
            # 兩端點在同一個 60s 快取窗內同時打進來時 `as_of` 會相等（見
            # `_cached_strategy_portfolio_with_ts` 檔頭）。
            perf, as_of = _strategy_perf_with_as_of(entry.address)
            view = build_strategy_view(entry, perf)
            # ⭐ 缺鍵＝「這個 leader 目前沒有任何 active follower」，不是「不知道」
            # ——聚合成功時一律回真正的整數（`.get(addr, 0)`），只有 counts 整體
            # 為 None（資料源不可用）才回 null。用 `.get(addr)` 少寫預設值會把
            # 兩種完全不同的處境（0 個／不知道）疊成同一個 None（工程原則 1）。
            view["follower_count"] = (counts.get(entry.address, 0)
                                      if counts is not None else None)
            view["as_of"] = as_of
            strategies.append(view)
        return {"strategies": strategies, "updated_at": int(now_fn())}

    @app.get("/api/public/strategies/{slug}")
    def public_strategy_detail(slug: str):
        """策略詳情頁。`slug` 比對 `entry.slug`，缺 slug 的條目回退比對完整位址
        （沿 `strategies.build_strategy_view` 的同一個回退規則）。

        404：slug 不存在，或條目 `enabled=False`（安全撤銷——不得讓客戶用舊連結
        繞過「連 address 都不外流」的既有政策）。
        """
        entry = next((r for r in _public_strategy_entries()
                     if (r.slug or r.address) == slug), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="策略不存在")
        # ⭐ M3 round3 Task 3（D5 數字一致性）：與列表端點同一支
        # `_strategy_perf_with_as_of`——同一個 perf 快照、同一個 as_of 來源。
        perf, as_of = _strategy_perf_with_as_of(entry.address)
        view = build_strategy_view(entry, perf)
        counts = _strategy_follower_counts()
        view["follower_count"] = (counts.get(entry.address, 0)
                                  if counts is not None else None)
        view["as_of"] = as_of
        # ⭐ M3 round3 Task 3：`sample_days`／`sample_threshold`／`cagr_pct`——
        # `sample_days` 直接沿用 `view["live_days"]`（`build_strategy_view` 已用
        # `int(covered_days)` 算過一次，同一個值、同一個來源，不重算第二次以免
        # 兩處日後各自漂移，工程原則 1）。`sample_days < sample_threshold`（60 天，
        # `strategies.CAGR_SAMPLE_THRESHOLD_DAYS`）時**整個不放 `cagr_pct` 鍵**
        # ——結構性防呆：前端不需要另外判斷門檻，鍵不存在就是不存在。
        view["sample_days"] = view["live_days"]
        view["sample_threshold"] = CAGR_SAMPLE_THRESHOLD_DAYS
        if view["live_days"] >= CAGR_SAMPLE_THRESHOLD_DAYS:
            cagr_pct = build_cagr_pct(perf)
            if cagr_pct is not None:
                view["cagr_pct"] = cagr_pct
        view["equity_index"] = build_equity_index(perf)
        initial_deposit_usd = None
        rows = _cached_strategy_portfolio(entry.address)
        if rows is not None:
            window = extract_window(rows, "perpAllTime")
            if window is not None:
                av, _pnl = window
                if av:
                    initial_deposit_usd = av[0][1]
        view["methodology"] = build_methodology(
            perf, initial_deposit_usd=initial_deposit_usd, updated_at=int(now_fn()))
        return view

    # ---------- /api/public/stats、/api/public/status（策略平台 Task 6）----------
    # ⭐ 兩端點共用同一種 60s in-process cache 機制（`public_stats.TTLCache`），
    # 各自一個獨立實例——一端資料源掛掉不得拖累另一端的快取新鮮度。
    _public_stats_cache = public_stats.TTLCache(now_fn=now_fn)
    _public_status_cache = public_stats.TTLCache(now_fn=now_fn)

    @app.get("/api/public/stats")
    def public_stats_endpoint():
        """首頁證據列用的公開統計。無需登入。**任一子項取不到 → 該欄 null，
        端點仍 200**（不變量：狀態/統計端點本身要比被監控對象可靠）。

        ⭐ 這裡再包一層防禦：即使 `public_stats.build_stats_payload` 本身已對
        每個子來源 try/except，路由層仍不放心讓任何未預期例外冒泡成 500——
        公開端點的可靠度是它存在的唯一理由。
        """
        def _compute():
            return public_stats.build_stats_payload(
                accrued_history_path=cfg.accrued_history_path,
                entries=_public_strategy_entries(),
                perf_for=_strategy_perf_for,
                now_fn=now_fn)
        try:
            return _public_stats_cache.get(_compute)
        except Exception as e:  # noqa: BLE001 — 公開端點：絕不 500
            logger.error("/api/public/stats 計算失敗（全欄降級為 null）: %r", e)
            return {"routed_volume_usd_total": None,
                    "builder_fee_bps": public_stats.BUILDER_FEE_BPS,
                    "live_days": None, "updated_at": int(now_fn())}

    @app.get("/api/public/status")
    def public_status_endpoint():
        """footer／`/status` 頁用的系統狀態燈。無需登入。`engine` 元件由 heartbeat
        檔案 mtime 新鮮度判定，**不揭露 follower 數與身分**（不變量 4：只讀檔名／
        mtime，不解析心跳內容）。"""
        def _compute():
            return public_stats.build_status_payload(
                heartbeat_dir=heartbeat_dir_for(cfg.exchange_dir), now_fn=now_fn)
        try:
            return _public_status_cache.get(_compute)
        except Exception as e:  # noqa: BLE001 — 公開端點：絕不 500
            logger.error("/api/public/status 計算失敗（降級為 unknown）: %r", e)
            return {"status": "unknown",
                    "components": [{"name": "api", "status": "ok"},
                                   {"name": "engine", "status": "unknown"}],
                    "updated_at": int(now_fn())}

    # ---------- /api/public/leaderboard（M3 round2 Task 5）----------
    # ⭐ 上游 stats-data 全量 leaderboard 是 36MB JSON——絕不可讓瀏覽器直連。
    # 本端點是唯一出口：10 分鐘 in-process 快取（`hl_leaderboard.LeaderboardCache`，
    # fail-open 到舊值），依 window 排序後只回傳裁切列。無需登入，無 cookie 副作用
    # （與 /api/public/strategies* 同一類公開展示端點）。
    _leaderboard_cache = hl_leaderboard.LeaderboardCache(
        now_fn=now_fn, get_fn=leaderboard_get_fn)

    @app.get("/api/public/leaderboard")
    def public_leaderboard(window: str = "month", limit: int = 100):
        """Hyperliquid 主網公開交易排行榜（展示用，非本站策略/客戶資料）。
        壞 `window`／`limit` → 422；快取從未成功抓過任何資料（首次抓取即失敗，
        無舊值可回退）→ 503；其餘情況一律回 200（沿用 fail-open 到 stale 資料
        的既有精神——見 `hl_leaderboard` 檔頭）。"""
        if window not in hl_leaderboard.WINDOWS:
            raise HTTPException(
                status_code=422,
                detail=f"window 須為 {'/'.join(hl_leaderboard.WINDOWS)} 之一")
        if not (1 <= limit <= 100):
            raise HTTPException(status_code=422, detail="limit 須介於 1 到 100")
        # ⭐ [C2] 走快取物件自己的 `top_rows`（記憶化排序，見 hl_leaderboard 檔頭），
        # 不再自己 `.get()` 完 payload 後每個請求重排一次全量列表。
        rows = _leaderboard_cache.top_rows(window, limit)
        if rows is None:
            raise HTTPException(status_code=503, detail="排行榜資料暫時不可用，請稍後重試")
        # ⭐ [8b-4] `updated_at` 是資料實際抓取完成的時間戳（`LeaderboardCache.
        # cached_at`），不是請求當下的 `now_fn()`——後者會讓客戶端誤以為資料
        # 剛更新，即便實際上是 fail-open 續用的十分鐘前舊值。走到這裡代表
        # `top_rows` 已成功取得 payload，`cached_at` 理論上不會是 None；
        # `or now_fn()` 只是防禦性兜底，不是預期路徑。
        updated_at = _leaderboard_cache.cached_at
        return {"window": window, "updated_at": int(updated_at or now_fn()), "rows": rows}

    # ---------- /api/public/explore（M3 round3 Task 1）----------
    # ⭐ 可跟單對象探索榜（R2·A）。候選池來源沿用**同一個** `_leaderboard_cache`
    # 實例（D1：不重複下載 36MB stats-data payload）；排除清單＝精選白名單
    # （D8，Filet 自營 leader）。詳見 `hl_explore.py` 檔頭。
    def _explore_excluded_addresses() -> set[str]:
        """Filet 自營 leader 地址集合（D8）。白名單載入失敗 → 空集合＋記錄
        （fail-open：這是「要不要把自己的策略也列進探索榜」的展示層判斷，不是
        資金安全判斷，不比照 `_load_leaders_or_503` 503 掉整個公開端點）。"""
        try:
            return {r.address for r in load_leaders(cfg.leaders_path)}
        except (OSError, ValueError) as e:
            logger.error("explore 排除清單（精選白名單）載入失敗，本輪視為空清單: %s", e)
            return set()

    _explore_index = hl_explore.ExploreIndex(
        leaderboard_source_fn=_leaderboard_cache.get,
        hl=hl,
        excluded_fn=_explore_excluded_addresses,
        cfg=hl_explore.ExploreConfig.from_env(),
        now_fn=now_fn,
    )
    app.state.explore_index = _explore_index  # 唯讀 introspection seam（沿既有慣例）

    @app.get("/api/public/explore")
    def public_explore(window: str = "30d", page: int = 1, qualified: int = 1,
                       max_dd: int = 1, exclude_concentrated: int = 1):
        """探索跟單對象榜（無需登入）。本輪只實作 `window=30d`（D1：7D/90D/全部
        的 enrich 成本是 ×4，前端 chip 先 disabled）；壞 `window`／`page` 非正
        整數／布林參數不是 0 或 1 → 422。資格過濾與風險調整排序全在後端
        （R2-01）；建置中或上游從未成功過 → `building: true` ＋空 rows
        （200，不是 503——這是漸進式建置中的正常狀態，不是故障，見
        `hl_explore.ExploreIndex.query`）。"""
        if window != "30d":
            raise HTTPException(status_code=422, detail="window 本輪僅支援 30d")
        if page < 1:
            raise HTTPException(status_code=422, detail="page 須為正整數")
        for name, v in (("qualified", qualified), ("max_dd", max_dd),
                        ("exclude_concentrated", exclude_concentrated)):
            if v not in (0, 1):
                raise HTTPException(status_code=422, detail=f"{name} 須為 0 或 1")
        return _explore_index.query(page=page, require_sample=bool(qualified),
                                    max_dd_filter=bool(max_dd),
                                    exclude_concentrated=bool(exclude_concentrated))

    # ---------- /api/public/traders/{address}（M3 round2 Task 6）----------
    # ⭐ leaderboard 任意地址的詳情頁——不受精選白名單管轄（`leaders.py` 唯讀，
    # 本區塊完全不 import 它）。計算重用 `filet.strategies` 的純函式
    # （`build_metrics`／`build_equity_index`／`build_methodology`），與
    # `/api/public/strategies/{slug}` 共用同一份公式——不重算。回應形狀刻意
    # 只對齊 `metrics`／`equity_index`／`methodology`（plan Task 6 明訂範圍），
    # 不硬套 `build_strategy_view`：那個函式吃 `LeaderRef`（name/slug/tagline/
    # featured/listable…），這些欄位對一個任意鏈上地址沒有意義，硬塞一個假
    # LeaderRef 只會產生看起來像策展資訊、實際是編造的欄位。
    # ⭐ [8b-1] 2026-08-29 二輪複審 Critical：`clearinghouse_state` 原本每個請求
    # 無條件打一次上游（複審實測 50 個匿名請求 → 50 次上游，完全不受任何快取或
    # 限流保護——它跟 `portfolio` 是「兩個不同端點」沒錯，但兩者的**快取時機**
    # 不該分開：同一次 cache miss 應該一起抓、一起算一次配額，而不是 portfolio
    # 有快取、account_value 卻是無底洞的放大面）。修法：併入同一個快取條目，
    # tuple 多存一個 account_value 欄位；`_enforce_probe_ratelimit` 只在這次
    # 合併 miss 呼叫一次。
    _trader_portfolio_cache: dict[str, tuple[float, list, str | None]] = {}
    _trader_portfolio_lock = threading.Lock()
    TRADER_PORTFOLIO_CACHE_TTL_S = 300.0  # 5 分鐘（plan 明訂）
    TRADER_PORTFOLIO_CACHE_MAX = 256      # 上限地址數（防濫用，plan 明訂）
    # 唯讀 introspection seam（沿 `probe_ratelimit_hits` 的既有模式，[8b-3]）：
    # 讓測試能直接斷言快取 dict 大小／內容是否真的守住 256 上限，不必靠時鐘
    # 推進去間接推論（那條路徑會被 TTL 過期悄悄混淆，見 [8b-3] 的複審 mutation
    # 實證：把上限改成 999999，靠時鐘推進的舊測試照樣通過）。
    app.state.trader_portfolio_cache = _trader_portfolio_cache
    # ⭐ [C1] 2026-08-29 opus 審查：portfolio 抓取失敗的短 TTL 負面快取——防同一個
    # 壞地址（不存在／上游持續 5xx）被重複打上游。與成功快取分開一個 dict，理由是
    # 兩者的淘汰與 TTL 語意不同（成功快取有 256 上限＋LRU 淘汰，失敗快取沒有值可存，
    # 只記「何時失敗過」，靠 TTL 自然過期，不需要另外設上限）。
    _trader_portfolio_negative_cache: dict[str, float] = {}
    TRADER_PORTFOLIO_NEGATIVE_TTL_S = 60.0

    def _cached_trader_data(address: str, ratelimit_key: str) -> tuple[list | None, str | None]:
        """`hl.portfolio()` ＋ `hl.clearinghouse_state()` 的 5 分鐘 per-address
        **合併**快取，上限 256 個地址。同一次 cache miss 把兩個上游一起抓、
        `_enforce_probe_ratelimit` 只計費一次（見 [8b-1]）——`account_value`
        （clearinghouseState）失敗只讓它降級為 `None`，不影響 `rows`（portfolio，
        equity 曲線與 metrics 的唯一資料源）是否成功快取；`rows` 失敗才走負面
        快取短路整個條目（工程原則 1：兩個來源分別展示各自的數字，不混進同一個
        對比，但**快取時機**合併不影響這條原則——兩個欄位在回應裡仍各自標明
        來源、各自可能為 `None`）。

        超過上限時淘汰最舊一筆（近似 LRU）；`rows` 失敗改記負面快取（見上）而非
        完全不快取——同一壞地址在 60s 內重打會直接短路，不再二次打上游。

        `ratelimit_key`：呼叫端派生的 per-client 識別（本端點無 session，見
        `public_trader_detail`），不是位址——按位址計費擋不住「同一個 client
        輪流枚舉不同位址」這個真正的上游放大面（見 [C1]）。"""
        now = now_fn()
        with _trader_portfolio_lock:
            cached = _trader_portfolio_cache.get(address)
            failed_at = _trader_portfolio_negative_cache.get(address)
        if cached is not None and now - cached[0] < TRADER_PORTFOLIO_CACHE_TTL_S:
            return cached[1], cached[2]
        if failed_at is not None and now - failed_at < TRADER_PORTFOLIO_NEGATIVE_TTL_S:
            return None, None
        # ⭐ [C1] per-client rate limit：只在真的要打上游（快取未命中、負面快取也
        # 已過期）這一刻才計費——cache/negative-cache 命中不消耗額度，因為那兩條
        # 路徑本來就不會產生上游流量，計費在那兩條路徑上只會誤傷正常瀏覽。
        _enforce_probe_ratelimit(ratelimit_key)
        try:
            rows = hl.portfolio(address)
        except Exception as e:  # noqa: BLE001 — 公開端點：上游任何失敗都不得 500
            logger.error("交易員績效上游查詢失敗 address=%s: %s", address, e)
            with _trader_portfolio_lock:
                _trader_portfolio_negative_cache[address] = now
            return None, None
        account_value = None
        try:
            cs = hl.clearinghouse_state(address)
            account_value = cs.get("marginSummary", {}).get("accountValue")
        except Exception as e:  # noqa: BLE001 — 額外欄位，失敗只降級該欄位
            logger.error("交易員 account_value 查詢失敗 address=%s: %s", address, e)
        with _trader_portfolio_lock:
            _trader_portfolio_negative_cache.pop(address, None)
            if (address not in _trader_portfolio_cache
                    and len(_trader_portfolio_cache) >= TRADER_PORTFOLIO_CACHE_MAX):
                oldest = min(_trader_portfolio_cache,
                            key=lambda k: _trader_portfolio_cache[k][0])
                del _trader_portfolio_cache[oldest]
            _trader_portfolio_cache[address] = (now, rows, account_value)
        return rows, account_value

    def _trader_follow_blocked(addr: str) -> bool:
        """[W4] 已被安全撤銷（`enabled=false`）的 leader 不該在交易員詳情頁看到
        跟單 CTA。合併視圖與 `leaders_preview`/`leaders_select` 同一份（精選優先、
        缺則由 user registry 遞補，見 `merge_leaders` 檔頭）；白名單檔案唯讀，
        本函式只讀不寫。

        載入失敗 → fail-closed（回 True，隱藏 CTA）：這是安全相關判斷（會不會把
        新客戶導去跟一個已撤銷的 leader），寧可誤傷「暫時看不到 CTA」也不要誤放
        行——不像 `account_value` 那種純展示欄位可以安靜降級成 `null`。"""
        try:
            merged = merge_leaders(load_leaders(cfg.leaders_path),
                                   load_user_leaders(cfg.user_leaders_path))
        except (OSError, ValueError) as e:
            logger.error("交易員 follow_blocked 白名單查詢失敗 address=%s: %s", addr, e)
            return True
        ref = find_leader(addr, merged)
        return bool(ref is not None and not ref.enabled)

    _TRADER_PROBE_KEY_PREFIX = "public_trader_probe:"

    @app.get("/api/public/traders/{address}")
    def public_trader_detail(address: str, request: Request):
        """任意 HL 地址的鏈上績效詳情（無需登入）。`address` 需為 `0x` + 40 hex，
        壞格式 → 422。

        `hl.portfolio()`（equity 曲線與 metrics 的唯一資料源）查詢失敗 → 503——
        沒有它整頁沒有東西可畫，與 `/api/public/leaderboard` 首次抓取失敗同一
        個判準。`account_value`（來自 `clearinghouseState`，**與 portfolio 是
        兩個不同端點**，工程原則 1：不得把兩者混進同一個對比裡，這裡只是分別
        展示各自的數字）查詢失敗只降級該欄位為 `null`，不拖累整頁——它是
        plan 明訂的「額外」欄位，不是頁面的主要內容。兩者現在**併入同一個
        5 分鐘快取條目**一起抓（見 [8b-1]、`_cached_trader_data`），不是各自
        獨立打上游。

        ⭐ [C1] 本端點無需登入、任意人可枚舉任意位址各打一次上游查詢
        （上游放大面）——套用與 `/api/leaders/preview` 同款的 sliding-window
        rate limit（見 `_enforce_probe_ratelimit`），但因為本端點沒有 session，
        key 改用 client IP（`request.client.host`）：按位址計費擋不住「同一個
        client 輪流枚舉不同位址」這個真正的放大面，按 client 計費才對。只在真的
        打上游那一刻消耗額度（見 `_cached_trader_data`）。
        """
        try:
            addr = normalize_address(address)
        except ValueError:
            raise HTTPException(status_code=422, detail="位址格式不合法（需 0x 開頭 + 40 hex）")

        client_host = request.client.host if request.client else "unknown"
        rows, account_value = _cached_trader_data(addr, _TRADER_PROBE_KEY_PREFIX + client_host)
        if rows is None:
            raise HTTPException(status_code=503, detail="鏈上績效查詢暫時不可用，請稍後重試")

        try:
            perf = compute_window_performance(rows, "perpAllTime")
        except Exception as e:  # noqa: BLE001 — schema 漂移不得炸掉整頁
            logger.error("交易員績效計算失敗 address=%s: %s", addr, e)
            perf = None

        initial_deposit_usd = None
        window = extract_window(rows, "perpAllTime")
        if window is not None:
            av, _pnl = window
            if av:
                initial_deposit_usd = av[0][1]

        return {
            "address": addr,
            "account_value": account_value,
            "follow_blocked": _trader_follow_blocked(addr),
            "metrics": build_metrics(perf),
            "equity_index": build_equity_index(perf),
            "methodology": build_methodology(
                perf, initial_deposit_usd=initial_deposit_usd, updated_at=int(now_fn())),
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
                                           added_by=account_id,
                                           kind=custom["kind"])
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
                detail={"code": "agent_exists",
                        "message": "已有 agent，不重生（避免 rotate 作廢既有鏈上授權）"})
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
                        detail={"code": "agent_conflict",
                                "message": "keystore 與 DB 狀態不一致且無法自動復原，"
                                           "請聯絡管理員"}) from e2
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

    # ---------- 風控設定（客戶簽章；2026-07-30）----------
    # ⭐⭐ 本路徑與換 leader／資金設定**同一套信任錨**：客戶用自己的私鑰簽一份逐項
    # 列出門檻的原文，API 只落記錄，引擎在套用前自己重新驗章。威脅模型與「為什麼
    # 風控設定也必須簽章」的全文見 `filet/risk_settings.py` 檔頭。
    # 兩個動作、兩份記錄、兩個檔（時效語意相反）：
    #   - 風控設定＝**持續意圖**（引擎每輪讀，不檢查時效）；
    #   - 解除熔斷＝**一次性動作**（強制時效，一份三天前的解鎖記錄若還能生效，
    #     等於客戶簽一次就永久放棄了熔斷保護）。
    # ⚠️ API 端**兩者都強制時效**（`RISK_SETTINGS_MAX_AGE_S`）：這裡驗的是「客戶
    # 剛剛按下的那一次」，nonce 也是本進程幾分鐘前才發的。放行時效的是引擎端。

    def _resume_at(tripped_at: str | None, cooldown_hours: str | None) -> str | None:
        """自動恢復的時刻＝熔斷時刻 ＋ 冷靜期；算不出來一律 None。

        `None` 的三種來源刻意不分開（對客戶都是「沒有可顯示的自動恢復時刻」）：
        讀不到熔斷時刻、讀不到冷靜期、或冷靜期為 0（＝不自動恢復）。前端對
        `null` 的正確反應是顯示「不會自動恢復／無法確認」，而不是留白。
        """
        if not tripped_at or cooldown_hours is None:
            return None
        try:
            hours = Decimal(str(cooldown_hours))
            if hours <= 0:
                return None
            base = datetime.fromisoformat(tripped_at)
        except (ValueError, TypeError, ArithmeticError):
            return None
        return (base + timedelta(hours=float(hours))).isoformat()

    def _halt_note(halt: dict | None, cooldown_h: str | None = None) -> str:
        """熔斷中的人話說明。⭐ 依「可否自助恢復」分岔，不要用同一句含糊帶過——
        `leader_revoked` 的鎖定簽了也不會解除，把它與一般風險熔斷寫成同一句，
        等於邀請客戶去簽一份注定失敗的請求。"""
        if halt is None:
            return ("你的跟單目前因熔斷而停止交易。這顆引擎回報的版本較舊，"
                    "尚無法確認熔斷原因與能否自助恢復——請稍候重新整理。")
        reason = halt.get("reason")
        residual = ("⚠️ 熔斷當下有部位未能平倉或掛單未撤，那些部位仍在市場上。"
                    "恢復跟單後引擎會在下一輪把它們往 leader 的目標收斂。"
                    if halt.get("residual_exposure") else "")
        if halt.get("resumable"):
            # ⭐ 冷靜期 0 ＝**不會**自動恢復，不能沿用同一句（審查 F6）：那會讓客戶
            # 以為只要等就好，而實際上他不按就永遠不會恢復。
            # ⚠️ 三態，不得把「讀不到」折疊成「0」：0 是客戶的選擇（不自動恢復），
            # None 是我們不知道他設了多久——後者仍然會自動恢復，只是說不出時間。
            if cooldown_h == "0":
                auto = ("你把冷靜期設為 0（不自動恢復），所以只有在本頁簽署一次"
                        "「立即恢復跟單」才會解除。")
            elif cooldown_h is None:
                auto = ("冷靜期屆滿後會自動恢復（目前讀不到你設定的時數）；"
                        "要立即恢復請在本頁簽署一次「立即恢復跟單」。")
            else:
                auto = (f"冷靜期（{cooldown_h} 小時）屆滿後會自動恢復；"
                        f"要立即恢復請在本頁簽署一次「立即恢復跟單」。")
            base = ("你的跟單目前因**累計虧損達到你設定的絕對底線**而停止交易。"
                    if reason == "total_drawdown"
                    else "你的跟單目前因風控熔斷而停止交易。")
            return base + auto + residual
        # ⚠️ 不可自助恢復有三種來源，不得一律說成「leader 被撤銷」（審查 F4）：
        # 客戶會被告知一件不曾發生的事，並被導去做一個解決不了問題的動作。
        if reason == "leader_revoked":
            return ("你的跟單目前因 leader 被撤銷而停止交易。這不是風控熔斷，"
                    "**無法**由你自助恢復——請聯絡我們。")
        if reason:
            return (f"你的跟單目前因 `{reason}` 而停止交易，這個原因**無法**由你"
                    f"自助恢復（例如營運端的緊急處置，或熔斷時有部位未收乾淨）"
                    f"——請聯絡我們。")
        return ("你的跟單目前處於熔斷鎖定，但引擎回報的原因無法判讀"
                "——為安全起見不提供自助恢復，請聯絡我們。")

    def _my_signed_risk_record(account_id: str) -> dict | None:
        """交換目錄 → **只**這一個帳號的風控設定記錄，且**只投影安全欄位**。

        兩層窄化都是結構性的，理由與 `_load_own_capital_record` 逐字相同：
        多帳號清單的生命週期不離開本函式；`signature`／`message` 原文結構上到不了
        回應。記錄壞掉／讀不到一律回 None（＝「尚無已簽章的設定」），**不 raise**。
        """
        try:
            records = load_risk_settings(cfg.risk_settings_path)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.error("風控設定記錄讀取失敗 %s: %r", cfg.risk_settings_path, e)
            return None
        mine = [r for r in records if isinstance(r, dict)
                and r.get("account_id") == account_id]
        if not mine:
            return None
        # write_risk_settings 是「同 account 覆蓋」，正常至多一筆；取最後一筆＝取最新
        # 意圖（沿引擎 applier 的同一個選法，兩邊看的是同一筆）。
        rec = mine[-1]
        issued_at = rec.get("issued_at")
        return {"prefs": rec.get("prefs"),
                "submitted_at": issued_at if isinstance(issued_at, str) else None}

    def _risk_base_prefs(account_id: str) -> dict:
        """局部更新的補值基底＝**這個帳號目前已簽章的值**，不是產品預設（審查 F1）。

        否則一份只寫 `{"enabled": true}` 的請求會把客戶調過的門檻靜默重設回預設值。
        目前存的值本身壞掉時退到安全側（風控開啟），不拿壞資料當合法基準。
        """
        rec = _my_signed_risk_record(account_id)
        try:
            return canonical_prefs(rec["prefs"] if rec else None)
        except RiskPrefsError:
            return safe_fallback_prefs()

    @app.get("/api/me/risk")
    def me_risk(address: str = Depends(_require_session)):
        """我的風控設定：**已提交**（簽章記錄）、**已生效**（引擎心跳）與熔斷狀態。

        ⭐⭐ `prefs`（已提交）與 `applied`（已生效）刻意分成兩格，理由與
        `/api/me/capital` 的 pending/effective 完全相同：記錄只代表「客戶簽了、
        API 收了」，引擎可能還沒套用、也可能因為驗章或邊界檢查而永遠不會套用。
        把記錄當成生效值顯示，會讓一個剛把回撤上限調低的客戶以為保護已經生效。
        生效值的唯一來源是**引擎自己發布的心跳**（`read_heartbeat` 在心跳過期時
        結構性地不回傳 payload ⇒ 過期的值到不了這裡）。

        ⭐ `halted`：這顆引擎是不是正在熔斷鎖定中——`null` ＝**無從得知**
        （心跳缺席／過期／引擎自己也不知道），絕不畫成「沒有熔斷」。前端據此顯示
        「無法確認」而不是一個令人安心的綠燈。

        ⭐ `editable` **恆為 true**（2026-07-30 改版）：偏好改成執行期套用之後，
        已啟用的帳號同樣能改——引擎每輪重讀簽章記錄。舊版的 `not_editable_reason`
        與 409 分支隨舊的「啟用當下烙進 env」路徑一起移除。
        """
        account_id = derive_account_id(address)
        rec = _my_signed_risk_record(account_id)
        # ⭐ 存著的偏好驗不過（手改／舊格式）**不得** 500：那會讓客戶連改回來的
        # 介面都打不開，而 500 又長得像「稍後重試就好」的暫時故障（工程原則 2）。
        # 顯示的是引擎遇到同一份壞資料時會採用的那一份（風控開啟的安全側），
        # 並明講「這不是你存的值」——兩邊對同一個壞資料的解讀必須一致，
        # 否則畫面說關、引擎跑開，客戶無從得知哪個是真的。
        unreadable = False
        try:
            summary = prefs_summary(rec["prefs"] if rec else None)
        except RiskPrefsError:
            logger.warning("風控設定記錄無法解析，改以安全側顯示 account=%s",
                           account_id)
            unreadable = True
            summary = prefs_summary(safe_fallback_prefs())

        hb = _read_heartbeat(account_id, now_fn())
        # ⭐ `hb.data` 只有在心跳新鮮時才非 None（engine_health 的結構性保證）。
        risk = (hb.data or {}).get("risk") or {}
        applied = None
        if risk.get("source") in ("customer_signed", "env_default"):
            applied = {
                "controls_enabled": risk.get("controls_enabled"),
                # `customer_signed` ＝這組門檻是客戶自己簽的；`env_default` ＝仍是
                # 部署當下寫進 env 的值。少了它，客戶分不出「我的設定生效了」與
                # 「我看到的是部署預設」。
                "source": risk.get("source"),
                "changed_at": risk.get("changed_at"),
                # 這組值是引擎在哪一刻回報的——沒有這個時間戳，一份接近過期的心跳
                # 會被當成即時查詢讀（工程原則 1 的變形：連時點都不同源）。
                "as_of": hb.at,
            }
        tripped = (hb.data or {}).get("killswitch_tripped")
        # ⭐ 熔斷原因與「可否自助恢復」取自心跳的 `risk.halt`（引擎以
        # `killswitch.halt_status` 產出，`resumable` 由 `rearm_allowed_for` 導出）。
        # 沒有這一格的話，前端只能讓客戶對一個 `leader_revoked` 的鎖定簽一份注定被
        # 引擎拒絕的解鎖請求——白費一次真實簽章，而失敗訊息出現在他按下之後。
        # 心跳是舊版引擎寫的（沒有 `halt` 這一格）時 → `resumable` 為 None ＝未知，
        # 前端據此顯示「無法確認」而不是給出可能錯誤的按鈕。
        halt = risk.get("halt") if isinstance(risk.get("halt"), dict) else None
        applied_prefs = risk.get("prefs") if isinstance(risk.get("prefs"), dict) else None
        cooldown_h = (applied_prefs or {}).get("cooldown_hours")
        halted = None if tripped is None else {
            "tripped": bool(tripped),
            "reason": (halt or {}).get("reason"),
            "tripped_at": (halt or {}).get("tripped_at"),
            "resumable": (halt or {}).get("resumable") if tripped else None,
            # ⭐ 熔斷當下有部位沒平乾淨——**不擋**自助恢復（2026-07-31 使用者裁決），
            # 但客戶按下那顆按鈕之前有權知道。None ＝ 引擎版本較舊或讀不到。
            "residual_exposure": (halt or {}).get("residual_exposure"),
            # 冷靜期取自**引擎實際在執法的**那一組（心跳的 applied prefs），不是客戶
            # 剛提交但可能還沒生效的那一份——顯示的恢復時刻必須是真的會發生的那個。
            "cooldown_hours": cooldown_h,
            "resume_at": _resume_at((halt or {}).get("tripped_at"), cooldown_h),
            "as_of": hb.at,
            "note": (_halt_note(halt, cooldown_h) if tripped
                     else "目前沒有熔斷鎖定。"),
        }
        return {
            **summary,
            "stored_unreadable": unreadable,
            "stored_unreadable_note": (
                "你先前儲存的風控設定讀取異常，畫面顯示的是系統採用的安全預設"
                "（風控開啟）。請重新確認並儲存一次。" if unreadable else None),
            # 「已提交」的時刻。與 `applied.changed_at`（引擎**實際套用**的時刻）
            # 是兩個不同的問題，刻意不合併成一格。
            "submitted": {"issued_at": rec["submitted_at"] if rec else None},
            "applied": applied,
            "halted": halted,
            "heartbeat": {"status": hb.status, "at": hb.at, "age_s": hb.age_s,
                          "stale_after_s": HEARTBEAT_STALE_S},
            "editable": True,
            "note": ("這是你已簽署的風控設定。引擎會在下一輪（約一分鐘內）重新驗證"
                     "你的簽章後套用；「目前生效」那一格才是引擎實際在執法的值。"
                     if applied is not None else
                     "目前無法確認生效中的風控設定（引擎的健康心跳缺席或已過期）。"
                     "**請不要**把已提交的數值當成生效值。"),
        }

    def _consume_risk_nonce(address: str):
        """本節兩個 POST 端點共用的 nonce 消耗式（與換 leader／資金設定同一張表）。

        兩道要求同 `leaders_select` 的 `_consume`：nonce 必須是**發給本人**的，且
        必須是**這個 chain_id 域**發的（擋 SIWE 登入 nonce 被挪用）。
        ⚠️ 這裡**不**分辨「哪個端點發的」——分辨動作的是**簽章本身**：客戶簽的原文
        第一行寫死了動作類型，拿風控設定的 nonce 配解除熔斷的簽章，重建出來的訊息
        對不上任何一邊（完整論證見 filet/risk_settings.py 檔頭的域分隔）。
        """
        def _consume(nonce: str) -> bool:
            rec = store.consume_nonce(nonce, now_s=now_fn())
            return (rec is not None and rec.address == address
                    and rec.chain_id == _LEADER_CHANGE_CHAIN_ID)
        return _consume

    @app.post("/api/me/risk/message")
    def risk_settings_message(body: RiskSettingsMessageBody,
                              address: str = Depends(_require_session)):
        """回傳風控設定的 **canonical 待簽原文** ＋ 配套的一次性 nonce。

        ⭐ 為什麼是 **POST 而不是 GET**（與資金設定的 GET 版刻意不同）：待簽的偏好
        有五個欄位（含 bool 與比例字串），塞進 query string 既難讀、又多一層編碼／
        解碼——而伺服器重建原文用的是解碼後的值，任何一個編碼差異都會產生「客戶
        簽的字串」與「伺服器驗的字串」不同的縫（工程原則 1）。本端點唯一的副作用
        是簽發一顆 nonce（沿 capital／leader 兩個 message 端點的既有慣例），
        不改變任何狀態，所以用 POST 承載 body 不牴觸它的語意。

        ⭐ 邊界在**發原文之前**就檢查（超界 → 400，不發 nonce、不給原文）：
        讓客戶簽一份必定被 POST 拒絕的原文，是把閘門變成一個只會浪費他一次錢包
        簽名的陷阱。

        ⭐ 回傳的 `prefs` 是 **canonical 化後**的值，客戶端**原樣回填**進 POST body
        ——兩邊結構上不可能組出不同的字串（同 capital 回 canonical 金額字串）。
        缺鍵補值的來源是他目前已簽章的值（`_risk_base_prefs`），且 `enabled` 必須
        明確指定：省略它會被讀成「關閉」，而那不該由一個缺漏的欄位決定。
        """
        account_id = derive_account_id(address)
        try:
            prefs = canonical_prefs(body.prefs, base=_risk_base_prefs(account_id),
                                    require_enabled=True)
        except RiskPrefsError as e:
            # ⭐ 這裡回 `str(e)`（與驗簽失敗的分類化訊息不同）：客戶要改對數值就必須
            # 看到合法區間，而這類訊息只含參數名與數值，不含任何授權材料。
            raise HTTPException(status_code=400, detail=str(e)) from None

        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(address, _LEADER_CHANGE_CHAIN_ID, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        message = build_risk_settings_message(account_id=account_id, prefs=prefs,
                                              nonce=nonce, issued_at=issued_at)
        return {"message": message, "nonce": nonce, "issued_at": issued_at,
                "account_id": account_id, "prefs": prefs}

    @app.post("/api/me/risk")
    def me_risk_submit(body: RiskSettingsBody,
                       address: str = Depends(_require_session)):
        """客戶**自己簽章**調整風控門檻 → 寫一筆簽章記錄。

        ⭐ 本端點**不改任何引擎設定**，只落一筆記錄。引擎在套用前自己重新驗章、
        自己重新檢查邊界——繞過那道驗證等於把整套簽章設計降級成裝飾。

        失敗分類（工程原則 2）：驗簽失敗、動作類型不符、超界、session 不符
        **全是 semantic**（4xx，不得自動重試）；寫檔失敗才是 transient（5xx）。
        ⚠️ 驗簽失敗一律 **400 而非 401**：客戶的 session 是有效的（他已通過 SIWE），
        壞掉的是這一份請求內容——401 會讓前端把客戶登出，而那對他毫無幫助。
        """
        # 1) 只能改自己的（同 leaders_select：403 而非 404，不洩漏帳號是否存在）。
        account_id = derive_account_id(address)
        if body.account_id != account_id:
            raise HTTPException(status_code=403, detail="只能變更自己帳號的風控設定")

        # 2) 邊界（政策）。⭐ 超界一律 4xx，**不夾取**：夾取會讓流程順利跑完，
        #    代價是客戶簽了 A、系統執行了 B，而且沒有人會知道。
        #    ⚠️ 這裡**不**帶 base：記錄必須是自洽的（引擎只看記錄，不知道 API 這側
        #    存了什麼）。客戶端回填的是 message 端點給的完整 canonical prefs。
        try:
            canonical_prefs(body.prefs, require_enabled=True)
        except RiskPrefsError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

        # 3) 驗章。user_address 出自 **session**（可信來源），不是請求內容。
        try:
            record = build_risk_settings_record(
                account_id=body.account_id, prefs=body.prefs, nonce=body.nonce,
                issued_at=body.issued_at, signature=body.signature,
                message=body.message)
            verified = verify_risk_settings(
                record, account_id=account_id, user_address=address,
                now_s=now_fn(), consume_nonce=_consume_risk_nonce(address),
                # ⭐ API 端**強制時效**（引擎端刻意放行，見 risk_settings.py）：
                # 這裡驗的是「客戶剛剛按下的那一次」，nonce 也才剛發出去。
                max_age_s=RISK_SETTINGS_MAX_AGE_S)
        except RiskSettingsError as e:
            # 稽核痕跡（偽造探測）：記 reason 與帳號，**不記** signature／message
            # 原文，也不記偏好值（來路不明的內容不進 log）。
            logger.warning("風控設定驗簽失敗 account=%s reason=%s", account_id, e.reason)
            raise HTTPException(
                status_code=400,
                detail=RISK_SETTINGS_DETAIL.get(e.reason,
                                                RISK_SETTINGS_DETAIL_DEFAULT)
            ) from None

        # 4) 落檔（唯一的寫入，且在**全部驗證通過之後**）。落地的每一個欄位都取自
        #    **verified**（通過驗證的那一份），不是 body——否則落地的記錄可以與客戶
        #    簽的原文不一致。
        record = build_risk_settings_record(
            account_id=verified.account_id, prefs=verified.prefs,
            nonce=verified.nonce, issued_at=verified.issued_at,
            signature=body.signature, message=body.message)
        try:
            write_risk_settings(cfg.risk_settings_path, record)
        except OSError as e:
            logger.error("風控設定記錄落檔失敗 account=%s path=%s: %s",
                         account_id, cfg.risk_settings_path, e)
            raise HTTPException(status_code=500,
                                detail="設定記錄寫入失敗，請稍後重試") from e
        logger.info("風控設定記錄已落地 account=%s enabled=%s",
                    account_id, verified.prefs["enabled"])

        return {
            "ok": True,
            "account_id": account_id,
            "prefs": verified.prefs,
            "effective": "next_engine_cycle",
            "effective_note": "已記錄，於引擎的下一個 cycle 生效——不是立即生效；"
                              "引擎會在下一輪（約一分鐘內）**自己重新驗證你的簽章**"
                              "與數值範圍後套用，驗證不過則不會套用。",
        }

    @app.post("/api/me/risk/unlock/message")
    def risk_unlock_message(address: str = Depends(_require_session)):
        """回傳「立即解除熔斷鎖定」的待簽原文 ＋ 一次性 nonce（**無 body**）。

        用 POST 而非 GET 與 `/api/me/risk/message` 同一個理由（副作用是簽發 nonce，
        且與它成對出現的端點形狀一致）；本端點沒有任何輸入——要解除的永遠是
        **這個 session 的**帳號，沒有任何請求參數能指到別人的引擎。
        """
        account_id = derive_account_id(address)
        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(address, _LEADER_CHANGE_CHAIN_ID, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        message = build_risk_unlock_message(account_id=account_id, nonce=nonce,
                                            issued_at=issued_at)
        return {"message": message, "nonce": nonce, "issued_at": issued_at,
                "account_id": account_id}

    @app.post("/api/me/risk/unlock")
    def me_risk_unlock(body: RiskUnlockBody,
                       address: str = Depends(_require_session)):
        """客戶**自己簽章**要求立即恢復跟單（解除熔斷鎖定）→ 寫一筆一次性記錄。

        ⭐ 這是客戶**自己拿掉一道保護**的動作，所以走與門檻設定完全相同的信任錨，
        但兩者的記錄、檔案與待簽原文全部分開：一份「調整門檻」的簽章絕不能被兌換成
        一次解鎖（否則攻擊者只要等客戶下次調整風控，就能拿那份簽章把熔斷鎖打開）。

        時效**強制**（`verify_risk_unlock` 的預設）：一份三天前簽的解鎖記錄若還能
        生效，等於客戶簽一次就永久放棄了熔斷保護。
        """
        account_id = derive_account_id(address)
        if body.account_id != account_id:
            raise HTTPException(status_code=403, detail="只能解除自己帳號的熔斷鎖定")

        try:
            record = build_risk_unlock_record(
                account_id=body.account_id, nonce=body.nonce,
                issued_at=body.issued_at, signature=body.signature,
                message=body.message)
            verified = verify_risk_unlock(
                record, account_id=account_id, user_address=address,
                now_s=now_fn(), consume_nonce=_consume_risk_nonce(address))
        except RiskSettingsError as e:
            logger.warning("解除熔斷驗簽失敗 account=%s reason=%s", account_id, e.reason)
            raise HTTPException(
                status_code=400,
                detail=RISK_UNLOCK_DETAIL.get(e.reason, RISK_UNLOCK_DETAIL_DEFAULT)
            ) from None

        record = build_risk_unlock_record(
            account_id=verified.account_id, nonce=verified.nonce,
            issued_at=verified.issued_at, signature=body.signature,
            message=body.message)
        try:
            write_risk_unlock(cfg.risk_unlock_path, record)
        except OSError as e:
            logger.error("解除熔斷記錄落檔失敗 account=%s path=%s: %s",
                         account_id, cfg.risk_unlock_path, e)
            raise HTTPException(status_code=500,
                                detail="記錄寫入失敗，請稍後重試") from e
        logger.info("解除熔斷記錄已落地 account=%s", account_id)

        return {
            "ok": True,
            "account_id": account_id,
            "effective": "next_engine_cycle",
            "effective_note": "已記錄。引擎會在下一輪（約一分鐘內）重新驗證你的簽章，"
                              "通過後解除鎖定並恢復跟單——恢復後可能立刻依你的 leader "
                              "開新部位。權益基準已在熔斷當下重置，所以不會馬上再被"
                              "同一段跌幅熔斷一次。"
                              "⚠️ 若熔斷的原因是你的 leader 被撤銷，這份簽章**不會**"
                              "解除它（那需要你先選一個新的 leader）。",
        }

    # ---------- owner kill switch（Task 15：暫停／平倉並撤銷）----------

    @app.post("/api/me/pause")
    def me_pause(body: PauseBody, address: str = Depends(_require_session)):
        """暫停或恢復跟單（kill switch 第一級）。**無需簽章**——登入 session 即可：
        兩個方向都只在既有授權範圍內收窄（暫停）或恢復（resume）活動，不是新增
        任何主網寫入權限（專案 CLAUDE.md 紅線 5 對照）。

        寫入路徑 `pause_flag_path_for(cfg.exchange_dir, address)`——與引擎讀端
        （`spark/filet/pause_flag.py::read_pause_flag_for_engine`）共用同一個
        推導函式，兩端不可能各拼一份路徑而漂移（工程原則 1）。`address` 取自
        session（可信來源），不是請求內容——結構上不可能暫停別人的引擎。

        引擎每輪重讀（見 `scripts/run_copytrade.cycle()`），效果與資金/風控設定
        同一個「下一個 cycle 生效」語意，故本端點沒有寫死重試/冪等的額外機制：
        重複呼叫同一個 action 只是把同一份布林再寫一次，天然冪等。
        """
        if body.action not in ("pause", "resume"):
            raise HTTPException(status_code=400,
                                detail="action 必須是 'pause' 或 'resume'")
        path = pause_flag_path_for(cfg.exchange_dir, address)
        try:
            write_pause_flag(path, paused=(body.action == "pause"), now_s=now_fn())
        except OSError as e:
            logger.error("暫停旗標落檔失敗 address=%s path=%s: %s", address, path, e)
            raise HTTPException(status_code=500,
                                detail="設定寫入失敗，請稍後重試") from e
        paused = body.action == "pause"
        logger.info("暫停旗標已更新 address=%s paused=%s", address, paused)
        return {
            "ok": True,
            "paused": paused,
            "effective": "next_engine_cycle",
            "effective_note": ("已記錄。引擎會在下一輪（約一分鐘內）跳過新開倉與"
                               "加倉，但仍會處理減倉/平倉與既有風控動作。"
                               if paused else
                               "已記錄。引擎會在下一輪（約一分鐘內）恢復正常跟單。"),
        }

    @app.get("/api/me/close-all/message")
    def close_all_message(address: str = Depends(_require_session)):
        """回傳「平倉並撤銷」的待簽原文 ＋ 一次性 nonce（**無 body**，形狀沿
        `/api/me/risk/unlock/message`——本端點沒有任何輸入，要平倉的永遠是**這個
        session 的**帳號，沒有任何請求參數能指到別人的引擎）。
        """
        account_id = derive_account_id(address)
        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(address, _LEADER_CHANGE_CHAIN_ID, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        message = build_close_all_message(account_id=account_id, nonce=nonce,
                                          issued_at=issued_at)
        return {"message": message, "nonce": nonce, "issued_at": issued_at,
                "account_id": account_id}

    @app.post("/api/me/close-all")
    def close_all_submit(body: CloseAllBody, address: str = Depends(_require_session)):
        """客戶**自己簽章**要求平倉並撤銷 → 寫一筆一次性請求。

        ⭐ 本端點**不直接平倉**，只落一筆記錄——引擎在套用前自己重新驗章（同換
        leader／資金／風控設定的既有信任模型）。真正的收尾動作（撤單→reduce-only
        全平→halt）由引擎重用既有的 `killswitch.trip` 路徑執行，**不新造平倉邏輯**
        （CLAUDE.md 本 task 派工的明文要求）。

        失敗分類（工程原則 2）：驗簽失敗、動作類型不符、超期、session 不符全是
        semantic（4xx，不得自動重試）；寫檔失敗才是 transient（5xx）。驗簽失敗
        一律 400 而非 401（session 本身有效，壞的是這份請求內容）。
        """
        account_id = derive_account_id(address)
        if body.account_id != account_id:
            raise HTTPException(status_code=403, detail="只能平倉並撤銷自己的帳號")

        try:
            record = build_close_all_record(
                account_id=body.account_id, nonce=body.nonce,
                issued_at=body.issued_at, signature=body.signature,
                message=body.message)
            verified = verify_close_all(
                record, account_id=account_id, user_address=address,
                now_s=now_fn(), consume_nonce=_consume_risk_nonce(address))
        except CloseAllError as e:
            logger.warning("平倉並撤銷驗簽失敗 account=%s reason=%s",
                           account_id, e.reason)
            raise HTTPException(
                status_code=400,
                detail=CLOSE_ALL_DETAIL.get(e.reason, CLOSE_ALL_DETAIL_DEFAULT)
            ) from None

        record = build_close_all_record(
            account_id=verified.account_id, nonce=verified.nonce,
            issued_at=verified.issued_at, signature=body.signature,
            message=body.message)
        try:
            write_close_all_request(cfg.close_all_path, record)
        except OSError as e:
            logger.error("平倉並撤銷請求落檔失敗 account=%s path=%s: %s",
                         account_id, cfg.close_all_path, e)
            raise HTTPException(status_code=500,
                                detail="請求寫入失敗，請稍後重試") from e
        logger.info("平倉並撤銷請求已落地 account=%s", account_id)

        return {
            "ok": True,
            "account_id": account_id,
            "effective": "next_engine_cycle",
            "effective_note": "已記錄。引擎會在下一輪（約一分鐘內）重新驗證你的"
                              "簽章，通過後觸發受控收尾：撤銷全部掛單、以市價"
                              "reduce-only 平掉全部部位，完成後停止跟單且**不會"
                              "自動恢復**。此動作不可逆，且不會撤銷 API wallet 的"
                              "鏈上權限——收尾完成後請至 Hyperliquid 官方介面"
                              "自行移除。",
        }

    @app.get("/api/me/dashboard")
    def me_dashboard(address: str = Depends(_require_session)):
        """使用者 Dashboard 六塊 ＋ 持倉的**唯一資料源**（Task 13）。每一塊獨立
        nullable：子資料源丟例外只讓對應塊回 `None`，端點本身絕不 500。

        equity basis＝`accountValue`（同一次 clearinghouseState 內的
        accountValue/totalMarginUsed/withdrawable/totalNtlPos 互為同源，工程原則
        1；不變量 2）；PnL／30D 報酬走 follower 自己的 `portfolio()` 管線（同
        leader_perf，Task 13 規格明文授權「同管線餵 follower 位址」）；同步誤差
        過濾自 `/api/ops/trade-quality` 的同一個純函式，只餵這個帳號自己。找不到
        既有來源的欄位一律 `null`，不新造公式（不變量 6）。只回登入 session 自己
        的資料：`account_id`／`ref.user_address` 全部由 session 衍生，結構上沒有
        任何 account 參數（沿 `/api/me/leader`／`/api/me/capital` 的既有慣例）。
        """
        account_id = derive_account_id(address)
        now_s = now_fn()
        mine, _manifest_degraded = _load_own_follower(account_id)
        hb = _read_heartbeat(account_id, now_s)
        ref = FollowerRef(account_id=account_id, user_address=address,
                          builder_address=cfg.builder_address, network=cfg.network)

        state_raw = _safe_block("account_state", hl.clearinghouse_state, address)
        acct = _dashboard_account_snapshot(state_raw) if state_raw is not None else None
        positions = (_dashboard_positions_raw(state_raw)
                    if state_raw is not None else None)

        equity_block = None
        if acct is not None:
            equity_block = {
                "account_value": acct["account_value"],
                "margin_used": acct["margin_used"],
                "withdrawable": acct["withdrawable"],
                "available_pct": _available_pct(acct["withdrawable"],
                                                acct["margin_used"]),
                "ret_30d_pct": None,
            }
        exposure_block = (_dashboard_exposure(acct, positions)
                          if acct is not None else None)
        positions_block = None
        if positions is not None:
            positions_block = [
                {"symbol": p["coin"], "side": p["side"], "leverage": p["leverage"],
                 "margin_mode": p["margin_mode"], "value": p["value"],
                 "upnl": p["upnl"], "entry": p["entry"], "mark": p["mark"],
                 "deviation_pct": None}
                for p in positions
            ]

        pnl_and_ret = _safe_block("pnl", _dashboard_pnl_and_return, ref, hl,
                                  positions)
        pnl_block = None
        if pnl_and_ret is not None:
            pnl_block, ret_30d = pnl_and_ret
            if equity_block is not None:
                equity_block["ret_30d_pct"] = ret_30d

        status_block = _safe_block("status", _dashboard_status, mine, hb, acct,
                                   cfg.leaders_path, cfg.exchange_dir)
        sync_block = _safe_block("sync", _dashboard_sync, ref, hl, mine, hb, now_s)
        fees_month_block = _safe_block("fees_month", _dashboard_fees_month,
                                       ref, hl, now_s)
        # ⭐ M3 round3 Task 3（D5 風險護欄）：`risk_controls_enabled` 恆為布林
        # （`_dashboard_risk_controls_enabled` 的契約），不經 `_safe_block`——
        # 一個「不確定」的風控開關狀態不該被吞成 null，寧可用產品預設 False
        # 兜底（該函式內部已處理）。`signed_prefs` 讀取失敗（`_my_signed_risk_record`
        # 本身已內部 try/except 降級為 None）不會讓這裡炸開。
        signed_risk_rec = _my_signed_risk_record(account_id)
        risk_controls_enabled = _dashboard_risk_controls_enabled(
            hb, signed_risk_rec["prefs"] if signed_risk_rec else None)

        return jsonable({
            "status": status_block, "equity": equity_block,
            "exposure": exposure_block, "pnl": pnl_block, "sync": sync_block,
            "fees_month": fees_month_block, "positions": positions_block,
            "risk_controls_enabled": risk_controls_enabled,
            "updated_at": int(now_s),
        })

    @app.get("/api/me/fees")
    def me_fees(period: str = "this_month", address: str = Depends(_require_session)):
        """費用明細 tab（R2·B 重構）的期間切換資料源：`this_month`／`last_month`／
        `all`，逐日聚合與 `/api/me/dashboard` 的 `fees_month` 同一支計算層
        （`_dashboard_fees_period`／`_fee_daily_bars`，同一個 `_fetch_period_fills`
        資料源，不另拼第二來源；R-A C2/C3 修法後改一次分頁抓取，call 數 ∝
        筆數/2000，不再 ∝ 天數）。沿 `/api/me/fills` 的認證與快取慣例：per-
        (account_id, period) 快取、上游失敗一律 503、不回退自家 DB。"""
        if period not in _FEES_PERIODS:
            raise HTTPException(status_code=422,
                                detail=f"period 僅支援 {'/'.join(_FEES_PERIODS)}")
        account_id = derive_account_id(address)
        ref = FollowerRef(account_id=account_id, user_address=address,
                          builder_address=cfg.builder_address, network=cfg.network)
        now_s = now_fn()
        try:
            result = _dashboard_fees_period(ref, hl, now_s, period)
        except Exception as e:  # noqa: BLE001 — 上游任何失敗一律轉譯 503，不讓例外細節外洩
            logger.error("費用明細查詢失敗 address=%s period=%s: %s",
                        address.lower(), period, e)
            raise HTTPException(status_code=503,
                                detail="費用明細查詢暫時不可用，請稍後重試") from e
        return jsonable(result)

    # ---------- /api/me/fills、/api/me/authorizations（M3 round2 Task 7） ----------
    # 「成交記錄・授權歷程」tab 的唯一資料源：兩者都**直取 Hyperliquid**（userFillsByTime
    # ／explorer userDetails），結構上不讀自家 DB（per 使用者要求，見 plan 檔尾裁決）。
    # per-address 60s TTL 快取（防連點打爆 HL）；上游失敗一律 503，不 fallback。
    _ME_HL_CACHE_TTL_S = 60.0
    # ⭐ [W1] 2026-08-29 opus 審查：key 必須是 `(addr, days)`，不能只有 `addr`——
    # 同一個登入地址切換 `days`（例如 7 → 30）會撞到同一格快取，回傳錯誤天數
    # 範圍的成交明細卻不會出現任何錯誤（正確性缺陷，不是效能問題）。
    _fills_cache: dict[tuple[str, int], tuple[float, list]] = {}
    _fills_cache_lock = threading.Lock()
    _authorizations_cache: dict[str, tuple[float, list]] = {}
    _authorizations_cache_lock = threading.Lock()

    @app.get("/api/me/fills")
    def me_fills(days: int = 30, address: str = Depends(_require_session)):
        """登入地址近 `days` 天的成交明細（`userFillsByTime`，唯讀直取 HL）。
        `days` 須介於 1~90（沿 `/api/public/leaderboard` 的 422 慣例）；上游查詢
        失敗 → 503，不回退自家 DB。per-(address, days) 60s TTL 快取（見 [W1]：
        key 缺 `days` 會讓不同天數撞同一格快取）。"""
        if not (1 <= days <= 90):
            raise HTTPException(status_code=422, detail="days 須介於 1 到 90")
        now = now_fn()
        addr = address.lower()
        key = (addr, days)
        with _fills_cache_lock:
            cached = _fills_cache.get(key)
        if cached is not None and now - cached[0] < _ME_HL_CACHE_TTL_S:
            return {"fills": cached[1]}
        end = datetime.fromtimestamp(now, timezone.utc)
        start = end - timedelta(days=days)
        try:
            fills = hl.get_fills_detail(address, start, end)
        except Exception as e:  # noqa: BLE001 — 上游任何失敗一律轉譯 503，不讓例外細節外洩
            logger.error("成交記錄查詢失敗 address=%s: %s", addr, e)
            raise HTTPException(status_code=503,
                                detail="成交記錄查詢暫時不可用，請稍後重試") from e
        with _fills_cache_lock:
            _fills_cache[key] = (now, fills)
        return {"fills": fills}

    @app.get("/api/me/authorizations")
    def me_authorizations(address: str = Depends(_require_session)):
        """登入地址的授權歷程（explorer `userDetails`，只留 approveAgent／
        approveBuilderFee，唯讀直取 HL）。上游查詢失敗 → 503，不回退自家 DB。
        per-address 60s TTL 快取；explorer 回應可能數千筆 txs，過濾＋裁切至前
        100 筆後才回前端。"""
        now = now_fn()
        addr = address.lower()
        with _authorizations_cache_lock:
            cached = _authorizations_cache.get(addr)
        if cached is not None and now - cached[0] < _ME_HL_CACHE_TTL_S:
            return {"authorizations": cached[1]}
        try:
            detail = hl.user_details(address)
        except Exception as e:  # noqa: BLE001 — 上游任何失敗一律轉譯 503，不讓例外細節外洩
            logger.error("授權歷程查詢失敗 address=%s: %s", addr, e)
            raise HTTPException(status_code=503,
                                detail="授權歷程查詢暫時不可用，請稍後重試") from e
        txs = detail.get("txs") if isinstance(detail, dict) else None
        authorizations = filter_authorizations(txs, limit=100)
        with _authorizations_cache_lock:
            _authorizations_cache[addr] = (now, authorizations)
        return {"authorizations": authorizations}

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
