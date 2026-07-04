"""即時驗證：輪詢 query_builder_accrued 直到相對 baseline 有增量（Phase 1 主成功判定）。"""
import time
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter


class AccrualTimeout(Exception):
    pass


def wait_for_accrual(adapter: ExchangeAdapter, builder: str,
                     attempts: int = 10, sleep_s: float = 3.0,
                     baseline: Decimal = Decimal("0")) -> Decimal:
    """輪詢直到累計 builder fee 超過 baseline，回傳最新累計值。

    builderRewards 是「累計」計數器：驗證單筆訂單必須先取快照當 baseline，
    等待「增量」出現；用絕對值 > 0 在重跑／舊地址上是假陽性。
    """
    last = Decimal("0")
    for _ in range(attempts):
        last = adapter.query_builder_accrued(builder)
        if last > baseline:
            return last
        if sleep_s:
            time.sleep(sleep_s)
    raise AccrualTimeout(
        f"builder {builder} accrued 停在 {last}（baseline {baseline}），"
        f"輪詢 {attempts} 次後逾時")
