"""onboarding 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY。
狀態靠 API 查詢判定 → 冪等可重跑。只有此模組使用 main_signer（test harness）。"""
from dataclasses import dataclass, field
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
    # 新生成的 agent 私鑰（僅在本次 onboard 執行了 approve_agent 時非 None）。
    # 呼叫者（CLI/integration 層）負責立刻存入 Keychain。repr=False：絕不落 log。
    agent_key: str | None = field(default=None, repr=False)


def onboard(adapter: ExchangeAdapter, settings: Settings, main_signer: Signer,
            user_address: str, agent_name: str = "spark-agent",
            skip_agent_approval: bool = False) -> OnboardingResult:
    """FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY（狀態靠查詢，冪等可重跑）。

    agent 語意（HL）：approve_agent 會「生成新 key 並 rotate 舊 key」，不是冪等授權。
    因此由呼叫者判斷：Keychain 已有可用 agent key → skip_agent_approval=True（跳過，
    避免把既有 key 轉失效）；否則本函式執行 approve 並把新 key 放進回傳值，
    呼叫者必須立刻存入 Keychain。
    """
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

    agent_key = None
    if not skip_agent_approval:
        # 無 read-back 查詢可用，以 TxResult.ok 確認，失敗大聲丟出。
        res = adapter.approve_agent(main_signer, agent_name)
        if not res.ok:
            raise RuntimeError(f"approve_agent 失敗: {res.raw}")
        agent_key = res.agent_key
    return OnboardingResult(state=OnboardingState.READY, agent_key=agent_key)
