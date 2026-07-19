"""tests/test_api_capital_settings.py
`GET /api/me/capital/message` ＋ `POST /api/me/capital`——客戶簽章的資金設定端點。

全離線（FakeKeysvc / FakeHL），SIWE 登入與資金設定簽章都用**真密碼學**。
沿 tests/test_api_leader_select.py 的形狀。
"""
import json
import socket

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.capital_settings import (ACTION_CAPITAL_SETTINGS,
                                          CAPITAL_SETTINGS_FIELDS,
                                          build_capital_settings_message,
                                          load_capital_settings)
from spark.filet.leader_change import build_leader_change_message
from spark.publicapi.app import CapitalSettingsBody
from spark.publicapi.config import derive_account_id
from tests.publicapi_helpers import login, make_app

_LEADER = "0x" + "d4" * 20

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（沿 test_api_leaders）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 需要 loopback socket；外部網路仍由 conftest 的 autouse 斷網擋住。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


@pytest.fixture
def client_wallet(tmp_path):
    app, cfg, store, _keysvc, _hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")  # secure cookie 需 https
    wallet = login(client)
    return client, cfg, wallet


def _get_message(client, alloc="1000", util="0.5"):
    return client.get("/api/me/capital/message",
                      params={"allocated_capital": alloc,
                              "capital_utilization": util})


def _submit(client, wallet, *, alloc="1000", util="0.5", account_id=None,
            signer=None, tamper=None):
    """完整流程：取原文 → 簽 → POST。`tamper` 在簽完之後改 body。"""
    r = _get_message(client, alloc, util)
    assert r.status_code == 200, r.text
    m = r.json()
    sig = (signer or wallet).sign_message(
        encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": account_id or m["account_id"],
            "allocated_capital": m["allocated_capital"],
            "capital_utilization": m["capital_utilization"],
            "nonce": m["nonce"], "issued_at": m["issued_at"],
            "signature": sig, "message": m["message"]}
    if tamper:
        body.update(tamper)
    return client.post("/api/me/capital", json=body)


# ── 快樂路徑 ──────────────────────────────────────────────────────────

def test_message_endpoint_returns_canonical_text(client_wallet):
    """⭐ 原文由伺服器產生，客戶端原樣簽——兩邊結構上不可能組出不同的字串。"""
    client, _cfg, wallet = client_wallet
    r = _get_message(client, "1000", "0.5")
    assert r.status_code == 200
    m = r.json()
    assert m["account_id"] == derive_account_id(wallet.address)
    assert m["allocated_capital"] == "1000.00"     # canonical，非原樣回傳
    assert m["capital_utilization"] == "0.5000"
    assert m["message"] == build_capital_settings_message(
        account_id=m["account_id"], allocated_capital="1000.00",
        capital_utilization="0.5000", nonce=m["nonce"], issued_at=m["issued_at"])


def test_message_text_states_consequences_before_signing(client_wallet):
    """客戶在錢包裡看到的就是這段文字：曝險放大與生效時機必須出現在他按下簽名之前。"""
    client, _cfg, _wallet = client_wallet
    text = _get_message(client).json()["message"]
    assert "next cycle" in text
    assert "No immediate forced rebalance" in text
    assert "liquidation" in text


def test_submit_writes_a_signed_record(client_wallet):
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, alloc="2500", util="0.25")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["allocated_capital"] == "2500.00"
    assert body["capital_utilization"] == "0.2500"

    entries = load_capital_settings(cfg.capital_settings_path)
    assert len(entries) == 1
    rec = entries[0]
    assert rec["action"] == ACTION_CAPITAL_SETTINGS
    assert rec["account_id"] == derive_account_id(wallet.address)
    assert rec["allocated_capital"] == "2500.00"
    assert "signer" not in rec              # ⭐ 沒有 signer 欄位


def test_response_states_next_cycle_and_no_forced_rebalance(client_wallet):
    """⭐ 回應必須明講：下一個 cycle 生效、**不做即時強制再平衡**。
    不講清楚客戶會以為系統沒反應而重複提交（每次都是一次簽章與一顆 nonce）。"""
    client, _cfg, wallet = client_wallet
    body = _submit(client, wallet).json()
    assert body["effective"] == "next_engine_cycle"
    assert "下一個 cycle" in body["effective_note"]
    assert "不會立即強制" in body["consequences"]


def test_api_request_body_carries_exactly_the_record_fields():
    """⭐ HTTP 體的欄位集 ＝ 記錄欄位集減去 `action`（由伺服器寫死，不由客戶指定）。
    兩者漂移時，落檔記錄會少掉一個引擎需要的欄位，而症狀要到引擎那頭才看得到。"""
    assert (set(CapitalSettingsBody.model_fields)
            == set(CAPITAL_SETTINGS_FIELDS) - {"action"})


def test_record_is_plain_json_on_disk(client_wallet):
    client, cfg, wallet = client_wallet
    _submit(client, wallet)
    json.loads(open(cfg.capital_settings_path).read())


# ── 授權：未登入、改別人的 ────────────────────────────────────────────

def test_message_requires_session(tmp_path):
    app, _cfg, _s, _k, _h = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/api/me/capital/message",
                   params={"allocated_capital": "1000", "capital_utilization": "0.5"})
    assert r.status_code == 401


def test_submit_requires_session(tmp_path):
    app, _cfg, _s, _k, _h = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/api/me/capital", json={
        "account_id": "f" + "1" * 40, "allocated_capital": "1000.00",
        "capital_utilization": "0.5000", "nonce": "n", "issued_at": "i",
        "signature": "0x00", "message": ""})
    assert r.status_code == 401


def test_cannot_change_someone_elses_account(client_wallet):
    """⭐ 403（非 404）：對方確實通過身分驗證，只是無權變更這個帳號。"""
    client, cfg, wallet = client_wallet
    other = derive_account_id(Account.create().address)
    r = _submit(client, wallet, account_id=other)
    assert r.status_code == 403
    assert load_capital_settings(cfg.capital_settings_path) == []


# ── 邊界：4xx，不得靜默截斷 ───────────────────────────────────────────

@pytest.mark.parametrize("alloc,util", [
    ("0", "0.5"), ("-100", "0.5"), ("1000", "0"), ("1000", "-0.1"),
    ("1000", "1.0001"), ("1000", "2"),
])
def test_out_of_range_is_rejected_at_the_message_endpoint(client_wallet, alloc, util):
    """⭐ 邊界在**發原文之前**就擋：讓客戶簽一份必定被 POST 拒絕的原文，
    是把閘門變成一個只會浪費他一次錢包簽名的陷阱。"""
    client, _cfg, _wallet = client_wallet
    r = _get_message(client, alloc, util)
    assert r.status_code == 400
    assert "超出允許範圍" in r.json()["detail"]


def test_out_of_range_is_rejected_at_submit_even_if_signed(client_wallet):
    """⭐⭐ POST 端也必須擋（不能只靠 GET 端）：攻擊者可以跳過 GET 直接送 POST。
    超界 → 4xx ＋ **不落檔**，絕不夾取。"""
    client, cfg, wallet = client_wallet
    # 先用合法值取得 nonce 與原文，再把 body 的數值換成超界值後重簽。
    r = _get_message(client, "1000", "0.5")
    m = r.json()
    msg = build_capital_settings_message(
        account_id=m["account_id"], allocated_capital="1000.00",
        capital_utilization="9.0000", nonce=m["nonce"], issued_at=m["issued_at"])
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    r = client.post("/api/me/capital", json={
        "account_id": m["account_id"], "allocated_capital": "1000.00",
        "capital_utilization": "9.0000", "nonce": m["nonce"],
        "issued_at": m["issued_at"], "signature": sig, "message": msg})
    assert r.status_code == 400
    assert load_capital_settings(cfg.capital_settings_path) == []


def test_excess_precision_is_rejected_not_truncated(client_wallet):
    """小數位過多 → 4xx，**不是**悄悄四捨五入成一個客戶沒簽過的數字。"""
    client, _cfg, _wallet = client_wallet
    assert _get_message(client, "1000.005", "0.5").status_code == 400
    assert _get_message(client, "1000", "0.123456").status_code == 400


def test_non_numeric_is_rejected(client_wallet):
    client, _cfg, _wallet = client_wallet
    assert _get_message(client, "abc", "0.5").status_code == 400
    assert _get_message(client, "1000", "NaN").status_code == 400


# ── 驗簽失敗：不落地、不回顯請求原值 ──────────────────────────────────

def test_wrong_signer_is_rejected_and_nothing_is_written(client_wallet):
    """⭐ 簽章者不符 → 拒絕，且記錄**不落地**（驗簽失敗卻留下記錄，等於把被拒絕的
    請求偽裝成待套用的意圖）。"""
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, signer=Account.create())
    assert r.status_code == 400
    assert r.json()["detail"] == "簽章者不是本帳號的持有人"
    assert load_capital_settings(cfg.capital_settings_path) == []


def test_nonce_cannot_be_reused(client_wallet):
    """⭐ 同一份簽章只能兌現一次：原樣重送 → 400。"""
    client, cfg, wallet = client_wallet
    r = _get_message(client)
    m = r.json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": m["account_id"],
            "allocated_capital": m["allocated_capital"],
            "capital_utilization": m["capital_utilization"],
            "nonce": m["nonce"], "issued_at": m["issued_at"],
            "signature": sig, "message": m["message"]}
    assert client.post("/api/me/capital", json=body).status_code == 200
    second = client.post("/api/me/capital", json=body)
    assert second.status_code == 400
    assert second.json()["detail"] == "這份授權已被使用或已過期，請重新取得待簽原文並重簽"
    assert len(load_capital_settings(cfg.capital_settings_path)) == 1


def test_tampered_amount_after_signing_is_rejected(client_wallet):
    """簽完之後把使用比例改大 → 重建訊息不同 → 簽章者對不上 → 拒絕。"""
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, alloc="1000", util="0.2",
                tamper={"capital_utilization": "1.0000"})
    assert r.status_code == 400
    assert load_capital_settings(cfg.capital_settings_path) == []


def test_error_detail_never_echoes_client_input(client_wallet):
    """⭐ 回**分類化訊息**而非 str(e)：例外訊息內嵌客戶送來的 nonce／金額原值，
    回顯它與「不記 signature／message 原文」的政策直接矛盾。"""
    from spark.publicapi.app import (CAPITAL_SETTINGS_DETAIL,
                                     CAPITAL_SETTINGS_DETAIL_DEFAULT)
    client, _cfg, wallet = client_wallet
    marker = "SENTINEL_NONCE_VALUE"
    r = _submit(client, wallet, tamper={"nonce": marker})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert marker not in detail
    assert detail in set(CAPITAL_SETTINGS_DETAIL.values()) | {
        CAPITAL_SETTINGS_DETAIL_DEFAULT}


# ── ⭐⭐ 域分隔：HTTP 層的雙向拒絕 ────────────────────────────────────

def test_leader_change_signature_cannot_be_submitted_as_capital_settings(client_wallet):
    """⭐⭐ 拿一份**換 leader 的待簽原文**去簽，再送到資金設定端點 → 拒絕。

    這是攻擊者最可能的實際手法：誘導客戶簽一份看起來無害的換 leader 授權，
    再把它兌換成一次「使用比例拉滿」。
    """
    client, cfg, wallet = client_wallet
    m = _get_message(client).json()
    leader_msg = build_leader_change_message(
        account_id=m["account_id"], leader_address=_LEADER,
        nonce=m["nonce"], issued_at=m["issued_at"])
    sig = wallet.sign_message(encode_defunct(text=leader_msg)).signature.hex()
    r = client.post("/api/me/capital", json={
        "account_id": m["account_id"], "allocated_capital": "1000.00",
        "capital_utilization": "1.0000", "nonce": m["nonce"],
        "issued_at": m["issued_at"], "signature": sig, "message": leader_msg})
    assert r.status_code == 400
    assert load_capital_settings(cfg.capital_settings_path) == []


def test_capital_signature_cannot_be_submitted_as_leader_change(tmp_path):
    """⭐⭐ 反方向：拿一份**資金設定的待簽原文**去簽，再送到換 leader 端點 → 拒絕。

    需要一個白名單裡有 leader 的 app（否則會先被 is_selectable 擋下，那樣就測不到
    域分隔本身）。
    """
    leaders = tmp_path / "leaders.json"
    leaders.write_text(json.dumps({"leaders": [{"address": _LEADER, "name": "D"}]}))
    from tests.publicapi_helpers import make_cfg
    cfg = make_cfg(tmp_path, leaders_path=str(leaders))
    app, cfg, _store, _k, _h = make_app(tmp_path, cfg=cfg)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)

    # 先確認這個 leader 在正常流程下是選得到的（否則本測試會誤報成功）
    ok = client.get("/api/leaders/select/message",
                    params={"leader_address": _LEADER})
    assert ok.status_code == 200, ok.text
    m = ok.json()

    # 客戶簽的卻是**資金設定**的原文（同一顆 nonce、同一個帳號）
    cap_msg = build_capital_settings_message(
        account_id=m["account_id"], allocated_capital="1000.00",
        capital_utilization="1.0000", nonce=m["nonce"], issued_at=m["issued_at"])
    sig = wallet.sign_message(encode_defunct(text=cap_msg)).signature.hex()
    r = client.post("/api/leaders/select", json={
        "account_id": m["account_id"], "leader_address": _LEADER,
        "nonce": m["nonce"], "issued_at": m["issued_at"],
        "signature": sig, "message": cap_msg})
    assert r.status_code == 400

    from spark.filet.leader_change import load_leader_changes
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_capital_and_leader_records_land_in_separate_files(client_wallet):
    """兩份記錄分開落檔：其中一方的格式問題不得連坐另一方（各自都能造成資金損失）。"""
    client, cfg, wallet = client_wallet
    _submit(client, wallet)
    assert cfg.capital_settings_path != cfg.leader_changes_path
    assert load_capital_settings(cfg.capital_settings_path)
