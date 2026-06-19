"""onboarding 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY。
狀態靠 API 查詢判定 → 冪等可重跑。只有此模組使用 main_signer（test harness）。"""
from dataclasses import dataclass
from enum import Enum
from spark.config import Settings, MIN_BUILDER_BALANCE
from spark.exchange.base import ExchangeAdapter, Signer


class OnboardingState(str, Enum):
    # 中間狀態僅文件化所模擬的流程；onboard() 是 all-or-nothing：
    # 成功只回 READY，任何階段失敗則 raise 對應例外（不回傳部分狀態）。
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

    # ApproveBuilderFee（冪等：已授權則跳過）。
    # 刻意不檢查 TxResult.ok：下面的 re-query 直接讀回鏈上 maxBuilderFee，是更強的確認。
    if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
        adapter.approve_builder_fee(main_signer, settings.builder_address, settings.max_rate)
        if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
            raise RuntimeError("approve_builder_fee 後 maxBuilderFee 仍為 0")

    # ApproveAgent（每次重跑都送；HL approve 同 agent 為冪等動作）。
    # 介面沒有 agent 授權的 read-back 查詢，故以 TxResult.ok 確認，避免失敗無聲通過
    # 導致 orchestrator 下單時才在遠處爆炸。
    res = adapter.approve_agent(main_signer, agent_address)
    if not res.ok:
        raise RuntimeError(f"approve_agent 失敗: {res.raw}")
    return OnboardingResult(state=OnboardingState.READY)
