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
                     skip_agent_approval=False, on_agent_key=captured.append)
    assert result.state == OnboardingState.READY
    assert captured == ["0x" + "ab" * 32]
    assert fake.calls["approve_builder_fee"][0]["max_rate"] == "0.1%"
    assert fake.calls["approve_agent"][0]["agent_name"] == "spark-agent"


def test_onboard_requires_persistence_plan_before_rotation():
    fake = FakeAdapter(account_value=Decimal("150"))
    with pytest.raises(ValueError):
        onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=False)


def test_onboard_rejects_below_min_balance():
    fake = FakeAdapter(account_value=Decimal("50"))
    with pytest.raises(InsufficientFunds):
        onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=True)


def test_onboard_rejects_ineligible_builder():
    fake = FakeAdapter(account_value=Decimal("150"),
                       account_values={"0xbuilder": Decimal("50")})
    with pytest.raises(BuilderNotEligible):
        onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=True)


def test_onboard_skips_agent_approval_when_key_exists():
    fake = FakeAdapter(account_value=Decimal("150"))
    result = onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=True)
    assert result.state == OnboardingState.READY
    assert len(fake.calls["approve_agent"]) == 0  # 不 rotate 既有 key


def test_onboard_idempotent_builder_fee_across_reruns():
    fake = FakeAdapter(account_value=Decimal("150"))
    onboard(fake, _settings(), "MAIN", "0xuser",
            skip_agent_approval=False, on_agent_key=lambda k: None)
    onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=True)
    assert len(fake.calls["approve_builder_fee"]) == 1


def test_onboard_raises_loudly_when_persistence_fails():
    fake = FakeAdapter(account_value=Decimal("150"))
    def boom(key):
        raise OSError("keychain unavailable")
    with pytest.raises(RuntimeError) as exc:
        onboard(fake, _settings(), "MAIN", "0xuser",
                skip_agent_approval=False, on_agent_key=boom)
    assert "0x" + "ab" * 32 not in str(exc.value)  # key 不得出現在例外訊息
