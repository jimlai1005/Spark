"""tests/test_api_me_leader.py — GET /api/me/leader（客戶查自己目前跟隨的 leader）。

盯住三件事：(1) ⭐ **只查得到自己的**（session 隔離，且結構上沒有 account 參數）；
(2) 四種狀態語意明確，前端不必從 null 猜；(3) signature 之類的機密不外流。
"""
import json
import socket
import time

import pytest
from fastapi.testclient import TestClient

from spark.filet.engine_health import build_heartbeat, heartbeat_path_for, write_heartbeat
from tests.publicapi_helpers import login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


_LEADER_A = "0x" + "a1" * 20
_LEADER_B = "0x" + "b2" * 20
BUILDER = "0x" + "b1" * 20


def acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def write_manifest(tmp_path, followers) -> str:
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": followers}))
    return str(p)


def follower(wallet, leader=None, **over):
    f = {"account_id": acct(wallet), "user_address": wallet.address.lower(),
         "builder_address": BUILDER, "network": "testnet", "label": "t"}
    if leader is not None:
        f["leader_address"] = leader
    f.update(over)
    return f


def write_leaders(tmp_path, entries) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def write_hb(cfg, account_id, *, leader_address, age_s=5.0):
    """引擎心跳（僅本測試檔關心的欄位；其餘用最保守的已知值填滿）。"""
    payload = build_heartbeat(
        account_id=account_id, now_s=time.time() - age_s,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=leader_address, leader_source="manifest", leader_kind="standard",
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, account_id), payload)


def make_me_app(tmp_path, *, followers=None, leaders=None, manifest=None):
    """manifest 可直接給路徑（測「不存在」的情境）；否則由 followers 寫一份。"""
    cfg = make_cfg(
        tmp_path,
        followers_path=manifest if manifest is not None
        else write_manifest(tmp_path, followers or []),
        leaders_path=write_leaders(tmp_path, leaders or []))
    app, *_ = make_app(tmp_path, cfg=cfg)
    return app


def test_requires_session(tmp_path):
    app = make_me_app(tmp_path)
    assert _client(app).get("/api/me/leader").status_code == 401


def test_returns_own_leader_with_name(tmp_path):
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, followers=[follower(w, _LEADER_A)],
                      leaders=[{"address": _LEADER_A, "name": "Alpha"}])
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "following"
    assert body["leader_address"] == _LEADER_A
    assert body["leader_name"] == "Alpha"
    assert body["account_id"] == acct(w)
    assert body["note"]


def test_session_isolation_never_reveals_another_customers_leader(tmp_path):
    """⭐ 兩個客戶各跟不同 leader：每個 session 只看得到自己的那一個，
    另一個人的 leader 位址**不得出現在回應的任何角落**。"""
    from eth_account import Account
    wa, wb = Account.create(), Account.create()
    app = make_me_app(
        tmp_path,
        followers=[follower(wa, _LEADER_A), follower(wb, _LEADER_B)],
        leaders=[{"address": _LEADER_A, "name": "Alpha"},
                 {"address": _LEADER_B, "name": "Bravo"}])

    ca = _client(app)
    login(ca, wallet=wa)
    ra = ca.get("/api/me/leader")
    assert ra.json()["leader_address"] == _LEADER_A
    assert ra.json()["account_id"] == acct(wa)
    assert _LEADER_B not in ra.text and "Bravo" not in ra.text
    assert acct(wb) not in ra.text

    cb = _client(app)
    login(cb, wallet=wb)
    rb = cb.get("/api/me/leader")
    assert rb.json()["leader_address"] == _LEADER_B
    assert _LEADER_A not in rb.text and "Alpha" not in rb.text


def test_account_id_cannot_be_overridden_by_query_or_body(tmp_path):
    """⭐ 端點沒有 account 參數：夾帶任何自訂參數都不會改變答案（結構保證，
    不是檢查——想查別人只能先拿到別人的 session）。"""
    from eth_account import Account
    wa, wb = Account.create(), Account.create()
    app = make_me_app(tmp_path,
                      followers=[follower(wa, _LEADER_A), follower(wb, _LEADER_B)])
    c = _client(app)
    login(c, wallet=wa)
    for params in ({"account_id": acct(wb)}, {"address": wb.address},
                   {"user_address": wb.address}, {"leader_address": _LEADER_B}):
        r = c.get("/api/me/leader", params=params)
        assert r.status_code == 200, r.text
        assert r.json()["leader_address"] == _LEADER_A
        assert _LEADER_B not in r.text


def test_engine_default_is_distinct_from_not_activated(tmp_path):
    """⭐ manifest 有這筆但沒指定 leader ＝ **正在跟單**，只是沿用引擎預設。
    與「還沒活化」是兩種完全不同的處境，不可都回 null 讓前端自己猜。"""
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, followers=[follower(w)])   # 無 leader_address 鍵
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "engine_default"
    assert body["leader_address"] is None
    assert "跟單仍在進行中" in body["note"]


def test_not_activated_when_absent_from_manifest(tmp_path):
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, followers=[])
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "not_activated"
    assert body["leader_address"] is None and body["pending_change"] is None
    assert "尚未啟用" in body["note"]


def test_indeterminate_when_manifest_has_unparsable_entries(tmp_path):
    """⭐ 帳號查無 **且** manifest 有壞條目 → 壞的那筆可能就是他自己的。
    回 not_activated 會讓一個正在跟單的客戶以為資金沒在動（危險方向的誤讀）。"""
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, followers=[{"account_id": "bad!!", "oops": 1}])
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "indeterminate"
    assert "不要當作" in body["note"]
    assert "bad!!" not in body["note"]      # 內部解析錯誤不外流給客戶


def test_missing_manifest_returns_503_not_a_false_negative(tmp_path):
    """manifest 讀不到 → 503。回「你沒在跟單」比回錯誤危險：客戶會因此
    以為資金沒在動而不去看它（工程原則 3）。"""
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, manifest=str(tmp_path / "nope.json"))
    c = _client(app)
    login(c, wallet=w)
    assert c.get("/api/me/leader").status_code == 503


def test_broken_allowlist_still_returns_the_leader_address(tmp_path):
    """白名單壞掉只影響顯示名稱：leader 位址出自 manifest，是獨立的真相。"""
    from eth_account import Account
    w = Account.create()
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=str(p))
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "following"
    assert body["leader_address"] == _LEADER_A
    assert body["leader_name"] is None


# --- 引擎心跳優先於 manifest（2026-09-30，leader 顯示真相化） ------------
_LEADER_ENGINE = "0x" + "ed" * 20


def test_fresh_heartbeat_leader_overrides_manifest(tmp_path):
    """心跳 ok 且回報的 leader 與 manifest 不同 → 以心跳為準、`leader_source`
    標成 engine；已提交、目標==有效 leader 的換 leader 記錄視為已生效
    （`pending_change` 為 None，比對基準是有效 leader 而非 manifest 值）。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, []))
    write_hb(cfg, acct(w), leader_address=_LEADER_ENGINE)
    write_change(cfg.exchange_dir, acct(w), _LEADER_ENGINE)   # 目標==心跳有效值
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["leader_address"] == _LEADER_ENGINE
    assert body["leader_source"] == "engine"
    assert body["pending_change"] is None
    assert body["note"] == "這是引擎目前為你跟隨的 leader。"


def test_missing_heartbeat_falls_back_to_manifest(tmp_path):
    """心跳從未寫過（剛活化／子目錄不存在）→ 退回 manifest，`leader_source`
    標成 manifest，note 提示引擎回報暫時不可用。"""
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, followers=[follower(w, _LEADER_A)],
                      leaders=[{"address": _LEADER_A, "name": "Alpha"}])
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["leader_address"] == _LEADER_A
    assert body["leader_source"] == "manifest"
    assert body["note"] == ("引擎回報暫時不可用，以下為登記的 leader；"
                            "引擎恢復回報後會自動更新。")


def test_stale_heartbeat_falls_back_to_manifest(tmp_path):
    """心跳存在但超過 600 秒 → 過期的值不可信，退回 manifest（與缺席同一種
    `leader_source`／note）。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, [{"address": _LEADER_A,
                                                          "name": "Alpha"}]))
    write_hb(cfg, acct(w), leader_address=_LEADER_ENGINE, age_s=700)  # > 600 HEARTBEAT_STALE_S
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["leader_address"] == _LEADER_A
    assert body["leader_source"] == "manifest"
    assert body["note"] == ("引擎回報暫時不可用，以下為登記的 leader；"
                            "引擎恢復回報後會自動更新。")
    assert _LEADER_ENGINE not in c.get("/api/me/leader").text


def test_governance_flags_never_leak_for_own_leader(tmp_path):
    """自己正在跟的 leader 被撤銷（enabled=false）：名稱照顯示（他就是你在跟的人，
    而且是你當初在目錄看過的名字），但**治理旗標不外流**——回應不得讓客戶推得出
    是「安全撤銷」還是「例行下架」（沿 /api/leaders 的既有理由）。"""
    from eth_account import Account
    w = Account.create()
    app = make_me_app(tmp_path, followers=[follower(w, _LEADER_A)],
                      leaders=[{"address": _LEADER_A, "name": "Alpha",
                                "enabled": False}])
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["leader_address"] == _LEADER_A
    # enabled=false 的 leader 不在「可選清單」的語意內——名稱照查得到（他就是
    # 你正在跟的人），但回應裡不得出現任何治理旗標。
    assert "enabled" not in body and "accepting_new" not in body


# --- C1／W1（2026-09-30 reviewer）：三路狀態各自的測試 ----------------------
def test_engine_no_leader_when_heartbeat_ok_but_revoked(tmp_path):
    """⭐⭐ C1：心跳 ok 但明確回報「沒有 leader」（撤銷後
    `scripts/run_copytrade.py` 每輪寫 `leader: {address: None, source: None,
    kind: None}`）→ 新狀態 `engine_no_leader`，**不得**落進 manifest 退回分支
    （舊 bug：客戶會看到一個引擎早已不再跟隨的舊 manifest leader，且 note
    誤稱「引擎回報暫時不可用」）。`pending_change` 恆為 None。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, [{"address": _LEADER_A,
                                                          "name": "Alpha"}]))
    payload = build_heartbeat(
        account_id=acct(w), now_s=time.time() - 5.0,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=None, leader_source=None, leader_kind=None,
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result="revoked", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, acct(w)), payload)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "engine_no_leader"
    assert body["leader_address"] is None
    assert body["leader_name"] is None
    assert body["leader_source"] == "engine"
    assert body["pending_change"] is None
    assert body["note"] == ("引擎目前沒有跟隨任何 leader（跟單已撤銷或已停止）；"
                            "請看本頁的風控與狀態區塊。")
    assert _LEADER_A not in c.get("/api/me/leader").text  # 舊 manifest 值不外流


def test_malformed_engine_leader_address_falls_back_to_manifest_not_engine_no_leader(tmp_path):
    """⭐⭐ W1（2026-09-30 第二輪 reviewer）：心跳 ok 但 `leader.address` 是壞字串
    （非合法位址，normalize 失敗）——這是「引擎回報格式異常」，不是「明確沒有
    leader」，不得落進 `engine_no_leader`（那會讓客戶誤以為撤銷了）；必須退回
    manifest 的登記值，`leader_source="manifest"`，`status` 依 manifest 判斷
    （manifest 有指定 → following）。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, [{"address": _LEADER_A,
                                                          "name": "Alpha"}]))
    payload = build_heartbeat(
        account_id=acct(w), now_s=time.time() - 5.0,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address="not-a-valid-address", leader_source="manifest",
        leader_kind="standard",
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, acct(w)), payload)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] != "engine_no_leader"
    assert body["status"] == "following"
    assert body["leader_address"] == _LEADER_A
    assert body["leader_source"] == "manifest"
    assert body["note"] == "引擎回報格式異常，以下為登記的 leader。"


def test_non_dict_heartbeat_leader_falls_back_to_manifest_without_5xx(tmp_path):
    """第三輪 reviewer：心跳合法 JSON 但 `leader` 整個不是 dict（手改／寫入交錯）
    → 與壞位址同一類「格式異常」：退回 manifest、`leader_source="manifest"`，
    端點不得 AttributeError 500。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, [{"address": _LEADER_A,
                                                          "name": "Alpha"}]))
    payload = build_heartbeat(
        account_id=acct(w), now_s=time.time() - 5.0,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=_LEADER_A, leader_source="manifest", leader_kind="standard",
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result="no_action", cycle_detail=None)
    payload["leader"] = "garbage"
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, acct(w)), payload)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    r = c.get("/api/me/leader")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "following"
    assert body["leader_address"] == _LEADER_A
    assert body["leader_source"] == "manifest"
    assert body["note"] == "引擎回報格式異常，以下為登記的 leader。"


def test_engine_default_carries_address_when_heartbeat_reports_env_default(tmp_path):
    """⭐ W1：心跳 ok 且明確回報 `leader.source == "env_default"` →
    `engine_default` 帶著真正的 `leader_address`（不再是 null）——引擎知道
    自己沿用哪個位址，沒有理由讓客戶對著一個 null 猜。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w)]),  # 無 leader_address
                   leaders_path=write_leaders(tmp_path, []))
    payload = build_heartbeat(
        account_id=acct(w), now_s=time.time() - 5.0,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=_LEADER_ENGINE, leader_source="env_default", leader_kind="standard",
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, acct(w)), payload)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "engine_default"
    assert body["leader_address"] == _LEADER_ENGINE
    assert body["leader_source"] == "engine"


def test_following_when_heartbeat_reports_customer_signed_override(tmp_path):
    """⭐ 簽章 override（`LeaderResolution.source == "customer_signed"`）生效時
    → `following`，`leader_source` 標成 engine（有效值來自心跳，不是 manifest
    退回）。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, []))
    payload = build_heartbeat(
        account_id=acct(w), now_s=time.time() - 5.0,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=_LEADER_ENGINE, leader_source="customer_signed",
        leader_kind="standard",
        allocated_capital="0", capital_utilization="0", use_full_equity=True,
        capital_source="env_default", capital_changed_at=None,
        risk_controls_enabled=True, risk_source="env_default",
        risk_changed_at=None, risk_prefs=None,
        risk_halt=None, cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, acct(w)), payload)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["status"] == "following"
    assert body["leader_address"] == _LEADER_ENGINE
    assert body["leader_source"] == "engine"


# --- 已簽署但尚未生效的換 leader ------------------------------------------
def write_change(cfg_exchange_dir, account_id, leader, issued_at="2026-07-19T00:00:00Z"):
    from pathlib import Path

    from spark.filet.leader_change import leader_changes_path_for
    p = Path(leader_changes_path_for(cfg_exchange_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"changes": [{
        "account_id": account_id, "leader_address": leader, "nonce": "n" * 32,
        "issued_at": issued_at, "signature": "0xdeadbeefSECRET", "message": "原文"}]}))


def test_pending_change_is_surfaced_with_effective_semantics(tmp_path):
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, [{"address": _LEADER_A,
                                                          "name": "Alpha"}]))
    write_change(cfg.exchange_dir, acct(w), _LEADER_B)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    r = c.get("/api/me/leader")
    body = r.json()
    assert body["leader_address"] == _LEADER_A            # 現況仍是舊 leader
    assert body["pending_change"]["leader_address"] == _LEADER_B
    assert body["pending_change"]["effective"] == "next_engine_cycle"
    assert body["pending_change"]["issued_at"] == "2026-07-19T00:00:00Z"
    # ⭐ signature／message 原文絕不外流（沿 leaders_select 的既有政策）
    assert "SECRET" not in r.text and "signature" not in r.text


def test_applied_change_is_not_reported_as_pending(tmp_path):
    """⭐ 記錄是「同 account 覆蓋」而非流水帳，套用後仍留在檔裡。已生效
    （記錄的 leader == manifest 的 leader）就不該再顯示「處理中」。"""
    from eth_account import Account
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_B)]),
                   leaders_path=write_leaders(tmp_path, [{"address": _LEADER_B,
                                                          "name": "Bravo"}]))
    write_change(cfg.exchange_dir, acct(w), _LEADER_B)     # 已被引擎套用
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    body = c.get("/api/me/leader").json()
    assert body["leader_address"] == _LEADER_B
    assert body["pending_change"] is None


def test_another_customers_pending_change_is_never_returned(tmp_path):
    """⭐ 變更記錄檔是全客戶共用一份 → 必須按 account_id 過濾，只回自己的。"""
    from eth_account import Account
    wa, wb = Account.create(), Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(
                       tmp_path, [follower(wa, _LEADER_A), follower(wb, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, []))
    write_change(cfg.exchange_dir, acct(wb), _LEADER_B)    # 只有 B 提交了變更
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=wa)                                    # 以 A 的身分查
    r = c.get("/api/me/leader")
    assert r.json()["pending_change"] is None
    assert _LEADER_B not in r.text and acct(wb) not in r.text


def test_corrupt_change_file_degrades_without_500(tmp_path):
    from eth_account import Account
    from pathlib import Path

    from spark.filet.leader_change import leader_changes_path_for
    w = Account.create()
    cfg = make_cfg(tmp_path,
                   followers_path=write_manifest(tmp_path, [follower(w, _LEADER_A)]),
                   leaders_path=write_leaders(tmp_path, []))
    p = Path(leader_changes_path_for(cfg.exchange_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c, wallet=w)
    r = c.get("/api/me/leader")
    assert r.status_code == 200, r.text
    assert r.json()["leader_address"] == _LEADER_A     # 主要答案不受影響
    assert r.json()["pending_change"] is None
