"""回撤 kill switch（spec T3.1）：鎖檔 → 撤全部掛單 → reduce-only 全平 → 告警。

設計拍板（不得放寬）：
- `flatten_on_breach` 預設開（CopySettings，拍板 #2）。
- re-arm＝**人工**刪除 ARM_FILE（`var/copytrade/killswitch.tripped`）。
  ⚠️ **2026-07-30 使用者裁決放寬**：新增 `auto_rearm_if_cooled_down()`，冷靜期
  （`COPY_RISK_COOLDOWN_HOURS`，預設 12 小時）屆滿後自動刪 ARM 檔恢復跟單。
  理由是保留客戶該有的權力——保護要提供，但不該把客戶鎖在門外。放寬僅限該函式，
  且它對三種情形仍然 fail-closed（leader 撤銷、時間戳讀不到、冷靜期設為 0）；
  `trip()` 本身完全不變。詳見該函式 docstring。
  ⚠️ **2026-07-30 第二次放寬**：新增 `manual_rearm()`，客戶以錢包簽章授權即可
  **立即**解除鎖定（不必等冷靜期）。驗章在 `spark.filet.risk_settings_apply`，
  ARM 檔的判定與刪除全在本模組（誰擁有鎖，誰負責開鎖）。兩條恢復路徑共用同一份
  「哪些 reason 可以恢復」判定（`rearm_allowed_for`），且各自都要求「請求晚於熔斷」
  或「冷靜期已過」——`trip()` 仍然完全不變。
- 門檻語意對照線上引擎 hl-copytrader/main.py:176：`drawdown > max` 嚴格大於才觸發。
- **Lock-first**：trip 進場先寫 preliminary ARM 檔再動手——flatten 中途 process 被殺，
  重啟後 is_tripped 仍為 True，絕不因鎖檔沒落地而照常交易。鎖不住（ARM 寫入 OSError）
  → critical＋上拋、不動手：沒鎖就平倉，重啟後引擎會照常跟單重新開倉，比不平更糟。

主迴圈接入接口（Task 12 的 run_cycle 實作；本模組只提供積木）：
    cycle 開頭 `is_tripped(root)` → True 則本輪只讀報狀態（＋每小時一次 critical 提醒），
    不做任何交易動作；False 則 `evaluate(perp_equity_view(adapter, addr, root), settings, notifier)`
    （**必須用 evaluate() 而非直呼 check_drawdown**——degenerate equity 的 warn 在
    evaluate 內結構性保證）→ breached 且 settings.flatten_on_breach → `trip(...)`。

重試語意（工程原則 2/5）：trip 內的冪等呼叫（get_open_orders 讀取、reduce-only 平倉）
一律經 `spark.resilience.run` 單一邊界（重用，不另建 try/except 叢林；close 整包重試
會連帶重試其內部的 mid 讀取）。重試耗盡或語意錯誤即終態——記錄、critical、繼續
下一個動作，此層絕不無限重試；殘留暴險交由人工處置（ARM_FILE 已鎖死交易）。

持久證據層（M1）：trip 內每則 critical 同步 append 至 `var/copytrade/alerts.log`
（時間戳＋訊息）——「大聲」不能只等於 Telegram uptime，通知端掛掉時本地仍有證據。

緊急平倉滑價（2026-07-19 已裁決並實作）：本模組平倉一律以 `emergency=True` 呼叫
`ExecutorPort.close_reduce_only`，走 `CopySettings.flatten_slippage`（預設 0.30，
`COPY_FLATTEN_SLIPPAGE` 可調）而非一般跟單用的 `slippage`（0.05）。理由：一般平倉
未成交只是下一輪再試，緊急平倉未成交＝保護整個失效、部位繼續曝險。IOC 限價語意是
「可接受的最差價」而非「成交在該價」——寬頻寬只確保跳空時仍能出場，不等於接受該幅度虧損。
仍可能未成交（如市場暫停），該情形如實記入 failures 並 critical。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from spark.copytrade.config import CopySettings
from spark.copytrade.costbreaker import reset_log as reset_cost_log
from spark.copytrade.equity import reset_samples
from spark.copytrade.executor import ExecutorPort
from spark.copytrade.notifier import Notifier
from spark.exchange.base import EquityView, Position
from spark.resilience import run as resilient_run

logger = logging.getLogger(__name__)

ARM_FILE_RELPATH = Path("var/copytrade/killswitch.tripped")
ALERTS_LOG_RELPATH = Path("var/copytrade/alerts.log")


@dataclass(frozen=True)
class DrawdownStatus:
    current: Decimal
    peak: Decimal
    drawdown_pct: Decimal
    breached: bool


def check_drawdown(ev: EquityView, max_dd_pct: Decimal) -> DrawdownStatus:
    """純函式回撤判定。drawdown = (peak - current) / peak；`drawdown > max` 才觸發。

    同源不變量（工程原則 1）：ev.current 與 ev.recent_peak 必須同源同單位——引擎路徑
    由 `perp_equity_view()` 保證（同一欄位 perp accountValue 的即時值與滾動樣本最大值）。
    呼叫端不得拿不同來源（例如一邊 portfolio 一邊 marginSummary）拼一個 EquityView 進來。

    peak <= 0（新帳戶無歷史、或 portfolio 回應異常）→ drawdown=0、breached=False；
    這是「無資料」不是「安全」——warn 由 `evaluate()` 結構性保證。引擎呼叫端一律走
    evaluate()，不要直呼本函式（本函式保持純函式、不做 IO，供測試與 evaluate 使用）。
    """
    if ev.recent_peak <= 0:
        return DrawdownStatus(current=ev.current, peak=ev.recent_peak,
                              drawdown_pct=Decimal("0"), breached=False)
    dd = (ev.recent_peak - ev.current) / ev.recent_peak
    return DrawdownStatus(current=ev.current, peak=ev.recent_peak,
                          drawdown_pct=dd, breached=dd > max_dd_pct)


def evaluate(ev: EquityView, settings: CopySettings, notifier: Notifier,
             *, coverage=None, lifetime_peak: Decimal | None = None) -> DrawdownStatus:
    """回撤判定＋degenerate／覆蓋不足／慢速底線的結構性告警出口。

    **Task 12 run_cycle 必須用本函式，而非直呼 check_drawdown**——三種告警都在這裡發，
    不靠呼叫端記得補：
    1. peak<=0 的 degenerate warn（dedup_key="equity_degenerate"）
    2. 樣本覆蓋不足 → **critical**（findings F1/C1：空樣本會讓 drawdown 恆 0，
       「無資料」偽裝成「無回撤」；此時回撤保護實質不存在，必須大聲）
    3. 慢速絕對底線（findings F1/C2）：7 天窗只量虧損速度，慢跌可繞過；
       以 lifetime_peak 為基準的回撤超過 max_total_drawdown_pct 即判 breached
    """
    status = check_drawdown(ev, settings.max_drawdown_pct)
    if ev.recent_peak <= 0:
        notifier.warn(
            "killswitch",
            f"權益資料 degenerate（peak={ev.recent_peak}）——回撤判定停用"
            f"（breached=False），請檢查資料源",
            dedup_key="equity_degenerate",
        )
    if coverage is not None and not coverage.sufficient:
        notifier.critical(
            "killswitch",
            f"**回撤保護尚未生效**：樣本覆蓋 {coverage.count} 筆／最舊 "
            f"{coverage.oldest_age_s / 60:.0f} 分鐘"
            f"{'（樣本檔讀取失敗！）' if coverage.read_error else ''}"
            f"——熔斷在覆蓋足夠前不會觸發，請勿據 drawdown 數字判斷風險",
            dedup_key="equity_coverage_insufficient",
        )
    if (lifetime_peak is not None and settings.max_total_drawdown_pct > 0
            and lifetime_peak > 0):
        total_dd = (lifetime_peak - status.current) / lifetime_peak
        if total_dd > settings.max_total_drawdown_pct:
            notifier.critical(
                "killswitch",
                f"**慢速絕對底線觸發**：自開始跟單以來回撤 {total_dd}"
                f"（高水位 {lifetime_peak} → {status.current}）"
                f"超過上限 {settings.max_total_drawdown_pct}",
                dedup_key="equity_total_drawdown",
            )
            status = replace(status, drawdown_pct=total_dd, breached=True)
    return status


def is_tripped(root: Path) -> bool:
    """ARM_FILE 存在即 tripped。root 為專案根（ARM_FILE 相對路徑掛在其下）。"""
    return (root / ARM_FILE_RELPATH).exists()


# ⭐ 可自動恢復的觸發原因（2026-07-30 使用者裁決，見下方 auto_rearm_if_cooled_down）。
# `""`＝回撤觸發（trip 的預設 reason）。**`leader_revoked` 刻意不在此列**。
# ⚠️ 這份清單同時是**客戶自助解鎖**（`manual_rearm`）的判定依據——兩條恢復路徑
# 共用同一個常數與同一個述詞（`rearm_allowed_for`），不各自抄一份：抄一份的下場是
# 有人在其中一條路徑加了新的 reason，另一條卻仍然放行／仍然擋著，而「leader 被撤銷
# 卻能被恢復」這個方向是 fail-open（引擎回去跟一個已撤銷的 leader）。
_AUTO_REARM_REASONS = ("", "cost_breach")


def rearm_allowed_for(reason: object) -> bool:
    """這個觸發原因可否被恢復（自動冷靜期與客戶自助解鎖**共用**的唯一判定）。

    非字串（payload 被手改成別的型別）一律回 False：判不出來就不恢復，方向與
    「ARM payload 讀不到就不恢復」一致（讀不到 ≠ 可以解鎖）。
    """
    return isinstance(reason, str) and reason in _AUTO_REARM_REASONS


def _read_arm_payload(arm_path: Path) -> tuple[str, str, float] | None:
    """ARM 檔 → `(tripped_at 原字串, reason, tripped_at 的 epoch 秒)`；讀不出來回 None。

    **單一解析點**（兩條恢復路徑共用）：`tripped_at` 是「冷靜期過了沒」與「這筆解鎖
    請求是不是在熔斷之後簽的」兩個比較的基準，兩處各解析一次就會有兩個答案，而
    其中一個答案正好是擋重放的那一半（工程原則 1）。
    """
    try:
        payload = json.loads(arm_path.read_text())
        tripped_at = payload.get("tripped_at")
        reason = payload.get("reason", "")
        return tripped_at, reason, datetime.fromisoformat(tripped_at).timestamp()
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None


def halt_status(root: Path) -> dict | None:
    """目前的熔斷狀態摘要，供心跳發布給營運面板與客戶頁面；未熔斷 → None。

    ⭐ 為什麼要把 `reason` 發布出去（2026-07-30）：客戶頁面上的「立即恢復跟單」是一次
    真實的錢包簽章。`reason="leader_revoked"` 的鎖定**不可**由客戶自助解除
    （`rearm_allowed_for`），前端若不知道原因就只能讓他簽一份注定被拒的請求——
    白費一次簽名，而且失敗訊息出現在他按下之後，看起來像系統壞了。
    `resumable` 由 `rearm_allowed_for` 導出（**不是**前端自己比對字串），
    這樣「哪些原因可恢復」永遠只有一個定義點。

    ⚠️ 讀不到 ARM payload（檔在但內容壞掉）→ 回 `resumable=False` ＋ `reason=None`：
    判不出來就不宣稱可以恢復，與兩條恢復路徑的 fail-closed 方向一致。
    payload 內容不含任何簽章材料（見 `trip` 寫入的欄位），可安全進心跳。
    """
    arm_path = root / ARM_FILE_RELPATH
    if not arm_path.exists():
        return None
    parsed = _read_arm_payload(arm_path)
    if parsed is None:
        return {"tripped": True, "reason": None, "tripped_at": None, "resumable": False}
    tripped_at, reason, _ = parsed
    return {"tripped": True, "reason": reason, "tripped_at": tripped_at,
            "resumable": rearm_allowed_for(reason)}


def auto_rearm_if_cooled_down(root: Path, settings: CopySettings, notifier: Notifier,
                              *, now_s: float | None = None) -> bool:
    """冷靜期屆滿 → 自動解除鎖定（刪 ARM 檔）。回傳是否真的解除了。

    ⭐⭐ **這條推翻了本模組原本的拍板**（2026-07-30 使用者裁決）：原設計是
    「re-arm 一律人工，本模組不提供任何自動恢復路徑」。使用者的理由是保留客戶
    該有的權力——保護要提供，但不該把客戶鎖在門外；冷靜期（預設 12 小時）是
    「保護」與「權力」之間的折衷。改動範圍僅限本函式，`trip()` 的行為不變。

    **不會自動恢復的情形（每一條都是刻意的 fail-closed）**：
    - `reason="leader_revoked"`：那是**治理動作**（leader 被平台撤銷），不是客戶
      可以等 12 小時就作廢的風險事件。自動恢復等於讓引擎回去跟一個已撤銷的 leader。
    - ARM payload 讀不到、或 `tripped_at` 解析不出來：**無法證明冷靜期已過**就不
      恢復。「讀不到」不等於「已經過期」（同一條判準見權益讀取失敗的處理）。
    - `cooldown_hours <= 0`：客戶明確選擇「只有我人工才能恢復」。
    - 刪檔失敗（OSError）：維持鎖定並 critical——鎖不掉就不該宣稱已解除。

    冷靜期結束後**不需要**重置權益基準：`trip()` 已呼叫 `reset_samples()`，
    7 天滾動樣本與全期高水位在觸發當下就一併清空了，所以恢復後不會被崩跌前的
    舊 peak 立刻再熔斷。
    """
    arm_path = root / ARM_FILE_RELPATH
    if not arm_path.exists():
        return False
    hours = settings.risk_cooldown_hours
    if hours <= 0:
        return False
    now_s = time.time() if now_s is None else now_s

    def _stay(reason_text: str, key: str) -> bool:
        notifier.warn("killswitch", f"維持鎖定：{reason_text}", dedup_key=key)
        return False

    parsed = _read_arm_payload(arm_path)
    if parsed is None:
        return _stay(
            f"ARM 檔的觸發時間無法解析（{arm_path}），不能證明冷靜期已過——"
            f"自動恢復不執行，需人工刪檔", "rearm_unparseable")
    tripped_at, reason, tripped_s = parsed
    if not rearm_allowed_for(reason):
        return _stay(
            f"觸發原因為 `{reason}`，不屬於可自動恢復的風險事件"
            f"（leader 被撤銷等治理動作只能人工處理）", f"rearm_blocked:{reason}")

    elapsed_h = (now_s - tripped_s) / 3600
    if elapsed_h < float(hours):
        return False        # 還在冷靜期內：安靜等待（tripped 的提醒由呼叫端負責）
    try:
        arm_path.unlink()
    except OSError as e:
        notifier.critical("killswitch",
                          f"冷靜期已滿但 ARM 檔刪除失敗 {arm_path}: {e!r}"
                          f"——維持鎖定，需人工處理")
        _append_alert(root, f"自動恢復失敗（刪檔）: {e!r}")
        return False
    msg = (f"**已自動恢復跟單**：冷靜期 {hours} 小時已滿"
           f"（觸發於 {tripped_at}，實際經過 {elapsed_h:.1f} 小時）。"
           f"權益基準已於觸發當下重置，下一輪起恢復交易動作。")
    notifier.critical("killswitch", msg)
    _append_alert(root, msg)
    return True


def manual_rearm(root: Path, notifier: Notifier, *,
                 requested_at_iso: str) -> bool:
    """客戶**自助**解除熔斷鎖定（刪 ARM 檔）。回傳是否真的解除了。

    ⭐⭐ 呼叫端的責任分工（2026-07-30）：**驗章在外、刪檔在內**。
    `spark.filet.risk_settings_apply.RiskSettingsApplier.consume_unlock_request`
    負責證明「這是錢包主人本人、而且是剛剛簽的」（EIP-191 驗章＋600 秒時效），
    本函式負責 ARM 檔那一側的所有判定與唯一的刪除動作。ARM 檔是本模組的東西——
    第二個模組長出自己的 `arm_path.unlink()` 之後，`trip()` 的鎖語意就有兩個擁有者。

    `requested_at_iso` ＝客戶簽署解鎖請求的時間（記錄的 `issued_at`）。

    **不會解除的情形（每一條都是刻意的 fail-closed，與 auto_rearm 同一套判定）**：
    - ARM 檔不存在：沒有鎖可解（回 False，不告警——這是最常見的正常狀態）。
    - payload 讀不到／`tripped_at` 解析不出來：**無法證明這筆請求晚於熔斷**就不解除。
    - `reason` 不在 `_AUTO_REARM_REASONS`（目前唯一的例外是 `leader_revoked`）：
      那是**治理動作**，不是客戶可以自己作廢的風險事件。共用 `rearm_allowed_for`，
      不複製清單——兩份清單漂移的方向是 fail-open。
    - `requested_at_iso` **不晚於** `tripped_at`：⭐ 這是防重放的那一半。少了它，
      一份熔斷**之前**簽好的解鎖請求（客戶當時只是預先簽著、或攻擊者留存的舊記錄）
      會在下一次熔斷發生的當下把鎖自動打開——保護等於不存在。時效檢查擋不住這一格：
      600 秒內先簽好解鎖、再讓熔斷觸發，在時效上完全合法。
    - 刪檔失敗（OSError）：維持鎖定並 critical——鎖不掉就不該宣稱已解除。

    冷靜期結束後不需要重置權益基準的理由同 `auto_rearm_if_cooled_down`：
    `trip()` 已在觸發當下呼叫 `reset_samples()`／`reset_log()`。
    """
    arm_path = root / ARM_FILE_RELPATH
    if not arm_path.exists():
        return False

    def _stay(reason_text: str, key: str) -> bool:
        notifier.warn("killswitch", f"維持鎖定：{reason_text}", dedup_key=key)
        return False

    parsed = _read_arm_payload(arm_path)
    if parsed is None:
        return _stay(
            f"ARM 檔的觸發時間無法解析（{arm_path}），不能證明這筆解除請求晚於"
            f"熔斷本身——自助解除不執行，需人工處理", "manual_rearm_unparseable")
    tripped_at, reason, tripped_s = parsed
    if not rearm_allowed_for(reason):
        return _stay(
            f"觸發原因為 `{reason}`，不屬於客戶可自助解除的風險事件"
            f"（leader 被撤銷等治理動作只能人工處理）",
            f"manual_rearm_blocked:{reason}")
    try:
        requested_s = datetime.fromisoformat(requested_at_iso).timestamp()
    except (ValueError, TypeError):
        return _stay(
            f"解除請求的簽署時間無法解析（{requested_at_iso!r}）——"
            f"不能證明它晚於熔斷，維持鎖定", "manual_rearm_bad_request_time")
    if requested_s <= tripped_s:
        return _stay(
            f"解除請求簽署於 {requested_at_iso}，**不晚於**熔斷時間 {tripped_at}"
            f"——一份熔斷前就簽好的解除授權不得用來解除這次熔斷", "manual_rearm_stale")
    try:
        arm_path.unlink()
    except OSError as e:
        notifier.critical("killswitch",
                          f"客戶自助解除熔斷失敗（ARM 檔刪除失敗 {arm_path}: {e!r}）"
                          f"——維持鎖定，需人工處理")
        _append_alert(root, f"自助解除失敗（刪檔）: {e!r}")
        return False
    msg = (f"**已依客戶簽章授權解除熔斷鎖定**（熔斷於 {tripped_at}，"
           f"解除請求簽署於 {requested_at_iso}）。權益基準已於熔斷當下重置，"
           f"下一輪起恢復交易動作——這是客戶本人的決定，不是冷靜期屆滿。")
    notifier.critical("killswitch", msg)
    _append_alert(root, msg)
    return True


@dataclass(frozen=True)
class CloseAction:
    coin: str
    is_buy: bool   # 平倉下單方向：平多=False（賣出）、平空=True（買回）
    size: Decimal  # 全量 |szi|


def plan_close_actions(positions: Iterable[Position]) -> tuple[CloseAction, ...]:
    """由持倉產生平倉動作清單（純函式）。szi==0 跳過（無倉可平）。

    trip() 的實際動作與 scripts/panic.py 的 dry-run 預覽**共用本函式**——
    預覽與實際執行不得雙實作（漂移＝預覽騙人）。
    """
    return tuple(
        CloseAction(coin=p.coin, is_buy=p.szi < 0, size=abs(p.szi))
        for p in positions if p.szi != 0
    )


@dataclass(frozen=True)
class FlattenReport:
    cancelled: int               # 成功撤銷的掛單張數
    closed: tuple[str, ...]      # 成功平掉的 coin
    failures: tuple[str, ...]    # close 失敗（ok=False 或例外）的 coin
    arm_file: str                # 寫入的 ARM_FILE 絕對路徑
    orders_not_cancelled: bool = False  # get_open_orders 重試耗盡→掛單清單未知、一張都沒撤


def _append_alert(root: Path, text: str) -> None:
    """持久告警檔（M1 持久證據層）：critical 同步落地本地檔。
    寫失敗不擋主流程（平倉優先於留證據），但 stderr 印出，不靜默。"""
    try:
        path = root / ALERTS_LOG_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {text}\n")
    except OSError as e:
        print(f"alerts.log 寫入失敗: {e!r}｜原訊息: {text}", file=sys.stderr)


def count_alerts(path) -> int | None:
    """告警記錄的行數；**讀不到 → None（不是 0）**。檔案不存在 → 0。

    ⭐ 落在寫端旁邊（`_append_alert` 是唯一的寫入者）且**全 repo 只有這一份**：
    兩個讀端各數一次會漂移。營運面板有兩條取得這個數字的路徑——filet-api 直讀
    狀態根，以及引擎把它放進健康心跳——兩條路徑必須算出同一個數，否則面板上
    「直讀說 3、心跳說 0」而讀者無從判斷該信哪一個（工程原則 1：同源同基準）。

    0 是「沒有任何告警」＝面板上最令人安心的數字。讀不到卻顯示 0，等於在
    「告警檔權限壞掉」的當下告訴操作者一切正常——健康面板謊報健康比沒有面板更危險。
    「檔案不存在」在**讀得到的**狀態根之下確實是 0（引擎從未寫過告警）；狀態根本身
    讀不到或不存在時，呼叫端根本不該走到這裡（見 publicapi.ops.follower_health）。
    """
    p = Path(path)
    if not p.exists():
        return 0        # 檔案不存在＝引擎從未寫過告警，這確實是 0
    try:
        return sum(1 for line in p.read_text().splitlines() if line.strip())
    except OSError as e:
        logger.warning("告警記錄讀取失敗（%s）: %r", p, e)
        return None


def _write_arm(arm_path: Path, payload: dict, notifier: Notifier, root: Path) -> None:
    """寫 ARM 檔（mkdir 與 write 併入同一失敗處理）。OSError → critical＋持久告警後
    上拋——ARM 寫不進去＝交易鎖不住，絕不吞掉（工程原則 3）。"""
    try:
        arm_path.parent.mkdir(parents=True, exist_ok=True)
        arm_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        msg = f"ARM_FILE 寫入失敗 {arm_path}: {e!r}——交易未鎖死！"
        notifier.critical("killswitch", msg)
        _append_alert(root, msg)
        raise


def trip(ex: ExecutorPort, my_positions: dict[str, Position], notifier: Notifier,
         root: Path, status: DrawdownStatus, *, sleep_fn=time.sleep,
         reason: str = "") -> FlattenReport:
    """觸發 kill switch：鎖檔 → 撤單 → 全平 → 覆寫 ARM → 總結告警。順序是紅線，不得重排。

    reason：非回撤觸發時的原因標籤（例如 leader 被白名單撤銷 → `"leader_revoked"`），
    寫進 ARM payload 與總結告警。留空＝回撤觸發（既有行為，payload 不變）。
    沒有它的話，非回撤觸發寫出的 ARM 檔會是一份 `drawdown_pct=0 breached=false` 的
    payload，操作者讀了只會更困惑——鎖死交易的理由必須寫在鎖上。

    0. **Lock-first**：先寫 preliminary ARM（時間戳＋status＋phase=flatten_in_progress）
       ——flatten 中途 process 被殺，重啟後仍 tripped。寫不進去（OSError）→ critical
       ＋上拋、不執行任何交易動作（沒鎖就平倉，重啟後會重新開倉，比不平更糟）。
    1. 撤**全部** resting（避免平倉期間舊掛單成交增加暴險）。get_open_orders 經
       resilience 邊界重試（讀取冪等），耗盡 → **critical**＋orders_not_cancelled=True
       ＋照樣續平倉；單張 cancel 失敗/例外 → critical、繼續撤下一張，絕不中斷。
    2. 逐部位 reduce-only 全量平倉（plan_close_actions 產生動作；is_buy = not is_long）。
       每筆 close 整包經 resilience 邊界（reduce-only 冪等可重試，整包重試連帶重試
       其內部的 mid 讀取）；失敗（ok=False 或例外）→ 記 failures＋逐 coin critical、
       **繼續平下一個**——一個 coin 的失敗不能擋其他部位的平倉。
    3. 覆寫 ARM_FILE 為完整 payload（含 failures/orders_not_cancelled，phase=complete）。
       **部分失敗也要寫**：鎖死交易優先於完美平倉。
    4. 總結 critical（觸發數字＋成功/失敗清單）。

    每則 critical 同步 append 至 alerts.log（持久證據層，見 _append_alert）。
    絕不靜默；此層不在 resilience 邊界之外自行加重試（見模組 docstring）。
    re-arm＝人工刪 ARM_FILE；本函式與本模組不提供刪除路徑。
    sleep_fn：注入給 resilience 重試退避（測試不真睡）。
    """
    arm_path = root / ARM_FILE_RELPATH

    def crit(text: str) -> None:
        notifier.critical("killswitch", text)
        _append_alert(root, text)

    base_payload = {
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "current": str(status.current),
        "peak": str(status.peak),
        "drawdown_pct": str(status.drawdown_pct),
        "breached": status.breached,
        **({"reason": reason} if reason else {}),
    }

    # 0) Lock-first：任何交易動作之前先落鎖檔
    _write_arm(arm_path, {**base_payload, "phase": "flatten_in_progress"}, notifier, root)

    # 1) 撤全部 resting
    cancelled = 0
    orders_not_cancelled = False
    try:
        open_orders = resilient_run(ex.get_open_orders, what="killswitch 取掛單",
                                    idempotent=True, sleep_fn=sleep_fn)
    except Exception as e:  # noqa: BLE001 —— 安全關鍵路徑：讀失敗也要繼續平倉
        crit(f"取得掛單失敗（重試耗盡或語意錯誤）: {e!r}——掛單一張未撤，"
             f"直接平倉；殘留掛單需人工處置")
        open_orders = []
        orders_not_cancelled = True
    for o in open_orders:
        try:
            ok = ex.cancel(o.coin, o.oid)
        except Exception as e:  # noqa: BLE001
            ok = False
            crit(f"撤單例外 {o.coin} oid={o.oid}: {e!r}（繼續撤下一張）")
        else:
            if not ok:
                crit(f"撤單失敗 {o.coin} oid={o.oid}（繼續撤下一張）")
        if ok:
            cancelled += 1

    # 2) 逐部位 reduce-only 全量平倉（與 panic dry-run 共用 plan_close_actions）
    closed: list[str] = []
    failures: list[str] = []
    for act in plan_close_actions(my_positions.values()):
        try:
            res = resilient_run(
                lambda a=act: ex.close_reduce_only(a.coin, a.is_buy, a.size,
                                                   emergency=True),
                what=f"killswitch 平倉 {act.coin}", idempotent=True, sleep_fn=sleep_fn)
            ok, detail = res.ok, res.raw
        except Exception as e:  # noqa: BLE001 —— 邊界重試已耗盡或語意錯誤，即終態
            ok, detail = False, repr(e)
        if ok:
            closed.append(act.coin)
        else:
            failures.append(act.coin)
            crit(f"平倉失敗 {act.coin} size={act.size} is_buy={act.is_buy}: {detail}"
                 f"——殘留暴險，需人工處置")

    # 3) 覆寫 ARM_FILE 為完整 payload（部分失敗也要寫——鎖死交易優先）
    _write_arm(arm_path, {
        **base_payload,
        "phase": "complete",
        "cancelled": cancelled,
        "orders_not_cancelled": orders_not_cancelled,
        "closed": closed,
        "failures": failures,
    }, notifier, root)
    # 清空 perp 權益樣本：否則人工 re-arm 後，崩跌前的舊 peak 仍在 7 天窗內會立刻再熔斷。
    reset_samples(root)
    # 同理清空成本熔斷器的觸發歷史：否則人工 re-arm 後，窗內的舊觸發記錄仍在，
    # 下一次觸發就立刻再度累犯升級、再鎖死一次——人工複查等於沒有效果。
    # 兩者都是「已由人接手處理」的重置點，語意一致（reset_log 同樣絕不拋例外，
    # 它位於 ARM 落地與總結 critical 之間，拋錯會吃掉那則告警）。
    reset_cost_log(root)

    # 4) 總結告警
    crit(
        f"KILL SWITCH TRIPPED{f'（原因：{reason}）' if reason else ''}："
        f"dd={status.drawdown_pct} current={status.current} "
        f"peak={status.peak}｜撤單成功 {cancelled} 張"
        f"{'（掛單清單未知，一張未撤）' if orders_not_cancelled else ''}｜"
        f"平倉成功 {closed or '無'}｜平倉失敗 {failures or '無'}｜"
        f"已鎖死 {arm_path}（re-arm＝人工刪此檔）"
    )
    return FlattenReport(cancelled=cancelled, closed=tuple(closed),
                         failures=tuple(failures), arm_file=str(arm_path),
                         orders_not_cancelled=orders_not_cancelled)
