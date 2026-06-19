"""CSV 對帳（可獨立重跑）：解 builder_fills 找對應成交，輸出小型對帳報告。"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter, Fill
from spark.exchange.csv_fills import total_builder_fee


@dataclass
class ReconcileReport:
    builder: str
    day: date
    matched: bool
    fill_count: int
    total_builder_fee: Decimal
    fills: list[Fill]

    def render(self) -> str:
        lines = [
            f"== Builder 對帳報告 {self.day} ==",
            f"builder: {self.builder}",
            f"matched: {self.matched}  fills: {self.fill_count}",
            f"total_builder_fee: {self.total_builder_fee}",
        ]
        for f in self.fills:
            lines.append(f"  {f.time} {f.coin} {f.side} px={f.px} sz={f.sz} "
                         f"builder_fee={f.builder_fee}")
        return "\n".join(lines)


def reconcile(adapter: ExchangeAdapter, builder: str, day: date,
              expected_coin: str | None = None) -> ReconcileReport:
    fills = adapter.fetch_builder_fills(builder, day)
    if expected_coin is not None:
        fills = [f for f in fills if f.coin == expected_coin]
    return ReconcileReport(
        builder=builder, day=day, matched=len(fills) > 0, fill_count=len(fills),
        total_builder_fee=total_builder_fee(fills), fills=fills,
    )
