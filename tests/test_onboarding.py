from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.exchange.base import TxResult
from spark.config import Settings
from spark.onboarding import onboard, OnboardingState, InsufficientFunds


def _settings():
    return Settings(builder_address="0xbuilder", account_id="acct1", network="testnet")


def test_onboard_reaches_ready_when_funded():
    fake = FakeAdapter(account_value=Decimal("150"))
    result = onboard(fake, _settings(), main_signer="MAIN", agent_address="0xagent",
                     user_address="0xuser")
    assert result.state == OnboardingState.READY
    assert fake.calls["approve_builder_fee"][0]["max_rate"] == "0.1%"
    assert fake.calls["approve_agent"][0]["agent_address"] == "0xagent"


def test_onboard_rejects_below_min_balance():
    fake = FakeAdapter(account_value=Decimal("50"))  # < 100 門檻
    with pytest.raises(InsufficientFunds):
        onboard(fake, _settings(), main_signer="MAIN", agent_address="0xagent",
                user_address="0xuser")


def test_onboard_idempotent_when_already_approved():
    fake = FakeAdapter(account_value=Decimal("150"))
    onboard(fake, _settings(), "MAIN", "0xagent", "0xuser")
    onboard(fake, _settings(), "MAIN", "0xagent", "0xuser")  # 再跑一次
    # 已授權則不重複送 approve_builder_fee
    assert len(fake.calls["approve_builder_fee"]) == 1


def test_onboard_raises_when_agent_approval_fails():
    fake = FakeAdapter(account_value=Decimal("150"))
    fake.approve_agent = lambda main_signer, agent_address: TxResult(ok=False, raw={"status": "err"})
    with pytest.raises(RuntimeError):
        onboard(fake, _settings(), "MAIN", "0xagent", "0xuser")
