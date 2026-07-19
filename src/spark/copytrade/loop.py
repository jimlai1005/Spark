"""主迴圈：單輪同步組裝（run_cycle）與固定間隔排程（main_loop）。

run_cycle 順序（killswitch docstring 的主迴圈接入接口 + Task 12 spec）：
  is_tripped 短路 → 回撤判定（breach → flatten_on_breach 時 trip）→ **成本熔斷判定**
  → leader/my 讀取 → weight/scale → sync_open_orders（safety_net=sync_positions 接線）
  → skip_trigger 告警。

⭐ 這個順序編碼了三道閘門的優先序（成本熔斷計畫 D8）：
`leader 撤銷`（在 run_copytrade 層，run_cycle 之前）> `kill switch（回撤）`
> `成本熔斷器`。前面的閘一旦成立就已 return，後面的走不到——優先序由位置保證，
不是靠呼叫端記得檢查旗標。

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
from spark.copytrade.costbreaker import evaluate_cost
from spark.copytrade.equity import perp_equity_view, sample_coverage, update_lifetime_peak
from spark.copytrade.killswitch import DrawdownStatus, evaluate, is_tripped, trip
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


def tripped_report() -> CycleReport:
    """零交易動作的一輪。公開（非 _ 前綴）是因為 run_copytrade 的 leader 撤銷路徑
    也要回報「這一輪什麼都沒做」——兩處各造一份會漂移。"""
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
    - 回撤判定用 `perp_equity_view()`（perp accountValue 為基準，與 scale 同一數字；
      peak 為本地 7 天滾動樣本最大值）。2026-07-19 起改用此基準——原 `get_equity_view()`
      的 portfolio 資料源含 spot，會稀釋熔斷保護（findings F1）。
    """
    records_start = len(ex.records)

    # ── 1. killswitch 短路：tripped 只讀報狀態，零交易動作 ─────────────
    if is_tripped(root):
        notifier.critical(
            "killswitch",
            f"kill switch 已 tripped（{root / 'var/copytrade/killswitch.tripped'}），"
            f"本輪跳過所有交易動作；re-arm＝人工刪除該檔",
            # TelegramNotifier 在 CRITICAL_DEDUP_TTL_S 內去重，避免每分鐘洗版；
            # 重送時會附上累計抑制次數，所以「還在 tripped」不會因去重而消失。
            # （2026-07-19 前這行註解宣稱有去重，但 critical 當時完全忽略 dedup_key
            #   ——註解描述的保護並不存在；I2 已補上實作，註解同步修正。）
            dedup_key="tripped",
        )
        return tripped_report()

    # ── 2. 回撤判定（同一次 portfolio 回應的 current/peak）─────────────
    # 必須用 evaluate() 而非直呼 check_drawdown（killswitch.py 主迴圈接入接口）：
    # degenerate equity（peak<=0）的 warn 在 evaluate 內結構性內建，不靠這裡記得補。
    ev = perp_equity_view(adapter, ex.my_address, root)
    cov = sample_coverage(root)
    lifetime = update_lifetime_peak(root, ev.current)
    status = evaluate(ev, settings, notifier, coverage=cov, lifetime_peak=lifetime)
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
        return tripped_report()

    # ── 2.5 成本熔斷器（計畫 D8 的優先序在此結構性成立）─────────────────
    # `leader 撤銷` > `kill switch（回撤）` > `成本熔斷器`。前兩者一旦成立，
    # 上面兩段已經 return，走不到這裡——優先序不是靠註解約定，是靠位置。
    # 成本熔斷器是最輕的一道：只把 no_new_exposure 交給下游（停開新倉、
    # reduce-only 照常），不自己平倉、不覆寫前兩者的行為。
    #
    # ⭐⭐ 同基準（D2）：分母用**這一輪、這一次讀取**的 `ev.current`
    # ——就是上面回撤判定用的同一個 perp accountValue（`perp_equity_view` 的產出）。
    # 絕不改成 my_state.account_value 或 adapter.get_equity_view()：後者含 spot，
    # 分母灌水會讓換手率被低估、保護靜默失效（findings F1 同型問題）。
    # 也不要在這裡重讀一次 get_account_value：同源但不同輪，仍是混基準。
    cost = evaluate_cost(adapter, ex.my_address, ev, settings, notifier, root)
    if cost.escalate:
        # D6 累犯升級：滾動 24h 內反覆觸發 ⇒ 交給 kill switch（需人工 re-arm）。
        # 刻意重用 killswitch.trip 而非另寫收尾（同 leader_revoked 的理由：
        # 兩套實作必然漂移，而漂移的那套只在真出事時才被執行到）。
        my_positions = {p.coin: p for p in adapter.get_positions(ex.my_address)}
        trip(ex, my_positions, notifier, root,
             DrawdownStatus(current=ev.current, peak=ev.recent_peak,
                            drawdown_pct=Decimal("0"), breached=False),
             reason="cost_breach")
        return tripped_report()

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
            no_new_exposure=cost.breached,
        )

    report = sync_open_orders(
        ex, leader_orders, my_orders, my_positions, scale,
        settings=settings, notifier=notifier, state=state, live=ex.live,
        protected=set(protected), no_new_exposure=cost.breached,
        leverage_by_coin=leverage_by_coin,
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
