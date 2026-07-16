"""回撤 kill switch（spec T3.1）：撤全部掛單 → reduce-only 全平 → 檔案鎖死 → 告警。

設計拍板（不得放寬）：
- `flatten_on_breach` 預設開（CopySettings，拍板 #2）。
- re-arm＝**人工**刪除 ARM_FILE（`var/copytrade/killswitch.tripped`）；本模組不提供
  任何刪除/自動恢復路徑——tripped 之後只有人能決定重新武裝。
- 門檻語意對照線上引擎 hl-copytrader/main.py:176：`drawdown > max` 嚴格大於才觸發。

主迴圈接入接口（Task 12 的 run_cycle 實作；本模組只提供積木）：
    cycle 開頭 `is_tripped(root)` → True 則本輪只讀報狀態（＋每小時一次 critical 提醒），
    不做任何交易動作；False 則 `check_drawdown(adapter.get_equity_view(addr), settings.max_drawdown_pct)`
    → breached 且 settings.flatten_on_breach → `trip(...)`。

重試語意（工程原則 2）：transient 失敗的 3 次重試已存在於 adapter 的 resilience
boundary（src/spark/resilience.py），此層**不再重試**——close/cancel 走到這裡還失敗
就是終態，記錄、告警、繼續下一個動作，殘留暴險交由人工處置（ARM_FILE 已鎖死交易）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from spark.copytrade.executor import ExecutorPort
from spark.copytrade.notifier import Notifier
from spark.exchange.base import EquityView, Position

ARM_FILE_RELPATH = Path("var/copytrade/killswitch.tripped")


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
    這是「無資料」不是「安全」，**呼叫端負責 notifier.warn** 讓人看到（本函式保持純函式，
    不做 IO）。
    """
    if ev.recent_peak <= 0:
        return DrawdownStatus(current=ev.current, peak=ev.recent_peak,
                              drawdown_pct=Decimal("0"), breached=False)
    dd = (ev.recent_peak - ev.current) / ev.recent_peak
    return DrawdownStatus(current=ev.current, peak=ev.recent_peak,
                          drawdown_pct=dd, breached=dd > max_dd_pct)


def is_tripped(root: Path) -> bool:
    """ARM_FILE 存在即 tripped。root 為專案根（ARM_FILE 相對路徑掛在其下）。"""
    return (root / ARM_FILE_RELPATH).exists()


@dataclass(frozen=True)
class FlattenReport:
    cancelled: int               # 成功撤銷的掛單張數
    closed: tuple[str, ...]      # 成功平掉的 coin
    failures: tuple[str, ...]    # close 失敗（ok=False 或例外）的 coin
    arm_file: str                # 寫入的 ARM_FILE 絕對路徑


def trip(ex: ExecutorPort, my_positions: dict[str, Position], notifier: Notifier,
         root: Path, status: DrawdownStatus) -> FlattenReport:
    """觸發 kill switch：撤單 → 全平 → 寫 ARM_FILE → 總結告警。順序是紅線，不得重排。

    1. 先撤**全部** resting（避免平倉期間舊掛單成交增加暴險）；單張失敗 → warn、
       繼續撤下一張，絕不中斷。
    2. 逐部位 reduce-only 全量平倉（is_buy = not is_long）；單一 coin 失敗（ok=False
       或例外）→ 記 failures + 逐 coin critical、**繼續平下一個**——一個 coin 的失敗
       不能擋其他部位的平倉。
    3. 寫 ARM_FILE（時間戳 + status 數字 + failures）。**部分失敗也要寫**：鎖死交易
       優先於完美平倉；殘留暴險已在 failures/告警中大聲呈現，由人工處置。
    4. 總結 critical（觸發數字 + 成功/失敗清單）。

    絕不靜默、絕不在此層重試（見模組 docstring 的重試語意）。
    re-arm＝人工刪 ARM_FILE；本函式與本模組不提供刪除路徑。
    """
    # 1) 撤全部 resting
    cancelled = 0
    try:
        open_orders = ex.get_open_orders()
    except Exception as e:  # noqa: BLE001 —— 安全關鍵路徑：讀失敗也要繼續平倉
        notifier.warn("killswitch", f"取得掛單失敗（{e!r}），跳過撤單直接平倉")
        open_orders = []
    for o in open_orders:
        try:
            ok = ex.cancel(o.coin, o.oid)
        except Exception as e:  # noqa: BLE001
            ok = False
            notifier.warn("killswitch", f"撤單例外 {o.coin} oid={o.oid}: {e!r}")
        if ok:
            cancelled += 1
        else:
            notifier.warn("killswitch", f"撤單失敗 {o.coin} oid={o.oid}（繼續撤下一張）")

    # 2) 逐部位 reduce-only 全量平倉
    closed: list[str] = []
    failures: list[str] = []
    for coin, pos in my_positions.items():
        if pos.szi == 0:
            continue
        is_buy = pos.szi < 0  # 平多 → 賣（False）；平空 → 買（True）
        size = abs(pos.szi)
        try:
            res = ex.close_reduce_only(coin, is_buy, size)
            ok, detail = res.ok, res.raw
        except Exception as e:  # noqa: BLE001 —— resilience 重試已耗盡，此層不再重試
            ok, detail = False, repr(e)
        if ok:
            closed.append(coin)
        else:
            failures.append(coin)
            notifier.critical("killswitch",
                              f"平倉失敗 {coin} size={size} is_buy={is_buy}: {detail}"
                              f"——殘留暴險，需人工處置")

    # 3) 寫 ARM_FILE（部分失敗也要寫——鎖死交易優先）
    arm_path = root / ARM_FILE_RELPATH
    arm_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "current": str(status.current),
        "peak": str(status.peak),
        "drawdown_pct": str(status.drawdown_pct),
        "breached": status.breached,
        "cancelled": cancelled,
        "closed": closed,
        "failures": failures,
    }
    try:
        arm_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        # ARM_FILE 寫不進去＝交易鎖不住——最高等級告警後上拋，絕不吞掉（工程原則 3）。
        notifier.critical("killswitch", f"ARM_FILE 寫入失敗 {arm_path}: {e!r}——交易未鎖死！")
        raise

    # 4) 總結告警
    notifier.critical(
        "killswitch",
        f"KILL SWITCH TRIPPED：dd={status.drawdown_pct} current={status.current} "
        f"peak={status.peak}｜撤單成功 {cancelled} 張｜平倉成功 {closed or '無'}｜"
        f"平倉失敗 {failures or '無'}｜已鎖死 {arm_path}（re-arm＝人工刪此檔）",
    )
    return FlattenReport(cancelled=cancelled, closed=tuple(closed),
                         failures=tuple(failures), arm_file=str(arm_path))
