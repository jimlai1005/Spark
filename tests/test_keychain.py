import pytest
from spark.keystore.keychain import MacKeychainBackend

SERVICE = "spark-test"
# 一個合法的 testnet 私鑰（測試用固定值，非真資產）
PRIV = "0x4c0883a69102937d6231471b5dbb6204fe512961708279f1f6e3d2b7c1f0f2aa"


@pytest.fixture
def fake_keyring(monkeypatch):
    store = {}
    monkeypatch.setattr("spark.keystore.keychain.keyring.get_password",
                        lambda svc, key: store.get((svc, key)))
    monkeypatch.setattr("spark.keystore.keychain.keyring.set_password",
                        lambda svc, key, val: store.__setitem__((svc, key), val))
    return store


def test_get_agent_signer_loads_account_from_keychain(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    ks.import_key("acct1", "agent", PRIV)
    signer = ks.get_agent_signer("acct1")
    assert signer.address.lower().startswith("0x")


def test_main_and_agent_are_separate_roles(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    ks.import_key("acct1", "main", PRIV)
    with pytest.raises(KeyError):
        ks.get_agent_signer("acct1")  # 只匯入了 main，沒有 agent


def test_missing_key_raises(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    with pytest.raises(KeyError):
        ks.get_main_signer("nope")
