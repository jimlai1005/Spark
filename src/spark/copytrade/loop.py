"""主迴圈：單輪同步組裝（run_cycle）與固定間隔排程（main_loop）。

run_cycle 順序（killswitch docstring 的主迴圈接入接口 + Task 12 spec）：
  is_tripped 短路 → 回撤判定（breach → flatten_on_breach 時 trip）→ leader/my 讀取
  → weight/scale → sync_open_orders（safety_net=sync_positions 接線）→ skip_trigger 告警。

main_loop 排程（port hl-copytrader main.py:122-131 的分鐘鍵防重跑與
:291-292,351-363 的連續錯誤熔斷；頻率由 CopySettings.interval_s 決定，
刻意覆蓋 hl 的 hourly——見 config.py）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import check_drawdown, is_tripped, trip
from spark.copytrade.notifier import Notifier
from spark.copytrade.orders import (
    CycleReport,
    ReconcileResult,
    ReconcileState,
    sync_open_orders,
)
from spark.copytrade.positions import sync_positions
from spark.copytrade.sizing import (
    compute_scale_factor,
    compute_volatility_stats,
    position_weight,
)

logger = logging.getLogger(__name__)

_EMPTY_RECONCILE = ReconcileResult(placed=0, cancelled=0, modified=0, matched=0,
                                   sync_failed=False, skipped_small=())


def _tripped_report() -> CycleReport:
    return CycleReport(reconcile=_EMPTY_RECONCILE, safety_net={"skipped": True},
                       scale=Decimal("0"), tripped=True)


def run_cycle(adapter, ex, settings: CopySettings, notifier: Notifier,
              state: ReconcileState, root: Path) -> CycleReport:
    """單輪同步。adapter=讀側（ExchangeAdapter）、ex=寫側（ActionExecutor，
    自帶 live/my_address）。例外不在此攔截——上拋給 main_loop 計連續錯誤。

    同源不變量（工程原則 1）：
    - scale 分子分母的 equity **同用 `get_account_state().account_value`**
      （同一 endpoint 同一欄位，只差地址）——絕不一邊用 portfolio、一邊用
      marginSummary 拼裝。
    - 回撤判定用 `get_equity_view()`（current/peak 出自單一次 portfolio 呼叫，
      EquityView 型別即此契約），與 scale 的 equity 各自成對、互不混用。
    """
    records_start = len(ex.records)

    # ── 1. killswitch 短路：tripped 只讀報狀態，零交易動作 ─────────────
    if is_tripped(root):
        notifier.critical(
            "killswitch",
            f"kill switch 已 tripped（{root / 'var/copytrade/killswitch.tripped'}），"
            f"本輪跳過所有交易動作；re-arm＝人工刪除該檔",
            dedup_key="tripped",  # TelegramNotifier TTL 內去重，避免每分鐘洗版
        )
        return _tripped_report()

    # ── 2. 回撤判定（同一次 portfolio 回應的 current/peak）─────────────
    ev = adapter.get_equity_view(ex.my_address)
    status = check_drawdown(ev, settings.max_drawdown_pct)
    if status.peak <= 0:
        # 「無資料」不是「安全」——check_drawdown docstring 指定呼叫端告警。
        notifier.warn("killswitch", "權益歷史 peak<=0（新帳戶或 portfolio 異常），"
                      "回撤保護本輪無法判定", dedup_key="dd_no_data")
    if status.breached:
        notifier.critical(
            "killswitch",
            f"回撤 {status.drawdown_pct} 超過上限 {settings.max_drawdown_pct}"
            f"（current={status.current} peak={status.peak}）",
            dedup_key="dd_breach",
        )
        if settings.flatten_on_breach:
            my_positions = {p.coin: p for p in adapter.get_positions(ex.my_address)}
            trip(ex, my_positions, notifier, root, status)
        return _tripped_report()

    # ── 3. leader / my 狀態讀取 ────────────────────────────────────────
    leader = settings.leader_address
    leader_orders = adapter.get_open_orders(leader)
    leader_positions = {p.coin: p for p in adapter.get_positions(leader)}
    leader_state = adapter.get_account_state(leader)

    my_orders = ex.get_open_orders()  # 經 ex：dry 讀虛擬簿、live 讀交易所
    my_positions = {p.coin: p for p in adapter.get_positions(ex.my_address)}
    my_state = adapter.get_account_state(ex.my_address)

    # ── 4. weight / scale ─────────────────────────────────────────────
    if settings.volatility_weight_enabled:
        daily = adapter.get_daily_abs_pnl(leader)
        vol = compute_volatility_stats(daily, leader_state.account_value)
        weight = position_weight(settings, vol)  # vol=None（資料不足）→ 不縮，安全預設
    else:
        weight = position_weight(settings, None)
    scale = compute_scale_factor(
        leader_equity=leader_state.account_value,   # 同源：account_state.account_value
        my_equity=my_state.account_value,           # 同源：account_state.account_value
        target_notional=leader_state.total_ntl_pos,
        settings=settings,
        weight=weight,
    )

    # ── 5. protected（M1：holding protection 預設關 → 空集合）──────────
    protected: frozenset[str] = frozenset()
    if settings.holding_protection_enabled:
        # M1 資料源缺口：anti_holding_flags 需要 fills 的 startPosition 欄位，
        # 現行 UserFill 型別未攜帶——大聲告警而非靜默假裝有保護（工程原則 3）。
        notifier.warn("protection",
                      "holding_protection_enabled=true 但 M1 尚無資料源支援，"
                      "本輪以無保護執行", dedup_key="hp_unsupported")

    # leader 有部位的 coin 才有已知槓桿；其餘 coin 交由 _set_entry_leverage
    # 的「map 查無 → 靜默跳過」降級路徑（orders.py docstring）。
    leverage_by_coin = {c: (p.leverage, p.is_cross) for c, p in leader_positions.items()}

    # ── 6. A 段掛單對帳 + B 段部位安全網 ──────────────────────────────
    def _safety_net() -> dict:
        # live 時重抓我方部位（A 段的撤掛/成交可能已改變部位——hl orders.py:338-340
        # 語意，重抓是 callable 自己的責任）；dry 沿用本輪快照。
        pos = ({p.coin: p for p in adapter.get_positions(ex.my_address)}
               if ex.live else my_positions)
        return sync_positions(
            ex, leader_positions, pos, scale,
            settings=settings, notifier=notifier, protected=set(protected),
            size_decimals=ex.get_size_decimals, mids=adapter.get_all_mids(),
        )

    report = sync_open_orders(
        ex, leader_orders, my_orders, my_positions, scale,
        settings=settings, notifier=notifier, state=state, live=ex.live,
        protected=set(protected), leverage_by_coin=leverage_by_coin,
        safety_net=_safety_net,
    )

    # ── 7. 本輪新出現的 skip_trigger → warn（per-coin dedup）──────────
    skip_coins = sorted({r.coin for r in ex.records[records_start:]
                         if r.kind == "skip_trigger"})
    for coin in skip_coins:
        notifier.warn(
            "orders",
            f"[M1 限制] {coin} 出現 trigger 單，adapter 尚無 trigger 下單支援，已跳過",
            dedup_key=f"skip_trigger:{coin}",
        )
    return report


def _seconds_until_next_interval(now_ts: float, interval_s: int) -> float:
    """距離下一個 interval 邊界的秒數（對齊邊界；port hl main.py:122-126 一般化）。"""
    return max(1.0, interval_s - (now_ts % interval_s))


def _minute_key(dt: datetime) -> tuple:
    """「年月日時分」鍵，同一分鐘內不重跑（1:1 hl main.py:129-131）。"""
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute)


def main_loop(mk_cycle: Callable[[], CycleReport], settings: CopySettings,
              notifier: Notifier, *, clock=time.time, sleep_fn=time.sleep,
              now_fn=datetime.now) -> None:
    """固定間隔主迴圈。

    - 對齊 interval 邊界醒來；minute-key 防同一分鐘重跑（hl main.py:122-131）。
    - 連續錯誤 >= settings.max_consecutive_errors → notifier.critical + SystemExit(1)
      （hl main.py:291-292,351-363）；任一輪成功即歸零。
    - tripped 的輪次不算錯誤也不停迴圈——killswitch 短路由 run_cycle 內部處理，
      re-arm 後（人工刪 ARM_FILE）迴圈自動恢復交易。
    - KeyboardInterrupt → 通知後正常返回。
    """
    consecutive_errors = 0
    last_key: tuple | None = None
    try:
        while True:
            now = now_fn()
            key = _minute_key(now)
            if key != last_key:
                last_key = key
                try:
                    mk_cycle()
                    consecutive_errors = 0
                except Exception as e:  # noqa: BLE001 —— 熔斷計數層，KeyboardInterrupt/SystemExit 不攔
                    consecutive_errors += 1
                    logger.error("同步錯誤 (%d/%d): %s", consecutive_errors,
                                 settings.max_consecutive_errors, e, exc_info=True)
                    if consecutive_errors >= settings.max_consecutive_errors:
                        notifier.critical(
                            "loop",
                            f"連續 {consecutive_errors} 次同步失敗（最後錯誤：{e!r}），"
                            f"引擎停止，需人工介入",
                        )
                        raise SystemExit(1) from e
            sleep_fn(_seconds_until_next_interval(clock(), settings.interval_s))
    except KeyboardInterrupt:
        notifier.info("loop", "使用者中斷，引擎停止")
