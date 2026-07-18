"""onboarding 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY。
兩步皆為鏈上查詢驅動 → 真正冪等可重跑：builder-fee 步驟查 maxBuilderFee；
agent 步驟對照鏈上 extraAgents（本機持有的 agent key 是否為當前有效授權），
不再單純信任呼叫端旗標（B1-F1 修正：舊版 skip_agent_approval 只看本機
Keychain 是否有 key，drift 時會用過期/未授權 key 靜默簽名失敗）。
只有此模組使用 main_signer（test harness）。"""
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


class BuilderNotEligible(Exception):
    pass


@dataclass
class OnboardingResult:
    # 不再帶 agent_key：唯一出口是 on_agent_key callback（持久化即發生在授權當下），
    # 縮小 key 的暴露面——呼叫端無法在事後才從回傳值取出 key 並延遲存放。
    state: OnboardingState


def onboard(adapter: ExchangeAdapter, settings: Settings, main_signer: Signer,
            user_address: str, agent_name: str = "spark-agent", *,
            local_agent_address: str | None = None,
            on_agent_key: Callable[[str], None] | None = None) -> OnboardingResult:
    """FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY（兩步皆鏈上查詢驅動，冪等可重跑）。

    agent 語意（HL，已查證 src/spark/exchange/hyperliquid.py:approve_agent）：
    approve_agent 一律「生成一把新 agent key 並以 main 錢包簽名授權（named agent），
    重複呼叫會 rotate 掉舊 key」——它本身不是冪等操作。真正的冪等由本函式包出來：
    是否呼叫 approve_agent，由**鏈上查詢**（`adapter.query_agent_addresses`，即
    extraAgents）決定，不是呼叫端傳入的旗標。

    判定：`local_agent_address`（呼叫端本機 Keychain 目前持有的 agent key 地址，
    沒有就傳 None）正規化為小寫後，若落在鏈上 extraAgents 內 → 視為當前有效授權
    （AGENT_AUTHORIZED），跳過 approve_agent（不 rotate，冪等）。否則——本機沒 key，
    或本機 key 已非鏈上當前授權（drift：帶外 rotate、還原舊 Keychain 等）——執行
    approve_agent 重新授權/生成，並要求呼叫者提供 on_agent_key callback 在 key 生成
    當下立刻持久化：結構性防呆，「將要 (re)authorize 卻不給 callback」是呼叫錯誤
    （ValueError），不是「等呼叫者記得存」的口頭約定。
    """
    agent_authorized = False
    if local_agent_address is not None:
        live_agents = {a.lower() for a in adapter.query_agent_addresses(user_address)}
        agent_authorized = local_agent_address.lower() in live_agents

    if not agent_authorized and on_agent_key is None:
        raise ValueError(
            "本機 agent key 缺失或與鏈上 extraAgents 不符，approve_agent 將生成新 key"
            "（rotate 舊 key），必須提供 on_agent_key 持久化 callback")

    # FUNDED gate（builder 啟用門檻 ≥ 100 USDC）
    if adapter.get_account_value(user_address) < MIN_BUILDER_BALANCE:
        raise InsufficientFunds(
            f"account value < {MIN_BUILDER_BALANCE} USDC builder 門檻")

    # Builder 啟用門檻（spec §4）：builder 地址本身需 ≥ 100 USDC，否則 builder code
    # 不生效 —— 症狀是「下單成交但 fee 永不累計」，必須在這裡大聲擋下。
    if adapter.get_account_value(settings.builder_address) < MIN_BUILDER_BALANCE:
        raise BuilderNotEligible(
            f"builder 地址 {settings.builder_address} 餘額 < {MIN_BUILDER_BALANCE} USDC，"
            "builder code 不會生效")

    # ApproveBuilderFee（冪等：已授權則跳過）。
    # 刻意不檢查 TxResult.ok：下面的 re-query 直接讀回鏈上 maxBuilderFee，是更強的確認。
    if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
        adapter.approve_builder_fee(main_signer, settings.builder_address, settings.max_rate)
        if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
            raise RuntimeError("approve_builder_fee 後 maxBuilderFee 仍為 0")

    if not agent_authorized:
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
