"""tests/test_envfile_keystore.py"""
import os
import stat

import pytest
from eth_account import Account

from spark.keystore.envfile import EnvFileKeyStore

_PK = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
_ADDR = Account.from_key(_PK).address


def test_import_and_read_agent_roundtrip(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    ks.import_agent_key("acct1", _PK)
    assert ks.get_agent_signer("acct1").address == _ADDR


def test_imported_key_file_is_600(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    ks.import_agent_key("acct1", _PK)
    assert stat.S_IMODE((tmp_path / "acct1" / "agent.key").stat().st_mode) == 0o600


def test_get_main_signer_always_refuses(tmp_path):
    with pytest.raises(PermissionError):
        EnvFileKeyStore(tmp_path).get_main_signer("acct1")


def test_unsafe_permissions_refused(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    ks.import_agent_key("acct1", _PK)
    os.chmod(tmp_path / "acct1" / "agent.key", 0o644)
    with pytest.raises(PermissionError):
        ks.get_agent_signer("acct1")


def test_missing_key_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        EnvFileKeyStore(tmp_path).get_agent_signer("nope")


def test_private_key_never_in_exception(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    ks.import_agent_key("acct1", _PK)
    os.chmod(tmp_path / "acct1" / "agent.key", 0o644)
    try:
        ks.get_agent_signer("acct1")
    except PermissionError as e:
        assert _PK not in str(e) and _PK[2:] not in str(e)


# ── M2 Task 10：account_id 路徑安全縱深防禦 ──────────────────────────────

def test_get_agent_signer_rejects_unsafe_account_id_no_filesystem_touch(tmp_path):
    """繞過 registry 直接呼叫（縱深防禦）：不合法 account_id → ValueError（非
    KeyError），且不得觸及檔案系統（root 內不應多出任何東西）。"""
    ks = EnvFileKeyStore(tmp_path)
    with pytest.raises(ValueError):
        ks.get_agent_signer("../evil")
    assert list(tmp_path.iterdir()) == []


def test_import_agent_key_rejects_unsafe_account_id_no_filesystem_touch(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    with pytest.raises(ValueError):
        ks.import_agent_key("../evil", _PK)
    assert list(tmp_path.iterdir()) == []


def test_import_agent_key_refuses_overwrite(tmp_path):
    """O_EXCL：既有金鑰存在時再 import 同一 account → FileExistsError（絕不覆寫）。"""
    ks = EnvFileKeyStore(tmp_path)
    ks.import_agent_key("acct1", _PK)
    other = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    with pytest.raises(FileExistsError):
        ks.import_agent_key("acct1", other)
    # 既有金鑰未被截斷：仍是第一把
    assert ks.get_agent_signer("acct1").address == _ADDR
