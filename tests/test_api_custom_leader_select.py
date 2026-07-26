"""tests/test_api_custom_leader_select.py — 自訂 leader 走簽章選擇流程（2026-07-27 spec）。

盯住五件事：
(1) 訊息端點對**可准入**的非精選位址發待簽原文（取代原本的一律拒絕）；
(2) POST select 在驗簽通過後**重新執行全部准入檢查**（不信任客戶端曾呼叫
    preview，防 TOCTOU）；
(3) 通過 → **冪等**寫入 user registry（source:"user"＋added_by 稽核欄位），
    再落簽章換 leader 記錄；registry 寫入失敗 → 5xx 且**不**記錄換 leader；
(4) already_listed 的位址走既有精選流程，**不寫** registry；
(5) 公開目錄 /api/leaders 不含 user-sourced 條目（僅本人可用的可見性）。

簽名用真密碼學（eth_account 本地運算，不觸網，沿 test_api_leader_select）。
"""
import json
import socket
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.leader_change import build_leader_change_message, load_leader_changes
from spark.filet.user_leaders import load_user_leaders
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（沿 test_api_leaders）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_CURATED = "0x" + "a1" * 20    # 精選、正常營運
_DISABLED = "0x" + "c3" * 20   # 精選、安全撤銷（enabled=false）
_CUSTOM = "0x" + "e5" * 20     # 清單外的自訂位址

_LEADERS = [{"address": _CURATED, "name": "Alpha"},
            {"address": _DISABLED, "name": "Charlie", "enabled": False}]

_ACTIVE_STATE = {"marginSummary": {"accountValue": "12345.6"},
                 "assetPositions": [{"position": {"coin": "ETH", "szi": "1.5"},
                                     "type": "oneWay"}]}


def _make(tmp_path, entries=None):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    return TestClient(app, base_url="https://testserver"), cfg, store, hl


def _login(client, wallet):
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return r.json()["account_id"]


def _now_iso() -> str:
    import time
    return datetime.fromtimestamp(time.time(), timezone.utc).isoformat()


def _payload(*, account_id, leader, nonce, wallet, issued_at=None):
    issued_at = issued_at or _now_iso()
    msg = build_leader_change_message(account_id=account_id, leader_address=leader,
                                      nonce=nonce, issued_at=issued_at)
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    return {"account_id": account_id, "leader_address": leader, "nonce": nonce,
            "issued_at": issued_at, "signature": sig, "message": msg}


# ---------- 訊息端點：可准入的自訂位址拿得到待簽原文 ----------

def test_message_is_issued_for_an_admissible_custom_address(tmp_path):
    """⭐ 非精選但通過准入（格式合法、非自己、鏈上活躍）→ 200，回正規化位址的
    待簽原文＋nonce（取代原本 is_selectable 的一律拒絕）。"""
    c, cfg, store, hl = _make(tmp_path)
    _login(c, Account.create())
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = c.get("/api/leaders/select/message",
              params={"leader_address": "0x" + "E5" * 20})   # 大小寫變體 → 正規化
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["leader_address"] == _CUSTOM
    assert _CUSTOM in body["message"] and body["nonce"]


# ---------- POST select：驗簽通過 → 冪等寫 registry → 落簽章記錄 ----------

def _select_custom(c, hl, wallet, acct, *, leader=_CUSTOM, chain_state="active"):
    """走完整流程：message 端點取 nonce → 本人簽名 → POST select。"""
    if chain_state == "active":
        hl.clearinghouse[leader.lower()] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": leader})
    assert r0.status_code == 200, r0.text
    body = _payload(account_id=acct, leader=leader, nonce=r0.json()["nonce"],
                    wallet=wallet)
    return c.post("/api/leaders/select", json=body)


def test_custom_select_writes_registry_then_records_the_change(tmp_path):
    """⭐ 自訂 leader happy path：驗簽通過 → 寫入 user registry（source:"user"＋
    added_by=加入者 account_id）→ 落簽章換 leader 記錄。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct)
    assert r.status_code == 200, r.text
    assert r.json()["leader_address"] == _CUSTOM

    (ref,) = load_user_leaders(cfg.user_leaders_path)
    assert ref.address == _CUSTOM and ref.enabled is True
    raw = json.loads(Path(cfg.user_leaders_path).read_text())["leaders"][0]
    assert raw["source"] == "user" and raw["added_by"] == acct

    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _CUSTOM


# ---------- ⭐ POST 獨立重跑准入（不信任 preview／message，防 TOCTOU） ----------

def test_post_reruns_admission_account_emptied_after_message(tmp_path):
    """⭐ 訊息端點通過後、提交前帳戶被清空 → POST 重跑准入擋下（404 not_found），
    registry 與換 leader 記錄**都不落地**——即使簽章完全合法。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r0.status_code == 200
    del hl.clearinghouse[_CUSTOM]                    # 帳戶清空（TOCTOU 窗口）
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"],
                    wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "not_found"
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_post_reruns_admission_operator_disables_after_message(tmp_path):
    """⭐ 訊息端點通過後 operator 把該位址列入精選檔並停用 → POST 擋下
    （leader_disabled）——kill-switch 在提交那一刻仍然有效。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r0.status_code == 200
    (tmp_path / "leaders.json").write_text(json.dumps(
        {"leaders": _LEADERS + [{"address": _CUSTOM, "name": "Evil",
                                 "enabled": False}]}))
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"],
                    wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_post_rejects_self_follow_without_needing_preview(tmp_path):
    """⭐ 完全跳過 preview／message、直接 POST 自己的登入位址 → 400 self_follow，
    無任何落地——證明 POST 的准入是獨立的，不依賴客戶端先走過任何檢查。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[w.address.lower()] = _ACTIVE_STATE
    body = _payload(account_id=acct, leader=w.address.lower(), nonce="deadbeef",
                    wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "self_follow"
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_admission_failure_does_not_burn_the_nonce(tmp_path):
    """准入失敗發生在 nonce 消耗之前 → 同一顆 nonce 在位址恢復活躍後仍可完成
    一次合法變更（否則一次鏈上打嗝就作廢客戶手上的授權，自我 DoS）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    nonce = r0.json()["nonce"]
    del hl.clearinghouse[_CUSTOM]
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=nonce, wallet=w)
    assert c.post("/api/leaders/select", json=body).status_code == 404
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE        # 鏈上恢復
    assert c.post("/api/leaders/select", json=body).status_code == 200


def test_bad_signature_leaves_registry_untouched(tmp_path):
    """驗簽失敗（另一把私鑰簽的）→ 400，registry 與記錄都不落地：
    寫入只發生在**全部**驗證通過之後（准入通過 ≠ 授權成立）。"""
    c, cfg, store, hl = _make(tmp_path)
    w, attacker = Account.create(), Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"],
                    wallet=attacker)                 # 簽章來自另一把鑰匙
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


# ---------- registry 寫入：冪等 ＋ fail loudly ----------

def test_selecting_the_same_custom_leader_twice_writes_one_entry(tmp_path):
    """⭐ 冪等：同一自訂 leader 選兩次（兩顆 nonce、兩次簽名）→ registry 恰一筆，
    第二次照樣 200（重送安全）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct).status_code == 200
    assert _select_custom(c, hl, w, acct).status_code == 200
    assert len(load_user_leaders(cfg.user_leaders_path)) == 1


def test_registry_write_failure_is_5xx_and_no_change_recorded(tmp_path):
    """⭐ registry 寫入失敗（落點被目錄佔住 → OSError）→ 5xx 且**不**記錄換
    leader（fail loudly，工程原則 3）：leader 進不了引擎的驗證來源卻記了換手，
    引擎會永遠拒絕套用一筆客戶已簽章的意圖。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    Path(cfg.user_leaders_path).mkdir()              # 讓寫入必然失敗
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct)
    assert r.status_code == 500
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_already_listed_address_does_not_touch_the_registry(tmp_path):
    """已在精選清單且可選的位址 → 走既有精選流程，**不寫** registry
    （同一位址不出現兩種身分）。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct, leader=_CURATED, chain_state="none")
    assert r.status_code == 200, r.text
    assert not Path(cfg.user_leaders_path).exists()
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _CURATED


# ---------- 可見性與兩端接線 ----------

def test_public_directory_excludes_user_sourced_leaders(tmp_path):
    """⭐ /api/leaders 公開目錄**不含** user-sourced 條目：策展門面不被任意位址
    稀釋、平台不為未審核 leader 背書（「僅本人可用」由 API 層可見性實現）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct).status_code == 200   # registry 已有 _CUSTOM
    assert len(load_user_leaders(cfg.user_leaders_path)) == 1  # 前提確認：真的寫進去了
    r = c.get("/api/leaders")
    assert r.status_code == 200
    assert [x["address"] for x in r.json()["leaders"]] == [_CURATED]


def test_config_registry_path_is_the_engine_visible_sibling(tmp_path):
    """cfg.user_leaders_path＝精選白名單同目錄的 user_leaders.json（獨立事實的
    字面推導）——引擎端由同一個 FILET_LEADERS_PATH 推導出同一個檔。"""
    cfg = make_cfg(tmp_path)
    assert cfg.user_leaders_path == str(tmp_path / "user_leaders.json")


def test_engine_resolves_the_leader_the_api_admitted(tmp_path):
    """⭐ end-to-end：API 准入寫入 registry 的自訂 leader，引擎 resolve_leader
    拿**同一個** leaders_path 推導 registry 後放行——寫端與讀端真的接上，
    合法選定的自訂 leader 不會在引擎層被拒（spec user story 18）。"""
    from spark.filet.leader_resolve import resolve_leader
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct).status_code == 200
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": [
        {"account_id": acct, "user_address": w.address,
         "builder_address": "0x" + "22" * 20, "network": "mainnet",
         "label": "", "leader_address": _CUSTOM}]}))
    res = resolve_leader(account_id=acct, manifest_path=manifest,
                         leaders_path=cfg.leaders_path,
                         env_default="", self_address=w.address)
    assert res.address == _CUSTOM
