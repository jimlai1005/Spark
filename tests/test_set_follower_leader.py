"""tests/test_set_follower_leader.py
管理端 CLI（scripts/set_follower_leader.py）：更新名冊**既有**條目的 leader_address。
與 filet_activate 的差異：activate 對已存在條目一律拒絕（新增流程），本 CLI 反過來
只更新既有條目，不新增——兩者互補，見 filet_activate.py 檔頭「拒絕重複啟用」。

盯住五件事（plan Task 1）：
1. dry-run 不改檔，且輸出含舊→新。
2. --yes 寫入後 load_followers 讀到新 leader，其餘欄位逐字不變。
3. 新 leader 不在白名單（合併精選＋registry 後仍不過 is_still_permitted）→ exit 3、檔案不變。
4. account_id 不存在於 manifest → exit 2。
5. 新舊 leader 相同 → exit 0、檔案不變（不印任何私鑰／簽章欄位，manifest 本來就沒有）。

純檔案操作，離線（tests/conftest.py 的 autouse socket-ban）。
"""
import json
import time

import pytest

from scripts.set_follower_leader import set_leader
from spark.filet.engine_health import (build_heartbeat, heartbeat_path_for,
                                       write_heartbeat)
from spark.filet.followers import load_followers

_ACCT = "f" + "ab" * 20
_USER = "0x" + "ab" * 20
_BUILDER = "0x" + "b1" * 20
_OLD_LEADER = "0x" + "aa" * 20
_NEW_LEADER = "0x" + "ee" * 20
_UNLISTED_LEADER = "0x" + "99" * 20


def _write_manifest(tmp_path, *, leader_address=_OLD_LEADER):
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": [{
        "account_id": _ACCT,
        "user_address": _USER,
        "builder_address": _BUILDER,
        "network": "testnet",
        "label": "alice",
        "leader_address": leader_address,
    }]}))
    return manifest


def _write_leaders(tmp_path, *addresses, enabled=True):
    leaders = tmp_path / "leaders.json"
    leaders.write_text(json.dumps({"leaders": [
        {"address": a, "name": f"leader-{a[-4:]}", "enabled": enabled}
        for a in addresses
    ]}))
    return leaders


# ── 1. dry-run 不改檔 ────────────────────────────────────────────────────

def test_dry_run_does_not_write_and_reports_old_to_new(tmp_path, capsys):
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    before_mtime = manifest.stat().st_mtime_ns
    before_content = manifest.read_text()

    msg = set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                     user_leaders_path=None, apply=False)

    assert manifest.stat().st_mtime_ns == before_mtime
    assert manifest.read_text() == before_content
    out = capsys.readouterr().out
    assert _OLD_LEADER in out
    assert _NEW_LEADER in out
    assert "dry-run" in msg or "未寫入" in msg


# ── 2. --yes 寫入後其餘欄位逐字不變 ──────────────────────────────────────

def test_yes_writes_new_leader_and_preserves_other_fields(tmp_path):
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=True)

    refs = load_followers(manifest)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.leader_address == _NEW_LEADER
    assert ref.account_id == _ACCT
    assert ref.user_address == _USER
    assert ref.builder_address == _BUILDER
    assert ref.network == "testnet"
    assert ref.label == "alice"


def test_yes_merges_user_registry_when_curated_missing(tmp_path):
    """新 leader 只在 user registry、不在精選白名單 → 合併後仍應通過（同引擎合併方式）。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path)  # 精選白名單空的
    registry = tmp_path / "user_leaders.json"
    registry.write_text(json.dumps({"leaders": [
        {"address": _NEW_LEADER, "name": "custom", "enabled": True,
         "source": "user", "added_by": _ACCT},
    ]}))

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=str(registry), apply=True)

    refs = load_followers(manifest)
    assert refs[0].leader_address == _NEW_LEADER


# ── 3. 新 leader 不在白名單 → exit 3、檔案不變 ───────────────────────────

def test_leader_not_permitted_exits_3_and_leaves_file_unchanged(tmp_path):
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _OLD_LEADER)  # 白名單裡沒有 _UNLISTED_LEADER
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _UNLISTED_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True)

    assert exc.value.code == 3
    assert manifest.read_text() == before_content


def test_leader_disabled_in_whitelist_exits_3(tmp_path):
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER, enabled=False)
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True)

    assert exc.value.code == 3
    assert manifest.read_text() == before_content


# ── 4. account_id 不存在 → exit 2 ────────────────────────────────────────

def test_unknown_account_id_exits_2(tmp_path):
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader("f" + "99" * 20, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True)

    assert exc.value.code == 2
    assert manifest.read_text() == before_content


# ── 5. 新舊相同 → exit 0、檔案不變 ───────────────────────────────────────

def test_same_leader_exits_0_and_leaves_file_unchanged(tmp_path):
    manifest = _write_manifest(tmp_path, leader_address=_NEW_LEADER)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True)

    assert exc.value.code == 0
    assert manifest.read_text() == before_content


def test_same_leader_case_insensitive_exits_0(tmp_path):
    """位址比較同基準（工程原則 1）：大小寫不同不算變更。"""
    manifest = _write_manifest(tmp_path, leader_address=_NEW_LEADER)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER.upper().replace("0X", "0x"), str(manifest),
                  leaders_path=str(leaders), user_leaders_path=None, apply=True)

    assert exc.value.code == 0


# ── W2＋S2＋S3（2026-09-30 reviewer）：引擎心跳核對／讀取失敗降級 ───────────
_ENGINE_LEADER = "0x" + "ed" * 20


def _write_hb(exchange_dir, account_id, *, leader_address, leader_source="manifest",
             age_s=5.0, cycle_result="no_action"):
    payload = build_heartbeat(
        account_id=account_id, now_s=time.time() - age_s,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=leader_address, leader_source=leader_source, leader_kind="standard",
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result=cycle_result, cycle_detail=None)
    write_heartbeat(heartbeat_path_for(exchange_dir, account_id), payload)


def test_engine_heartbeat_mismatch_exits_4_and_leaves_file_unchanged(tmp_path):
    """W2：心跳 ok 且回報的 leader（_ENGINE_LEADER）與要寫入的新 leader
    （_NEW_LEADER）不同 → `--yes` 拒絕（exit 4），manifest 不被碰。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_ENGINE_LEADER)
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir))

    assert exc.value.code == 4
    assert manifest.read_text() == before_content


def test_engine_heartbeat_mismatch_allowed_with_override_flag(tmp_path):
    """`--allow-engine-mismatch` 略過 W2 的心跳核對，正常寫入。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_ENGINE_LEADER)

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir),
              allow_engine_mismatch=True)

    refs = load_followers(manifest)
    assert refs[0].leader_address == _NEW_LEADER


def test_engine_heartbeat_matching_leader_does_not_block_write(tmp_path):
    """心跳回報的 leader 就是要寫入的新 leader（例如客戶已簽章、引擎已套用，
    CLI 只是把 manifest 補齊）→ 不觸發 exit 4，正常寫入。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_NEW_LEADER)

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir))

    refs = load_followers(manifest)
    assert refs[0].leader_address == _NEW_LEADER


def test_engine_heartbeat_mismatch_does_not_block_dry_run(tmp_path, capsys):
    """dry-run 從不寫入，不受 exit 4 攔阻——只印心跳資訊。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_ENGINE_LEADER)

    msg = set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                     user_leaders_path=None, apply=False, exchange_dir=str(exchange_dir))

    assert "dry-run" in msg or "未寫入" in msg
    out = capsys.readouterr().out
    assert "引擎心跳" in out
    assert _ENGINE_LEADER in out


def test_missing_exchange_dir_heartbeat_skips_engine_check(tmp_path):
    """`--exchange-dir` 有給但心跳檔不存在（`status="missing"`）→ 不攔阻寫入
    （心跳不可用時不該讓 CLI 卡死，同 `me_leader` 退回 manifest 的既有精神）。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"  # 從未寫過心跳

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir))

    refs = load_followers(manifest)
    assert refs[0].leader_address == _NEW_LEADER


# ── C2＋S3（2026-09-30 第二輪 reviewer）：撤銷心跳／過期不可讀心跳 ─────────

def test_engine_heartbeat_revoked_exits_4_and_leaves_file_unchanged(tmp_path):
    """C2：心跳 ok 但明確回報「沒有 leader」（撤銷後，`leader.address` 鍵存在
    且為 None，`scripts/run_copytrade.py` 的 revoke 路徑每輪這樣寫）→ `--yes`
    拒絕（exit 4），manifest 不被碰。舊版只在 `hb_leader_address is not None`
    時才核對，這種情況會整段跳過、繞過紅線 5。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=None, leader_source=None,
             cycle_result="revoked")
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir))

    assert exc.value.code == 4
    assert manifest.read_text() == before_content


def test_engine_heartbeat_revoked_allowed_with_override_flag(tmp_path):
    """撤銷情境下 `--allow-engine-mismatch` 仍能明確承認風險略過（同 mismatch
    情境），正常寫入。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=None, leader_source=None,
             cycle_result="revoked")

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir),
              allow_engine_mismatch=True)

    refs = load_followers(manifest)
    assert refs[0].leader_address == _NEW_LEADER


def test_engine_heartbeat_stale_exits_4_and_leaves_file_unchanged(tmp_path):
    """S3：心跳存在但過期（> `HEARTBEAT_STALE_S`）→ 不知道引擎目前實際在跟誰，
    `--yes` 一律拒絕（exit 4），manifest 不被碰。舊版只核對 `status == "ok"`，
    stale／unreadable 會直接放行（看不到就當安全，危險方向的默認）。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_ENGINE_LEADER, age_s=700)
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir))

    assert exc.value.code == 4
    assert manifest.read_text() == before_content


def test_engine_heartbeat_stale_allowed_with_override_flag(tmp_path):
    """S3：`--allow-engine-mismatch` 能明確承認風險略過 stale 心跳的攔阻。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_ENGINE_LEADER, age_s=700)

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir),
              allow_engine_mismatch=True)

    refs = load_followers(manifest)
    assert refs[0].leader_address == _NEW_LEADER


def test_corrupt_leaders_whitelist_exits_2_no_traceback(tmp_path, capsys):
    """S2＋S3：白名單壞 JSON → exit 2＋一行訊息，manifest 不變、不炸
    traceback（`load_leaders` 的 `ValueError` 必須被本 CLI 接住）。"""
    manifest = _write_manifest(tmp_path)
    leaders = tmp_path / "leaders.json"
    leaders.write_text("{ not json")
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True)

    assert exc.value.code == 2
    assert manifest.read_text() == before_content
    out = capsys.readouterr().out
    assert "白名單" in out


def test_corrupt_manifest_exits_2_no_traceback(tmp_path):
    """S2：manifest 本身壞 JSON → exit 2，不是未捕捉的 JSONDecodeError。"""
    manifest = tmp_path / "followers.json"
    manifest.write_text("{ not json")
    leaders = _write_leaders(tmp_path, _NEW_LEADER)

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True)

    assert exc.value.code == 2


def test_dry_run_prints_manifest_owner_and_mode(tmp_path, capsys):
    """S3：dry-run 印出 manifest 的 owner／mode（供操作者確認執行身分是否
    對得上，見檔頭「dry-run 印名冊檔的 owner／mode」）。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)

    set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
              user_leaders_path=None, apply=False)

    out = capsys.readouterr().out
    assert "owner=" in out and "mode=" in out


def test_engine_heartbeat_non_dict_leader_exits_4_without_traceback(tmp_path):
    """第三輪 reviewer：心跳合法 JSON 但 `leader` 不是 dict → 視同取不到位址，
    `--yes` 走 exit 4（安全方向），不得 AttributeError 炸 traceback；manifest 不變。"""
    manifest = _write_manifest(tmp_path)
    leaders = _write_leaders(tmp_path, _NEW_LEADER)
    exchange_dir = tmp_path / "exchange"
    _write_hb(str(exchange_dir), _ACCT, leader_address=_NEW_LEADER)
    hb_path = heartbeat_path_for(str(exchange_dir), _ACCT)
    raw = json.loads(hb_path.read_text())
    raw["leader"] = "garbage"
    hb_path.write_text(json.dumps(raw))
    before_content = manifest.read_text()

    with pytest.raises(SystemExit) as exc:
        set_leader(_ACCT, _NEW_LEADER, str(manifest), leaders_path=str(leaders),
                  user_leaders_path=None, apply=True, exchange_dir=str(exchange_dir))

    assert exc.value.code == 4
    assert manifest.read_text() == before_content
