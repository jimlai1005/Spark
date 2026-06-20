"""CSV 對帳（可獨立重跑）：解 builder_fills 找對應成交，輸出小型對帳報告。"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter, Fill
from spark.exchange.csv_fills import total_builder_fee


@dataclass(frozen=True)
class ReconcileReport:
    builder: str
    day: date
    expected_coin: str | None
    total_builder_fee: Decimal
    fills: list[Fill]

    @property
    def matched(self) -> bool:
        # 以 fills 為單一真相來源衍生
        return bool(self.fills)

    @property
    def fill_count(self) -> int:
        return len(self.fills)

    def render(self) -> str:
        lines = [
            f"== Builder 對帳報告 {self.day} ==",
            f"builder: {self.builder}",
            f"filter_coin: {self.expected_coin or '(all)'}",
            f"matched: {self.matched}  fills: {self.fill_count}",
            f"total_builder_fee: {self.total_builder_fee}",
        ]
        for f in self.fills:
            lines.append(f"  {f.time.isoformat(timespec='seconds')} {f.coin} {f.side} "
                         f"px={f.px} sz={f.sz} builder_fee={f.builder_fee}")
        return "\n".join(lines)


def reconcile(adapter: ExchangeAdapter, builder: str, day: date,
              expected_coin: str | None = None) -> ReconcileReport:
    """對帳：抓當日 builder_fills，選擇性以 coin 篩選，產出報告。

    注意（Phase 1 範圍）：matched 僅代表「該 builder 當日（經 coin 篩選後）存在成交」，
    不做 order-level 連結。單 builder／單日範圍下這已足夠；多訂單對帳屬後續階段。
    """
    fills = adapter.fetch_builder_fills(builder, day)
    if expected_coin is not None:
        fills = [f for f in fills if f.coin == expected_coin]
    return ReconcileReport(
        builder=builder, day=day, expected_coin=expected_coin,
        total_builder_fee=total_builder_fee(fills), fills=fills,
    )
