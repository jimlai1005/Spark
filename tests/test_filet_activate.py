"""tests/test_filet_activate.py
activate：pending → followers.json（人工 CLI；builder 結構性核對）。純檔案操作，離線。"""
import json

import pytest

from scripts.filet_activate import activate
from spark.filet.followers import load_followers
from spark.publicapi.pending import load_pending, write_pending_entry

_BUILDER = "0x" + "b1" * 20
_USER = "0x" + "ab" * 20
_ACCT = "f" + "ab" * 20


def _setup(tmp_path, builder=_BUILDER):
    pending = tmp_path / "pending.json"
    manifest = tmp_path / "followers.json"
    write_pending_entry(pending, account_id=_ACCT, user_address=_USER,
                        builder_address=builder, network="testnet",
                        agent_address="0x" + "cd" * 20, label="alice")
    return pending, manifest


def test_activate_moves_entry_to_manifest(tmp_path):
    pending, manifest = _setup(tmp_path)
    msg = activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)
    refs = load_followers(manifest)  # fail-fast 載入 = 引擎視角驗證
    assert len(refs) == 1
    assert refs[0].account_id == _ACCT
    assert refs[0].user_address == _USER
    assert refs[0].builder_address == _BUILDER
    assert refs[0].network == "testnet"
    assert refs[0].label == "alice"
    assert load_pending(pending) == []          # 已從佇列移除
    assert f"filet-follower@{_ACCT}" in msg     # 印出啟動指令（預設不執行）


def test_activate_rejects_builder_mismatch(tmp_path):
    """⭐ 紅線 6：pending 條目 builder != 部署常數 → 條目可疑，拒絕啟用。"""
    pending, manifest = _setup(tmp_path, builder="0x" + "ee" * 20)
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)
    assert not manifest.exists()                # manifest 未被碰
    assert len(load_pending(pending)) == 1      # 條目留在佇列供調查


def test_activate_rejects_duplicate_in_manifest(tmp_path):
    pending, manifest = _setup(tmp_path)
    manifest.write_text(json.dumps({"followers": [{
        "account_id": _ACCT, "user_address": _USER,
        "builder_address": _BUILDER, "network": "testnet"}]}))
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)


def test_activate_unknown_account(tmp_path):
    pending, manifest = _setup(tmp_path)
    with pytest.raises(SystemExit):
        activate("f" + "99" * 20, str(pending), str(manifest), _BUILDER, start=False)


def test_activate_case_insensitive_builder_check(tmp_path):
    """比對前同 normalize 基準（工程原則 1）：大小寫不同不該誤判。"""
    pending, manifest = _setup(tmp_path)
    activate(_ACCT, str(pending), str(manifest), _BUILDER.upper().replace("0X", "0x"),
             start=False)
    assert len(load_followers(manifest)) == 1


def test_activate_rejects_account_id_mismatch(tmp_path):
    """⭐ 紅線 6：pending 條目 account_id != derive_account_id(user_address) → 拒絕啟用。"""
    pending = tmp_path / "pending.json"
    manifest = tmp_path / "followers.json"
    # 故意寫入不符的 account_id（真正的應該是 f+ab...，但我們寫 f+99...）
    wrong_acct = "f" + "99" * 20
    write_pending_entry(pending, account_id=wrong_acct, user_address=_USER,
                        builder_address=_BUILDER, network="testnet",
                        agent_address="0x" + "cd" * 20, label="alice")
    with pytest.raises(SystemExit):
        activate(wrong_acct, str(pending), str(manifest), _BUILDER, start=False)
    assert not manifest.exists()                # manifest 未被碰
    assert len(load_pending(pending)) == 1      # 條目留在佇列供調查
