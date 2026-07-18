"""ExecutorPort Protocol——引擎唯一寫入通道的介面——與其正式實作 ActionExecutor。

OrderSpec 於 copytrade/orders.py（Task 7）定義。
Task 8/11/13 的 fake 與 Task 12 的 ActionExecutor 都必須符合此 Protocol。
builder/agent_signer/slippage 是 executor 建構時注入，不在本介面上（紅線 3）。

M1 已知限制（trigger 單）：adapter 尚無 trigger 下單支援（`place_order` 只組
`{"limit": {"tif": ...}}` order_type）；ActionExecutor 對 `is_trigger=True` 的
place/modify 一律不下單、記 `ActionRecord(kind="skip_trigger")`。shadow 期間若
leader 出現 trigger 單，會浮現於 skip_trigger 記錄（loop 端讀 records 發 warn
帶 per-coin dedup）。連帶效應：live 模式下 trigger desired 永遠不會出現在
settle 重抓結果中 → `_reconcile_orders` 會將其計入 missing 並發 critical——
大聲失敗是刻意的（工程原則 3），在 trigger 支援落地前不應對含 trigger 的
leader 開 live。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from spark.exchange.base import BuilderCode, Order, OpenOrder, OrderResult, Signer

if TYPE_CHECKING:
    from spark.copytrade.config import CopySettings
    from spark.copytrade.orders import OrderSpec


@runtime_checkable
class ExecutorPort(Protocol):
    """引擎唯一寫入通道。包裝了 Hyperliquid API 的有序下單/修改/撤單操作。

    方法簽章中 OrderSpec 使用字串 forward reference，因 OrderSpec 於 orders.py 定義。
    """

    records: list

    def place(self, spec: "OrderSpec") -> bool:  # noqa: F821
        """掛新單。spec 含幣種、方向、大小、價格等。回傳成功否。"""
        ...

    def modify(self, oid: int, spec: "OrderSpec") -> bool:  # noqa: F821
        """修改既有掛單。oid 是單號，spec 是新參數。回傳成功否。"""
        ...

    def cancel(self, coin: str, oid: int) -> bool:
        """撤銷掛單。回傳成功否。"""
        ...

    def market_open(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        """市價開倉。"""
        ...

    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        """市價平倉（reduce-only）。"""
        ...

    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool:
        """調整槓桿。is_cross=True 用全倉，False 用逐倉。"""
        ...

    def get_open_orders(self) -> list[OpenOrder]:
        """取全部掛單。"""
        ...

    def get_size_decimals(self, coin: str) -> int:
        """單一標的的 size 精度（捨入用）。純讀，不受 live gate 限制——`_build_desired`
        （orders.py）duck-type 依賴此方法，正式入 Protocol 後型別可靜態檢查。"""
        ...


# ═══════════════════════════════════════════════════════════════════════
# Task 12：正式實作——ActionRecord / VirtualBook / ActionExecutor。
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ActionRecord:
    """單一寫入動作的稽核記錄（live 與 dry 皆記，shadow JSONL 的資料源）。

    payload 紅線：數值一律存 str（Decimal 不落 float）、**絕不含任何 key/signer
    材料**——payload 會被原樣序列化到 shadow 檔與可能的告警文字。
    """

    ts: float
    kind: str  # "place"|"modify"|"cancel"|"market_open"|"close"|"update_leverage"|"skip_trigger"
    coin: str
    payload: dict = field(default_factory=dict)


def _order_from_spec(oid: int, spec: "OrderSpec") -> OpenOrder:
    """OrderSpec → OpenOrder（VirtualBook 存簿用；欄位 1:1，oid 由簿指派）。"""
    return OpenOrder(
        oid=oid, coin=spec.coin, is_buy=spec.is_buy, limit_px=spec.limit_px,
        sz=spec.sz, reduce_only=spec.reduce_only, is_trigger=spec.is_trigger,
        trigger_px=spec.trigger_px, tpsl=spec.tpsl, is_market=spec.is_market,
        tif=spec.tif,
    )


class VirtualBook:
    """dry-run 記憶體掛單簿：place/modify/cancel 施加於 list[OpenOrder]（oid 自增），
    使 --shadow 連續多輪「desired vs 虛擬簿」對帳收斂——第二輪起 desired 未變時
    plan 應為全 matched、零動作。只模擬掛單簿，不模擬成交/部位。
    真交易所語意對齐：modify 會重新配發 oid（2026-07-19 testnet 實測），本假件比照。
    """

    def __init__(self) -> None:
        self._orders: dict[int, OpenOrder] = {}  # 插入序即掛單序
        self._next_oid = 1

    def place(self, spec: "OrderSpec") -> int:
        oid = self._next_oid
        self._next_oid += 1
        self._orders[oid] = _order_from_spec(oid, spec)
        return oid

    def modify(self, oid: int, spec: "OrderSpec") -> bool:
        """改單：模擬 HL batchModify 語意——舊 oid 作廢、配發新 oid（非就地）。oid 不存在 → False。"""
        if oid not in self._orders:
            return False
        del self._orders[oid]
        new_oid = self._next_oid
        self._next_oid += 1
        self._orders[new_oid] = _order_from_spec(new_oid, spec)
        return True

    def cancel(self, coin: str, oid: int) -> bool:
        o = self._orders.get(oid)
        if o is None or o.coin != coin:
            return False
        del self._orders[oid]
        return True

    def open_orders(self) -> list[OpenOrder]:
        return list(self._orders.values())


class ActionExecutor:
    """ExecutorPort 的正式實作：唯一寫入通道，live gate 結構性擋住 dry-run 寫入。

    - live=True：轉呼叫 adapter 寫入方法（builder/slippage/agent_signer 建構時注入，
      紅線 3/4）＋記 ActionRecord。
    - live=False：只記 ActionRecord ＋ 更新 VirtualBook，**零 adapter 寫入**
      （紅線 5：live_trading 預設關，dry 路徑結構上碰不到寫入方法）。
    - 讀方法（get_open_orders 的 live 分支、get_size_decimals）不受 live gate 限制。
    - trigger 單：見模組 docstring 的 M1 已知限制。
    """

    def __init__(self, adapter, agent_signer: Signer | None, builder: BuilderCode, *,
                 live: bool, my_address: str, settings: "CopySettings",
                 virtual_book: VirtualBook | None = None, clock=time.time):
        if live and agent_signer is None:
            raise ValueError("live=True 需要 agent_signer（dry/shadow 零 Keychain 才可為 None）")
        self.records: list[ActionRecord] = []
        self.live = live
        self.my_address = my_address
        self._adapter = adapter
        self._signer = agent_signer
        self._builder = builder
        self._settings = settings
        self._book = virtual_book if virtual_book is not None else VirtualBook()
        self._clock = clock
        # per-coin update_leverage 同值去重（port hl trader.py:118-130）：
        # 槓桿是交易所端 per-coin 持久狀態，重設同值零效果、徒耗限流額度。
        self._lev_set: dict[str, tuple[int, bool]] = {}

    def _record(self, kind: str, coin: str, payload: dict) -> None:
        self.records.append(
            ActionRecord(ts=self._clock(), kind=kind, coin=coin, payload=payload))

    def _skip_trigger(self, op: str, spec: "OrderSpec") -> bool:
        self._record("skip_trigger", spec.coin, {
            "op": op, "is_buy": spec.is_buy, "sz": str(spec.sz),
            "trigger_px": str(spec.trigger_px), "tpsl": spec.tpsl,
            "is_market": spec.is_market,
        })
        return False

    def _order_from(self, spec: "OrderSpec") -> Order:
        return Order(coin=spec.coin, is_buy=spec.is_buy, size=spec.sz,
                     limit_px=spec.limit_px, tif=spec.tif, reduce_only=spec.reduce_only)

    # ── 寫入（live gate）──────────────────────────────────────────────
    def place(self, spec: "OrderSpec") -> bool:
        if spec.is_trigger:
            return self._skip_trigger("place", spec)
        payload = {"is_buy": spec.is_buy, "sz": str(spec.sz),
                   "limit_px": str(spec.limit_px), "reduce_only": spec.reduce_only,
                   "tif": spec.tif}
        if self.live:
            ok = self._adapter.place_order(self._signer, self._order_from(spec),
                                           self._builder).ok
        else:
            payload["oid"] = str(self._book.place(spec))
            ok = True
        payload["ok"] = ok
        self._record("place", spec.coin, payload)
        return ok

    def modify(self, oid: int, spec: "OrderSpec") -> bool:
        if spec.is_trigger:
            return self._skip_trigger("modify", spec)
        payload = {"oid": str(oid), "is_buy": spec.is_buy, "sz": str(spec.sz),
                   "limit_px": str(spec.limit_px), "reduce_only": spec.reduce_only,
                   "tif": spec.tif}
        if self.live:
            ok = self._adapter.modify_order(self._signer, oid, self._order_from(spec))
        else:
            ok = self._book.modify(oid, spec)
        payload["ok"] = ok
        self._record("modify", spec.coin, payload)
        return ok

    def cancel(self, coin: str, oid: int) -> bool:
        if self.live:
            ok = self._adapter.cancel_order(self._signer, coin, oid)
        else:
            ok = self._book.cancel(coin, oid)
        self._record("cancel", coin, {"oid": str(oid), "ok": ok})
        return ok

    def market_open(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        if self.live:
            res = self._adapter.market_open(self._signer, coin, is_buy, size,
                                            self._settings.slippage, self._builder)
        else:
            # dry：不模擬部位，回成功讓引擎分支照走（shadow 差異由 records 對照）。
            res = OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"),
                              raw={"dry_run": True})
        self._record("market_open", coin, {
            "is_buy": is_buy, "sz": str(size),
            "slippage": str(self._settings.slippage), "ok": res.ok,
        })
        return res

    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        if self.live:
            res = self._adapter.close_reduce_only(self._signer, coin, is_buy, size,
                                                  self._settings.slippage, self._builder)
        else:
            res = OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"),
                              raw={"dry_run": True})
        self._record("close", coin, {
            "is_buy": is_buy, "sz": str(size),
            "slippage": str(self._settings.slippage), "ok": res.ok,
        })
        return res

    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool:
        # 同值去重：已成功設定過的 (leverage, is_cross) 不重送、不重記
        # （hl trader.py:118-119 語意；只有成功才入快取，hl:130）。
        if self._lev_set.get(coin) == (leverage, is_cross):
            return True
        if self.live:
            ok = self._adapter.update_leverage(self._signer, coin, leverage, is_cross)
        else:
            ok = True
        if ok:
            self._lev_set[coin] = (leverage, is_cross)
        self._record("update_leverage", coin, {
            "leverage": str(leverage), "is_cross": is_cross, "ok": ok,
        })
        return ok

    # ── 讀（不受 live gate）────────────────────────────────────────────
    def get_open_orders(self) -> list[OpenOrder]:
        """live 讀交易所我方掛單；dry 讀虛擬簿（使 shadow 多輪對帳收斂）。"""
        if self.live:
            return self._adapter.get_open_orders(self.my_address)
        return self._book.open_orders()

    def get_size_decimals(self, coin: str) -> int:
        return self._adapter.get_size_decimals(coin)
