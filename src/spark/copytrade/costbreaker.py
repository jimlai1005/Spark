"""成本熔斷器：對「客戶的資金被交易磨損的速度」設上限，**與 leader 是誰無關**。

計畫：`docs/superpowers/plans/2026-07-19-cost-circuit-breaker.md`（D1-D8）。

它擋三種「合法值域內的濫用」——白名單對這三種全都零防護：
1. **leader 抖動**：在白名單內的合法 leader 之間高頻切換，每次收斂都付真實 taker 成本。
2. **白名單內的自營對敲**：策劃的 leader 若由營運方控制，用對敲榨 builder fee——
   依定義落在白名單內。
3. **換手率爆炸的合法 leader**：沒有惡意，策略就是這樣，但客戶的錢被手續費磨光。

⚠️ **激勵錯位是本機制存在的根本理由**：我方從客戶的每一筆成交賺 builder fee，
**從磨損中獲利的一方正是被指定守住磨損的一方**。所以這道閘門是結構性的、預設開啟，
不靠營運端自律。放寬門檻請走 CopySettings（有註記與依據），不要在本模組加後門。

## D1 度量：換手率（成交名目 ÷ 權益），不是手續費
手續費資料不完整（`UserFill.builder_fee` 只有我方 builder 抽成，HL 自身費率與滑價
成本不在裡面），拿不完整的分子去算「成本佔比」會**系統性低估**。換手率的兩個數
都拿得到且可信：成交名目來自 `get_user_fills`、權益來自 `get_account_value`。
次要度量：成交筆數（抓高頻小額對敲——大帳戶碎單洗量的名目佔比可以很小）。

## D2 ⭐⭐ 同基準（工程原則 1，本專案已犯過兩次的坑）
分母**必須**是 `get_account_value()`（perp accountValue，**與 sizing 用的是同一個數**）。
本模組因此收 `EquityView` 而非裸 `Decimal`——引擎唯一的合法產生者是
`equity.perp_equity_view()`，它在**同一輪、同一次讀取**內取得該值並同時餵給回撤判定，
分子分母不會一個用本輪值一個用上輪值。
❌ 絕對不要改成 `get_equity_view()`（portfolio，含 spot）、`get_account_state()`、
或 spot+perp 總和：分母灌水 = 換手率被低估 = 保護靜默失效。
（本專案的實盤事故：混源比較產生幻影 41.9% 回撤而停掉交易；F1 則是分母含 spot
使回撤保護被稀釋到接近失效。兩次都不是「算錯」，是「兩邊不同源」。）

## D3 ⭐ 無狀態：每輪從 fills 重算，**不維護帳本**
本專案已因「帳本遺失」出過兩次事故（一次 Critical）。換手率可以從交易所的 fills
完全重建，所以不要建帳本：重啟不重置預算、沒有帳本可遺失、沒有 fail-closed 分支。

**唯一的持久狀態是 `cost_breaker.json`，它刻意不是帳本**——只存「觸發事件的時間戳」
與「上一輪是否處於觸發狀態」，兩者都無法從 fills 重建（它們是我方的判定歷史，
不是交易所事實）。它遺失的後果被刻意限縮成：累犯計數歸零、stale 回退基準歸零；
**主閘（停開新倉）完全不受影響**，因為換手率下一輪就從 fills 重算出來。

## D4 滾動窗，不是日曆日
日曆日在午夜重置，攻擊者可以跨午夜規避（23:50 衝一半、00:10 衝另一半，兩個日曆日
都不超標）。窗以 **fill 時間戳**為準，長度 `WINDOW_S`（24h）。

## D5 ⭐ 觸發後：停止開新倉，**reduce-only 一律放行**
**絕不把客戶困在部位裡——這是硬規則。** 本模組不做強制平倉（那是回撤熔斷器的
職責，兩者不要混）。閘門由呼叫端接進 `orders._build_desired` 與
`positions.sync_positions` 的 `no_new_exposure` 參數，兩處都只擋「增加曝險」的動作。

## D6 自動恢復 ＋ 累犯升級
滾動窗掉回門檻以下 → 自動恢復（否則一次尖峰會讓客戶停到有人發現為止），恢復時
也發告警留痕。**但**純自動恢復會讓濫用穩定在門檻上持續進行，所以滾動 24h 內觸發
達 `cost_breach_escalate_count` 次 → `escalate=True`，呼叫端 trip kill switch。

觸發次數是**邊緣觸發**（episode）不是輪次計數：連續 10 輪都超標算 1 次，
掉回門檻下再超標才算第 2 次。否則 60s 一輪的引擎會在 3 分鐘內自動升級到 kill switch。

## D7 門檻預設值
見 `CopySettings.cost_max_turnover_24h` 的註記（含真實資料、推導、與未校準標註）。

## D8 與既有閘門的優先序
`leader 撤銷` > `kill switch（回撤）` > `成本熔斷器`。成本熔斷器是最輕的一道
（只停開新倉），**不得覆蓋前兩者的行為**——接線上由 `loop.run_cycle` 把本模組排在
tripped 短路與回撤判定**之後**保證：前兩者一旦成立就已經 return，走不到這裡。

## 失敗語意（工程原則 2/3/5）
fills 查詢失敗屬 **transient**：經 resilience 邊界重試（讀取冪等），耗盡後**沿用
上一輪判定**（`stale=True`）＋ critical 告警，**絕不因為查不到就放行**。
理由：查不到 fills 的當下，我們對「客戶正在被磨損多快」一無所知，而放行的代價
是繼續開新倉（增加曝險與成本），擋下的代價只是暫停開新倉（平倉仍放行、不困住客戶）。
兩者不對稱，所以往「維持上一輪判定」倒——上一輪沒觸發就照常，上一輪觸發就繼續擋。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from spark.copytrade.config import CopySettings
from spark.copytrade.notifier import Notifier
from spark.exchange.base import EquityView, UserFill
from spark.resilience import run as resilient_run

logger = logging.getLogger(__name__)

STATE_RELPATH = Path("var/copytrade/cost_breaker.json")
WINDOW_S = 24 * 3600  # D4：滾動 24 小時（不是日曆日）

# 觸發原因標籤（進 CostStatus.reasons 與告警文字）
REASON_TURNOVER = "turnover"
REASON_FILL_COUNT = "fill_count"


@dataclass(frozen=True)
class CostStatus:
    """單輪成本判定結果。

    `breached=True` ⇒ 呼叫端必須停止開新倉（reduce-only 仍放行）。
    `escalate=True` ⇒ 呼叫端必須 trip kill switch（累犯，需人工 re-arm）。
    """
    turnover: Decimal          # 成交名目 ÷ 權益；權益 <= 0 時為 0（見 check_cost）
    notional: Decimal          # 窗內成交名目總和（sz × px）
    equity: Decimal            # ⭐ perp accountValue，與分子同一輪同一次讀取（D2）
    fill_count: int            # 窗內成交筆數
    breached: bool
    reasons: tuple[str, ...] = ()
    stale: bool = False        # True＝fills 查不到，本結果沿用上一輪判定，非本輪實算
    escalate: bool = False     # True＝滾動窗內觸發次數達標，呼叫端應 trip kill switch
    breach_count: int = 0      # 滾動窗內的觸發**次數**（episode 數，含本次）
    enabled: bool = True       # False＝兩項門檻皆停用，本輪未查 fills（向後相容路徑）


def _disabled_status() -> CostStatus:
    """完全停用時的零值結果——不查 fills、不讀寫狀態檔、不發告警。"""
    return CostStatus(turnover=Decimal("0"), notional=Decimal("0"), equity=Decimal("0"),
                      fill_count=0, breached=False, enabled=False)


def is_enabled(settings: CopySettings) -> bool:
    """兩項門檻皆 <= 0 ⇒ 完全停用（行為與加入本功能前完全一致，向後相容）。"""
    return settings.cost_max_turnover_24h > 0 or settings.cost_max_fills_24h > 0


# ═══════════════════════════════════════════════════════════════════════
# 純函式層（無 IO，供測試與 evaluate_cost 使用）
# ═══════════════════════════════════════════════════════════════════════


def window_fills(fills: Iterable[UserFill], *, now_s: float,
                 window_s: int = WINDOW_S) -> list[UserFill]:
    """濾出滾動窗內的 fills（D4，以 fill 時間戳為準）。

    `0 <= age` 不可省（沿用 equity.py 的同一道防護）：時鐘前跳／NTP 校正會產生
    未來戳，`now - ts` 為負而恆滿足上界，該筆將永不出窗——攻擊者若能影響時間戳，
    未來戳會讓一筆成交永久佔住換手率額度。未來戳一律丟棄。

    無時區資訊的 datetime 一律當 UTC（HL adapter 產生的都帶 tz，這是防呼叫端手造）。
    """
    out: list[UserFill] = []
    for f in fills:
        t = f.time if f.time.tzinfo is not None else f.time.replace(tzinfo=timezone.utc)
        age = now_s - t.timestamp()
        if 0 <= age <= window_s:
            out.append(f)
    return out


def check_cost(ev: EquityView, fills: Sequence[UserFill], settings: CopySettings, *,
               now_s: float, window_s: int = WINDOW_S) -> CostStatus:
    """純函式成本判定。**分子分母同基準的唯一責任點**（D2）。

    分母 `ev.current` 必須來自 `equity.perp_equity_view()`（perp accountValue）——
    見模組 docstring 的 D2 節。本函式不自己取權益，正是為了讓「哪一輪、哪一次讀取」
    這件事只有一個答案：呼叫端拿到的那一個 EquityView。

    權益 <= 0（新帳戶／資料異常）⇒ 換手率無定義，該項判定停用（turnover=0、
    不觸發），但**成交筆數項照常判定**——後者不需要分母。「無資料」不是「安全」，
    故 evaluate_cost 會在此情形發 warn。

    門檻語意 `>` 嚴格大於（對齊 killswitch.check_drawdown，兩道熔斷器的門檻讀法
    必須一致，否則同一份文件寫的「上限」在兩處是兩個意思）。
    """
    win = window_fills(fills, now_s=now_s, window_s=window_s)
    notional = sum((f.sz * f.px for f in win), Decimal("0"))
    equity = ev.current
    turnover = (notional / equity) if equity > 0 else Decimal("0")

    reasons: list[str] = []
    if settings.cost_max_turnover_24h > 0 and equity > 0 \
            and turnover > settings.cost_max_turnover_24h:
        reasons.append(REASON_TURNOVER)
    if settings.cost_max_fills_24h > 0 and len(win) > settings.cost_max_fills_24h:
        reasons.append(REASON_FILL_COUNT)

    return CostStatus(
        turnover=turnover, notional=notional, equity=equity, fill_count=len(win),
        breached=bool(reasons), reasons=tuple(reasons),
    )


# ═══════════════════════════════════════════════════════════════════════
# 判定歷史（**不是帳本**，見模組 docstring D3）
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BreachLog:
    """觸發事件歷史。刻意只存無法從 fills 重建的兩件事（見 D3）。

    read_error 與「檔案不存在」分開（沿用 equity.SampleCoverage 的慣例）：
    讀不到 ≠ 沒有歷史。讀不到時把累犯計數當 0 會讓「檔案權限壞掉」變成
    「累犯保護靜默消失」，故讀取失敗要告警。
    """
    breaches: tuple[float, ...] = ()   # 各次觸發（episode 起點）的 epoch 秒
    active: bool = False               # 上一輪是否處於觸發狀態（邊緣觸發用）
    read_error: bool = False


def load_log(root: Path) -> BreachLog:
    """讀判定歷史。不存在 → 空（首次啟動）；壞檔／讀不到 → 空 + read_error。"""
    path = root / STATE_RELPATH
    if not path.exists():
        return BreachLog()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("成本熔斷器狀態讀取失敗（%s）: %r", path, e)
        return BreachLog(read_error=True)
    if not isinstance(raw, dict):
        return BreachLog(read_error=True)
    items = raw.get("breaches")
    breaches: list[float] = []
    if isinstance(items, list):
        for it in items:
            try:
                breaches.append(float(it))
            except (TypeError, ValueError):
                continue
    return BreachLog(breaches=tuple(breaches), active=bool(raw.get("active", False)))


def save_log(root: Path, log: BreachLog) -> None:
    """原子寫（tmp+os.replace，同目錄；tmp 檔名帶 pid——沿用 equity._save 的理由：
    固定 tmp 名在多 follower 誤共用 state root 時會互相覆寫成撕裂檔）。

    **絕不拋例外**：本函式在 evaluate_cost 的判定之後被呼叫，寫不進去不能反過來
    讓引擎當掉或讓本輪的閘門失效——閘門的依據是本輪從 fills 算出的換手率，不是
    這個檔。寫入失敗只影響累犯計數的持久性，故 warn 並繼續（工程原則 3 的比例原則：
    大聲，但不要用「留不下證據」去換掉「保護本身」）。
    """
    path = root / STATE_RELPATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(
            {"breaches": list(log.breaches), "active": log.active}, ensure_ascii=False))
        os.replace(tmp, path)
    except OSError as e:  # noqa: BLE001 —— 見 docstring：不得讓留痕失敗擋掉保護
        logger.warning("成本熔斷器狀態寫入失敗（%s）: %r——累犯計數不會保存", path, e)


def reset_log(root: Path) -> None:
    """清空判定歷史。人工 re-arm 流程用（與 equity.reset_samples 同語意：
    否則 re-arm 後窗內的舊觸發記錄仍在，下一次觸發就立刻再升級到 kill switch）。
    絕不拋例外——同 reset_samples 的理由（它被呼叫的位置在告警之前）。"""
    try:
        (root / STATE_RELPATH).unlink(missing_ok=True)
    except OSError as e:  # noqa: BLE001
        logger.warning("清空 %s 失敗: %r", STATE_RELPATH, e)


# ═══════════════════════════════════════════════════════════════════════
# 引擎接入點
# ═══════════════════════════════════════════════════════════════════════


def evaluate_cost(adapter, address: str, ev: EquityView, settings: CopySettings,
                  notifier: Notifier, root: Path, *,
                  now_fn=time.time, sleep_fn=time.sleep,
                  window_s: int = WINDOW_S) -> CostStatus:
    """單輪成本判定＋狀態轉換＋告警。**引擎一律用本函式，不要直呼 check_cost**
    ——自動恢復告警、累犯升級、stale 回退三件事都在這裡，不靠呼叫端記得補
    （比照 killswitch.evaluate 對 check_drawdown 的關係）。

    ⭐ `ev` 必須是本輪 `equity.perp_equity_view()` 的產出（D2 同基準，見模組
    docstring）。不要在這裡自己再讀一次權益：那會變成「分子這次讀、分母那次讀」，
    同源但不同輪，仍然是混基準。

    Args:
        adapter: 讀側 ExchangeAdapter（只用 `get_user_fills`）。
        address: 我方地址（**不是 leader**——磨損發生在客戶帳上）。
        ev: 本輪 perp EquityView；`ev.current` 是分母。
        root: 狀態根（判定歷史掛在其下）。

    Returns:
        CostStatus。`breached` 接 `no_new_exposure`；`escalate` 接 trip kill switch。
    """
    if not is_enabled(settings):
        return _disabled_status()

    now_s = float(now_fn())
    log = load_log(root)
    if log.read_error:
        notifier.critical(
            "costbreaker",
            "成本熔斷器狀態檔讀取失敗——累犯升級計數已歸零（停開新倉的主閘不受影響，"
            "換手率每輪從 fills 重算）；請檢查狀態根權限",
            dedup_key="cost_state_read_error",
        )

    # ── 1. 取 fills（transient：重試耗盡 → 沿用上一輪判定，絕不放行）──────
    end = datetime.fromtimestamp(now_s, timezone.utc)
    start = end - timedelta(seconds=window_s)
    try:
        fills = resilient_run(
            lambda: adapter.get_user_fills(address, start, end),
            what="成本熔斷器取成交", idempotent=True, sleep_fn=sleep_fn)
    except Exception as e:  # noqa: BLE001 —— 安全關鍵：查不到不等於安全
        notifier.critical(
            "costbreaker",
            f"成交查詢失敗（重試耗盡或語意錯誤）: {e!r}——無法計算換手率，"
            f"沿用上一輪判定（{'停止開新倉' if log.active else '照常'}）；"
            f"reduce-only 平倉不受影響",
            dedup_key="cost_fills_unavailable",
        )
        return CostStatus(turnover=Decimal("0"), notional=Decimal("0"), equity=ev.current,
                          fill_count=0, breached=log.active,
                          reasons=("stale",) if log.active else (),
                          stale=True,
                          breach_count=len(_prune(log.breaches, now_s, window_s)))

    status = check_cost(ev, fills, settings, now_s=now_s, window_s=window_s)

    if ev.current <= 0 and settings.cost_max_turnover_24h > 0:
        # 「無資料」不是「安全」（比照 killswitch.evaluate 的 degenerate warn）。
        notifier.warn(
            "costbreaker",
            f"權益 degenerate（current={ev.current}）——換手率判定停用，"
            f"本輪僅以成交筆數把關（{status.fill_count} 筆）",
            dedup_key="cost_equity_degenerate",
        )

    # ── 2. 狀態轉換（邊緣觸發：連續超標算一次，見 D6）────────────────────
    breaches = _prune(log.breaches, now_s, window_s)
    active = log.active
    if status.breached and not active:
        breaches = (*breaches, now_s)
        active = True
        notifier.critical(
            "costbreaker",
            f"**成本熔斷觸發**：{_describe(status, settings)}"
            f"｜已停止開新倉，**reduce-only 平倉仍放行**（不會把你困在部位裡）"
            f"｜滾動窗內第 {len(breaches)}/{settings.cost_breach_escalate_count} 次"
            f"，達次數上限將鎖死交易並需人工 re-arm",
            dedup_key="cost_breach",
        )
    elif not status.breached and active:
        active = False
        notifier.warn(
            "costbreaker",
            f"成本熔斷自動恢復：{_describe(status, settings)}｜已恢復開新倉"
            f"｜滾動 24h 內累計觸發 {len(breaches)} 次",
            dedup_key="cost_recovered",
        )

    escalate = bool(breaches) and len(breaches) >= max(1, settings.cost_breach_escalate_count)
    save_log(root, BreachLog(breaches=breaches, active=active))

    return CostStatus(
        turnover=status.turnover, notional=status.notional, equity=status.equity,
        fill_count=status.fill_count, breached=status.breached, reasons=status.reasons,
        stale=False, escalate=escalate, breach_count=len(breaches),
    )


def _prune(breaches: Iterable[float], now_s: float, window_s: int) -> tuple[float, ...]:
    """只保留滾動窗內的觸發記錄（D4；未來戳一併丟棄，同 window_fills 的理由）。"""
    return tuple(ts for ts in breaches if 0 <= now_s - ts <= window_s)


def _describe(status: CostStatus, settings: CopySettings) -> str:
    """告警文字：目前換手率、門檻、窗口、觸發了哪一項（D5 要求四項齊全）。"""
    hit = "＋".join(
        {REASON_TURNOVER: "換手率", REASON_FILL_COUNT: "成交筆數"}.get(r, r)
        for r in status.reasons) or "無"
    return (f"觸發項={hit}｜換手率 {status.turnover:.2f}×"
            f"（成交名目 {status.notional:.2f} ÷ perp 權益 {status.equity:.2f}）"
            f"／上限 {settings.cost_max_turnover_24h}×"
            f"｜成交 {status.fill_count} 筆／上限 {settings.cost_max_fills_24h}"
            f"｜滾動窗 {WINDOW_S // 3600}h")
