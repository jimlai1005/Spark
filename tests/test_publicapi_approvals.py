"""tests/test_publicapi_approvals.py
typed-data builder：無私鑰建構、動態 chainId、agentName 一律給、SDK pin round-trip。
簽名/recover 全為本地密碼學運算，不觸網。"""
import time

from eth_account import Account
from eth_account.messages import encode_typed_data
from hyperliquid.utils.signing import recover_user_from_user_signed_action

from spark.publicapi.approvals import (
    APPROVE_AGENT_PRIMARY, APPROVE_AGENT_SIGN_TYPES,
    APPROVE_BUILDER_FEE_PRIMARY, APPROVE_BUILDER_FEE_SIGN_TYPES,
    build_approve_agent, build_approve_builder_fee)

_AGENT = "0x" + "ab" * 20
_BUILDER = "0x" + "cd" * 20


def test_approve_agent_typed_data_shape():
    td, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                     wallet_chain_id=0xA4B1, is_mainnet=False,
                                     nonce_ms=1234)
    assert td["domain"] == {"name": "HyperliquidSignTransaction", "version": "1",
                            "chainId": 0xA4B1,
                            "verifyingContract": "0x" + "0" * 40}
    assert td["primaryType"] == APPROVE_AGENT_PRIMARY
    assert td["message"] == action
    assert action["type"] == "approveAgent"
    assert action["hyperliquidChain"] == "Testnet"
    assert action["signatureChainId"] == "0xa4b1"
    assert action["agentAddress"] == _AGENT
    assert action["agentName"] == "filet"   # 一律給名字（research：避開 SDK 空名刪欄位特例）
    assert action["nonce"] == 1234


def test_domain_chain_id_follows_wallet():
    """research 風險 1：signatureChainId 動態取自前端錢包，不硬編 0x66eee。"""
    td, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                     wallet_chain_id=1, is_mainnet=True, nonce_ms=1)
    assert td["domain"]["chainId"] == 1
    assert action["signatureChainId"] == "0x1"
    assert action["hyperliquidChain"] == "Mainnet"


def test_builder_fee_typed_data_shape():
    td, action = build_approve_builder_fee(builder=_BUILDER, max_fee_rate="0.1%",
                                           wallet_chain_id=0xA4B1, is_mainnet=True,
                                           nonce_ms=5)
    assert td["primaryType"] == APPROVE_BUILDER_FEE_PRIMARY
    assert action["type"] == "approveBuilderFee"
    assert action["maxFeeRate"] == "0.1%"
    assert action["builder"] == _BUILDER
    assert action["hyperliquidChain"] == "Mainnet"
    assert action["nonce"] == 5


def test_nonce_defaults_to_now_ms():
    _, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                    wallet_chain_id=1, is_mainnet=False)
    assert abs(action["nonce"] - time.time() * 1000) < 60_000


def _sign(wallet, typed_data):
    sm = wallet.sign_message(encode_typed_data(full_message=typed_data))
    return {"r": hex(sm.r), "s": hex(sm.s), "v": sm.v}


def test_sign_recover_roundtrip_pins_sdk_types_agent():
    """SDK pin 測試（research 風險 6）：本模組常數建的 typed data 簽出後，SDK
    recover 得回同一地址——SDK 升版若改 sign types，這裡先爆。"""
    wallet = Account.create()
    td, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                     wallet_chain_id=0xA4B1, is_mainnet=False,
                                     nonce_ms=1720000000000)
    rec = recover_user_from_user_signed_action(
        dict(action), _sign(wallet, td), APPROVE_AGENT_SIGN_TYPES,
        APPROVE_AGENT_PRIMARY, False)
    assert rec.lower() == wallet.address.lower()


def test_sign_recover_roundtrip_pins_sdk_types_builder_fee():
    wallet = Account.create()
    td, action = build_approve_builder_fee(builder=_BUILDER, max_fee_rate="0.1%",
                                           wallet_chain_id=0xA4B1, is_mainnet=False,
                                           nonce_ms=1720000000000)
    rec = recover_user_from_user_signed_action(
        dict(action), _sign(wallet, td), APPROVE_BUILDER_FEE_SIGN_TYPES,
        APPROVE_BUILDER_FEE_PRIMARY, False)
    assert rec.lower() == wallet.address.lower()
