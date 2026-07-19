"""run_copytrade 多實例接線（M2 Task 5）。

驗證 select_keystore() 依 env FILET_KEYSTORE 選對後端（未設/keychain → Mac
開發用 MacKeychainBackend；envfile → VPS 用 EnvFileKeyStore，讀 FILET_KEYS_DIR），
以及 wrap_notifier() 只在 account_id 有值時才包 TaggedNotifier（dry/shadow 無
account 時原樣回傳 inner，不炸）。全程 monkeypatch env，不真取 key、不觸網。

另含 leader 解析接線（per-follower leader ＋ 白名單二次驗證）：env 路徑選檔、
manifest/env 兩條來源、以及「leader 不在白名單即拒絕啟動」的 main() 層證據。
"""
import json

import pytest

import scripts.run_copytrade as rc
from spark.copytrade.notifier import NullNotifier
from spark.filet.leader_resolve import SOURCE_ENV_DEFAULT, SOURCE_MANIFEST
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


# ── leader 解析接線（per-follower ＋ 白名單二次驗證）────────────────────

_ME = "0x" + "11" * 20
_BUILDER = "0x" + "22" * 20
_LEADER = "0x" + "d4" * 20
_OTHER = "0x" + "e5" * 20


def _wire_env(monkeypatch, tmp_path, *, manifest_leader, allowlist):
    """佈置一份 manifest ＋ 白名單，並把引擎需要的 env 全部設好。"""
    m = tmp_path / "followers.json"
    entry = {"account_id": "alice", "user_address": _ME,
             "builder_address": _BUILDER, "network": "mainnet", "label": ""}
    if manifest_leader is not None:
        entry["leader_address"] = manifest_leader
    m.write_text(json.dumps({"followers": [entry]}))
    lp = tmp_path / "leaders.json"
    lp.write_text(json.dumps({"leaders": allowlist}))
    monkeypatch.setenv("FILET_FOLLOWERS", str(m))
    monkeypatch.setenv("FILET_LEADERS_PATH", str(lp))
    monkeypatch.setenv("SPARK_USER_ADDR", _ME)
    monkeypatch.setenv("SPARK_BUILDER_ADDR", _BUILDER)
    monkeypatch.setenv("SPARK_ACCOUNT_ID", "alice")
    monkeypatch.setenv("SPARK_NETWORK", "mainnet")
    monkeypatch.delenv("COPY_LIVE_TRADING", raising=False)


def test_make_leader_resolver_reads_env_paths(monkeypatch, tmp_path):
    """FILET_FOLLOWERS / FILET_LEADERS_PATH 決定引擎驗哪兩份檔。"""
    _wire_env(monkeypatch, tmp_path, manifest_leader=_LEADER,
              allowlist=[{"address": _LEADER, "name": "Alpha"}])
    res = rc.make_leader_resolver("alice", _ME, _OTHER)()
    assert res.address == _LEADER and res.source == SOURCE_MANIFEST


def test_make_leader_resolver_env_fallback(monkeypatch, tmp_path):
    """manifest 未指定 → 回退 env 預設（且該預設仍要在白名單內）。"""
    _wire_env(monkeypatch, tmp_path, manifest_leader=None,
              allowlist=[{"address": _OTHER, "name": "Env"}])
    res = rc.make_leader_resolver("alice", _ME, _OTHER)()
    assert res.address == _OTHER and res.source == SOURCE_ENV_DEFAULT


def test_main_refuses_to_start_when_leader_not_allowlisted(monkeypatch, tmp_path, capsys):
    """⭐ 啟動時 leader 不在白名單 → 拒絕啟動（SystemExit(2)），且在碰網路之前。

    本測試同時是「解析發生在 Info() 建構前」的結構性證據：conftest 的 autouse
    socket-ban 會讓任何連線炸成 RuntimeError，而這裡拿到的是乾淨的 SystemExit(2)。
    """
    _wire_env(monkeypatch, tmp_path, manifest_leader=_OTHER,
              allowlist=[{"address": _LEADER, "name": "Alpha"}])
    with pytest.raises(SystemExit) as e:
        rc.main(["--once"])
    assert e.value.code == 2
    assert "leader 解析失敗，拒絕啟動" in capsys.readouterr().out
