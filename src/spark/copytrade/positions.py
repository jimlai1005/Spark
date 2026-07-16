"""部位安全網：比較 leader 部位與我方部位，執行開倉/調整/趨平。

1:1 port 自 hl-copytrader（分支邏輯與抗單保護 gate 對齊行號如下）：
  - `src/sync.py:54-172`（`sync_positions` 三分支：新開 104-119、調整
    121-151、趨平 154-167；min_notional 只 debug log 100-102；
    抗單保護 gate 106-108/128-130）
  - `src/trader.py:254-300`（`adjust_position`：方向反轉全平再開
    269-279、加倉 285-292、減倉 293-300）
  - `src/trader.py:110-130`（`set_leverage`：呼叫時機——hl 的每次
    `open_position` 都先呼叫 `set_leverage`（trader.py:163），故本 port
    在**每次** `market_open` 前（新開/反轉重開/加倉）都呼叫
    `ex.update_leverage`。per-coin「同值不重送」快取屬 Task 12 的
    executor 範疇，此處不做快取。）

刻意偏離 hl 之處（皆為本 port 的簡化取捨，逐一記錄）：
  1. **size 先捨入再算名目（單向保守）**：hl `sync.py:92-102` 用未捨入
     的 `target_size = size*scale` 算 notional 再過濾，捨入延後到
     `trader.open_position` 內部（`trader.py:148`）。本 port 為了讓
     「開倉/調整/名目過濾」共用同一個 target_size，改成先呼叫
     `_round_size` 再算 notional。方向性：`_round_size` 是 ROUND_DOWN，
     故恆有 `notional_spark ≤ notional_hl`——差異**只會**使 spark
     「少開/不開」，絕不會「多開」；且過濾與下單用同一個數字，順帶
     修正了 hl「過濾基準（未捨入）≠ 下單基準（已捨入）」的原始不一致。
     Shadow 對照（T4.1 diff 分類）預期差異形態：僅出現在 min_notional
     門檻邊界附近——hl 開了、spark 記 skipped(min_notional)；或兩邊都
     開但 spark size 少最後一檔。反向差異（spark 開了 hl 沒開、或
     spark size 較大）不應出現，出現即為 bug。
  2. **leverage/is_cross 直接取 leader 部位欄位**，不像 hl
     `trader.entry_leverage`/`entry_is_cross` 那樣依「我方帳戶」的
     max leverage／onlyIsolated 動態計算——那層邏輯需要查我方 exchange
     meta，屬 Task 12 ActionExecutor 建構時的職責；`ExecutorPort.
     market_open` 本身也不收 leverage 參數，leverage 只走獨立的
     `update_leverage` 呼叫。
  3. **entry_px 不下傳**：hl 把 mid_px 一路傳進 `open_position`/
     `adjust_position` 純粹是為了組 Telegram 通知文字，不影響下單決策。
     `ExecutorPort.market_open`/`close_reduce_only` 內部自行從注入的
     mids 算價，此處 `mids` 僅用於 min_notional 過濾。
  4. **protected 對「加倉」的擋法沿用 hl 的字面條件**
     `target_size > my_size`（不額外套 size_tolerance 判斷）——與
     hl `sync.py:128-130` 完全一致；代價是極小幅度、原本落在容忍帶內
     不會被觸發調整的「加倉」，若剛好命中 protected，仍會被記
     skipped+warn（沒有實際下單動作，只是多一次告警噪音，行為上無害）。
  5. **ex.* 呼叫失敗一律記入 "failed" + notifier.warn，但不中止後續步驟**
     ——沿用 hl 的 best-effort 精神（hl 對 `set_leverage`/
     `close_position` 的回傳值都不檢查，永遠往下走），只是額外加上
     可見性（工程原則 3：安全關鍵動作失敗必須大聲告警，不能吞掉）。
     下一輪同步會用最新部位重新對帳，失敗會自癒或再次被看見。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ExecutorPort
from spark.copytrade.instrument import _coin_dex, _is_spot_coin, _round_size
from spark.copytrade.notifier import Notifier
from spark.exchange.base import Position

_EPS = Decimal("1e-8")


def _side_of(szi: Decimal) -> str:
    """有號 size 轉方向字串。

    szi != 0 由上游 adapter 保證：HyperliquidAdapter.get_positions 對
    szi == 0 的項目直接過濾不回傳（hyperliquid.py:113）。
    """
    return "long" if szi > 0 else "short"


def _try_update_leverage(
    ex: ExecutorPort, notifier: Notifier, result: dict, coin: str,
    leverage: int, is_cross: bool,
) -> None:
    """呼叫 ex.update_leverage；失敗記 failed+warn 但不中止（hl 同樣不檢查回傳值，best-effort）。"""
    ok = ex.update_leverage(coin, leverage, is_cross)
    if not ok:
        result["failed"].append({"coin": coin, "action": "update_leverage"})
        notifier.warn(
            "execution",
            f"{coin} 設定槓桿失敗 leverage={leverage}x is_cross={is_cross}",
            dedup_key=f"leverage_fail:{coin}",
        )


def _try_market_open(
    ex: ExecutorPort, notifier: Notifier, result: dict, coin: str,
    is_buy: bool, size: Decimal,
) -> bool:
    """呼叫 ex.market_open；失敗記 failed+warn，回傳是否成功。"""
    order = ex.market_open(coin, is_buy, size)
    if not order.ok:
        result["failed"].append({"coin": coin, "action": "market_open"})
        notifier.warn(
            "execution",
            f"{coin} 開倉失敗 is_buy={is_buy} size={size}",
            dedup_key=f"open_fail:{coin}",
        )
        return False
    return True


def _try_close_reduce_only(
    ex: ExecutorPort, notifier: Notifier, result: dict, coin: str,
    is_buy: bool, size: Decimal,
) -> bool:
    """呼叫 ex.close_reduce_only；失敗記 failed+warn，回傳是否成功。"""
    order = ex.close_reduce_only(coin, is_buy, size)
    if not order.ok:
        result["failed"].append({"coin": coin, "action": "close_reduce_only"})
        notifier.warn(
            "execution",
            f"{coin} 平倉失敗 is_buy={is_buy} size={size}",
            dedup_key=f"close_fail:{coin}",
        )
        return False
    return True


def _leverage_and_open(
    ex: ExecutorPort, notifier: Notifier, result: dict, coin: str,
    target_side: str, size: Decimal, leverage: int, is_cross: bool,
) -> bool:
    """先設槓桿（照 hl trader.py:163 時機）再市價開倉，回傳開倉是否成功。

    新開/反轉重開/加倉共用。成功與否的記錄（opened/adjusted）由呼叫端
    依回傳值決定——失敗已由 _try_* 記入 failed，同一動作絕不同時出現在
    成功清單與 failed 兩處。
    """
    _try_update_leverage(ex, notifier, result, coin, leverage, is_cross)
    is_buy = target_side == "long"
    return _try_market_open(ex, notifier, result, coin, is_buy, size)


def sync_positions(
    ex: ExecutorPort, leader_positions: dict[str, Position],
    my_positions: dict[str, Position], scale: Decimal, *,
    settings: CopySettings, notifier: Notifier,
    protected: AbstractSet[str] = frozenset(),
    size_decimals: Callable[[str], int],
    mids: Mapping[str, Decimal],
) -> dict:
    """同步我方部位到 leader 部位（乘以 scale）。

    回傳 {"opened": [...], "adjusted": [...], "flattened": [...],
          "skipped": [...], "failed": [...]}。
    每個 list 內是 dict，至少含 "coin" 欄位；"skipped"/"failed" 額外附
    "reason"/"action" 說明原因。
    """
    result: dict = {"opened": [], "adjusted": [], "flattened": [], "skipped": [], "failed": []}

    # ── 1. leader 有部位的標的：新開或調整 ──────────────────────────
    for coin, tgt in leader_positions.items():
        if _is_spot_coin(coin) or _coin_dex(coin):
            result["skipped"].append({"coin": coin, "reason": "spot_or_non_primary_dex"})
            continue

        target_side = _side_of(tgt.szi)
        sz_dec = size_decimals(coin)
        target_size = _round_size(abs(tgt.szi) * scale, sz_dec)

        mid_px = mids.get(coin) or tgt.entry_px
        notional = target_size * mid_px

        if notional < settings.min_order_notional:
            # hl sync.py:100-102 語意：只 debug log，不告警。
            result["skipped"].append({"coin": coin, "reason": "min_notional"})
            continue

        if coin not in my_positions:
            # ── 新開倉（hl sync.py:104-119）──
            if coin in protected:
                result["skipped"].append({"coin": coin, "reason": "protected_open"})
                notifier.warn(
                    "protection",
                    f"{coin} 持倉保護中，跳過新開倉 side={target_side} size={target_size}",
                    dedup_key=f"protected_open:{coin}",
                )
                continue
            if _leverage_and_open(ex, notifier, result, coin, target_side, target_size,
                                  tgt.leverage, tgt.is_cross):
                result["opened"].append(
                    {"coin": coin, "side": target_side, "size": target_size})
            continue

        # ── 兩邊都有部位：調整（hl sync.py:121-151 / trader.py:254-300）──
        my_pos = my_positions[coin]
        my_side = _side_of(my_pos.szi)
        my_size = abs(my_pos.szi)

        # 抗單保護：只擋「同向加倉」，字面沿用 hl sync.py:128-130（target_size > my_size，
        # 不套 size_tolerance）。
        if coin in protected and target_side == my_side and target_size > my_size:
            result["skipped"].append({"coin": coin, "reason": "protected_increase"})
            notifier.warn(
                "protection",
                f"{coin} 持倉保護中，跳過加倉 {my_size}→{target_size}",
                dedup_key=f"protected_increase:{coin}",
            )
            continue

        if my_side != target_side:
            # ── 方向反轉：全平再開（trader.py:269-279）──
            # 平倉下單方向 = 持倉反向：平多賣（is_buy=False）、平空買（is_buy=True）。
            # 兩腿皆 best-effort 嘗試（hl 不檢查 close 回傳值、照樣重開）；
            # 但 adjusted 只在兩腿皆成功才記錄——任一腿失敗已記入 failed，
            # 下游依 adjusted 判定「已同步」，不得與 failed 並存。
            is_buy_close = my_side == "short"
            close_ok = _try_close_reduce_only(ex, notifier, result, coin, is_buy_close, my_size)
            open_ok = _leverage_and_open(ex, notifier, result, coin, target_side, target_size,
                                         tgt.leverage, tgt.is_cross)
            if close_ok and open_ok:
                result["adjusted"].append({
                    "coin": coin, "kind": "flip",
                    "from_side": my_side, "to_side": target_side,
                    "from_size": my_size, "to_size": target_size,
                })
            continue

        size_diff_pct = abs(target_size - my_size) / max(my_size, _EPS)
        if size_diff_pct > settings.size_tolerance:
            diff = target_size - my_size
            if diff > 0:
                # ── 同向加倉（trader.py:285-292；經 open_position → set_leverage
                # trader.py:163，故加倉前同樣先設槓桿）──
                if _leverage_and_open(ex, notifier, result, coin, target_side, diff,
                                      tgt.leverage, tgt.is_cross):
                    result["adjusted"].append({
                        "coin": coin, "kind": "increase",
                        "from_size": my_size, "to_size": target_size, "diff": diff,
                    })
            else:
                # ── 同向減倉（trader.py:293-300，reduce-only）──
                reduce_size = abs(diff)
                is_buy_to_close = my_side == "short"
                if _try_close_reduce_only(ex, notifier, result, coin, is_buy_to_close,
                                          reduce_size):
                    result["adjusted"].append({
                        "coin": coin, "kind": "decrease",
                        "from_size": my_size, "to_size": target_size, "diff": diff,
                    })
        # else: 容忍帶內，零動作（hl sync.py:150-151，只 debug log）。

    # ── 2. 我有但 leader 已平的標的 → 跟著平（hl sync.py:154-167）──────
    for coin, my_pos in my_positions.items():
        if coin in leader_positions:
            continue
        if _is_spot_coin(coin) or _coin_dex(coin):
            result["skipped"].append({"coin": coin, "reason": "spot_or_non_primary_dex"})
            continue
        my_side = _side_of(my_pos.szi)
        my_size = abs(my_pos.szi)
        is_buy = my_side == "short"  # 平多賣、平空買：is_buy = not is_long
        if _try_close_reduce_only(ex, notifier, result, coin, is_buy, my_size):
            result["flattened"].append({"coin": coin, "side": my_side, "size": my_size})

    return result
