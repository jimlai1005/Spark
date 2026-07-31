"""tests/test_api_risk_settings.py
客戶簽章的風控設定與解除熔斷（2026-07-30）：
`POST /api/me/risk/message`／`POST /api/me/risk`／
`POST /api/me/risk/unlock/message`／`POST /api/me/risk/unlock`／`GET /api/me/risk`。

全離線（FakeKeysvc / FakeHL），SIWE 登入與風控簽章都用**真密碼學**。
沿 tests/test_api_capital_settings.py 的形狀。

盯住五件事：
(1) ⭐⭐ 能改這些門檻的人就能拿掉客戶帳戶的全部保護 ⇒ 一律驗章，且驗簽失敗
    **400 而非 401**（客戶的 session 沒問題，壞的是這一份請求）。
(2) ⭐⭐ 「已提交」（簽章記錄）與「已生效」（引擎心跳）分得開——把記錄當成生效值
    顯示，會讓一個剛調低回撤上限的客戶以為保護已經生效。
(3) ⭐ 心跳過期／缺席 ⇒ `applied` 與 `halted` 皆為 null，**絕不**畫成「沒有熔斷」。
(4) ⭐ 域分隔：一份「調整門檻」的簽章不得被兌換成一次「解除熔斷」（反向亦然）。
(5) signature／待簽原文不外流、不進錯誤訊息。
"""
import json
import socket
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.capital_settings import load_capital_settings
from spark.filet.engine_health import (HEARTBEAT_STALE_S, build_heartbeat,
                                       heartbeat_path_for, write_heartbeat)
from spark.filet.leader_change import build_leader_change_message
from spark.filet.risk_prefs import default_prefs
from spark.filet.risk_settings import (ACTION_RISK_SETTINGS, ACTION_RISK_UNLOCK,
                                       RISK_SETTINGS_FIELDS, RISK_UNLOCK_FIELDS,
                                       build_risk_settings_message,
                                       build_risk_unlock_message,
                                       load_risk_settings, load_risk_unlocks)
from spark.publicapi.app import RiskSettingsBody, RiskUnlockBody
from spark.publicapi.config import derive_account_id
from tests.publicapi_helpers import login, make_app, make_cfg

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


# ── 共用 helper ──────────────────────────────────────────────────────

def _get_message(client, prefs=None):
    return client.post("/api/me/risk/message",
                       json={"prefs": {"enabled": True} if prefs is None else prefs})


def _submit(client, wallet, prefs=None, *, account_id=None, signer=None, tamper=None):
    """完整流程：取原文 → 簽 → POST。`tamper` 在簽完之後改 body。"""
    r = _get_message(client, prefs)
    assert r.status_code == 200, r.text
    m = r.json()
    sig = (signer or wallet).sign_message(
        encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": account_id or m["account_id"], "prefs": m["prefs"],
            "nonce": m["nonce"], "issued_at": m["issued_at"],
            "signature": sig, "message": m["message"]}
    if tamper:
        body.update(tamper)
    return client.post("/api/me/risk", json=body)


def _unlock(client, wallet, *, account_id=None, signer=None, tamper=None):
    r = client.post("/api/me/risk/unlock/message")
    assert r.status_code == 200, r.text
    m = r.json()
    sig = (signer or wallet).sign_message(
        encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": account_id or m["account_id"], "nonce": m["nonce"],
            "issued_at": m["issued_at"], "signature": sig, "message": m["message"]}
    if tamper:
        body.update(tamper)
    return client.post("/api/me/risk/unlock", json=body)


def write_hb(cfg, account_id, *, enabled=True, source="customer_signed",
             changed_at="2026-07-30T03:00:00+00:00", tripped=False, age_s=5.0,
             now_s=None, risk_halt=None, risk_prefs=None):
    """以引擎的**同一個產生器**寫一份心跳（不自己拼 JSON——自己拼的話，寫端改了
    欄位這裡不會紅，端點測試就變成一份與現實無關的自證）。"""
    payload = build_heartbeat(
        account_id=account_id, now_s=(now_s or time.time()) - age_s,
        killswitch_tripped=tripped, coverage=None, alerts_count=0,
        leader_address=_LEADER, leader_source="manifest", leader_kind="standard",
        allocated_capital="1000.00", capital_utilization="0.5000",
        use_full_equity=False, capital_source="customer_signed",
        capital_changed_at=None,
        risk_controls_enabled=enabled, risk_source=source,
        risk_changed_at=changed_at, risk_prefs=risk_prefs, risk_halt=risk_halt,
        cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, account_id), payload)


# ── 快樂路徑 ──────────────────────────────────────────────────────────

def test_message_endpoint_returns_canonical_text(client_wallet):
    """⭐ 原文由伺服器產生，客戶端原樣簽——兩邊結構上不可能組出不同的字串。
    回傳的 `prefs` 是 canonical 化後的值（客戶端原樣回填進 POST body）。"""
    client, _cfg, wallet = client_wallet
    m = _get_message(client, {"enabled": True, "max_drawdown_pct": "0.3"}).json()
    assert m["account_id"] == derive_account_id(wallet.address)
    assert m["prefs"]["max_drawdown_pct"] == "0.3"      # canonical，非原樣 "0.30"
    assert m["prefs"]["enabled"] is True
    assert m["message"] == build_risk_settings_message(
        account_id=m["account_id"], prefs=m["prefs"],
        nonce=m["nonce"], issued_at=m["issued_at"])


def test_message_lists_every_parameter_not_a_hash(client_wallet):
    """⭐ 客戶在錢包裡看到的是**每一個門檻的值**，不是一串他無從驗證的雜湊——
    「我簽的到底是什麼」正是簽章這道防線唯一的價值來源。"""
    client, _cfg, _wallet = client_wallet
    text = _get_message(client, {"enabled": True, "max_drawdown_pct": "0.3"}).json()["message"]
    for name in ("max_drawdown_pct", "max_total_drawdown_pct", "flatten_on_breach",
                 "cooldown_hours", "size_tolerance"):
        assert f"{name}:" in text
    assert "Risk Controls: enabled" in text


def test_message_text_states_consequences_before_signing(client_wallet):
    """客戶按下簽名之前必須看到後果：放寬＝可以虧更多才熔斷、關掉＝永不熔斷。"""
    client, _cfg, _wallet = client_wallet
    text = _get_message(client).json()["message"]
    assert "loosening them means your account can lose more" in text
    assert "next cycle" in text


def test_submit_writes_a_signed_record(client_wallet):
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, {"enabled": True, "max_drawdown_pct": "0.25"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["prefs"]["max_drawdown_pct"] == "0.25"
    assert body["effective"] == "next_engine_cycle"
    assert "下一個 cycle" in body["effective_note"]
    assert "重新驗證你的簽章" in body["effective_note"]

    entries = load_risk_settings(cfg.risk_settings_path)
    assert len(entries) == 1
    rec = entries[0]
    assert rec["action"] == ACTION_RISK_SETTINGS
    assert rec["account_id"] == derive_account_id(wallet.address)
    assert rec["prefs"]["max_drawdown_pct"] == "0.25"
    assert "signer" not in rec              # ⭐ 沒有 signer 欄位


def test_submitting_twice_overwrites_rather_than_appends(client_wallet):
    """記錄檔代表「客戶當前的風控意圖」，不是流水帳：同 account 覆蓋。
    附加會留下一堆舊意圖，而套用端挑錯一筆就是把保護設回他早已改掉的值。"""
    client, cfg, wallet = client_wallet
    assert _submit(client, wallet, {"enabled": True,
                                    "max_drawdown_pct": "0.3"}).status_code == 200
    assert _submit(client, wallet, {"enabled": True,
                                    "max_drawdown_pct": "0.4"}).status_code == 200
    entries = load_risk_settings(cfg.risk_settings_path)
    assert len(entries) == 1
    assert entries[0]["prefs"]["max_drawdown_pct"] == "0.4"


def test_unlock_round_trip_writes_a_one_shot_record(client_wallet):
    client, cfg, wallet = client_wallet
    r = _unlock(client, wallet)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "解除鎖定" in body["effective_note"]
    assert "leader 被撤銷" in body["effective_note"]   # 不適用的情形要講明
    entries = load_risk_unlocks(cfg.risk_unlock_path)
    assert len(entries) == 1
    assert entries[0]["action"] == ACTION_RISK_UNLOCK
    assert entries[0]["account_id"] == derive_account_id(wallet.address)


def test_unlock_message_states_that_trading_resumes(client_wallet):
    """解除熔斷是客戶**自己拿掉一道保護**：恢復後會依 leader 開新部位、
    權益基準已重置——兩句話都必須出現在他按下簽名之前。"""
    client, _cfg, _wallet = client_wallet
    text = client.post("/api/me/risk/unlock/message").json()["message"]
    assert "resumes trading on its next cycle" in text
    assert "equity baseline was already reset" in text
    assert "one resume only" in text


def test_settings_and_unlock_land_in_separate_files(client_wallet):
    """兩份記錄分開落檔：一個是持續意圖、一個是一次性動作，時效語意相反；
    共用一個檔會讓弄錯的方向變成 fail-open（舊解鎖記錄被當成持續意圖套用）。"""
    client, cfg, wallet = client_wallet
    assert _submit(client, wallet).status_code == 200
    assert _unlock(client, wallet).status_code == 200
    assert cfg.risk_settings_path != cfg.risk_unlock_path
    assert cfg.risk_settings_path != cfg.capital_settings_path
    assert load_risk_settings(cfg.risk_settings_path)
    assert load_risk_unlocks(cfg.risk_unlock_path)
    assert load_capital_settings(cfg.capital_settings_path) == []


def test_api_request_bodies_carry_exactly_the_record_fields():
    """⭐ HTTP 體的欄位集 ＝ 記錄欄位集減去 `action`（由伺服器寫死，不由客戶指定）。
    兩者漂移時，落檔記錄會少掉一個引擎需要的欄位，而症狀要到引擎那頭才看得到。"""
    assert (set(RiskSettingsBody.model_fields)
            == set(RISK_SETTINGS_FIELDS) - {"action"})
    assert (set(RiskUnlockBody.model_fields)
            == set(RISK_UNLOCK_FIELDS) - {"action"})


def test_record_is_plain_json_on_disk(client_wallet):
    client, cfg, wallet = client_wallet
    _submit(client, wallet)
    json.loads(open(cfg.risk_settings_path).read())


# ── 授權：未登入、改別人的 ────────────────────────────────────────────

def test_all_four_endpoints_require_session(tmp_path):
    app, _cfg, _s, _k, _h = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/me/risk").status_code == 401
    assert client.post("/api/me/risk/message",
                       json={"prefs": {"enabled": True}}).status_code == 401
    assert client.post("/api/me/risk", json={
        "account_id": "f" + "1" * 40, "prefs": {"enabled": True}, "nonce": "n",
        "issued_at": "i", "signature": "0x00", "message": ""}).status_code == 401
    assert client.post("/api/me/risk/unlock/message").status_code == 401
    assert client.post("/api/me/risk/unlock", json={
        "account_id": "f" + "1" * 40, "nonce": "n", "issued_at": "i",
        "signature": "0x00", "message": ""}).status_code == 401


def test_cannot_change_someone_elses_risk_settings(client_wallet):
    """⭐ 403（非 404）：對方確實通過身分驗證，只是無權變更這個帳號。"""
    client, cfg, wallet = client_wallet
    other = derive_account_id(Account.create().address)
    r = _submit(client, wallet, account_id=other)
    assert r.status_code == 403
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_cannot_unlock_someone_elses_account(client_wallet):
    client, cfg, wallet = client_wallet
    other = derive_account_id(Account.create().address)
    r = _unlock(client, wallet, account_id=other)
    assert r.status_code == 403
    assert load_risk_unlocks(cfg.risk_unlock_path) == []


def test_a_signature_from_one_session_cannot_touch_another_account(tmp_path):
    """⭐⭐ session 隔離的實測：A 完整簽好一份風控設定，把 body 原樣送進 **B 的**
    session → 帳號不符 403；反過來把 account_id 換成 B 的也不行（簽章者對不上）。"""
    app, cfg, _s, _k, _h = make_app(tmp_path)
    ca = TestClient(app, base_url="https://testserver")
    cb = TestClient(app, base_url="https://testserver")
    wa, wb = login(ca), login(cb)

    m = _get_message(ca, {"enabled": True, "max_drawdown_pct": "0.5"}).json()
    sig = wa.sign_message(encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": m["account_id"], "prefs": m["prefs"], "nonce": m["nonce"],
            "issued_at": m["issued_at"], "signature": sig, "message": m["message"]}
    # A 的 body 送進 B 的 session：account_id 是 A 的 → 403
    assert cb.post("/api/me/risk", json=body).status_code == 403
    # 把 account_id 換成 B 的：簽章者仍是 A → 400（且 nonce 是 A 的，不屬於 B）
    body["account_id"] = derive_account_id(wb.address)
    assert cb.post("/api/me/risk", json=body).status_code == 400
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_risk_settings_are_isolated_between_users(tmp_path):
    """account 由 session 衍生——沒有任何請求參數能讓我讀到別人的設定。"""
    app, _cfg, _s, _k, _h = make_app(tmp_path)
    c1 = TestClient(app, base_url="https://testserver")
    c2 = TestClient(app, base_url="https://testserver")
    w1 = login(c1)
    login(c2)
    assert _submit(c1, w1, {"enabled": True}).status_code == 200
    assert c2.get("/api/me/risk").json()["prefs"]["enabled"] is False


# ── 邊界：4xx，不得靜默夾取 ───────────────────────────────────────────

def test_out_of_range_is_rejected_at_the_message_endpoint(client_wallet):
    """⭐ 邊界在**發原文之前**就擋：讓客戶簽一份必定被 POST 拒絕的原文，
    是把閘門變成一個只會浪費他一次錢包簽名的陷阱。訊息要說得出合法區間。"""
    client, _cfg, _wallet = client_wallet
    r = _get_message(client, {"enabled": True, "max_drawdown_pct": "0.001"})
    assert r.status_code == 400
    assert "0.05" in r.json()["detail"] and "0.5" in r.json()["detail"]


def test_out_of_range_is_rejected_at_submit_too(client_wallet):
    """⭐⭐ POST 端也必須擋（不能只靠 message 端）：攻擊者可以跳過它直接送 POST。
    超界 → 400 ＋ **不落檔**，絕不夾取。"""
    client, cfg, wallet = client_wallet
    m = _get_message(client).json()
    r = client.post("/api/me/risk", json={
        "account_id": m["account_id"],
        "prefs": {**m["prefs"], "max_drawdown_pct": "0.9"},
        "nonce": m["nonce"], "issued_at": m["issued_at"],
        "signature": "0x" + "ab" * 65, "message": ""})
    assert r.status_code == 400
    assert "0.05" in r.json()["detail"]
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_an_out_of_range_value_cannot_even_be_rendered_for_signing(client_wallet):
    """⭐ 結構性的那一半：待簽原文的產生器本身就拒絕超界值 ⇒ 不存在一份「客戶簽過
    的超界設定」。POST 端的檢查是縱深，不是唯一那道。"""
    client, _cfg, _wallet = client_wallet
    m = _get_message(client).json()
    with pytest.raises(Exception):
        build_risk_settings_message(
            account_id=m["account_id"],
            prefs={**m["prefs"], "max_drawdown_pct": "0.9"},
            nonce=m["nonce"], issued_at=m["issued_at"])


def test_unknown_field_is_rejected_not_ignored(client_wallet):
    """打錯字不該被靜默忽略——客戶會以為自己設了某個東西。"""
    client, _cfg, _wallet = client_wallet
    r = _get_message(client, {"enabled": True, "max_drawdwon_pct": "0.3"})
    assert r.status_code == 400
    assert "max_drawdwon_pct" in r.json()["detail"]


def test_missing_enabled_cannot_silently_disable_protection(client_wallet):
    """⭐⭐ `{"prefs": {}}` **不得**被讀成「關閉風控」。觸發情境：客戶已開啟風控，
    之後任何一次沒帶 enabled 的請求（表單重置、重試掉了 body、第三方呼叫）
    都會把保護靜默關掉——「沒設定」不等於「不要保護」。"""
    client, _cfg, wallet = client_wallet
    assert _submit(client, wallet, {"enabled": True,
                                    "max_drawdown_pct": "0.15"}).status_code == 200
    r = _get_message(client, {})
    assert r.status_code == 400
    assert "enabled" in r.json()["detail"]
    assert client.get("/api/me/risk").json()["prefs"]["enabled"] is True


def test_partial_update_keeps_my_other_settings(client_wallet):
    """缺鍵補值的來源是「我目前**已簽章**的值」，不是產品預設：
    只送 enabled 的請求不該把我調過的 0.15 重設回 0.2。"""
    client, _cfg, wallet = client_wallet
    assert _submit(client, wallet, {"enabled": True,
                                    "max_drawdown_pct": "0.15"}).status_code == 200
    assert _submit(client, wallet, {"enabled": False}).status_code == 200
    got = client.get("/api/me/risk").json()["prefs"]
    assert got["enabled"] is False
    assert got["max_drawdown_pct"] == "0.15", "未提及的欄位不得被重設"


# ── 驗簽失敗：400（不是 401）、不落地、不回顯請求原值 ──────────────────

def test_wrong_signer_is_rejected_with_400_not_401(client_wallet):
    """⭐⭐ 400 而非 401：客戶的 session 是有效的（他已通過 SIWE），壞掉的是這一份
    請求內容。401 會讓前端把客戶登出，而那對他毫無幫助。"""
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, signer=Account.create())
    assert r.status_code == 400
    assert r.json()["detail"] == "簽章者不是本帳號的持有人"
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_wrong_signer_on_unlock_is_rejected(client_wallet):
    client, cfg, wallet = client_wallet
    r = _unlock(client, wallet, signer=Account.create())
    assert r.status_code == 400
    assert load_risk_unlocks(cfg.risk_unlock_path) == []


def test_garbage_signature_is_rejected(client_wallet):
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, tamper={"signature": "0xdeadbeef"})
    assert r.status_code == 400
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_tampered_prefs_after_signing_are_rejected(client_wallet):
    """簽完之後把回撤上限放寬 → 重建訊息不同 → 簽章者對不上 → 拒絕。"""
    client, cfg, wallet = client_wallet
    r = _submit(client, wallet, {"enabled": True, "max_drawdown_pct": "0.1"},
                tamper={"prefs": {**default_prefs(), "enabled": False}})
    assert r.status_code == 400
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_nonce_cannot_be_reused(client_wallet):
    """⭐ 同一份簽章只能兌現一次：原樣重送 → 400。"""
    client, cfg, wallet = client_wallet
    m = _get_message(client).json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": m["account_id"], "prefs": m["prefs"], "nonce": m["nonce"],
            "issued_at": m["issued_at"], "signature": sig, "message": m["message"]}
    assert client.post("/api/me/risk", json=body).status_code == 200
    second = client.post("/api/me/risk", json=body)
    assert second.status_code == 400
    assert second.json()["detail"] == "這份授權已被使用或已過期，請重新取得待簽原文並重簽"
    assert len(load_risk_settings(cfg.risk_settings_path)) == 1


def test_unlock_nonce_cannot_be_reused(client_wallet):
    """⭐ 解鎖尤其不能重放：一份能反覆兌現的解鎖授權等於熔斷保護永久失效。"""
    client, cfg, wallet = client_wallet
    m = client.post("/api/me/risk/unlock/message").json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": m["account_id"], "nonce": m["nonce"],
            "issued_at": m["issued_at"], "signature": sig, "message": m["message"]}
    assert client.post("/api/me/risk/unlock", json=body).status_code == 200
    assert client.post("/api/me/risk/unlock", json=body).status_code == 400
    assert len(load_risk_unlocks(cfg.risk_unlock_path)) == 1


def test_expired_signature_is_rejected_by_the_api(tmp_path):
    """⭐ API 端**強制時效**（引擎端刻意放行）：這裡驗的是「客戶剛剛按下的那一次」。
    觸發情境：一份十幾分鐘前取得原文的分頁被重新送出。"""
    clock = [time.time()]
    app, cfg, _s, _k, _h = make_app(tmp_path, now_fn=lambda: clock[0])
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    m = _get_message(client).json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    clock[0] += 700          # > RISK_SETTINGS_MAX_AGE_S（600s）
    r = client.post("/api/me/risk", json={
        "account_id": m["account_id"], "prefs": m["prefs"], "nonce": m["nonce"],
        "issued_at": m["issued_at"], "signature": sig, "message": m["message"]})
    assert r.status_code == 400
    assert r.json()["detail"] == "簽章已過期，請重新取得待簽原文並重簽"
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_error_detail_never_echoes_client_input(client_wallet):
    """⭐ 回**分類化訊息**而非 str(e)：例外訊息內嵌客戶送來的 nonce／issued_at 原值，
    回顯它與「不記 signature／message 原文」的政策直接矛盾。"""
    from spark.publicapi.app import (RISK_SETTINGS_DETAIL,
                                     RISK_SETTINGS_DETAIL_DEFAULT)
    client, _cfg, wallet = client_wallet
    marker = "SENTINEL_NONCE_VALUE"
    r = _submit(client, wallet, tamper={"nonce": marker})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert marker not in detail
    assert detail in set(RISK_SETTINGS_DETAIL.values()) | {
        RISK_SETTINGS_DETAIL_DEFAULT}


# ── ⭐⭐ 域分隔：HTTP 層的雙向拒絕 ────────────────────────────────────

def test_risk_settings_signature_cannot_be_redeemed_as_an_unlock(client_wallet):
    """⭐⭐ 攻擊者最可能的手法：等客戶調一次門檻，拿那份簽章把熔斷鎖打開。
    同一顆 nonce、同一個帳號，送到解鎖端點 → 拒絕。"""
    client, cfg, wallet = client_wallet
    m = _get_message(client).json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    r = client.post("/api/me/risk/unlock", json={
        "account_id": m["account_id"], "nonce": m["nonce"],
        "issued_at": m["issued_at"], "signature": sig, "message": m["message"]})
    assert r.status_code == 400
    assert load_risk_unlocks(cfg.risk_unlock_path) == []


def test_unlock_signature_cannot_be_redeemed_as_risk_settings(client_wallet):
    """反方向同樣被擋：一次「恢復跟單」的授權不得變成一次門檻變更。"""
    client, cfg, wallet = client_wallet
    m = client.post("/api/me/risk/unlock/message").json()
    unlock_msg = build_risk_unlock_message(account_id=m["account_id"],
                                           nonce=m["nonce"],
                                           issued_at=m["issued_at"])
    sig = wallet.sign_message(encode_defunct(text=unlock_msg)).signature.hex()
    r = client.post("/api/me/risk", json={
        "account_id": m["account_id"], "prefs": {**default_prefs(), "enabled": False},
        "nonce": m["nonce"], "issued_at": m["issued_at"],
        "signature": sig, "message": unlock_msg})
    assert r.status_code == 400
    assert load_risk_settings(cfg.risk_settings_path) == []


def test_leader_change_signature_cannot_be_submitted_as_risk_settings(client_wallet):
    """誘導客戶簽一份看起來無害的換 leader 授權，再兌換成「風控全關」→ 拒絕。"""
    client, cfg, wallet = client_wallet
    m = _get_message(client).json()
    leader_msg = build_leader_change_message(
        account_id=m["account_id"], leader_address=_LEADER,
        nonce=m["nonce"], issued_at=m["issued_at"])
    sig = wallet.sign_message(encode_defunct(text=leader_msg)).signature.hex()
    r = client.post("/api/me/risk", json={
        "account_id": m["account_id"], "prefs": {**default_prefs(), "enabled": False},
        "nonce": m["nonce"], "issued_at": m["issued_at"],
        "signature": sig, "message": leader_msg})
    assert r.status_code == 400
    assert load_risk_settings(cfg.risk_settings_path) == []


# ── GET /api/me/risk：已提交 vs 已生效 vs 熔斷 ────────────────────────

def test_get_ships_specs_so_the_form_hardcodes_nothing(client_wallet):
    client, _cfg, _wallet = client_wallet
    r = client.get("/api/me/risk").json()
    assert r["prefs"] == default_prefs()          # 尚無記錄 → 產品預設
    assert r["submitted"]["issued_at"] is None
    names = {s["name"] for s in r["specs"]}
    assert names == {"size_tolerance", "max_drawdown_pct", "max_total_drawdown_pct",
                     "flatten_on_breach", "cooldown_hours"}
    for s in r["specs"]:
        assert s["label"] and s["help"]
        assert s["recommended"] is not None
        assert s["group"] in ("tracking", "risk")
    assert next(s for s in r["specs"]
                if s["name"] == "size_tolerance")["group"] == "tracking"
    assert not any("cost" in n for n in names)    # 成本熔斷器不開放給客戶調


def test_get_reflects_the_submitted_signed_prefs(client_wallet):
    client, _cfg, wallet = client_wallet
    _submit(client, wallet, {"enabled": True, "max_drawdown_pct": "0.25",
                             "max_total_drawdown_pct": "0.5",
                             "flatten_on_breach": False})
    got = client.get("/api/me/risk").json()
    assert got["prefs"]["enabled"] is True
    assert got["prefs"]["max_drawdown_pct"] == "0.25"
    assert got["prefs"]["flatten_on_breach"] is False
    assert got["submitted"]["issued_at"]          # 有提交時刻


def test_get_is_editable_even_for_an_activated_account(client_wallet):
    """⭐ `editable` 恆為 true：偏好改成執行期套用之後，已啟用的帳號同樣能改。
    舊版對非 pending 帳號回 409／`not_editable_reason` 的分支已移除。"""
    client, _cfg, wallet = client_wallet
    body = client.get("/api/me/risk").json()
    assert body["editable"] is True
    assert "not_editable_reason" not in body
    assert _submit(client, wallet, {"enabled": True}).status_code == 200


def test_applied_and_halted_come_from_a_fresh_heartbeat(client_wallet):
    client, cfg, wallet = client_wallet
    account_id = derive_account_id(wallet.address)
    write_hb(cfg, account_id, enabled=True, tripped=True,
             risk_halt={"tripped": True, "reason": "drawdown", "resumable": True,
                        "tripped_at": "2026-07-30T02:00:00+00:00"})
    body = client.get("/api/me/risk").json()
    assert body["applied"]["controls_enabled"] is True
    assert body["applied"]["source"] == "customer_signed"
    assert body["applied"]["as_of"] is not None
    assert body["halted"]["tripped"] is True
    assert body["halted"]["resumable"] is True
    assert "立即恢復" in body["halted"]["note"]


def test_leader_revoked_halt_is_not_self_resumable(client_wallet):
    """⭐⭐ leader 被撤銷的鎖定：`resumable=False`，且說明必須明講**無法**自助恢復。
    前端據此收起「立即恢復跟單」按鈕——否則客戶會為一份注定被引擎拒絕的解鎖請求
    真的簽一次名，而失敗訊息出現在他按下之後，看起來像系統壞了。"""
    client, cfg, wallet = client_wallet
    account_id = derive_account_id(wallet.address)
    write_hb(cfg, account_id, tripped=True,
             risk_halt={"tripped": True, "reason": "leader_revoked",
                        "resumable": False,
                        "tripped_at": "2026-07-30T02:00:00+00:00"})
    halted = client.get("/api/me/risk").json()["halted"]
    assert halted["resumable"] is False
    assert halted["reason"] == "leader_revoked"
    assert "無法" in halted["note"] and "立即恢復" not in halted["note"]


def test_halt_from_an_older_engine_says_unknown_rather_than_guessing(client_wallet):
    """舊版引擎的心跳沒有 `risk.halt` 這一格 ⇒ `resumable` 為 None（未知）。
    不得預設成 True——那會讓客戶對一個不可恢復的鎖定簽一份無效請求。"""
    client, cfg, wallet = client_wallet
    write_hb(cfg, derive_account_id(wallet.address), tripped=True, risk_halt=None)
    halted = client.get("/api/me/risk").json()["halted"]
    assert halted["resumable"] is None
    assert "尚無法確認" in halted["note"]


def test_stale_heartbeat_means_applied_and_halted_are_unknown(client_wallet):
    """⭐⭐ 心跳過期 ⇒ `applied` 與 `halted` **皆為 null**——一份 40 分鐘前的
    「kill switch 未觸發」在客戶的引擎已經熔斷的當下顯示成現況，正是這裡最不能
    犯的錯。null ＝「無從得知」，不是「沒有熔斷」。"""
    client, cfg, wallet = client_wallet
    account_id = derive_account_id(wallet.address)
    write_hb(cfg, account_id, tripped=True, age_s=HEARTBEAT_STALE_S + 60)
    body = client.get("/api/me/risk").json()
    assert body["applied"] is None
    assert body["halted"] is None
    assert body["heartbeat"]["status"] == "stale"
    assert "無法確認" in body["note"]


def test_missing_heartbeat_means_applied_and_halted_are_unknown(client_wallet):
    """引擎從未寫過心跳（尚未啟用）→ 同樣是 null，不是「沒有熔斷」。"""
    client, _cfg, _wallet = client_wallet
    body = client.get("/api/me/risk").json()
    assert body["applied"] is None and body["halted"] is None
    assert body["heartbeat"]["status"] == "missing"


def test_engine_unsure_about_killswitch_is_not_painted_as_not_halted(client_wallet):
    """`killswitch_tripped=None`（引擎自己也讀不到 ARM 檔）⇒ `halted` 為 null。
    折疊成 False 會在引擎已經熔斷的當下顯示一顆綠燈。"""
    client, cfg, wallet = client_wallet
    write_hb(cfg, derive_account_id(wallet.address), tripped=None)
    assert client.get("/api/me/risk").json()["halted"] is None


def test_submitted_and_applied_are_reported_separately(client_wallet):
    """⭐⭐ 客戶改了門檻但引擎還沒套用時，畫面必須看得出兩者不同——
    把記錄當成生效值顯示，會讓他以為保護已經降下來了。"""
    client, cfg, wallet = client_wallet
    account_id = derive_account_id(wallet.address)
    write_hb(cfg, account_id, enabled=False, source="env_default")
    _submit(client, wallet, {"enabled": True, "max_drawdown_pct": "0.2"})
    body = client.get("/api/me/risk").json()
    assert body["prefs"]["enabled"] is True            # 已提交
    assert body["applied"]["controls_enabled"] is False  # 尚未生效
    assert body["applied"]["source"] == "env_default"


def test_corrupt_stored_record_renders_the_safe_side_not_a_500(client_wallet):
    """⭐ 壞掉的記錄不得讓 GET 500——那會讓客戶連改回來的介面都打不開，而 500 又
    長得像「稍後重試就好」。顯示的必須是引擎對同一份壞資料會採用的那一份
    （風控開啟），並明講「這不是你存的值」。"""
    client, cfg, wallet = client_wallet
    _submit(client, wallet, {"enabled": False})
    data = json.loads(open(cfg.risk_settings_path).read())
    data["settings"][0]["prefs"]["max_drawdown_pct"] = "999"
    open(cfg.risk_settings_path, "w").write(json.dumps(data))

    r = client.get("/api/me/risk")
    assert r.status_code == 200
    body = r.json()
    assert body["stored_unreadable"] is True
    assert body["prefs"]["enabled"] is True, "與引擎的 fail-closed 方向一致"
    assert "安全預設" in body["stored_unreadable_note"]
    assert body["editable"] is True, "必須留一條讓客戶自己改回來的路"


def test_healthy_record_is_not_flagged_unreadable(client_wallet):
    client, _cfg, wallet = client_wallet
    _submit(client, wallet, {"enabled": True})
    assert client.get("/api/me/risk").json()["stored_unreadable"] is False


def test_get_never_leaks_signature_or_message(client_wallet):
    """⚠️ 紅線：簽章與待簽原文結構上到不了回應（`_my_signed_risk_record` 只投影
    安全欄位）。"""
    client, _cfg, wallet = client_wallet
    _submit(client, wallet, {"enabled": True})
    text = client.get("/api/me/risk").text
    assert "signature" not in text and "0x" not in text


# ── ⭐⭐ 寫端與讀端：API 落的記錄，watcher／引擎真的用得到 ────────────

def test_api_write_and_engine_read_resolve_to_the_same_file(client_wallet):
    """⭐ C3 的形狀（API 寫 A、引擎讀 B，而兩邊 log 都正常）：兩端的路徑推導必須是
    **同一個函式**吃**同一個** FILET_EXCHANGE_DIR。"""
    from spark.filet.risk_settings_apply import (resolve_risk_settings_path,
                                                 resolve_risk_unlock_path)
    client, cfg, wallet = client_wallet
    assert _submit(client, wallet).status_code == 200   # 交換目錄由第一次落檔建出來
    env = {"FILET_EXCHANGE_DIR": cfg.exchange_dir}
    assert cfg.risk_settings_path == resolve_risk_settings_path(env)
    assert cfg.risk_unlock_path == resolve_risk_unlock_path(env)


def test_a_record_signed_through_the_api_verifies_in_the_watcher(client_wallet):
    """⭐⭐ 端到端：客戶透過 API 簽下的記錄，auto-activate watcher **驗得過**並把值
    寫進 env。這是整條管線唯一真正重要的性質——訊息版型、canonical 化、落檔路徑、
    驗章的可信來源，四者只要有一處在兩端不同，客戶的設定就會靜默退回產品預設。"""
    from scripts.filet_auto_activate import _risk_lines
    from spark.copytrade.notifier import RecordingNotifier
    client, cfg, wallet = client_wallet
    assert _submit(client, wallet, {"enabled": True,
                                    "max_drawdown_pct": "0.3"}).status_code == 200
    notifier = RecordingNotifier()
    lines = _risk_lines(load_risk_settings(cfg.risk_settings_path),
                        derive_account_id(wallet.address), wallet.address, notifier)
    assert "COPY_RISK_CONTROLS_ENABLED=true" in lines
    assert "COPY_MAX_DRAWDOWN_PCT=0.3" in lines
    assert notifier.records == []          # 合法記錄不得產生任何告警


def test_get_survives_an_unreadable_record_file(tmp_path):
    """記錄檔整份壞掉（不是 JSON）→ 當成「尚無已簽章的設定」，不 500。"""
    cfg = make_cfg(tmp_path)
    app, cfg, _s, _k, _h = make_app(tmp_path, cfg=cfg)
    client = TestClient(app, base_url="https://testserver")
    login(client)
    from pathlib import Path
    Path(cfg.risk_settings_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.risk_settings_path).write_text("{not json")
    r = client.get("/api/me/risk")
    assert r.status_code == 200
    assert r.json()["prefs"] == default_prefs()
