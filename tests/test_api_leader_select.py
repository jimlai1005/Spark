"""tests/test_api_leader_select.py — POST /api/leaders/select（客戶簽章換 leader）。

盯住四件事：
(1) 客戶只能改**自己**的帳號（403）；
(2) leader 必須是**目錄可選**的——`enabled` 與 `accepting_new` 兩個旗標各自要擋；
(3) 驗簽失敗 → 4xx 且**記錄不落地**（被拒絕的請求不得偽裝成待套用的意圖）；
(4) 回應必須明講後果（有實際交易成本）與生效時機（下一個 cycle），不讓前端猜。

簽名用真密碼學（eth_account 本地運算，不觸網，沿 publicapi_helpers.login）。
"""
import json
import socket
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.leader_change import (LEADER_CHANGE_MAX_AGE_S,
                                       build_leader_change_message,
                                       load_leader_changes)
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（沿 test_api_leaders）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_A = "0x" + "a1" * 20   # 正常營運
_B = "0x" + "b2" * 20   # 例行下架（accepting_new=false）
_C = "0x" + "c3" * 20   # 安全撤銷（enabled=false）
_UNLISTED = "0x" + "e5" * 20

_LEADERS = [{"address": _A, "name": "Alpha"},
            {"address": _B, "name": "Bravo", "accepting_new": False},
            {"address": _C, "name": "Charlie", "enabled": False}]


def _make(tmp_path, entries=None):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, *_ = make_app(tmp_path, cfg=cfg)
    return TestClient(app, base_url="https://testserver"), cfg, store


def _login(client, store, cfg, wallet):
    """SIWE 登入（真密碼學），回同一個 wallet。"""
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return r.json()["account_id"]


def _fresh_nonce(client, wallet):
    """取一個一次性 nonce（沿用 SIWE 的同一張表——本端點刻意不另開 nonce 機具）。"""
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    assert r.status_code == 200, r.text
    return r.json()["nonce"]


def _now_iso(offset_s: float = 0.0) -> str:
    import time
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).isoformat()


def _payload(*, account_id, leader, nonce, wallet, issued_at=None,
             signer_wallet=None):
    issued_at = issued_at or _now_iso()
    msg = build_leader_change_message(account_id=account_id, leader_address=leader,
                                      nonce=nonce, issued_at=issued_at)
    sig = (signer_wallet or wallet).sign_message(
        encode_defunct(text=msg)).signature.hex()
    return {"account_id": account_id, "leader_address": leader, "nonce": nonce,
            "issued_at": issued_at, "signature": sig, "message": msg}


def _select(client, store, cfg, wallet, *, leader=_A, **over):
    acct = _login(client, store, cfg, wallet)
    nonce = _fresh_nonce(client, wallet)
    body = _payload(account_id=acct, leader=leader, nonce=nonce, wallet=wallet,
                    **over)
    return client.post("/api/leaders/select", json=body), acct


# ---------- 正常流程 ----------

def test_happy_path_records_the_change(tmp_path):
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    r, acct = _select(c, store, cfg, w)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["account_id"] == acct
    assert body["leader_address"] == _A

    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["account_id"] == acct and e["leader_address"] == _A
    assert e["signature"] and e["nonce"]
    assert "signer" not in e          # 身分宣稱欄位不存在（見 leader_change.py）


def test_response_states_consequences_and_effective_timing(tmp_path):
    """⭐ 回應要明確告知後果與生效時機——`effective` 是機器可讀的語意欄位，
    前端不該從散文裡猜「是不是已經生效了」。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create())
    body = r.json()
    assert body["effective"] == "next_engine_cycle"
    assert "cycle" in body["effective_note"]
    # 客戶必須被告知這會產生實際成交與成本，不是改一個設定值
    assert "平掉" in body["consequences"] and "成本" in body["consequences"]
    assert set(body) == {"ok", "account_id", "leader_address", "effective",
                         "effective_note", "consequences"}


def test_record_is_engine_verifiable_after_landing(tmp_path):
    """⭐ 落地的記錄必須能被**引擎側**用同一支原語重新驗章通過（本端點的整個意義：
    API 只是搬運，真正的閘門在引擎那一次獨立驗證）。"""
    from spark.filet.leader_change import verify_leader_change
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    r, acct = _select(c, store, cfg, w)
    assert r.status_code == 200, r.text
    rec = load_leader_changes(cfg.leader_changes_path)[0]
    import time
    out = verify_leader_change(rec, account_id=acct, user_address=w.address,
                               now_s=time.time(), consume_nonce=lambda n: True)
    assert out.leader_address == _A


_D = "0x" + "d4" * 20   # 第二個正常營運的 leader


def test_second_change_replaces_the_first(tmp_path):
    """同帳號再換一次 → 覆蓋而非附加（套用端不該有機會挑到一筆舊意圖，
    挑錯的代價是把客戶送回他剛剛離開的 leader，且要付一次真實的平倉開倉）。"""
    c, cfg, store = _make(tmp_path, entries=_LEADERS + [{"address": _D,
                                                         "name": "Delta"}])
    w = Account.create()
    assert _select(c, store, cfg, w, leader=_A)[0].status_code == 200
    assert _select(c, store, cfg, w, leader=_D)[0].status_code == 200
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _D


def test_rejected_second_change_leaves_the_first_intact(tmp_path):
    """被拒絕的請求不得動到已落地的記錄（拒絕 ≠ 清空意圖）。"""
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    assert _select(c, store, cfg, w, leader=_A)[0].status_code == 200
    assert _select(c, store, cfg, w, leader=_UNLISTED)[0].status_code == 400
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _A


# ---------- 授權 ----------

def test_requires_session(tmp_path):
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    body = _payload(account_id="f" + w.address[2:].lower(), leader=_A,
                    nonce="deadbeef", wallet=w)
    assert c.post("/api/leaders/select", json=body).status_code == 401


def test_changing_someone_elses_account_is_forbidden(tmp_path):
    """⭐ 客戶只能改自己的帳號。這裡的攻擊者**完整簽署**了一筆對受害者帳號的合法
    請求（他自己的私鑰、正確的訊息版型），只是那不是他的帳號 → 403。"""
    c, cfg, store = _make(tmp_path)
    attacker, victim = Account.create(), Account.create()
    _login(c, store, cfg, attacker)
    nonce = _fresh_nonce(c, attacker)
    victim_acct = "f" + victim.address[2:].lower()
    body = _payload(account_id=victim_acct, leader=_A, nonce=nonce,
                    wallet=attacker)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 403
    assert load_leader_changes(cfg.leader_changes_path) == []


# ---------- leader 目錄閘門（兩個旗標各自要擋） ----------

def test_leader_not_accepting_new_is_rejected(tmp_path):
    """⭐ accepting_new=false（例行下架）→ 不可選。

    ⚠️ 這條正是 `is_selectable` 與 `is_still_permitted` 的分水嶺：後者只看 enabled，
    用錯述詞的實作會在這裡放行，客戶被送給一個已經停止接客的 leader。
    """
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), leader=_B)
    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_disabled_leader_is_rejected(tmp_path):
    """⭐ enabled=false（安全撤銷：這個 leader 出事了）→ 不可選。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), leader=_C)
    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_leader_not_in_allowlist_is_rejected(tmp_path):
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), leader=_UNLISTED)
    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_rejection_does_not_leak_governance_state(tmp_path):
    """撤銷／下架／不在名單三種拒絕理由不得分辨——那是內部治理資訊
    （沿 /api/leaders 不外流 enabled/accepting_new 的既有理由）。"""
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    details = set()
    for leader in (_B, _C, _UNLISTED):
        acct = _login(c, store, cfg, w)
        nonce = _fresh_nonce(c, w)
        r = c.post("/api/leaders/select",
                   json=_payload(account_id=acct, leader=leader, nonce=nonce,
                                 wallet=w))
        details.add(r.json()["detail"])
    assert len(details) == 1


def test_broken_allowlist_returns_503_not_a_silent_accept(tmp_path):
    """白名單壞掉 → 503（transient，可重試）。**不得**當成空清單放行或拒絕：
    兩邊都會把一個手滑的編輯偽裝成正常狀態（沿 load_leaders 的 fail-fast）。"""
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, *_ = make_app(tmp_path, cfg=cfg)
    c = TestClient(app, base_url="https://testserver")
    r, _ = _select(c, store, cfg, Account.create())
    assert r.status_code == 503
    assert load_leader_changes(cfg.leader_changes_path) == []


# ---------- 驗簽（⭐ 核心）：失敗一律 4xx 且記錄不落地 ----------

def test_signature_from_another_wallet_is_rejected_and_not_recorded(tmp_path):
    """⭐ 登入的是客戶本人（session 合法），但簽章來自另一把私鑰 → 拒絕。

    這是「session 通過 ≠ 這筆變更是本人要求的」的分界：session 只證明他登入過，
    簽章才證明**這一筆意圖**是他的。
    """
    c, cfg, store = _make(tmp_path)
    victim, attacker = Account.create(), Account.create()
    r, _ = _select(c, store, cfg, victim, signer_wallet=attacker)
    assert 400 <= r.status_code < 500
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_tampered_leader_with_old_signature_is_rejected(tmp_path):
    """⭐ 重放：客戶簽了「換到 A」，送出前把 leader_address 改成別的（同樣在白名單
    裡、同樣可選）→ 驗簽必須擋下。白名單放行、只有簽章擋得住的正是這一步。"""
    c, cfg, store = _make(tmp_path, entries=[{"address": _A, "name": "Alpha"},
                                             {"address": _UNLISTED, "name": "Evil"}])
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    body["leader_address"] = _UNLISTED     # 竄改意圖，簽章原封不動
    r = c.post("/api/leaders/select", json=body)
    assert 400 <= r.status_code < 500
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_verification_failure_is_400_not_401(tmp_path):
    """⭐ 驗簽失敗回 400 而非 401：401 在本 app 的既有語意是「session 沒了」，
    前端據此把使用者踢回登入頁。簽章壞掉時 session 好端端的，回 401 會讓客戶
    被莫名登出，而真正的問題反而顯示不出來。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), signer_wallet=Account.create())
    assert r.status_code == 400


def test_expired_signature_is_rejected(tmp_path):
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(),
                   issued_at=_now_iso(-LEADER_CHANGE_MAX_AGE_S - 60))
    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_malformed_signature_is_rejected_without_500(tmp_path):
    """壞簽名不得逸出成未處理例外（500）——那是 semantic 失敗，必須是 4xx。"""
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    body["signature"] = "0xnot-a-signature"
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_nonce_is_single_use(tmp_path):
    """同一個 nonce 不能換兩次 leader（重放同一份請求）。"""
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    assert c.post("/api/leaders/select", json=body).status_code == 200
    assert c.post("/api/leaders/select", json=body).status_code == 400


def test_nonce_issued_to_another_address_is_rejected(tmp_path):
    """nonce 必須是發給本人的——否則 A 可以拿 B 的 nonce 去湊，雖偽造不出簽章，
    卻能無成本地作廢別人手上那張還沒用的授權。"""
    c, cfg, store = _make(tmp_path)
    w, other = Account.create(), Account.create()
    acct = _login(c, store, cfg, w)
    r = c.get("/api/auth/nonce",
              params={"address": other.address, "chain_id": 42161})
    foreign_nonce = r.json()["nonce"]
    body = _payload(account_id=acct, leader=_A, nonce=foreign_nonce, wallet=w)
    assert c.post("/api/leaders/select", json=body).status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_nonce_not_burned_when_signature_is_bad(tmp_path):
    """驗簽失敗不得燒掉 nonce：否則任何人送一筆垃圾請求就能作廢客戶剛取得的
    nonce（自我 DoS）。同一個 nonce 隨後仍應可用於一次合法變更。"""
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    bad = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w,
                   signer_wallet=Account.create())
    assert c.post("/api/leaders/select", json=bad).status_code == 400
    good = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    assert c.post("/api/leaders/select", json=good).status_code == 200


# ---------- 不得繞過引擎：本端點不碰 manifest ----------

def test_endpoint_never_touches_the_followers_manifest(tmp_path):
    """⭐ API 只落記錄，**不改 manifest**：filet-api 對引擎 manifest 沒有寫權，
    而且直接改會繞過引擎套用前的二次驗章（那道驗證的價值正在於獨立於本進程）。"""
    c, cfg, store = _make(tmp_path)
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": []}))
    cfg_with_manifest = make_cfg(tmp_path, leaders_path=cfg.leaders_path,
                                 followers_path=str(manifest))
    app, cfg2, store2, *_ = make_app(tmp_path, cfg=cfg_with_manifest)
    c2 = TestClient(app, base_url="https://testserver")
    before = manifest.read_text()
    r, _ = _select(c2, store2, cfg2, Account.create())
    assert r.status_code == 200, r.text
    assert manifest.read_text() == before


def test_leader_changes_path_lives_beside_pending(tmp_path):
    """⭐ 落點沿 pending.json 的同一個目錄＝API 進程本來就擁有寫權的那個目錄；
    不另開 env（多一個旋鈕就多一個「API 寫 A、引擎讀 B」的靜默錯位）。"""
    from pathlib import Path
    cfg = make_cfg(tmp_path)
    assert Path(cfg.leader_changes_path).parent == Path(cfg.pending_path).parent
    assert Path(cfg.leader_changes_path).name == "leader_changes.json"
