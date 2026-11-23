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
  4. 其餘 → 沿用上一次鎖定的倍率。

純函式、無 IO、無時間、無全域狀態；鎖定值只存記憶體（`ReconcileState.scale_latch`），
引擎重啟後第一輪必重算。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from spark.copytrade.config import CopySettings
from spark.exchange.base import OpenOrder, Position

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ScaleLatch:
    scale: Decimal
    leader: str                 # 小寫位址
    fingerprint: tuple          # leader 曝險指紋
    settings_key: tuple         # 影響倍率的 follower 設定


def leader_fingerprint(leader_positions: Mapping[str, Position],
                       leader_orders: Sequence[OpenOrder]) -> tuple:
    """leader 曝險指紋：((coin, szi)…, (oid, coin, is_buy, sz, limit_px, trigger_px,
    reduce_only)…)，各自排序。

    只取這些欄位；**不得**含時間戳、未實現損益、保證金、進場價、槓桿等會自己變動
    （或不代表「leader 有動作」）的值。倉位用 `Position.szi`、掛單剩餘量用 `OpenOrder.sz`。
    """
    positions = tuple(sorted((p.coin, p.szi) for p in leader_positions.values()))
    orders = tuple(sorted(
        ((o.oid, o.coin, o.is_buy, o.sz, o.limit_px, o.trigger_px, o.reduce_only)
         for o in leader_orders),
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
                follower_has_exposure: bool) -> tuple[Decimal, ScaleLatch | None]:
    """回傳 (本輪要用的倍率, 新的鎖定狀態)。規則見模組 docstring。"""
    if fresh_scale <= _ZERO:
        return fresh_scale, None
    if fingerprint == ((), ()):
        return fresh_scale, None
    if (prev is None
            or prev.leader != leader
            or prev.settings_key != key
            or prev.fingerprint != fingerprint
            or not follower_has_exposure):
        return fresh_scale, ScaleLatch(fresh_scale, leader, fingerprint, key)
    return prev.scale, prev
