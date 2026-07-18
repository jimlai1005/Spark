from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.config import Settings
from spark.onboarding import onboard, OnboardingState, InsufficientFunds, BuilderNotEligible


def _settings():
    return Settings(builder_address="0xbuilder", account_id="acct1", network="testnet")


def test_onboard_reaches_ready_and_persists_generated_agent_key():
    fake = FakeAdapter(account_value=Decimal("150"))
    captured = []
    result = onboard(fake, _settings(), main_signer="MAIN", user_address="0xuser",
                     on_agent_key=captured.append)
    assert result.state == OnboardingState.READY
    assert captured == ["0x" + "ab" * 32]
    assert fake.calls["approve_builder_fee"][0]["max_rate"] == "0.1%"
    assert fake.calls["approve_agent"][0]["agent_name"] == "spark-agent"


def test_onboard_requires_persistence_plan_before_rotation():
    fake = FakeAdapter(account_value=Decimal("150"))
    with pytest.raises(ValueError):
        onboard(fake, _settings(), "MAIN", "0xuser")


def test_onboard_rejects_below_min_balance():
    fake = FakeAdapter(account_value=Decimal("50"))
    with pytest.raises(InsufficientFunds):
        onboard(fake, _settings(), "MAIN", "0xuser", on_agent_key=lambda k: None)


def test_onboard_rejects_ineligible_builder():
    fake = FakeAdapter(account_value=Decimal("150"),
                       account_values={"0xbuilder": Decimal("50")})
    with pytest.raises(BuilderNotEligible):
        onboard(fake, _settings(), "MAIN", "0xuser", on_agent_key=lambda k: None)


def test_onboard_skips_agent_approval_when_local_key_matches_chain():
    # 本機持有的 agent key 地址「確實」在鏈上 extraAgents 內 → 冪等跳過（不 rotate）。
    # 鏈上回傳小寫、本機位址大小寫混寫（checksum 常見形態）：驗證同基準比較
    # （正規化小寫，工程原則 1）不會因為大小寫產生假性不符。
    fake = FakeAdapter(account_value=Decimal("150"),
                       extra_agents=["0xagent0000000000000000000000000000000000"])
    result = onboard(fake, _settings(), "MAIN", "0xuser",
                     local_agent_address="0xAgEnT0000000000000000000000000000000000")
    assert result.state == OnboardingState.READY
    assert len(fake.calls["approve_agent"]) == 0  # 不 rotate 既有已授權 key
    assert len(fake.calls["query_agent_addresses"]) == 1


def test_onboard_reauthorizes_when_local_key_not_in_chain_extra_agents():
    # drift：本機有 agent key，但鏈上 extraAgents 不含它（帶外 rotate／還原舊 Keychain）
    # → 必須重新 approve_agent（自我修復），不得靜默沿用過期 key。
    fake = FakeAdapter(account_value=Decimal("150"), extra_agents=["0xdeadbeef"])
    captured = []
    result = onboard(fake, _settings(), "MAIN", "0xuser",
                     local_agent_address="0xstalekey",
                     on_agent_key=captured.append)
    assert result.state == OnboardingState.READY
    assert len(fake.calls["approve_agent"]) == 1
    assert captured == ["0x" + "ab" * 32]


def test_onboard_approves_agent_when_no_local_key():
    # 本機無 agent key（local_agent_address=None）→ 一定執行 approve_agent。
    fake = FakeAdapter(account_value=Decimal("150"), extra_agents=["0xsomeoneelse"])
    captured = []
    result = onboard(fake, _settings(), "MAIN", "0xuser", on_agent_key=captured.append)
    assert result.state == OnboardingState.READY
    assert len(fake.calls["approve_agent"]) == 1
    assert captured == ["0x" + "ab" * 32]
    # local_agent_address 為 None 時不需要查鏈就能判定未授權——確認未發出多餘查詢。
    assert len(fake.calls["query_agent_addresses"]) == 0


def test_onboard_idempotent_builder_fee_across_reruns():
    fake = FakeAdapter(account_value=Decimal("150"))
    onboard(fake, _settings(), "MAIN", "0xuser", on_agent_key=lambda k: None)
    onboard(fake, _settings(), "MAIN", "0xuser", on_agent_key=lambda k: None)
    assert len(fake.calls["approve_builder_fee"]) == 1


def test_onboard_raises_loudly_when_persistence_fails():
    fake = FakeAdapter(account_value=Decimal("150"))
    def boom(key):
        raise OSError("keychain unavailable")
    with pytest.raises(RuntimeError) as exc:
        onboard(fake, _settings(), "MAIN", "0xuser", on_agent_key=boom)
    assert "0x" + "ab" * 32 not in str(exc.value)  # key 不得出現在例外訊息
