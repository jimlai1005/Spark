"""onboarding 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY。
狀態靠 API 查詢判定 → 冪等可重跑。只有此模組使用 main_signer（test harness）。"""
from collections.abc import Callable
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
    # 不再帶 agent_key：唯一出口是 on_agent_key callback（持久化即發生在授權當下），
    # 縮小 key 的暴露面——呼叫端無法在事後才從回傳值取出 key 並延遲存放。
    state: OnboardingState


def onboard(adapter: ExchangeAdapter, settings: Settings, main_signer: Signer,
            user_address: str, agent_name: str = "spark-agent", *,
            skip_agent_approval: bool,
            on_agent_key: Callable[[str], None] | None = None) -> OnboardingResult:
    """FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY（狀態靠查詢，冪等可重跑）。

    agent 語意（HL）：approve_agent 會「生成新 key 並 rotate 舊 key」，不是冪等授權。
    因此由呼叫者判斷：Keychain 已有可用 agent key → skip_agent_approval=True（跳過，
    避免把既有 key 轉失效）；否則本函式執行 approve，並要求呼叫者同時提供
    on_agent_key callback，在 key 生成的當下立刻持久化——結構性防呆：
    skip_agent_approval=False 卻不給 callback 是呼叫錯誤（ValueError），
    不是「等呼叫者記得存」的口頭約定。
    """
    if not skip_agent_approval and on_agent_key is None:
        raise ValueError(
            "approve_agent 會生成新 key（rotate 舊 key），必須提供 on_agent_key 持久化 callback")

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

    if not skip_agent_approval:
        res = adapter.approve_agent(main_signer, agent_name)
        if not res.ok:
            raise RuntimeError(f"approve_agent 失敗: {res.raw}")
        # 立刻持久化：授權已上鏈，key 只存在記憶體 —— callback 失敗必須大聲丟出
        # （不得在訊息中夾帶 key 本身）。
        try:
            on_agent_key(res.agent_key)
        except Exception as e:
            raise RuntimeError(
                "agent key 持久化失敗：agent 已授權但 key 未存入 Keychain，"
                "需重跑 onboarding 重新 rotate") from e
    return OnboardingResult(state=OnboardingState.READY)
