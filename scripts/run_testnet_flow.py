"""端到端：onboarding → 下單 → 即時累計驗證。
需求同 tests/integration/test_testnet_flow.py（Keychain main key、入金、環境變數）。
用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\
      [SPARK_NETWORK=testnet] uv run python -m scripts.run_testnet_flow"""
import os
from decimal import Decimal

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from spark.config import Settings
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.keystore.keychain import MacKeychainBackend
from spark.onboarding import onboard
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import wait_for_accrual


def main():
    network = os.environ.get("SPARK_NETWORK", "testnet")
    account_id = os.environ["SPARK_ACCOUNT_ID"]
    user_addr = os.environ["SPARK_USER_ADDR"]
    settings = Settings(builder_address=os.environ["SPARK_BUILDER_ADDR"],
                        account_id=account_id, network=network)
    ks = MacKeychainBackend()
    main_signer = ks.get_main_signer(account_id)
    info = Info(settings.api_url, skip_ws=True)

    main_adapter = HyperliquidAdapter(
        network, info=info, exchange=Exchange(main_signer, settings.api_url))
    try:
        ks.get_agent_signer(account_id)
        has_agent = True
    except KeyError:
        has_agent = False
    onboard(main_adapter, settings, main_signer=main_signer, user_address=user_addr,
            skip_agent_approval=has_agent,
            on_agent_key=lambda k: ks.import_key(account_id, "agent", k))
    print(f"onboarding OK（agent {'沿用既有' if has_agent else '新生成並已入 Keychain'}）")

    agent = ks.get_agent_signer(account_id)
    agent_adapter = HyperliquidAdapter(
        network, info=info,
        exchange=Exchange(agent, settings.api_url, account_address=user_addr))
    best_px = Decimal(str(info.all_mids()[settings.coin]))
    res = place_marketable_order(agent_adapter, settings, agent_signer=agent,
                                 is_buy=True, best_opposite_px=best_px)
    if not res.ok:
        raise SystemExit(f"下單未成交: {res.raw}")
    print(f"order filled_size={res.filled_size} avg_px={res.avg_px}")

    accrued = wait_for_accrual(main_adapter, settings.builder_address)
    print(f"✅ 即時累計 builder fee = {accrued}")


if __name__ == "__main__":
    main()
