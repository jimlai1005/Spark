import pytest
from spark.keystore.keychain import MacKeychainBackend

SERVICE = "spark-test"
# 一個合法的 testnet 私鑰（eth_account 測試向量，絕不可用於有資產的帳號）
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
    # 斷言確切衍生地址，證明載入的是「正確」的 key（而非任何 0x 物件）
    assert signer.address == "0x74C96c3B9BD3487aD4567fC48a4FCcA3c304B96D"


def test_main_and_agent_are_separate_roles(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    ks.import_key("acct1", "main", PRIV)
    with pytest.raises(KeyError):
        ks.get_agent_signer("acct1")  # 只匯入了 main，沒有 agent


def test_missing_key_raises(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    with pytest.raises(KeyError):
        ks.get_main_signer("nope")


def test_import_key_rejects_invalid_role(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    with pytest.raises(ValueError):
        ks.import_key("acct1", "treasurer", PRIV)
