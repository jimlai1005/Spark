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


# ── 多 leader：白名單硬閘 ──────────────────────────────────────────────

_LEADER = "0x" + "d4" * 20
_OTHER_LEADER = "0x" + "e5" * 20


def _leaders_file(tmp_path, entries):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def _pending_with_leader(tmp_path, leader):
    """pending.json 的 leader_address 由 web 層寫入（write_pending_entry 尚無此參數，
    測試直接補寫該鍵，模擬 API 側寫入的條目）。"""
    pending, manifest = _setup(tmp_path)
    data = json.loads(pending.read_text())
    data["pending"][0]["leader_address"] = leader
    pending.write_text(json.dumps(data))
    return pending, manifest


def test_activate_without_leader_writes_none(tmp_path):
    """未指定 leader → None，引擎沿用 env COPY_LEADER_ADDRESS（向後相容）。"""
    pending, manifest = _setup(tmp_path)
    activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)
    assert load_followers(manifest)[0].leader_address is None


def test_activate_accepts_allowlisted_leader_from_pending(tmp_path):
    pending, manifest = _pending_with_leader(tmp_path, _LEADER)
    leaders = _leaders_file(tmp_path, [{"address": _LEADER, "name": "Alpha"}])
    activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
             leaders_path=leaders)
    assert load_followers(manifest)[0].leader_address == _LEADER


def test_activate_rejects_leader_not_in_allowlist(tmp_path):
    """⭐ 資安核心：pending 條目由 filet-api 寫入；該進程被打穿即可塞入惡意 leader。
    白名單只有管理端能寫 → 不在清單內一律拒絕啟用，manifest 不被碰。"""
    pending, manifest = _pending_with_leader(tmp_path, _OTHER_LEADER)
    leaders = _leaders_file(tmp_path, [{"address": _LEADER, "name": "Alpha"}])
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
                 leaders_path=leaders)
    assert not manifest.exists()                # manifest 未被碰
    assert len(load_pending(pending)) == 1      # 條目留在佇列供調查


def test_activate_rejects_disabled_leader(tmp_path):
    """已下架的 leader 不得被啟用（條目仍在白名單裡，但 enabled=False）。"""
    pending, manifest = _pending_with_leader(tmp_path, _LEADER)
    leaders = _leaders_file(tmp_path, [
        {"address": _LEADER, "name": "Alpha", "enabled": False}])
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
                 leaders_path=leaders)
    assert not manifest.exists()


def test_activate_rejects_leader_when_allowlist_missing(tmp_path):
    """白名單檔不存在 → 空清單 → 任何指定的 leader 都被拒（安全預設）。"""
    pending, manifest = _pending_with_leader(tmp_path, _LEADER)
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
                 leaders_path=str(tmp_path / "nope.json"))
    assert not manifest.exists()


def test_activate_cli_leader_overrides_pending(tmp_path):
    """--leader 覆寫 pending 條目（管理端當下的明確指示優先）。"""
    pending, manifest = _pending_with_leader(tmp_path, _OTHER_LEADER)
    leaders = _leaders_file(tmp_path, [{"address": _LEADER, "name": "Alpha"},
                                       {"address": _OTHER_LEADER, "name": "Beta"}])
    activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
             leader=_LEADER, leaders_path=leaders)
    assert load_followers(manifest)[0].leader_address == _LEADER


def test_activate_cli_leader_still_gated_by_allowlist(tmp_path):
    """⭐ --leader 不是後門：管理端手打的地址一樣要過白名單。"""
    pending, manifest = _setup(tmp_path)
    leaders = _leaders_file(tmp_path, [{"address": _LEADER, "name": "Alpha"}])
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
                 leader=_OTHER_LEADER, leaders_path=leaders)
    assert not manifest.exists()


def test_activate_leader_normalized_before_allowlist_check(tmp_path):
    """大小寫不同不該誤判為不在白名單（同基準比較，工程原則 1）。"""
    pending, manifest = _pending_with_leader(tmp_path,
                                             _LEADER.upper().replace("0X", "0x"))
    leaders = _leaders_file(tmp_path, [{"address": _LEADER, "name": "Alpha"}])
    activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
             leaders_path=leaders)
    assert load_followers(manifest)[0].leader_address == _LEADER


def test_activate_rejects_malformed_leader(tmp_path):
    pending, manifest = _pending_with_leader(tmp_path, "0xshort")
    leaders = _leaders_file(tmp_path, [{"address": _LEADER, "name": "Alpha"}])
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False,
                 leaders_path=leaders)
    assert not manifest.exists()
