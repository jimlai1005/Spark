"""run_copytrade 多實例接線（M2 Task 5）。

驗證 select_keystore() 依 env FILET_KEYSTORE 選對後端（未設/keychain → Mac
開發用 MacKeychainBackend；envfile → VPS 用 EnvFileKeyStore，讀 FILET_KEYS_DIR），
以及 wrap_notifier() 只在 account_id 有值時才包 TaggedNotifier（dry/shadow 無
account 時原樣回傳 inner，不炸）。全程 monkeypatch env，不真取 key、不觸網。
"""
import scripts.run_copytrade as rc
from spark.copytrade.notifier import NullNotifier
from spark.filet.tagged_notifier import TaggedNotifier
from spark.keystore.envfile import EnvFileKeyStore
from spark.keystore.keychain import MacKeychainBackend


def test_select_keystore_default_keychain(monkeypatch):
    monkeypatch.delenv("FILET_KEYSTORE", raising=False)
    assert isinstance(rc.select_keystore(), MacKeychainBackend)


def test_select_keystore_envfile(monkeypatch, tmp_path):
    monkeypatch.setenv("FILET_KEYSTORE", "envfile")
    monkeypatch.setenv("FILET_KEYS_DIR", str(tmp_path))
    ks = rc.select_keystore()
    assert isinstance(ks, EnvFileKeyStore)


def test_wrap_notifier_tags_when_account(monkeypatch):
    n = rc.wrap_notifier(NullNotifier(), account_id="alice")
    assert isinstance(n, TaggedNotifier)


def test_wrap_notifier_passthrough_without_account(monkeypatch):
    base = NullNotifier()
    assert rc.wrap_notifier(base, account_id=None) is base
