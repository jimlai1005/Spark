"""對帳引擎純函式層。1:1 port 自 hl-copytrader src/orders.py:33-184，Decimal 化、
容忍度改為參數注入（hl 用模組級常數；spark 由呼叫端傳入 CopySettings 的
`px_rel_tol`/`size_tolerance`，符合「同一份配置貫穿引擎」的慣例）。

刻意未移植清單（hl-copytrader src/orders.py 原始碼行號對應）：
  - `_set_entry_leverage`（hl:187-193）——進場單前設槓桿（含 xyz/onlyIsolated 分支），
    屬 Task 11 範疇，本檔不放。
  - `_reconcile_orders`/`sync_open_orders`（hl:196-369）——實際呼叫 Trader 下單/改單/
    撤單、Telegram 告警、驗證重試迴圈，全是 I/O 副作用層，屬 Task 8 範疇。本檔只放
    無副作用的規劃/比對純函式（`_plan`/`_build_desired` 等），供 Task 8 的
    `_reconcile_orders` 呼叫。
  - `HOLDING_PROTECTION_ENABLED` 的 Z-Score 異常持倉偵測（hl protection.py）——不在
    本任務範圍；這裡的 `protected: set[str]` 是呼叫端已算好的結果，直接消費。

結構差異（相對 hl 語意的刻意調整，逐項說明）：
  - `_plan` 回傳型別改為 frozen dataclass `ReconcilePlan`，`matched` 欄位從 hl 的
    「相符張數」(int) 升級為「相符 oid 的 frozenset[int]」——語意更精確（呼叫端可知道
    具體哪些單被跳過不動），資訊量涵蓋 hl 原本的 `len(matched)`。
  - `spec_from_open_order` 轉換時 `is_market` 恆為 `False`：spark 的 `OpenOrder`
    （exchange/base.py）沒有攜帶此欄位（讀側 HyperliquidAdapter.get_open_orders 的既有
    設計，非本任務引入）。由於 `_build_desired` 的輸入 `leader_orders` 型別同樣是
    `OpenOrder`，desired spec 的 `is_market` 也恆為 `False`——`_orders_match` 對
    `is_market` 的比較因此兩側恆相等、實質不生效，是已知的資訊損失但不影響現有測試
    可觀測的行為（trigger 單的 market/limit 區分需等 OpenOrder 型別擴充後才能還原）。
  - `_build_desired` 的 reduce-only-無部位案例（hl 的 G4，hl:103-104）在 hl 原始碼裡是
    「靜默跳過」（continue，不記錄進任何 skipped 清單）；spark 版本明確記錄為
    `SkippedOrder(reason="reduce_only_no_pos")`，可觀測性優於 hl（呼叫端這裡要求要能
    看到這個原因，見 Task 7 spec）。size<=0 或 px<=0 的邊界情形則維持 hl 的靜默跳過
    （無 SkippedOrder 記錄），因為 hl 這裡本來就沒有分類原因可記。
"""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from spark.copytrade.instrument import _is_spot_coin, _round_size
from spark.exchange.base import OpenOrder, Position


@dataclass(frozen=True)
class OrderSpec:
    """跟單引擎內部的掛單規格（desired 或 mine 皆用此型別表示，便於直接比對）。"""

    coin: str
    is_buy: bool
    sz: Decimal
    limit_px: Decimal
    reduce_only: bool = False
    is_trigger: bool = False
    tpsl: str | None = None
    trigger_px: Decimal | None = None
    is_market: bool = False


@dataclass(frozen=True)
class SkippedOrder:
    """`_build_desired` 過濾掉的目標單記錄，附原因供上層記錄/告警。"""

    coin: str
    notional: Decimal
    reason: str  # ∈ {"small", "spot", "protected", "reduce_only_no_pos"}


@dataclass(frozen=True)
class ReconcilePlan:
    """`_plan` 的規劃結果。`matched` 是保留不動（完全相符）的我方 oid 集合。"""

    modifies: tuple[tuple[int, OrderSpec], ...]
    to_place: tuple[OrderSpec, ...]
    to_cancel: tuple[int, ...]
    matched: frozenset[int]


def spec_from_open_order(o: OpenOrder) -> OrderSpec:
    """exchange 型別 → 引擎型別轉換。`is_market` 恆為 False，見模組 docstring「結構差異」。"""
    return OrderSpec(
        coin=o.coin,
        is_buy=o.is_buy,
        sz=o.sz,
        limit_px=o.limit_px,
        reduce_only=o.reduce_only,
        is_trigger=o.is_trigger,
        tpsl=o.tpsl,
        trigger_px=o.trigger_px,
        is_market=False,
    )


def _prices_equal(a: Decimal, b: Decimal, rel: Decimal) -> bool:
    """相對容忍度價格相等判定。1:1 port 自 hl orders.py:40-43（rel 由呼叫端注入，非硬編）。"""
    if a == 0 and b == 0:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b), Decimal("1e-8"))


def _orders_match(
    desired: OrderSpec, mine: OrderSpec, *, px_rel_tol: Decimal, size_tol: Decimal
) -> bool:
    """判斷我的一張掛單是否等同於某個目標縮放後的掛單。1:1 port 自 hl orders.py:46-76。"""
    if desired.coin != mine.coin:
        return False
    if desired.is_buy != mine.is_buy:
        return False
    if desired.reduce_only != mine.reduce_only:
        return False
    if desired.is_trigger != mine.is_trigger:
        return False

    d_px = desired.trigger_px if desired.is_trigger else desired.limit_px
    m_px = mine.trigger_px if mine.is_trigger else mine.limit_px
    if not _prices_equal(d_px, m_px, px_rel_tol):
        return False

    if desired.is_trigger:
        if desired.tpsl != mine.tpsl:
            return False
        if desired.is_market != mine.is_market:
            return False
        # 觸發限價單還要比對限價
        if not desired.is_market and not _prices_equal(desired.limit_px, mine.limit_px, px_rel_tol):
            return False

    my_size = mine.sz
    if my_size <= 0:
        return False
    if abs(desired.sz - my_size) / max(my_size, Decimal("1e-8")) > size_tol:
        return False
    return True


def _slot_key(o: OrderSpec) -> tuple:
    """「同一張概念上的單」的判定鍵：同標的/方向/減倉旗標/觸發類型(+tp/sl)。
    同 slot 的單可用 modify 就地改價/量，不需取消重掛。1:1 port 自 hl orders.py:130-134。
    """
    return (o.coin, o.is_buy, bool(o.reduce_only), bool(o.is_trigger), o.tpsl if o.is_trigger else None)


def _ref_px(o: OrderSpec) -> Decimal:
    """port 自 hl orders.py:137-138。"""
    return o.trigger_px if o.is_trigger else o.limit_px


def _plan(
    desired: list[OrderSpec],
    mine: list[tuple[int, OrderSpec]],
    *,
    px_rel_tol: Decimal,
    size_tol: Decimal,
) -> ReconcilePlan:
    """規劃對帳動作，影響由小到大。1:1 port 自 hl orders.py:141-184。

      1. 完全相同 → 保留不動（matched）
      2. 同 slot 但價/量不同 → modify 就地改（影響最小）
      3. 目標多出來的 → place 新掛
      4. 我多出來的 → cancel 取消
    """
    # 1. 完全相符
    used: set[int] = set()
    matched: set[int] = set()
    rem_desired: list[OrderSpec] = []
    for d in desired:
        hit = next(
            (
                pair
                for pair in mine
                if pair[0] not in used
                and _orders_match(d, pair[1], px_rel_tol=px_rel_tol, size_tol=size_tol)
            ),
            None,
        )
        if hit:
            used.add(hit[0])
            matched.add(hit[0])
        else:
            rem_desired.append(d)
    rem_mine = [pair for pair in mine if pair[0] not in used]

    # 2~4. 依 slot 配對：同 slot 內依參考價排序後逐一對應
    mine_by_slot: dict[tuple, list[tuple[int, OrderSpec]]] = defaultdict(list)
    for oid, spec in rem_mine:
        mine_by_slot[_slot_key(spec)].append((oid, spec))
    des_by_slot: dict[tuple, list[OrderSpec]] = defaultdict(list)
    for d in rem_desired:
        des_by_slot[_slot_key(d)].append(d)

    modifies: list[tuple[int, OrderSpec]] = []
    to_place: list[OrderSpec] = []
    to_cancel: list[int] = []
    for slot in set(mine_by_slot) | set(des_by_slot):
        ms = sorted(mine_by_slot.get(slot, []), key=lambda pair: _ref_px(pair[1]))
        ds = sorted(des_by_slot.get(slot, []), key=_ref_px)
        for i, d in enumerate(ds):
            if i < len(ms):
                modifies.append((ms[i][0], d))  # 配到 → 改單
            else:
                to_place.append(d)  # 目標較多 → 新掛
        for j in range(len(ds), len(ms)):
            to_cancel.append(ms[j][0])  # 我較多 → 取消

    return ReconcilePlan(
        modifies=tuple(modifies),
        to_place=tuple(to_place),
        to_cancel=tuple(to_cancel),
        matched=frozenset(matched),
    )


def _build_desired(
    leader_orders: list[OpenOrder],
    scale: Decimal,
    *,
    min_notional: Decimal,
    size_decimals: Callable[[str], int],
    my_positions: dict[str, Position],
    protected: set[str],
) -> tuple[list[OrderSpec], list[SkippedOrder]]:
    """將 leader 掛單縮放成「我方期望掛單規格」清單。1:1 port 自 hl orders.py:79-127。

    過濾順序（與 hl 一致，決定優先順位）：
      1. 現貨標的（不支援跟單）→ 記 SkippedOrder(reason="spot")，先排除避免下單錯誤。
      2. 抗單保護標的且非 reduce-only（拒絕補倉，但保留減倉/止盈止損單）→
         記 SkippedOrder(reason="protected")。
      3. reduce-only 且我方無對應部位（G4：交易所會拒絕這種單）→
         記 SkippedOrder(reason="reduce_only_no_pos")（hl 原本靜默跳過，見模組 docstring）。
      4. 縮放捨入後 size<=0 或 px<=0 → 靜默跳過（hl 同樣不記錄，無分類原因）。
      5. 縮放後名目值 < min_notional → 記 SkippedOrder(reason="small")，含正確 notional。
    """
    desired: list[OrderSpec] = []
    skipped: list[SkippedOrder] = []
    for o in leader_orders:
        coin = o.coin
        if _is_spot_coin(coin):
            skipped.append(SkippedOrder(coin=coin, notional=Decimal("0"), reason="spot"))
            continue
        if coin in protected and not o.reduce_only:
            skipped.append(SkippedOrder(coin=coin, notional=Decimal("0"), reason="protected"))
            continue
        if o.reduce_only and coin not in my_positions:
            # G4: 沒有對應部位的 reduce-only 單會被交易所拒，直接跳過
            skipped.append(
                SkippedOrder(coin=coin, notional=Decimal("0"), reason="reduce_only_no_pos")
            )
            continue
        sz_dec = size_decimals(coin)
        size = _round_size(o.sz * scale, sz_dec)
        px = o.limit_px or o.trigger_px or Decimal("0")
        if size <= 0 or px <= 0:
            continue
        notional = size * px
        if notional < min_notional:
            skipped.append(SkippedOrder(coin=coin, notional=notional, reason="small"))
            continue
        desired.append(
            OrderSpec(
                coin=coin,
                is_buy=o.is_buy,
                sz=size,
                limit_px=o.limit_px,
                reduce_only=o.reduce_only,
                is_trigger=o.is_trigger,
                tpsl=o.tpsl,
                trigger_px=o.trigger_px,
                is_market=False,
            )
        )
    return desired, skipped
