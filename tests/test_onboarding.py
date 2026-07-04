from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.exchange.base import TxResult
from spark.config import Settings
from spark.onboarding import onboard, OnboardingState, InsufficientFunds


def _settings():
    return Settings(builder_address="0xbuilder", account_id="acct1", network="testnet")


def test_onboard_reaches_ready_and_returns_generated_agent_key():
    fake = FakeAdapter(account_value=Decimal("150"))
    result = onboard(fake, _settings(), main_signer="MAIN", user_address="0xuser")
    assert result.state == OnboardingState.READY
    assert result.agent_key == "0x" + "ab" * 32
    assert fake.calls["approve_builder_fee"][0]["max_rate"] == "0.1%"
    assert fake.calls["approve_agent"][0]["agent_name"] == "spark-agent"


def test_onboard_rejects_below_min_balance():
    fake = FakeAdapter(account_value=Decimal("50"))
    with pytest.raises(InsufficientFunds):
        onboard(fake, _settings(), main_signer="MAIN", user_address="0xuser")


def test_onboard_skips_agent_approval_when_key_exists():
    fake = FakeAdapter(account_value=Decimal("150"))
    result = onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=True)
    assert result.state == OnboardingState.READY
    assert result.agent_key is None
    assert len(fake.calls["approve_agent"]) == 0  # 不 rotate 既有 key


def test_onboard_idempotent_builder_fee_across_reruns():
    fake = FakeAdapter(account_value=Decimal("150"))
    onboard(fake, _settings(), "MAIN", "0xuser")
    onboard(fake, _settings(), "MAIN", "0xuser", skip_agent_approval=True)
    assert len(fake.calls["approve_builder_fee"]) == 1


def test_onboard_raises_when_agent_approval_fails():
    fake = FakeAdapter(account_value=Decimal("150"))
    fake.approve_agent = lambda main_signer, agent_name: TxResult(ok=False, raw={"status": "err"})
    with pytest.raises(RuntimeError):
        onboard(fake, _settings(), "MAIN", "0xuser")
