"""對帳引擎純函式層。1:1 port 自 hl-copytrader src/orders.py:33-184，Decimal 化、
容忍度改為參數注入（hl 用模組級常數；spark 由呼叫端傳入 CopySettings 的
`px_rel_tol`/`size_tolerance`，符合「同一份配置貫穿引擎」的慣例）。

刻意未移植清單（hl-copytrader src/orders.py 原始碼行號對應）：
  - `HOLDING_PROTECTION_ENABLED` 的 Z-Score 異常持倉偵測（hl protection.py）——不在
    本任務範圍；這裡的 `protected: set[str]` 是呼叫端已算好的結果，直接消費。
  - hl B 段的 `sync_positions`（部位安全網本體）——不在本任務範圍；`sync_open_orders`
    改吃 `safety_net: Callable[[], dict] | None` callable，呼叫端自行決定安全網實作
    要不要接、怎麼接。
  - M1 單 DEX：hl 的 `failed_dexs` 過濾（hl orders.py:328-329）不移植。

Task 8（本次新增，於檔案下半段）：`_set_entry_leverage`（hl:187-193）、
`_reconcile_orders`（hl:196-274）、`sync_open_orders`（hl:277-368）——含 I/O 副作用
（呼叫 ExecutorPort 下單/改單/撤單/設槓桿、Notifier 告警）。結構偏差詳見各函式
docstring。

結構差異（相對 hl 語意的刻意調整，逐項說明）：
  - `_plan` 回傳型別改為 frozen dataclass `ReconcilePlan`，`matched` 欄位從 hl 的
    「相符張數」(int) 升級為「相符 oid 的 frozenset[int]」——語意更精確（呼叫端可知道
    具體哪些單被跳過不動），資訊量涵蓋 hl 原本的 `len(matched)`。
  - `OrderSpec` 不攜帶 hl 的 `order_type_name`（人類可讀字串，hl 只在解析階段用它衍生
    tpsl/is_market，衍生結果已是獨立欄位）——對比對與下單語意零影響。
  - `_build_desired` 的 reduce-only-無部位案例（hl 的 G4，hl:103-104）在 hl 原始碼裡是
    「靜默跳過」（continue，不記錄進任何 skipped 清單）；spark 版本明確記錄為
    `SkippedOrder(reason="reduce_only_no_pos")`，可觀測性優於 hl（呼叫端這裡要求要能
    看到這個原因，見 Task 7 spec）。size<=0 或 px<=0 的邊界情形則維持 hl 的靜默跳過
    （無 SkippedOrder 記錄），因為 hl 這裡本來就沒有分類原因可記。
"""
import hashlib
import html
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable, Mapping

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
    tif: str = "Gtc"  # 限價單 time-in-force；hl monitor.py:205 預設 "Gtc"


@dataclass(frozen=True)
class SkippedOrder:
    """`_build_desired` 過濾掉的目標單記錄，附原因供上層記錄/告警。"""

    coin: str
    notional: Decimal
    reason: str  # ∈ {"small", "spot", "protected", "reduce_only_no_pos",
    #                 "cost_breaker", "ro_capped_out"}


@dataclass(frozen=True)
class ReconcilePlan:
    """`_plan` 的規劃結果。`matched` 是保留不動（完全相符）的我方 oid 集合。"""

    modifies: tuple[tuple[int, OrderSpec], ...]
    to_place: tuple[OrderSpec, ...]
    to_cancel: tuple[int, ...]
    matched: frozenset[int]


def spec_from_open_order(o: OpenOrder) -> OrderSpec:
    """exchange 型別 → 引擎型別轉換（欄位 1:1 直通，含 is_market/tif）。"""
    return OrderSpec(
        coin=o.coin,
        is_buy=o.is_buy,
        sz=o.sz,
        limit_px=o.limit_px,
        reduce_only=o.reduce_only,
        is_trigger=o.is_trigger,
        tpsl=o.tpsl,
        trigger_px=o.trigger_px,
        is_market=o.is_market,
        tif=o.tif,
    )


def _prices_equal(a: Decimal, b: Decimal, rel: Decimal) -> bool:
    """相對容忍度價格相等判定。1:1 port 自 hl orders.py:40-43（rel 由呼叫端注入，非硬編）。"""
    if a == 0 and b == 0:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b), Decimal("1e-8"))


def _orders_match(
    desired: OrderSpec, mine: OrderSpec, *, px_rel_tol: Decimal, size_tol: Decimal
) -> bool:
    """判斷我的一張掛單是否等同於某個目標縮放後的掛單。1:1 port 自 hl orders.py:46-76。

    注意：**不比較 tif**——hl 原始碼的 `_orders_match` 沒有 tif 比較項（已對照確認），
    照抄此行為；tif 只在下單時經 `_order_type_and_px` 傳遞，不參與相符判定。
    `is_market` 僅在雙方皆為 trigger 單時比較（同 hl:65）。
    """
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
    no_new_exposure: bool = False,
) -> tuple[list[OrderSpec], list[SkippedOrder]]:
    """將 leader 掛單縮放成「我方期望掛單規格」清單。1:1 port 自 hl orders.py:79-127。

    過濾順序（與 hl 一致，決定優先順位）：
      1. 現貨標的（不支援跟單）→ 記 SkippedOrder(reason="spot")，先排除避免下單錯誤。
      1.5 成本熔斷器（spark 新增，非 hl）：`no_new_exposure` 且非 reduce-only →
         記 SkippedOrder(reason="cost_breaker")。⭐ **reduce-only 一律放行**（D5
         硬規則，絕不把客戶困在部位裡）——語意與 protected 完全相同，差別只在
         protected 是 per-coin、成本熔斷是帳戶級。
      2. 抗單保護標的且非 reduce-only（拒絕補倉，但保留減倉/止盈止損單）→
         記 SkippedOrder(reason="protected")。
      3. reduce-only 且我方無對應部位（G4：交易所會拒絕這種單）→
         記 SkippedOrder(reason="reduce_only_no_pos")（hl 原本靜默跳過，見模組 docstring）。
      3.5 reduce-only 非 trigger 且方向與部位不符（ro 賣需多倉、ro 買需空倉）→
         同樣記 reduce_only_no_pos（交易所會把這種單修剪到 0，語意等同無部位）。
      4. 縮放捨入後 size<=0 或 px<=0 → 靜默跳過（hl 同樣不記錄，無分類原因）。
      5. 縮放後名目值 < min_notional → 記 SkippedOrder(reason="small")，含正確 notional。

    ⭐ reduce-only 每幣累計封頂（spark 新增，非 hl；2026-07-28 00:33 CRIT 事故修法）：
    Hyperliquid 會把 ro 掛單修剪到「每幣 ro 總量 ≤ 目前部位」（實測：部位 0.0289 時
    ro 賣單 0.0457 被剪到 0.0289；部位 0.0918 時兩張 0.0456+0.0461=0.0917 全額存活
    ——累計封頂，非逐單）。desired 若不做同樣封頂，`_verify_diff` 會把同一張單同時
    判進「缺少」與「多餘」→ 重試 cancel+place 也被同樣修剪 → 誤發 CRIT。
    修法（工程原則 1 同源同基準——desired 與實際單都以 follower 部位為約束）：
      - 對每幣的非 trigger ro 期望單，依「越接近成交越優先」分配容量 |部位|：
        減多倉（賣）價低者先、減空倉（買）價高者先；同價位以原始順序 tie-break
        （sorted 穩定排序），確保逐輪分配確定性、不產生 modify churn。
        ⚠️ 多張同時超額時交易所實際的修剪**順序**是推論、未實測——實測證據只
        涵蓋「單張被剪到部位量」與「未超額全存活」兩種形狀；「接近成交優先」
        是對交易所行為的合理假設，若日後實測相反，只需改此處排序方向。
      - `capped_sz = min(rounded_sz, 剩餘容量)`（只縮不放大），容量歸零後的單略過。
      - 順序是先封頂、再過 min_notional：被封頂成小額的「部分減倉」單交易所照拒
        （$10 豁免只適用全平），必須用封頂後的名目過濾。
      - trigger ro（止盈止損）不參與封頂也不消耗容量：修剪實測證據只涵蓋掛在簿上
        的 ro 限價單；trigger 未觸發前不佔簿，若對其封頂而交易所不剪，反而製造
        永久 mismatch → churn。
    """
    # entries 以 None 佔位保留原始順序；ro 封頂候選延後統一分配容量。
    entries: list[OrderSpec | None] = []
    skipped: list[SkippedOrder] = []
    # coin -> [(entries 索引, 縮放捨入後 sz, 參考價, sz_dec, 原掛單)]
    ro_pending: dict[str, list[tuple[int, Decimal, Decimal, int, OpenOrder]]] = defaultdict(list)

    def _spec_of(o: OpenOrder, size: Decimal) -> OrderSpec:
        return OrderSpec(
            coin=o.coin,
            is_buy=o.is_buy,
            sz=size,
            limit_px=o.limit_px,
            reduce_only=o.reduce_only,
            is_trigger=o.is_trigger,
            tpsl=o.tpsl,
            trigger_px=o.trigger_px,
            is_market=o.is_market,  # 1:1 hl orders.py:123——取 leader 掛單實際值
            tif=o.tif,              # 1:1 hl orders.py:124
        )

    for o in leader_orders:
        coin = o.coin
        if _is_spot_coin(coin):
            skipped.append(SkippedOrder(coin=coin, notional=Decimal("0"), reason="spot"))
            continue
        if no_new_exposure and not o.reduce_only:
            skipped.append(SkippedOrder(coin=coin, notional=Decimal("0"),
                                        reason="cost_breaker"))
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
        if o.reduce_only and not o.is_trigger:
            szi = my_positions[coin].szi
            direction_ok = (szi < 0) if o.is_buy else (szi > 0)
            if not direction_ok:
                # ro 賣配空倉／ro 買配多倉：交易所會修剪到 0，語意等同無部位
                skipped.append(SkippedOrder(coin=coin, notional=Decimal("0"),
                                            reason="reduce_only_no_pos"))
                continue
            ro_pending[coin].append((len(entries), size, px, sz_dec, o))
            entries.append(None)  # 佔位，容量分配後回填
            continue
        notional = size * px
        if notional < min_notional:
            skipped.append(SkippedOrder(coin=coin, notional=notional, reason="small"))
            continue
        entries.append(_spec_of(o, size))

    # ── reduce-only 每幣累計封頂（僅非 trigger；方向檢查保證同幣候選同方向）──
    for coin, cands in ro_pending.items():
        remaining = abs(my_positions[coin].szi)
        is_buy = cands[0][4].is_buy
        # 越接近成交越優先：減多倉（賣）價低者先；減空倉（買）價高者先。
        for idx, size, px, sz_dec, o in sorted(cands, key=lambda c: c[2], reverse=is_buy):
            capped = min(size, _round_size(remaining, sz_dec))  # 只縮不放大
            if capped <= 0:
                # 容量歸零 → 略過，但記錄不靜默（2026-07-28 審查 F4）
                skipped.append(SkippedOrder(coin=coin, notional=Decimal("0"),
                                            reason="ro_capped_out"))
                continue
            notional = capped * px
            if notional < min_notional:
                # 先封頂、再過名目：封頂後的小額部分減倉單交易所照拒。
                # 被濾掉的單**不扣容量**（2026-07-28 審查 F1）：它不會被送到
                # 交易所，交易所端容量完整——先扣會把同幣後續的保護性減倉單無故改小。
                skipped.append(SkippedOrder(coin=coin, notional=notional, reason="small"))
                continue
            remaining -= capped  # 只有真的納入 desired 的單才消耗容量
            entries[idx] = _spec_of(o, capped)

    return [e for e in entries if e is not None], skipped


# ═══════════════════════════════════════════════════════════════════════
# Task 8：對帳狀態機（I/O 副作用層）。port 自 hl orders.py:187-368。
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class ReconcileState:
    """跨輪次的對帳狀態。取代 hl 的模組層 `_modify_fail_until`（hl orders.py:37）——
    模組層全域在測試與多實例下是隱形共享狀態，改為顯式注入。"""

    modify_fail_until: dict[str, float] = field(default_factory=dict)  # coin -> epoch


@dataclass(frozen=True)
class ReconcileResult:
    """`_reconcile_orders` 的統計結果。對應 hl orders.py:273-274 的回傳 dict，外加
    `skipped_small`（由 `sync_open_orders` 以 `_build_desired` 的結果補入）。"""

    placed: int
    cancelled: int
    modified: int
    matched: int
    sync_failed: bool
    skipped_small: tuple[SkippedOrder, ...]


@dataclass(frozen=True)
class CycleReport:
    """`sync_open_orders` 單輪報告。對應 hl orders.py:351-361 的回傳 dict。
    `tripped` 預留給回撤熔斷（Task 13），本層恆為預設 False。"""

    reconcile: ReconcileResult
    safety_net: dict
    scale: Decimal
    tripped: bool = False


def _set_entry_leverage(
    ex, desired: OrderSpec, leverage_by_coin: Mapping[str, tuple[int, bool]], notifier
) -> None:
    """進場單（非 reduce-only）下單前設定名目槓桿。port 自 hl orders.py:187-193。

    結構偏差：hl 從 `trader.entry_leverage(coin)`/`trader.entry_is_cross(coin)` 現算
    （內部查 meta 快取，hl trader.py:81-108）；spark 由呼叫端預算好 `leverage_by_coin`
    （coin -> (leverage, is_cross)）注入。coin 不在 map 內 → 靜默跳過（呼叫端沒給
    就是不設，等同 hl dry-run 無 info 時的降級路徑）。

    失敗處理（比照 positions.py `_try_update_leverage`；hl 在 Trader.set_leverage 內
    自行 tg.alert，hl trader.py:127）：`ex.update_leverage` 回 False → warn 告警但
    流程繼續（best-effort：槓桿設定失敗不阻擋下單，交易所會沿用該 coin 既有槓桿）。
    """
    if desired.reduce_only:
        return
    pair = leverage_by_coin.get(desired.coin)
    if pair is None:
        return
    leverage, is_cross = pair
    ok = ex.update_leverage(desired.coin, leverage, is_cross)
    if not ok:
        notifier.warn(
            "orders",
            f"槓桿設定失敗 {desired.coin} leverage={leverage}x is_cross={is_cross}",
            dedup_key=f"lev_fail:{desired.coin}",
        )


def _verify_diff(
    desired: list[OrderSpec],
    after: list[OpenOrder],
    *,
    px_rel_tol: Decimal,
    size_tol: Decimal,
) -> tuple[list[OrderSpec], list[OpenOrder]]:
    """settle 驗證的 missing/extra 計算。1:1 hl orders.py:250-251/266-267——
    與 `_plan` 用同一個 `_orders_match` 與同一組容忍度（工程原則 1：同源同基準）。"""
    after_pairs = [(m, spec_from_open_order(m)) for m in after]
    missing = [
        d
        for d in desired
        if not any(
            _orders_match(d, s, px_rel_tol=px_rel_tol, size_tol=size_tol)
            for _m, s in after_pairs
        )
    ]
    extra = [
        m
        for m, s in after_pairs
        if not any(
            _orders_match(d, s, px_rel_tol=px_rel_tol, size_tol=size_tol) for d in desired
        )
    ]
    return missing, extra


# CRIT 訊息上限（2026-07-28 審查 F2）：Telegram sendMessage 超過 4096 字元回 400，
# notifier 吞掉 False → 最需要告警的場景（整本掛單全 missing）整則 CRIT 靜默消失。
# 逐類（missing/extra/補單失敗）最多列 _CRIT_MAX_ITEMS 張、其餘「另 M 張」摘要；
# 全文再保險截斷到 _CRIT_MAX_LEN（留餘裕給 notifier 的前綴與抑制次數後綴）。
_CRIT_MAX_ITEMS = 5
_CRIT_MAX_LEN = 3500


def _fmt_missing(missing: list[OrderSpec]) -> str:
    return ", ".join(
        f"{d.coin} {'B' if d.is_buy else 'S'} {d.sz}@{_ref_px(d)}"
        + (" [ro]" if d.reduce_only else "")
        for d in missing
    )


def _fmt_extra(extra: list[OpenOrder]) -> str:
    """extra 側逐單：oid 幣 方向 剩餘量@價 [ro] 原量X。

    2026-07-28 事故教訓：原版只印 oid——使用者拿著 CRIT 完全無法判斷那張單是什麼。
    剩餘量（sz）與原量（origSz）的差正是「被交易所修剪」的觀測點。"""
    parts = []
    for m in extra:
        px = m.trigger_px if m.is_trigger else m.limit_px
        s = f"oid={m.oid} {m.coin} {'B' if m.is_buy else 'S'} {m.sz}@{px}"
        if m.reduce_only:
            s += " [ro]"
        if m.orig_sz is not None:
            s += f" 原量{m.orig_sz}"
        parts.append(s)
    return ", ".join(parts)


def _sync_fail_dedup_key(account: str, missing: list[OrderSpec],
                         extra: list[OpenOrder]) -> str:
    """order-sync CRIT 的去重鍵：同一持續狀態重複告警可被 notifier 去重，
    內容變化時仍會發新告警。

    刻意**不含 oid**（每輪重試 cancel+place 都會換新 oid，含 oid 等於永不去重），
    也**不含 sz**（兩側皆然，2026-07-28 審查 F3）：missing 的 desired sz 隨
    scale（leader 權益/weight）每輪微動、extra 的剩餘量隨部位微動——含 sz 會讓
    同一持續狀態每輪產生新 key，dedup 失效變成告警轟炸。鍵只含 幣/方向/ro/價。"""
    m_key = sorted(
        (d.coin, d.is_buy, str(_ref_px(d)), d.reduce_only) for d in missing
    )
    e_key = sorted(
        (o.coin, o.is_buy, str(o.trigger_px if o.is_trigger else o.limit_px),
         o.reduce_only)
        for o in extra
    )
    digest = hashlib.sha256(repr((account, m_key, e_key)).encode()).hexdigest()[:16]
    return f"order_sync_failed:{digest}"


def _diagnose_trimmed_coins(
    missing: list[OrderSpec],
    extra: list[OpenOrder],
    my_positions: Mapping[str, Position],
    *,
    px_rel_tol: Decimal,
    size_tol: Decimal,
) -> list[str]:
    """找出「ro 掛單被交易所修剪至部位上限（部位落後）」形狀的幣，供 CRIT 自動診斷。

    判準（缺一不診斷，避免誤導）：missing 的 ro 單與 extra 的 ro 單以
    （同幣、同方向、價格在 px_rel_tol 內）配對，extra 剩餘量 < missing 期望量；
    且該幣 extra 側 ro 剩餘量合計與 |部位| 的相對差在 size_tol 內——
    這是「每幣累計封頂到部位」的指紋（2026-07-28 實測行為）。"""
    coins: list[str] = []
    extra_ro = [o for o in extra if o.reduce_only and not o.is_trigger]
    for coin in sorted({d.coin for d in missing if d.reduce_only and not d.is_trigger}):
        pos = my_positions.get(coin)
        if pos is None:
            continue
        paired = any(
            o.coin == coin and o.is_buy == d.is_buy and o.sz < d.sz
            and _prices_equal(_ref_px(d), o.limit_px, px_rel_tol)
            for d in missing
            if d.coin == coin and d.reduce_only and not d.is_trigger
            for o in extra_ro
        )
        if not paired:
            continue
        abs_pos = abs(pos.szi)
        total = sum((o.sz for o in extra_ro if o.coin == coin), Decimal("0"))
        if abs(total - abs_pos) <= size_tol * max(abs_pos, Decimal("1e-8")):
            coins.append(coin)
    return coins


def _reconcile_orders(
    ex,
    desired: list[OrderSpec],
    my_orders: list[OpenOrder],
    *,
    settings,
    notifier,
    state: ReconcileState,
    live: bool,
    clock=time.time,
    sleep_fn=time.sleep,
    my_positions: Mapping[str, Position] | None = None,
) -> ReconcileResult:
    """掛單對帳狀態機。port 自 hl orders.py:196-274，影響/風險由小到大：

      1. 相同的單保留不動（matched）。
      2. 同 slot 但價量不同 → 先試 modify 就地改（保留排隊優先權）。
         TTL 內（近期 modify 失敗過的 coin）或 modify_policy=="cancel-place" → 直接
         降級；modify 回 False → 登記 TTL（clock()+settings.modify_fail_ttl_s）並降級。
         注意：modify 回 False **不保證舊單仍在**（HL batchModify 非原子，2026-07-19 實測）；
         降級路徑的 cancel 可能撲空回 False，這是預期行為（`cancelled` 計數會低報），
         終態仍正確——place 新單後每輪重讀掛單會自然收斂。
      3. **先 cancel（降級舊單 + to_cancel，釋放保證金）後 place（to_place + 降級新規格）**
         ——順序是紅線（hl orders.py:228-243）。
      4. settle 驗證（僅 live）：sleep → 重抓 → 同容忍度算 missing/extra → 不符則
         先撤 extra 再補 missing（補單走 `place_with_reason`，拒因收集）→ 再
         sleep+重抓再驗 → 仍不符 → sync_failed=True + notifier.critical
         （工程原則 3：安全關鍵失敗大聲告警，絕不吞掉）。CRIT 內容為可自診斷格式
         （逐單價量方向＋[ro]＋原量、部位行、補單拒因、trim 形狀自動診斷），
         dedup_key 以內容雜湊（不含 oid）讓同一持續狀態被去重（2026-07-28 事故修法）。

    結構偏差（相對 hl，逐項）：
      - `trader.live_trading and my_address`（hl:247）→ 顯式 `live` 參數；
        my_address 不需要——`ex.get_open_orders()` 已綁定自己帳號。
      - 模組層 `_modify_fail_until` → 注入的 `ReconcileState`。
      - `tg.alert_order_sync_failed`（hl:271）→ `notifier.critical("orders", ...)`。
      - modify/place 前的 `_set_entry_leverage`（hl:219/240/259）不在本函式內——
        鎖定簽章無 leverage 來源，已由 `sync_open_orders` 於對帳前對全部 desired
        coin 前置設定（超集覆蓋，語意見該函式 docstring）。
      - `modify_policy=="cancel-place"` 是 spark 新增開關（hl 無），全部 modify 直接
        降級為 cancel+place。
      - 回傳 frozen dataclass（`skipped_small` 由上層補入，本函式恆回空 tuple）。

    例外語意：`ex.place/modify/cancel/get_open_orders` 拋出的 transient 例外
    （ConnectionError 等）**原樣上拋、不在此吞掉**——已執行的動作不回滾（cancel 冪等、
    place 由下一輪對帳自癒）。runner（Task 12）必須 try/except 本函式並計入
    `settings.max_consecutive_errors`（工程原則 2：transient 交給上層重試邊界，
    此處不盲重試非冪等寫入）。
    """
    mine_pairs = [(o.oid, spec_from_open_order(o)) for o in my_orders]
    coin_by_oid = {o.oid: o.coin for o in my_orders}
    plan = _plan(
        desired, mine_pairs, px_rel_tol=settings.px_rel_tol, size_tol=settings.size_tolerance
    )

    # ── 1. 就地改單；TTL 內/policy 降級/失敗的退回「取消舊單 + 重掛新單」──
    now = clock()
    modified = 0
    fallback: list[tuple[int, str, OrderSpec]] = []  # (舊單 oid, coin, 新單 spec)
    for oid, spec in plan.modifies:
        coin = spec.coin
        if now < state.modify_fail_until.get(coin, 0):
            fallback.append((oid, coin, spec))  # 近期失敗過 → 直接退回 cancel+place
            continue
        if settings.modify_policy == "cancel-place":
            fallback.append((oid, coin, spec))  # 政策指定：不走 modify
            continue
        if ex.modify(oid, spec):
            modified += 1
            state.modify_fail_until.pop(coin, None)
        else:
            state.modify_fail_until[coin] = now + settings.modify_fail_ttl_s
            fallback.append((oid, coin, spec))

    # ── 2. 先取消（改單退回的舊單 + 目標已無的舊單）釋放保證金 ──────────
    cancelled = 0
    for oid, coin, _spec in fallback:
        if ex.cancel(coin, oid):
            cancelled += 1
    for oid in plan.to_cancel:
        if ex.cancel(coin_by_oid[oid], oid):
            cancelled += 1

    # ── 3. 後掛新單（目標新增的 + 改單退回的）保證金已釋放 ──────────────
    placed = 0
    for d in list(plan.to_place) + [spec for _oid, _coin, spec in fallback]:
        if ex.place(d):
            placed += 1

    # ── 4. 驗證（僅 live）→ 不符先撤多再補缺 → 仍不符發 critical ────────
    sync_failed = False
    if live:
        sleep_fn(settings.settle_seconds)
        after = ex.get_open_orders()
        missing, extra = _verify_diff(
            desired, after, px_rel_tol=settings.px_rel_tol, size_tol=settings.size_tolerance
        )
        if missing or extra:
            for m in extra:  # 先撤多（釋放保證金）
                if ex.cancel(m.coin, m.oid):
                    cancelled += 1
            place_fails: list[tuple[OrderSpec, str]] = []
            for d in missing:  # 再補缺；失敗不得靜默，拒因收集進 CRIT（工程原則 3）
                ok, reason = ex.place_with_reason(d)
                if ok:
                    placed += 1
                else:
                    place_fails.append((d, reason))

            sleep_fn(settings.settle_seconds)
            after = ex.get_open_orders()
            missing, extra = _verify_diff(
                desired, after, px_rel_tol=settings.px_rel_tol, size_tol=settings.size_tolerance
            )
            if missing or extra:
                sync_failed = True
                # ── CRIT 必須可自行診斷（2026-07-28 事故教訓）：逐單價量方向、
                # 部位快照（本輪既有資料，不新增 API 呼叫）、補單拒因、自動診斷。
                involved = sorted({d.coin for d in missing} | {o.coin for o in extra})
                if my_positions is None:
                    pos_line = "未提供部位快照"
                else:
                    pos_line = ", ".join(
                        f"{c} {my_positions[c].szi}" if c in my_positions
                        else f"{c} 無部位"
                        for c in involved
                    )
                def _more(n_total: int) -> str:
                    hidden = n_total - _CRIT_MAX_ITEMS
                    return f"，另 {hidden} 張" if hidden > 0 else ""

                lines = [
                    f"掛單重試後仍不符：缺少 {len(missing)}"
                    f"［{_fmt_missing(missing[:_CRIT_MAX_ITEMS])}{_more(len(missing))}］、"
                    f"多餘 {len(extra)}"
                    f"［{_fmt_extra(extra[:_CRIT_MAX_ITEMS])}{_more(len(extra))}］，"
                    "需人工介入",
                    f"部位：{pos_line}",
                ]
                if place_fails:
                    # 拒因先 HTML escape：notifier 走 parse_mode=HTML，拒因含 '<'
                    # 會讓整則 400 消失（2026-07-28 審查改善 c）
                    lines.append("補單失敗：" + "；".join(
                        f"{_fmt_missing([d])}："
                        f"{html.escape(r, quote=False) or '交易所未回原因'}"
                        for d, r in place_fails[:_CRIT_MAX_ITEMS]
                    ) + _more(len(place_fails)))
                trimmed = _diagnose_trimmed_coins(
                    missing, extra, my_positions or {},
                    px_rel_tol=settings.px_rel_tol, size_tol=settings.size_tolerance,
                )
                if trimmed:
                    lines.append(
                        f"診斷：{'、'.join(trimmed)} 的 reduce-only 掛單已被交易所"
                        "修剪至部位上限（部位落後），屬暫態，通常下一輪自癒；"
                        "下一輪仍告警才需人工介入"
                    )
                elif not place_fails:
                    lines.append(
                        "原因未能自動判定，排查：POST /info historicalOrders "
                        "查被拒單與修剪"
                    )
                text = "\n".join(lines)
                if len(text) > _CRIT_MAX_LEN:  # 保險截斷：寧可少講也要送得出去
                    text = text[:_CRIT_MAX_LEN] + "…（已截斷）"
                notifier.critical(
                    "orders",
                    text,
                    dedup_key=_sync_fail_dedup_key(
                        getattr(ex, "my_address", ""), missing, extra
                    ),
                )

    return ReconcileResult(
        placed=placed,
        cancelled=cancelled,
        modified=modified,
        matched=len(plan.matched),
        sync_failed=sync_failed,
        skipped_small=(),
    )


def sync_open_orders(
    ex,
    leader_orders: list[OpenOrder],
    my_orders: list[OpenOrder],
    my_positions: dict[str, Position],
    scale: Decimal,
    *,
    settings,
    notifier,
    state,
    live: bool,
    protected: set[str] = frozenset(),
    no_new_exposure: bool = False,
    leverage_by_coin: Mapping[str, tuple[int, bool]] | None = None,
    safety_net: Callable[[], dict] | None = None,
    skip_safety_net: bool = False,
    clock=time.time,
    sleep_fn=time.sleep,
) -> CycleReport:
    """主同步入口：A 段掛單對帳 + B 段部位安全網。port 自 hl orders.py:277-368。

    結構偏差（相對 hl，逐項）：
      - scale 由呼叫端算好傳入（hl:296-301 在函式內呼叫 `compute_scale_factor`）。
      - 抗單保護偵測（hl:308-311 的 `get_anti_holding_flags`）由呼叫端做，這裡直接
        消費 `protected` 集合；被擋的補倉單警告沿用 hl:323-326 語意但無 Z 分數。
      - B 段 `sync_positions`（hl:333-349）→ `safety_net` callable：None 或
        `skip_safety_net=True` 時回 `{"skipped": True}`，否則呼叫並把回傳 dict
        原樣放進 `CycleReport.safety_net`。live 時重抓部位（hl:338-340）是
        callable 自己的責任。
      - `_set_entry_leverage` 呼叫點：hl 在每次 modify/place 前逐單呼叫（hl:219/240/
        259，靠 `Trader.set_leverage` 的 per-coin 快取去重，hl trader.py:116-119）；
        spark 的 `_reconcile_orders` 鎖定簽章無 leverage 來源，故提前到對帳前對
        desired 內每個非 reduce-only coin 各設一次（顯式去重，淨效果等價：每個會被
        下單的 coin 在任何動作前都已設好槓桿；matched coin 多設一次是無風險冪等
        操作——名目槓桿只影響保證金佔用，不影響倉位大小）。
      - `failed_dexs` 過濾（hl:328-329）不移植——M1 單 DEX。
      - 對 hl 的刻意增強：skipped_small 警告帶 `dedup_key=f"skipped_small:{coin}"`
        （hl:319 無去重；spark 每分鐘一輪 vs hl 每小時一輪，小帳戶會每輪必噴）。
        protected 警告同理帶 `dedup_key=f"protected:{coin}"`。現貨跳過維持 hl 語意
        （hl:320-322 僅 logger.info、不進 tg）——不發 notifier。
      - `_build_desired` 需要 per-coin size decimals（hl:105 `trader._get_sz_decimals`）；
        鎖定簽章無此參數，故要求 `ex` 額外提供 `get_size_decimals(coin) -> int`
        （duck-typed，超出 ExecutorPort 最小介面；缺少會在 A 段大聲 AttributeError，
        不會靜默錯捨入）。
    """
    # ── A. 掛單對帳 ──────────────────────────────────────────────────
    desired, skipped = _build_desired(
        leader_orders,
        scale,
        min_notional=settings.min_order_notional,
        size_decimals=ex.get_size_decimals,
        my_positions=my_positions,
        protected=set(protected),
        no_new_exposure=no_new_exposure,
    )
    small = tuple(s for s in skipped if s.reason == "small")
    for s in small:
        notifier.warn(
            "orders",
            f"[SKIP] {s.coin} 換算名目值 ${s.notional:.2f} < "
            f"${settings.min_order_notional}，跳過掛單",
            dedup_key=f"skipped_small:{s.coin}",
        )
    protected_coins = sorted({s.coin for s in skipped if s.reason == "protected"})
    for coin in protected_coins:
        notifier.warn(
            "orders",
            f"[抗單保護] 拒絕複製 {coin} 補倉單",
            dedup_key=f"protected:{coin}",
        )
    cost_coins = sorted({s.coin for s in skipped if s.reason == "cost_breaker"})
    for coin in cost_coins:
        # 單一 dedup_key（非 per-coin）：熔斷是帳戶級事件，per-coin 去重會讓
        # leader 掛 20 個標的時噴 20 則講同一件事的告警。真正的觸發數字已由
        # costbreaker 的 critical 帶出，這裡只是說明「這一輪誰被擋了」。
        notifier.warn(
            "orders",
            f"[成本熔斷] 停止開新倉中，跳過 {coin} 進場掛單（reduce-only 不受影響）",
            dedup_key="cost_breaker_orders",
        )

    if leverage_by_coin is not None:
        seen: set[str] = set()
        for d in desired:
            if d.reduce_only or d.coin in seen:
                continue
            seen.add(d.coin)
            _set_entry_leverage(ex, d, leverage_by_coin, notifier)

    rec = _reconcile_orders(
        ex,
        desired,
        my_orders,
        settings=settings,
        notifier=notifier,
        state=state,
        live=live,
        clock=clock,
        sleep_fn=sleep_fn,
        my_positions=my_positions,  # CRIT 自診斷用（本輪快照，不新增 API 呼叫）
    )
    rec = replace(rec, skipped_small=small)

    # ── B. 部位安全網 ────────────────────────────────────────────────
    if skip_safety_net or safety_net is None:
        net: dict = {"skipped": True}
    else:
        net = safety_net()

    return CycleReport(reconcile=rec, safety_net=net, scale=scale)
