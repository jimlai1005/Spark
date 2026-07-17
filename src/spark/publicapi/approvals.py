"""src/spark/publicapi/approvals.py
ApproveAgent / ApproveBuilderFee 的 EIP-712 typed-data 建構（無私鑰）。
站在 SDK 現成的 user_signed_payload 上（signing.py:217-237），零手工 EIP-712 hash。
sign types 常數抄自 SDK signing.py:410-438；SDK 升版由 pin round-trip 測試守。
research: docs/superpowers/research/2026-07-17-hl-sdk-external-signing.md。"""
import time

from hyperliquid.utils.signing import user_signed_payload

APPROVE_AGENT_PRIMARY = "HyperliquidTransaction:ApproveAgent"
APPROVE_AGENT_SIGN_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]

APPROVE_BUILDER_FEE_PRIMARY = "HyperliquidTransaction:ApproveBuilderFee"
APPROVE_BUILDER_FEE_SIGN_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "maxFeeRate", "type": "string"},
    {"name": "builder", "type": "address"},
    {"name": "nonce", "type": "uint64"},
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_approve_agent(*, agent_address: str, agent_name: str, wallet_chain_id: int,
                        is_mainnet: bool, nonce_ms: int | None = None
                        ) -> tuple[dict, dict]:
    """回 (typed_data 給前端 eth_signTypedData_v4, action 存伺服器待提交)。無私鑰。
    signatureChainId = 前端錢包當下 chain（research 風險 1：MetaMask 強制
    domain.chainId == active chain）；hyperliquidChain 才決定環境與防重放。"""
    action = {
        "type": "approveAgent",
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "signatureChainId": hex(wallet_chain_id),
        "agentAddress": agent_address,
        "agentName": agent_name,
        "nonce": nonce_ms if nonce_ms is not None else _now_ms(),
    }
    return user_signed_payload(APPROVE_AGENT_PRIMARY, APPROVE_AGENT_SIGN_TYPES,
                               action), action


def build_approve_builder_fee(*, builder: str, max_fee_rate: str, wallet_chain_id: int,
                              is_mainnet: bool, nonce_ms: int | None = None
                              ) -> tuple[dict, dict]:
    action = {
        "type": "approveBuilderFee",
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "signatureChainId": hex(wallet_chain_id),
        "maxFeeRate": max_fee_rate,
        "builder": builder,
        "nonce": nonce_ms if nonce_ms is not None else _now_ms(),
    }
    return user_signed_payload(APPROVE_BUILDER_FEE_PRIMARY,
                               APPROVE_BUILDER_FEE_SIGN_TYPES, action), action
