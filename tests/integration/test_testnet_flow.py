"""Testnet 端到端整合測試（spec §3.1-3.2）。預設被 -m 'not integration' 跳過。
需要：Keychain 已 bootstrap main key（scripts.bootstrap_keys）、testnet 帳戶已入金 ≥100 USDC、
環境變數 SPARK_ACCOUNT_ID / SPARK_USER_ADDR / SPARK_BUILDER_ADDR。
agent key 不需預先存在：首跑會由 approve_agent 生成並自動存入 Keychain。"""
import os
from decimal import Decimal

import pytest

from spark.config import Settings
from spark.keystore.keychain import MacKeychainBackend
from spark.onboarding import onboard, OnboardingState
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import wait_for_accrual

pytestmark = pytest.mark.integration

ACCOUNT_ID = os.environ.get("SPARK_ACCOUNT_ID", "")
USER_ADDR = os.environ.get("SPARK_USER_ADDR", "")
BUILDER_ADDR = os.environ.get("SPARK_BUILDER_ADDR", "")


def _mk_adapter(settings, wallet, user_address=None):
    # SDK Exchange 綁定建構時傳入的錢包；agent 交易需 account_address 指回主帳戶。
    # 已用 inspect.signature(Exchange.__init__) 驗證實際簽名（2026-07-04）：
    # (self, wallet: LocalAccount, base_url: str | None = None, meta=None,
    #  vault_address: str | None = None, account_address: str | None = None,
    #  spot_meta=None, perp_dexs=None, timeout: float | None = None)
    # → account_address 確實存在，用法與 spec 假設一致。
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from spark.exchange.hyperliquid import HyperliquidAdapter
    info = Info(settings.api_url, skip_ws=True)
    if user_address:
        exch = Exchange(wallet, settings.api_url, account_address=user_address)
    else:
        exch = Exchange(wallet, settings.api_url)
    return HyperliquidAdapter(settings.network, info=info, exchange=exch), info


def test_end_to_end_testnet():
    assert ACCOUNT_ID and USER_ADDR and BUILDER_ADDR, \
        "需設定 SPARK_ACCOUNT_ID / SPARK_USER_ADDR / SPARK_BUILDER_ADDR"
    settings = Settings(builder_address=BUILDER_ADDR, account_id=ACCOUNT_ID,
                        network="testnet")
    ks = MacKeychainBackend()
    main = ks.get_main_signer(ACCOUNT_ID)

    # onboarding：main-bound adapter；agent 步驟由 onboard() 對照鏈上 extraAgents
    # 查詢驅動判定是否需要 approve（真正冪等，不再單純信任本機 Keychain 是否有 key）。
    main_adapter, info = _mk_adapter(settings, main)
    try:
        local_agent_address = ks.get_agent_signer(ACCOUNT_ID).address
    except KeyError:
        local_agent_address = None
    result = onboard(main_adapter, settings, main_signer=main, user_address=USER_ADDR,
                     local_agent_address=local_agent_address,
                     on_agent_key=lambda k: ks.import_key(ACCOUNT_ID, "agent", k))
    assert result.state == OnboardingState.READY
    assert main_adapter.query_max_builder_fee(USER_ADDR, BUILDER_ADDR) != 0

    # 下單：agent-bound adapter（account_address 指回主帳戶）
    agent = ks.get_agent_signer(ACCOUNT_ID)
    agent_adapter, _ = _mk_adapter(settings, agent, user_address=USER_ADDR)
    best_px = Decimal(str(info.all_mids()[settings.coin]))
    baseline = main_adapter.query_builder_accrued(BUILDER_ADDR)
    order_res = place_marketable_order(agent_adapter, settings, agent_signer=agent,
                                       is_buy=True, best_opposite_px=best_px)
    # 診斷輔助：raw 不含私鑰（已審查確認），失敗時印出以診斷 szDecimals/tick 問題
    assert order_res.ok and order_res.filled_size > 0, f"下單未成交: {order_res.raw}"

    accrued = wait_for_accrual(main_adapter, BUILDER_ADDR, attempts=10, sleep_s=3,
                              baseline=baseline)
    assert accrued > baseline
    print(f"✅ testnet 累計 builder fee {baseline} → {accrued}（增量 {accrued - baseline}）")
