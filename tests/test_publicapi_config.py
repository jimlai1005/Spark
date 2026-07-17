"""tests/test_publicapi_config.py"""
import pytest

from spark.filet.followers import validate_account_id
from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address

_ADDR = "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"


def test_normalize_address_lowercases():
    assert normalize_address(_ADDR) == _ADDR.lower()
    assert normalize_address(_ADDR.lower()) == _ADDR.lower()


def test_normalize_address_rejects_bad():
    for bad in ["", "0x123", "abc", _ADDR[2:], "0x" + "g" * 40, None]:
        with pytest.raises((ValueError, TypeError)):
            normalize_address(bad)


def test_derive_account_id_full_40hex():
    acct = derive_account_id(_ADDR)
    assert acct == "f" + _ADDR[2:].lower()
    assert len(acct) == 41
    validate_account_id(acct)  # 恆為引擎合法 account_id


def test_derive_account_id_deterministic_case_insensitive():
    assert derive_account_id(_ADDR) == derive_account_id(_ADDR.lower())


def _env(**over):
    base = {
        "FILET_API_NETWORK": "testnet",
        "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
        "FILET_SIWE_DOMAIN": "filet.example",
        "FILET_SIWE_URI": "https://filet.example",
        "FILET_API_DB": "/tmp/api.db",
        "FILET_KEYSVC_SOCK": "/run/filet/keysvc.sock",
        "FILET_PENDING_PATH": "/tmp/pending.json",
        "FILET_ADMIN_ADDRESSES": "0x" + "ad" * 20,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_from_env_builds_config():
    cfg = ApiConfig.from_env(_env())
    assert cfg.network == "testnet"
    assert cfg.builder_address == "0x" + "b1" * 20
    assert cfg.is_mainnet is False
    assert cfg.api_url == "https://api.hyperliquid-testnet.xyz"
    assert cfg.admin_addresses == frozenset({"0x" + "ad" * 20})
    assert cfg.agent_name == "filet"
    assert cfg.max_fee_rate == "0.1%"


def test_from_env_missing_var_raises():
    with pytest.raises(ValueError, match="FILET_BUILDER_ADDR"):
        ApiConfig.from_env(_env(FILET_BUILDER_ADDR=None))


def test_from_env_bad_network_raises():
    with pytest.raises(ValueError, match="network"):
        ApiConfig.from_env(_env(FILET_API_NETWORK="devnet"))


def test_admin_addresses_optional_and_normalized():
    cfg = ApiConfig.from_env(_env(FILET_ADMIN_ADDRESSES=None))
    assert cfg.admin_addresses == frozenset()
    cfg2 = ApiConfig.from_env(_env(FILET_ADMIN_ADDRESSES="0x" + "AD" * 20))
    assert cfg2.admin_addresses == frozenset({"0x" + "ad" * 20})


def test_constants_single_source():
    """opus 審 M4：門檻與費率上限不重新宣告字面量，直接引用 spark.config 既有常數。"""
    from spark.config import MIN_BUILDER_BALANCE, Settings
    cfg = ApiConfig.from_env(_env())
    assert cfg.max_fee_rate == Settings.max_rate
    assert cfg.min_user_deposit is MIN_BUILDER_BALANCE
    assert cfg.min_builder_balance is MIN_BUILDER_BALANCE
