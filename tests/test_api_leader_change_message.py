"""tests/test_api_leader_change_message.py
GET /api/leaders/select/message — 換 leader 的 **canonical 待簽原文**端點。

⭐ 本檔的核心不是「回傳的字串長得對不對」，而是**端到端**：伺服器給的原文原樣簽了
之後，POST /api/leaders/select 必須驗得過。斷言字串長相只能證明「這個端點跟我寫的
測試一致」；只有把原文餵回驗證端，才證明**產生原文的那一份程式碼**與**重建原文的
那一份程式碼**沒有漂移——而那正是這個端點存在的唯一理由（少一個換行就會表現成
「我本人簽的卻一直被拒」，兩邊看起來都正常，極難診斷）。

簽名用真密碼學（eth_account 本地運算，不觸網，沿 test_api_leader_select）。
"""
import json
import socket

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.leader_change import (build_leader_change_message,
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

_PATH = "/api/leaders/select/message"


def _make(tmp_path, entries=None):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, *_ = make_app(tmp_path, cfg=cfg)
    return TestClient(app, base_url="https://testserver"), cfg, store


def _login(client, wallet):
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return r.json()["account_id"]


def _ask(client, leader=_A):
    return client.get(_PATH, params={"leader_address": leader})


# ---------- ⭐⭐ 核心：伺服器給的原文，原樣簽了必須通得過 select ----------

def test_message_signed_verbatim_passes_select(tmp_path):
    """⭐ 端到端：拿原文 → 原樣簽 → 原樣送進 select → 200，且記錄落地。

    這條是本端點的整個存在理由。它擋的變異是「產生原文」與「重建原文」兩份程式碼
    漂移（版型改了只改一邊、位址大小寫基準不同、issued_at 格式不同）——任何一種
    漂移都會讓這裡從 200 掉成 400。
    """
    c, cfg, _ = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)

    r = _ask(c)
    assert r.status_code == 200, r.text
    body = r.json()

    # ⭐ 客戶端**只**做一件事：把 message 原樣丟進錢包簽名，其餘欄位原樣回填。
    sig = w.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r2 = c.post("/api/leaders/select",
                json={"account_id": body["account_id"],
                      "leader_address": body["leader_address"],
                      "nonce": body["nonce"], "issued_at": body["issued_at"],
                      "signature": sig, "message": body["message"]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["leader_address"] == _A

    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["account_id"] == acct


def test_issued_nonce_is_accepted_by_select(tmp_path):
    """本端點簽發的 nonce 必須落在 **select 端點消耗的同一個 nonce 空間**。

    另開一套 nonce 機具的症狀正是這裡：原文完美、簽章完美，卻永遠 400
    （select 的 store.consume_nonce 查不到那顆 nonce）。
    """
    c, cfg, store = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    body = _ask(c).json()
    # 直接以**同一顆 nonce** 自行組原文簽名（不用伺服器回的 message 字串），
    # 單獨證明 nonce 這一半是通的。
    msg = build_leader_change_message(account_id=acct, leader_address=_A,
                                      nonce=body["nonce"],
                                      issued_at=body["issued_at"])
    sig = w.sign_message(encode_defunct(text=msg)).signature.hex()
    r = c.post("/api/leaders/select",
               json={"account_id": acct, "leader_address": _A,
                     "nonce": body["nonce"], "issued_at": body["issued_at"],
                     "signature": sig, "message": msg})
    assert r.status_code == 200, r.text


def test_message_matches_the_canonical_builder(tmp_path):
    """回傳的原文＝`build_leader_change_message` 的輸出（單一版型，不是另一份拼接）。"""
    c, _, _ = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    body = _ask(c).json()
    assert body["message"] == build_leader_change_message(
        account_id=acct, leader_address=_A, nonce=body["nonce"],
        issued_at=body["issued_at"])
    assert body["account_id"] == acct
    assert set(body) == {"message", "nonce", "issued_at", "leader_address",
                         "account_id"}


def test_leader_address_is_normalised_lowercase(tmp_path):
    """位址大小寫在此收斂成單一基準——客戶端不必猜該用 checksum 還是小寫
    （猜錯的症狀就是「本人簽的卻被拒」，工程原則 1）。"""
    c, _, _ = _make(tmp_path)
    _login(c, Account.create())
    body = _ask(c, leader=_A.upper().replace("0X", "0x")).json()
    assert body["leader_address"] == _A
    assert f"Leader: {_A}" in body["message"]


# ---------- 授權 ----------

def test_requires_session(tmp_path):
    c, _, _ = _make(tmp_path)
    assert _ask(c).status_code == 401


# ---------- 閘門：不可選的 leader 連原文都不給 ----------

@pytest.mark.parametrize("leader", [_B, _C, _UNLISTED])
def test_unselectable_leader_gets_no_message(tmp_path, leader):
    """⭐ 不可選（例行下架／安全撤銷／不在白名單）→ 4xx，不發原文也不發 nonce。

    ⚠️ 述詞必須是 `is_selectable`（enabled **且** accepting_new），與 select 端點
    同一個。放寬成引擎的 `is_still_permitted` 會讓 accepting_new=false 的 leader
    在這裡拿到原文、簽完之後必定被 select 拒絕——白白浪費客戶一次錢包簽名。
    """
    c, _, _ = _make(tmp_path)
    _login(c, Account.create())
    r = _ask(c, leader=leader)
    assert 400 <= r.status_code < 500
    assert "message" not in r.json()


def test_rejection_does_not_distinguish_disabled_from_paused(tmp_path):
    """撤銷（enabled=false）與例行下架（accepting_new=false）不得分辨——哪個 leader
    「出事了」是內部治理資訊。

    ⚠️ 2026-07-27 自訂 leader spec 後的新契約：非精選位址改走准入檢查，拒絕帶
    **機器可判的 reason code**（user story 10 明訂要告知「已被平台停用」）。
    所以「已列但不可選」（leader_disabled）與「不在清單且鏈上無活動」（not_found）
    **可以**分辨；不可分辨的邊界收窄為 disabled vs paused 這一對。
    """
    c, _, _ = _make(tmp_path)
    _login(c, Account.create())
    rb, rc = _ask(c, leader=_B).json(), _ask(c, leader=_C).json()
    assert rb["detail"]["reason"] == rc["detail"]["reason"] == "leader_disabled"
    assert rb["detail"] == rc["detail"]          # 訊息文字也不得分辨
    r_unlisted = _ask(c, leader=_UNLISTED).json()
    assert r_unlisted["detail"]["reason"] == "not_found"


def test_malformed_leader_address_is_4xx_not_500(tmp_path):
    """壞位址是 semantic 失敗 → 4xx；不得逸出成未處理例外。"""
    c, _, _ = _make(tmp_path)
    _login(c, Account.create())
    for bad in ("", "0xshort", "not-an-address", "0x" + "z" * 40):
        r = _ask(c, leader=bad)
        assert 400 <= r.status_code < 500, (bad, r.status_code)


def test_broken_allowlist_returns_503(tmp_path):
    """白名單壞掉 → 503（transient），沿 select／目錄兩端點的同一種失敗方式。"""
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, *_ = make_app(tmp_path, cfg=cfg)
    c = TestClient(app, base_url="https://testserver")
    _login(c, Account.create())
    assert _ask(c).status_code == 503


# ---------- 只產生原文，不改狀態 ----------

def test_does_not_write_any_change_record(tmp_path):
    """⭐ 取原文**不是**變更：呼叫再多次也不得有任何記錄落地
    （唯一的副作用是簽發 nonce，沿 auth_nonce 慣例）。"""
    c, cfg, _ = _make(tmp_path)
    _login(c, Account.create())
    for _ in range(3):
        assert _ask(c).status_code == 200
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_each_call_issues_a_fresh_nonce(tmp_path):
    """每次呼叫發新 nonce（一次性資源；重用同一顆會讓第二次簽名必定失敗）。"""
    c, _, _ = _make(tmp_path)
    _login(c, Account.create())
    assert _ask(c).json()["nonce"] != _ask(c).json()["nonce"]


def test_issued_nonce_cannot_be_used_to_log_in(tmp_path):
    """⭐ 本端點的 nonce 不得被挪去完成一次 SIWE 登入（跨協定重放）。

    它以 chain_id=0 簽發，auth_verify 會用 chain_id=0 重建 SIWE 訊息——客戶從來
    不會簽那一份，recover 必然對不上。這裡實測「拿它去 verify」的結果是 401。
    """
    c, _, _ = _make(tmp_path)
    w = Account.create()
    _login(c, w)
    nonce = _ask(c).json()["nonce"]
    # 攻擊者能拿到的最好素材：客戶對**換 leader 原文**的合法簽名。
    body = _ask(c).json()
    sig = w.sign_message(encode_defunct(text=body["message"])).signature.hex()
    assert c.post("/api/auth/verify",
                  json={"nonce": nonce, "signature": sig}).status_code == 401
