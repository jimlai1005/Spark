"""onboarding 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY。
狀態靠 API 查詢判定 → 冪等可重跑。只有此模組使用 main_signer（test harness）。"""
from dataclasses import dataclass
from enum import Enum
from spark.config import Settings, MIN_BUILDER_BALANCE
from spark.exchange.base import ExchangeAdapter, Signer


class OnboardingState(str, Enum):
    UNFUNDED = "UNFUNDED"
    BUILDER_APPROVED = "BUILDER_APPROVED"
    AGENT_AUTHORIZED = "AGENT_AUTHORIZED"
    READY = "READY"


class InsufficientFunds(Exception):
    pass


@dataclass
class OnboardingResult:
    state: OnboardingState


def onboard(adapter: ExchangeAdapter, settings: Settings, main_signer: Signer,
            agent_address: str, user_address: str) -> OnboardingResult:
    # FUNDED gate（builder 啟用門檻 ≥ 100 USDC）
    if adapter.get_account_value(user_address) < MIN_BUILDER_BALANCE:
        raise InsufficientFunds(
            f"account value < {MIN_BUILDER_BALANCE} USDC builder 門檻")

    # ApproveBuilderFee（冪等：已授權則跳過）
    if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
        adapter.approve_builder_fee(main_signer, settings.builder_address, settings.max_rate)
        if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
            raise RuntimeError("approve_builder_fee 後 maxBuilderFee 仍為 0")

    # ApproveAgent（每次重跑都送；HL approve 同 agent 為冪等動作）
    adapter.approve_agent(main_signer, agent_address)
    return OnboardingResult(state=OnboardingState.READY)
