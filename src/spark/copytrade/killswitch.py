"""回撤 kill switch（spec T3.1）：鎖檔 → 撤全部掛單 → reduce-only 全平 → 告警。

設計拍板（不得放寬）：
- `flatten_on_breach` 預設開（CopySettings，拍板 #2）。
- re-arm＝**人工**刪除 ARM_FILE（`var/copytrade/killswitch.tripped`）；本模組不提供
  任何刪除/自動恢復路徑——tripped 之後只有人能決定重新武裝。
- 門檻語意對照線上引擎 hl-copytrader/main.py:176：`drawdown > max` 嚴格大於才觸發。
- **Lock-first**：trip 進場先寫 preliminary ARM 檔再動手——flatten 中途 process 被殺，
  重啟後 is_tripped 仍為 True，絕不因鎖檔沒落地而照常交易。鎖不住（ARM 寫入 OSError）
  → critical＋上拋、不動手：沒鎖就平倉，重啟後引擎會照常跟單重新開倉，比不平更糟。

主迴圈接入接口（Task 12 的 run_cycle 實作；本模組只提供積木）：
    cycle 開頭 `is_tripped(root)` → True 則本輪只讀報狀態（＋每小時一次 critical 提醒），
    不做任何交易動作；False 則 `evaluate(adapter.get_equity_view(addr), settings, notifier)`
    （**必須用 evaluate() 而非直呼 check_drawdown**——degenerate equity 的 warn 在
    evaluate 內結構性保證）→ breached 且 settings.flatten_on_breach → `trip(...)`。

重試語意（工程原則 2/5）：trip 內的冪等呼叫（get_open_orders 讀取、reduce-only 平倉）
一律經 `spark.resilience.run` 單一邊界（重用，不另建 try/except 叢林；close 整包重試
會連帶重試其內部的 mid 讀取）。重試耗盡或語意錯誤即終態——記錄、critical、繼續
下一個動作，此層絕不無限重試；殘留暴險交由人工處置（ARM_FILE 已鎖死交易）。

持久證據層（M1）：trip 內每則 critical 同步 append 至 `var/copytrade/alerts.log`
（時間戳＋訊息）——「大聲」不能只等於 Telegram uptime，通知端掛掉時本地仍有證據。

已知取捨（待使用者裁決，本次不改碼）：緊急平倉沿用 CopySettings.slippage=5%
（hl trader.py:312 硬編值）。恐慌行情滑價可能超過 5% 導致 IOC 未成交（會如實記入
failures 並 critical）；是否為 kill switch 路徑單獨加寬 slippage，留待人工決定。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from spark.copytrade.config import CopySettings
from spark.copytrade.equity import reset_samples
from spark.copytrade.executor import ExecutorPort
from spark.copytrade.notifier import Notifier
from spark.exchange.base import EquityView, Position
from spark.resilience import run as resilient_run

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

    同源不變量（工程原則 1）：ev.current 與 ev.recent_peak 必須出自**同一次**
    `get_equity_view()` 呼叫（EquityView 型別本身即此契約，見 base.py docstring）——
    呼叫端不得拿不同來源/不同時刻的兩個數字拼一個 EquityView 進來。

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


def evaluate(ev: EquityView, settings: CopySettings, notifier: Notifier) -> DrawdownStatus:
    """回撤判定＋degenerate equity 的結構性告警出口。

    **Task 12 run_cycle 必須用本函式，而非直呼 check_drawdown**——peak<=0 的 warn
    在這裡發（dedup_key="equity_degenerate"，避免每分鐘洗版），不靠呼叫端記得補。
    """
    status = check_drawdown(ev, settings.max_drawdown_pct)
    if ev.recent_peak <= 0:
        notifier.warn(
            "killswitch",
            f"權益資料 degenerate（peak={ev.recent_peak}）——回撤判定停用"
            f"（breached=False），請檢查 portfolio 資料源",
            dedup_key="equity_degenerate",
        )
    return status


def is_tripped(root: Path) -> bool:
    """ARM_FILE 存在即 tripped。root 為專案根（ARM_FILE 相對路徑掛在其下）。"""
    return (root / ARM_FILE_RELPATH).exists()


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
         root: Path, status: DrawdownStatus, *, sleep_fn=time.sleep) -> FlattenReport:
    """觸發 kill switch：鎖檔 → 撤單 → 全平 → 覆寫 ARM → 總結告警。順序是紅線，不得重排。

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
                lambda a=act: ex.close_reduce_only(a.coin, a.is_buy, a.size),
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

    # 4) 總結告警
    crit(
        f"KILL SWITCH TRIPPED：dd={status.drawdown_pct} current={status.current} "
        f"peak={status.peak}｜撤單成功 {cancelled} 張"
        f"{'（掛單清單未知，一張未撤）' if orders_not_cancelled else ''}｜"
        f"平倉成功 {closed or '無'}｜平倉失敗 {failures or '無'}｜"
        f"已鎖死 {arm_path}（re-arm＝人工刪此檔）"
    )
    return FlattenReport(cancelled=cancelled, closed=tuple(closed),
                         failures=tuple(failures), arm_file=str(arm_path),
                         orders_not_cancelled=orders_not_cancelled)
