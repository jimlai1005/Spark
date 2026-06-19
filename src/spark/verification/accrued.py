"""即時驗證：輪詢 query_builder_accrued 直到 > 0（Phase 1 主成功判定）。"""
import time
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter


class AccrualTimeout(Exception):
    pass


def wait_for_accrual(adapter: ExchangeAdapter, builder: str,
                     attempts: int = 10, sleep_s: float = 3.0) -> Decimal:
    last = Decimal("0")
    for _ in range(attempts):
        last = adapter.query_builder_accrued(builder)
        if last > 0:
            return last
        if sleep_s:
            time.sleep(sleep_s)
    raise AccrualTimeout(f"builder {builder} accrued 仍為 {last}，輪詢 {attempts} 次後逾時")
