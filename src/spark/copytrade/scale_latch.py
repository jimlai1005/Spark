"""倍率鎖定（plan 2026-11-22 T2b／D7）：leader 沒有動作時，follower 用的倍率不變。

動機：倍率 = 本金×使用率×權重 ÷ leader 權益。分母（leader 現貨幣價、浮動損益）與分子
（follower 自己的浮動損益）每分鐘都在漂；既有的 8% 容差只能降低調倉頻率、不能消除
（recon §8：30 天模擬，leader 抱單期間仍有 0–23 次因分母漂移的調倉）。使用者要求
leader 沒動作就不動 follower。

規則（`apply_latch`，依序）：
  1. 新算倍率 ≤ 0 → 照用、不鎖（既有 scale=0＝全平語意不變）。
  2. leader 無倉位也無掛單（指紋為空）→ 照用、不鎖（無事可鎖）。
  3. 沒有鎖定值／leader 換了／影響倍率的設定變了／leader 曝險指紋變了（＝leader 有動作）／
     follower 自己沒有任何倉位掛單（沒有東西會被擾動）→ 重算並重新鎖定。
  3'. （D8）槓桿上限生效（`cap_binding`）且新算倍率**小於**鎖定值 → 也重算並重新鎖定：
     倍率鎖定不得凍結 `max_target_leverage` 的縮倉方向（leader 抱單期間權益崩跌、有效
     槓桿衝破上限時，follower 仍要被縮）。只縮不放——新倍率較大時仍沿用鎖定值；上限解除
     後也不自動加回，等 leader 下一次動作。
  4. 其餘 → 沿用上一次鎖定的倍率。

純函式、無 IO、無時間、無全域狀態；鎖定值只存記憶體（`ReconcileState.scale_latch`），
引擎重啟後第一輪必重算。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from spark.copytrade.config import CopySettings
from spark.copytrade.instrument import _coin_dex, _is_spot_coin
from spark.exchange.base import OpenOrder, Position

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ScaleLatch:
    scale: Decimal
    leader: str                 # 小寫位址
    fingerprint: tuple          # leader 曝險指紋
    settings_key: tuple         # 影響倍率的 follower 設定


def _position_mirrored(coin: str) -> bool:
    """引擎是否會鏡射這個 leader 倉位——與 `positions.sync_positions`（positions.py:199）
    同一個略過條件：現貨與非預設 dex 都不鏡射。"""
    return not (_is_spot_coin(coin) or _coin_dex(coin))


def _order_mirrored(coin: str) -> bool:
    """引擎是否會鏡射這張 leader 掛單——與 `orders._build_desired`（orders.py:323）
    同一個略過條件：只略過現貨（該處對非預設 dex 的掛單不另外略過，故這裡也不濾）。"""
    return not _is_spot_coin(coin)


def leader_has_mirrorable_exposure(leader_positions: Mapping[str, Position],
                                   leader_orders: Sequence[OpenOrder]) -> bool:
    """leader 是否有「會被引擎鏡射」的倉位或掛單（D10）。

    現貨單、現貨倉位、非預設 dex 倉位不算曝險：它們不影響 follower 的任何目標。"""
    return (any(_position_mirrored(c) for c in leader_positions)
            or any(_order_mirrored(o.coin) for o in leader_orders))


def leader_fingerprint(leader_positions: Mapping[str, Position],
                       leader_orders: Sequence[OpenOrder]) -> tuple:
    """leader 曝險指紋：((coin, szi)…, (oid, coin, is_buy, sz, limit_px, trigger_px,
    reduce_only)…)，各自排序。

    只取這些欄位；**不得**含時間戳、未實現損益、保證金、進場價、槓桿等會自己變動
    （或不代表「leader 有動作」）的值。倉位用 `Position.szi`、掛單剩餘量用 `OpenOrder.sz`。

    D10：只含引擎會鏡射的項目（`_position_mirrored`／`_order_mirrored`）；leader 掛／撤
    現貨單不是「引擎在跟的東西變了」，不得讓鎖定重算。
    """
    positions = tuple(sorted((p.coin, p.szi) for p in leader_positions.values()
                             if _position_mirrored(p.coin)))
    orders = tuple(sorted(
        ((o.oid, o.coin, o.is_buy, o.sz, o.limit_px, o.trigger_px, o.reduce_only)
         for o in leader_orders if _order_mirrored(o.coin)),
        key=repr))
    return (positions, orders)


def settings_key(settings: CopySettings) -> tuple:
    """影響倍率的 follower 設定：任一變動都要重算倍率。"""
    return (settings.capital_utilization, settings.allocated_capital,
            settings.use_full_equity, settings.position_weight,
            settings.max_target_leverage, settings.volatility_weight_enabled,
            settings.leader_flow_neutralization_enabled)


def apply_latch(fresh_scale: Decimal, prev: ScaleLatch | None, *, leader: str,
                fingerprint: tuple, key: tuple,
                follower_has_exposure: bool,
                cap_binding: bool = False) -> tuple[Decimal, ScaleLatch | None]:
    """回傳 (本輪要用的倍率, 新的鎖定狀態)。規則見模組 docstring。

    `cap_binding`：呼叫端判定「本輪 leader 有效槓桿已超過 `max_target_leverage`」
    （D8）；預設 False＝不影響既有規則。"""
    if fresh_scale <= _ZERO:
        return fresh_scale, None
    if fingerprint == ((), ()):
        return fresh_scale, None
    if (prev is None
            or prev.leader != leader
            or prev.settings_key != key
            or prev.fingerprint != fingerprint
            or not follower_has_exposure
            or (cap_binding and fresh_scale < prev.scale)):
        return fresh_scale, ScaleLatch(fresh_scale, leader, fingerprint, key)
    return prev.scale, prev
