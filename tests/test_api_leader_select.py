"""tests/test_api_leader_select.py — POST /api/leaders/select（客戶簽章換 leader）。

盯住四件事：
(1) 客戶只能改**自己**的帳號（403）；
(2) leader 必須是**目錄可選**的——`enabled` 與 `accepting_new` 兩個旗標各自要擋；
(3) 驗簽失敗 → 4xx 且**記錄不落地**（被拒絕的請求不得偽裝成待套用的意圖）；
(4) 回應必須明講後果（有實際交易成本）與生效時機（下一個 cycle），不讓前端猜；
(5)（2026-09-02，T10）簽章記錄落地後補寫 pending：READY 且未啟用才寫、已在
    manifest 或非 READY 都不寫、manifest 檔不存在視為未啟用而不是 503。

簽名用真密碼學（eth_account 本地運算，不觸網，沿 publicapi_helpers.login）。
"""
import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.copytrade.notifier import RecordingNotifier
from spark.filet.leader_change import (LEADER_CHANGE_MAX_AGE_S,
                                       build_leader_change_message,
                                       load_leader_changes)
from spark.publicapi.pending import load_pending
from tests.publicapi_helpers import BUILDER, make_app, make_cfg

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


def _fresh_nonce(client, wallet, *, leader=_A):
    """取一個**換 leader 域**的一次性 nonce（`/api/leaders/select/message`）。

    ⭐ 刻意不用 `/api/auth/nonce`（SIWE 登入域，chain_id > 0）：兩者共用同一張表，
    但 chain_id 是域分隔符——select 端點只收 chain_id == 0 的 nonce（opus 審查
    Minor 3）。原本這個 helper 拿登入 nonce 來換 leader，等於測試自己在示範那個
    被修掉的跨域挪用；見 test_login_nonce_cannot_be_used_to_change_leader。
    """
    r = client.get("/api/leaders/select/message",
                   params={"leader_address": leader})
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
    nonce = _fresh_nonce(client, wallet, leader=_A)
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
                         "effective_note", "consequences", "pending_written"}


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
    """被拒絕的請求不得動到已落地的記錄（拒絕 ≠ 清空意圖）。

    ⚠️ 2026-07-27 起清單外＋鏈上無活動的位址改為**放行**（不再是拒絕來源），故這裡
    用 _C（enabled=false，安全撤銷）當被拒的第二筆變更——leader_disabled 仍拒絕。
    """
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    assert _select(c, store, cfg, w, leader=_A)[0].status_code == 200
    assert _select(c, store, cfg, w, leader=_C)[0].status_code == 400
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

def test_leader_not_accepting_new_is_admitted_via_custom_path(tmp_path):
    """⭐ accepting_new=false（例行下架，enabled=true）→ 2026-07-27 使用者裁決：
    走自訂准入路徑**放行**（客戶堅持要跟就放行），落簽章換 leader 記錄。

    例行下架只是暫不收新客——引擎照跟正在跟的人（is_still_permitted 只看 enabled）。
    與 enabled=false 的安全撤銷（test_disabled_leader_is_rejected，硬擋）分兩支。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), leader=_B)
    assert r.status_code == 200, r.text
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _B


def test_disabled_leader_is_rejected(tmp_path):
    """⭐ enabled=false（安全撤銷：這個 leader 出事了）→ 不可選。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), leader=_C)
    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_leader_not_in_allowlist_and_dead_on_chain_is_admitted(tmp_path):
    """清單外位址自 2026-07-27 起改走自訂准入，且鏈上無活動**不再擋下**（裁決）：
    _UNLISTED 鏈上無 perp 活動（FakeHL 預設空帳戶）→ 仍放行 200，換 leader 記錄落地。
    leader 尚未進場時客戶可先完成配置，進場後引擎自動開始跟。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create(), leader=_UNLISTED)
    assert r.status_code == 200, r.text
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _UNLISTED


def test_disabled_is_rejected_but_paused_is_admitted(tmp_path):
    """⭐ 2026-07-27 拆旗標後的新契約：撤銷（enabled=false）與例行下架
    （accepting_new=false）**兩支不同**——早期兩者共用 leader_disabled、刻意不可
    分辨，現在依使用者裁決分流：

    - _C（enabled=false，安全撤銷）→ 400 leader_disabled，換 leader 記錄不落地；
    - _B（accepting_new=false，例行下架）→ 200 放行，換 leader 記錄落地。

    「哪個 leader 出事了」仍不外流——但「出事（撤銷）」與「暫不收新客（下架）」對
    客戶的**後果**本就不同（一個擋死、一個放行），這個差異是設計本身，不是洩密。"""
    c, cfg, store = _make(tmp_path)

    # _C 安全撤銷 → 硬擋
    rc, _ = _select(c, store, cfg, Account.create(), leader=_C)
    assert rc.status_code == 400
    assert rc.json()["detail"]["reason"] == "leader_disabled"

    # _B 例行下架 → 放行並落記錄
    rb, _ = _select(c, store, cfg, Account.create(), leader=_B)
    assert rb.status_code == 200, rb.text
    assert [e["leader_address"] for e in load_leader_changes(cfg.leader_changes_path)] \
        == [_B]


def test_broken_allowlist_returns_503_not_a_silent_accept(tmp_path):
    """白名單壞掉 → 503（transient，可重試）。**不得**當成空清單放行或拒絕：
    兩邊都會把一個手滑的編輯偽裝成正常狀態（沿 load_leaders 的 fail-fast）。"""
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, *_ = make_app(tmp_path, cfg=cfg)
    c = TestClient(app, base_url="https://testserver")
    # 先在白名單健康時取得 session 與 nonce（nonce 出自 select/message，該端點同樣
    # 會讀白名單），再把白名單弄壞——盯的是 **select 端點**對壞白名單的反應。
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    p.write_text("{ not json")

    r = c.post("/api/leaders/select",
               json=_payload(account_id=acct, leader=_A, nonce=nonce, wallet=w))
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


# ---------- 補寫 pending（T10，2026-09-02）----------
# 客戶簽完 agent／builder fee、入金到位後若重新整理／換頁，onboarding 精靈會直接
# 跳過唯一會寫 pending.json 的 `POST /api/onboard/verify`（只掛在 step 2 按鈕上）。
# 本端點是精靈必經的最後一步（選 leader），簽章記錄落地後補上同一道寫入，
# 客戶才不會走完精靈卻永遠不被 watcher 啟用。

def _app_with_hl(tmp_path, *, entries=None, followers_path=None, notifier=None):
    """同 `_make`，但額外把 `hl`（FakeHL）與 `keysvc` 交回，供本節設定鏈上狀態。
    `notifier` 可注入 `RecordingNotifier`，供斷言 W1 的 critical 告警。"""
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    over = {"leaders_path": str(p)}
    if followers_path is not None:
        over["followers_path"] = followers_path
    cfg = make_cfg(tmp_path, **over)
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, notifier=notifier)
    return TestClient(app, base_url="https://testserver"), cfg, store, keysvc, hl


def _make_ready(hl, wallet_addr: str, agent: str):
    """把 `wallet_addr` 佈成 `_progress` 眼中的 READY（沿 test_api_onboard.py
    同名 helper 的既有數值：150 USDC 超過門檻、100 為非零 builder fee）。"""
    hl.max_fees[(wallet_addr.lower(), BUILDER.lower())] = 100
    hl.agents[wallet_addr.lower()] = [agent]
    hl.account_values[wallet_addr.lower()] = Decimal("150")


def test_ready_and_not_activated_writes_pending(tmp_path):
    """⭐ 核心修法：READY 且尚未在 followers manifest（首次啟用，manifest 檔
    甚至不存在）→ 簽章記錄落地後補寫 pending.json，且回應誠實回報
    `pending_written: true`。"""
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    agent = c.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, w.address, agent)

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text   # manifest 檔不存在 → 不得變成 503
    assert r.json()["pending_written"] is True
    assert "pending_error" not in r.json()
    entries = load_pending(cfg.pending_path)
    assert len(entries) == 1
    assert entries[0]["account_id"] == acct
    assert entries[0]["user_address"] == w.address.lower()
    assert entries[0]["agent_address"] == agent.lower()
    assert entries[0]["builder_address"] == cfg.builder_address
    assert entries[0]["network"] == cfg.network


def test_ready_but_already_in_manifest_does_not_write_pending(tmp_path):
    """⭐ 已在 followers manifest（已啟用過、這次只是換 leader）→ 絕不補寫
    pending：寫了會讓 watcher 把老客戶當新客戶再走一次啟用路徑。"""
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": [
        {"account_id": "placeholder", "user_address": "0x" + "11" * 20,
         "builder_address": BUILDER, "network": "testnet"},
    ]}))
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path, followers_path=str(manifest))
    w = Account.create()
    acct = _login(c, store, cfg, w)
    # 帳號真正的 account_id 才是 manifest 要匹配的那筆——把佔位條目換成本人。
    manifest.write_text(json.dumps({"followers": [
        {"account_id": acct, "user_address": w.address.lower(),
         "builder_address": BUILDER, "network": "testnet"},
    ]}))
    agent = c.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, w.address, agent)

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["pending_written"] is False
    assert load_pending(cfg.pending_path) == []
    # 換 leader 記錄仍然落地——本測試盯的只是 pending，不是主要動作。
    assert len(load_leader_changes(cfg.leader_changes_path)) == 1


def test_not_ready_does_not_write_pending(tmp_path):
    """⭐ 非 READY（例如還沒入金）→ 不寫 pending、不報錯——客戶可能只是先選好
    leader，稍後才入金；`POST /api/onboard/verify` 之後仍會補上。"""
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    c.post("/api/onboard/agent")
    # 刻意不呼叫 _make_ready：builder fee／agent 授權／入金皆未完成 → IN_PROGRESS

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["pending_written"] is False
    assert load_pending(cfg.pending_path) == []


def test_pending_write_idempotent_across_repeated_leader_changes(tmp_path):
    """⭐ 冪等：同一帳號第二次換 leader（仍未被人工啟用）不得在 pending.json
    產生第二筆條目（`write_pending_entry` 本身冪等，這裡驗證呼叫端沒有繞過它）。"""
    c, cfg, store, keysvc, hl = _app_with_hl(
        tmp_path, entries=_LEADERS + [{"address": _D, "name": "Delta"}])
    w = Account.create()
    acct = _login(c, store, cfg, w)
    agent = c.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, w.address, agent)

    for leader in (_A, _D):
        nonce = _fresh_nonce(c, w, leader=leader)
        body = _payload(account_id=acct, leader=leader, nonce=nonce, wallet=w)
        r = c.post("/api/leaders/select", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["pending_written"] is True

    assert len(load_pending(cfg.pending_path)) == 1


def test_hl_read_failure_does_not_fail_the_already_landed_signature(tmp_path):
    """⭐ 補寫 pending 時 HL 讀取失敗（`_progress` 的三次查詢之一）不得讓整個
    端點回 500——簽章記錄已經驗證通過並落地，不能因為這個錦上添花的副作用
    把成功的回應變成失敗（工程原則 2/3：safety-critical 的主動作已成功，
    附加動作的 transient 失敗只需大聲留痕，不得拖垮主動作）。

    （2026-09-02，opus 審查 W1）大聲留痕不只是 `logger.error`——必須發告警，
    否則「客戶永遠卡在等待啟用」這件事沒有人看得見。"""
    notifier = RecordingNotifier()
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path, notifier=notifier)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    c.post("/api/onboard/agent")

    def _boom(*a, **kw):
        raise ConnectionError("HL unreachable")
    hl.get_account_value = _boom

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["pending_written"] is False
    assert "pending_error" in r.json()
    assert len(load_leader_changes(cfg.leader_changes_path)) == 1  # 主動作不受影響
    assert load_pending(cfg.pending_path) == []
    critical = [rec for rec in notifier.records if rec[0] == "critical"]
    assert len(critical) == 1
    assert acct in critical[0][2]


def test_disk_write_failure_does_not_fail_the_already_landed_signature_and_alerts(
        tmp_path, monkeypatch):
    """⭐（2026-09-02，opus 審查 W1／W4）磁碟寫入失敗（`write_pending_entry` 拋
    `OSError`）→ 端點仍 200、`pending_written=false`、`pending_error` 存在，
    且 notifier 收到 critical 告警——不是只留一條 log。"""
    notifier = RecordingNotifier()
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path, notifier=notifier)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    agent = c.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, w.address, agent)

    def _boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr("spark.publicapi.app.write_pending_entry", _boom)

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["pending_written"] is False
    assert "pending_error" in r.json()
    assert len(load_leader_changes(cfg.leader_changes_path)) == 1  # 主動作不受影響
    critical = [rec for rec in notifier.records if rec[0] == "critical"]
    assert len(critical) == 1
    assert acct in critical[0][2]


def test_corrupt_pending_json_does_not_fail_the_already_landed_signature_and_alerts(
        tmp_path):
    """⭐（2026-09-02，opus 審查 W4）pending.json 本身壞掉（非合法 JSON）→
    `write_pending_entry` 內部 `load_pending` 會拋 `json.JSONDecodeError`
    （`ValueError` 子類），原本只接 `OSError` 會讓它以 500 洩漏出端點——簽章
    記錄與 nonce 都已消耗，500 會讓客戶卡死。端點仍須 200 並發告警。"""
    notifier = RecordingNotifier()
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path, notifier=notifier)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    agent = c.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, w.address, agent)

    from pathlib import Path
    Path(cfg.pending_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.pending_path).write_text("{not valid json")

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["pending_written"] is False
    assert "pending_error" in r.json()
    assert len(load_leader_changes(cfg.leader_changes_path)) == 1  # 主動作不受影響
    critical = [rec for rec in notifier.records if rec[0] == "critical"]
    assert len(critical) == 1
    assert acct in critical[0][2]


def test_manifest_with_broken_entry_skips_pending_as_indeterminate(tmp_path):
    """⭐（2026-09-02，opus 審查 W3）manifest 有無法解析的條目（非「查無此帳號」）
    → 不確定 ≠ 不在，寧可不寫也不猜；`pending_written=false`、
    `pending_error="manifest_degraded"`。"""
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": [
        {"account_id": "broken", "user_address": "0x" + "11" * 20,
         "builder_address": BUILDER, "network": "not-a-real-network"},
    ]}))
    c, cfg, store, keysvc, hl = _app_with_hl(tmp_path, followers_path=str(manifest))
    w = Account.create()
    acct = _login(c, store, cfg, w)
    agent = c.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, w.address, agent)

    nonce = _fresh_nonce(c, w, leader=_A)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["pending_written"] is False
    assert r.json()["pending_error"] == "manifest_degraded"
    assert load_pending(cfg.pending_path) == []
    assert len(load_leader_changes(cfg.leader_changes_path)) == 1  # 主動作不受影響


def test_leader_changes_path_lives_in_the_dedicated_exchange_dir(tmp_path):
    """⭐⭐ 落點是**專屬交換目錄**，與 API 私有的 pending.json 分屬不同目錄。

    2026-07-19 opus 審查 C3：原本錨在 pending.json 上。那逼共享產物（API 寫、引擎讀）
    與私有產物（活化前客戶資料，只有 API 該讀得到）同住一個目錄，而該目錄的權限
    只能滿足其中一方——實際部署的結果是兩個進程讀寫**不同的檔案**，功能完全不通。
    專屬目錄讓兩件事各自拿到自己需要的權限（交換目錄 filet-api:filet-engine 0750）。
    """
    from pathlib import Path
    cfg = make_cfg(tmp_path)
    assert Path(cfg.leader_changes_path).name == "leader_changes.json"
    assert Path(cfg.leader_changes_path).parent == Path(cfg.exchange_dir)
    assert Path(cfg.leader_changes_path).parent != Path(cfg.pending_path).parent


# ── ⭐ 域分隔與回應衛生（opus 審查 Minor 2／3）─────────────────────────

def test_login_nonce_cannot_be_used_to_change_leader(tmp_path):
    """⭐ SIWE **登入** nonce（chain_id > 0）不得被挪來換 leader。

    兩種 nonce 共用同一張表（刻意——兩套一次性機具意味著兩套過期與兩套消耗語意，
    其中一套遲早漏掉原子性），所以 `chain_id` 就是域分隔符：換 leader 一律發 0，
    而登入端點拒收 chain_id <= 0。反方向（拿換 leader 的 nonce 去登入）本來就擋得住
    ——auth_verify 拿 chain_id=0 重建 SIWE 訊息必然 recover 不符。但**只擋一邊的
    分隔符不是分隔符**：這個方向原本沒有檢查，於是一顆登入 nonce 就能完成一次換手。
    """
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    # 登入域的 nonce（chain_id=42161），其餘欄位全部由本人正確簽署
    r0 = c.get("/api/auth/nonce",
               params={"address": w.address, "chain_id": 42161})
    login_nonce = r0.json()["nonce"]

    r = c.post("/api/leaders/select",
               json=_payload(account_id=acct, leader=_A, nonce=login_nonce, wallet=w))

    assert r.status_code == 400
    assert load_leader_changes(cfg.leader_changes_path) == []   # 記錄不得落地


def test_leader_change_nonce_still_works_after_the_domain_check(tmp_path):
    """反證：同一套流程換成**本域**的 nonce 就會成功——證明上面那條擋的是域，
    不是把整條路徑擋死了。"""
    c, cfg, store = _make(tmp_path)
    r, _ = _select(c, store, cfg, Account.create())
    assert r.status_code == 200, r.text


def test_error_detail_never_echoes_client_input(tmp_path):
    """⭐ 400 的 detail 不得回顯客戶送來的 nonce／issued_at／signature 原值。

    例外訊息為了除錯內嵌了請求原值（例如「nonce 格式不合法: '...'」），直接
    `detail=str(e)` 會讓本端點自陳的「不記 signature／message 原文」政策在 HTTP
    回應這一側破功。分類碼由伺服器決定，內容不含任何請求輸入。
    """
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w)
    marker = "PROBE-9f3a-injected"
    body["issued_at"] = marker                    # 壞格式 → reason=malformed
    body["signature"] = "0x" + "ab" * 65

    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert marker not in detail
    assert body["signature"] not in detail
    assert nonce not in detail
    assert detail in _all_leader_change_details()


def _all_leader_change_details():
    """端點允許回出去的**全部**字串（分類化訊息表）——白名單式斷言：任何新增的
    回應字串都必須是這張表裡的固定值，不能是由請求內容拼出來的。

    直接引用 app 模組的那張表（單一來源）：測試自己抄一份字串就會與實作漂移，
    而漂移的那一天沒有人會發現（工程原則 1）。
    """
    from spark.publicapi.app import LEADER_CHANGE_DETAIL, LEADER_CHANGE_DETAIL_DEFAULT
    return set(LEADER_CHANGE_DETAIL.values()) | {LEADER_CHANGE_DETAIL_DEFAULT}


def test_expired_signature_detail_is_classified_not_raw(tmp_path):
    """過期路徑同樣走分類表（原本 str(e) 會把秒數與請求時間戳一起回出去）。"""
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, store, cfg, w)
    nonce = _fresh_nonce(c, w)
    body = _payload(account_id=acct, leader=_A, nonce=nonce, wallet=w,
                    issued_at=_now_iso(-LEADER_CHANGE_MAX_AGE_S - 60))
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"] in _all_leader_change_details()
