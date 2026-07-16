"""ExecutorPort Protocol——引擎唯一寫入通道的介面。

OrderSpec 於 copytrade/orders.py（Task 7）定義。
Task 8/11/13 的 fake 與 Task 12 的 ActionExecutor 都必須符合此 Protocol。
builder/agent_signer/slippage 是 executor 建構時注入，不在本介面上（紅線 3）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from spark.exchange.base import OpenOrder, OrderResult


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
