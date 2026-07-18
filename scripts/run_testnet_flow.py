"""端到端：onboarding → 下單 → 即時累計驗證。
需求同 tests/integration/test_testnet_flow.py（Keychain main key、入金、環境變數）。

⚠️ 適用範圍：**M1 自有錢包模式專用**——本腳本需要**主鑰**（Mac Keychain），
   僅適用於自己持有主鑰的錢包。M2 非託管流程（客戶錢包）請改用 dashboard onboarding
   或 scripts/testnet_modify_probe.py（後者支援 FILET_KEYSTORE=envfile 且可跳過 onboarding）。

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
    # M1/M2 模式守衛：本腳本需要主鑰，只適用 M1 自有錢包（Keychain）模式。
    # M2 非託管的 EnvFileKeyStore 結構性沒有主鑰，與其丟出困惑的 KeyError，
    # 不如在最前面就明確擋下並指路。
    _ks_mode = os.environ.get("FILET_KEYSTORE", "keychain").strip().lower()
    if _ks_mode == "envfile":
        raise SystemExit(
            "本腳本僅適用 M1 自有錢包模式（主鑰在 Mac Keychain）。\n"
            "偵測到 FILET_KEYSTORE=envfile（M2 非託管模式，結構性無主鑰）。\n"
            "M2 流程請改用：dashboard onboarding（瀏覽器錢包簽名），"
            "或 scripts/testnet_modify_probe.py（支援 PROBE_SKIP_ONBOARD=true）。"
        )
    network = os.environ.get("SPARK_NETWORK", "testnet")
    account_id = os.environ["SPARK_ACCOUNT_ID"]
    user_addr = os.environ["SPARK_USER_ADDR"]
    settings = Settings(builder_address=os.environ["SPARK_BUILDER_ADDR"],
                        account_id=account_id, network=network)
    ks = MacKeychainBackend()
    try:
        main_signer = ks.get_main_signer(account_id)
    except (KeyError, PermissionError, NotImplementedError) as e:
        raise SystemExit(
            f"取不到 account {account_id} 的主鑰（{type(e).__name__}）。\n"
            "本腳本僅適用 M1 自有錢包模式：主鑰需先存入 Mac Keychain"
            "（見 scripts/bootstrap_keys.py）。M2 非託管錢包無主鑰，請改用 dashboard onboarding。"
        ) from e
    info = Info(settings.api_url, skip_ws=True)

    main_adapter = HyperliquidAdapter(
        network, info=info, exchange=Exchange(main_signer, settings.api_url))
    try:
        local_agent_address = ks.get_agent_signer(account_id).address
    except KeyError:
        local_agent_address = None
    onboard(main_adapter, settings, main_signer=main_signer, user_address=user_addr,
            local_agent_address=local_agent_address,
            on_agent_key=lambda k: ks.import_key(account_id, "agent", k))
    print("onboarding OK（agent 授權狀態已對照鏈上 extraAgents 查詢驅動）")

    agent = ks.get_agent_signer(account_id)
    agent_adapter = HyperliquidAdapter(
        network, info=info,
        exchange=Exchange(agent, settings.api_url, account_address=user_addr))
    best_px = Decimal(str(info.all_mids()[settings.coin]))
    baseline = main_adapter.query_builder_accrued(settings.builder_address)
    res = place_marketable_order(agent_adapter, settings, agent_signer=agent,
                                 is_buy=True, best_opposite_px=best_px)
    if not res.ok:
        raise SystemExit(f"下單未成交: {res.raw}")
    print(f"order filled_size={res.filled_size} avg_px={res.avg_px}")

    accrued = wait_for_accrual(main_adapter, settings.builder_address, baseline=baseline)
    print(f"✅ 累計 builder fee {baseline} → {accrued}（增量 {accrued - baseline}）")


if __name__ == "__main__":
    main()
