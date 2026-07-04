"""可獨立重跑的隔日 CSV 對帳（spec §3.2 第三項）。
用法: SPARK_BUILDER_ADDR=0x.. [SPARK_NETWORK=testnet] \\
      uv run python -m scripts.reconcile_day YYYYMMDD"""
import os
import sys
from datetime import datetime

from hyperliquid.info import Info

from spark.config import Settings
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.verification.reconcile import reconcile


def main():
    if len(sys.argv) != 2:
        print("用法: python -m scripts.reconcile_day YYYYMMDD")
        raise SystemExit(2)
    day = datetime.strptime(sys.argv[1], "%Y%m%d").date()
    network = os.environ.get("SPARK_NETWORK", "testnet")
    settings = Settings(builder_address=os.environ["SPARK_BUILDER_ADDR"],
                        account_id=os.environ.get("SPARK_ACCOUNT_ID", "acct"),
                        network=network)
    adapter = HyperliquidAdapter(network, info=Info(settings.api_url, skip_ws=True),
                                 exchange=None)
    report = reconcile(adapter, settings.builder_address, day,
                       expected_coin=settings.coin)
    print(report.render())
    raise SystemExit(0 if report.matched else 1)


if __name__ == "__main__":
    main()
