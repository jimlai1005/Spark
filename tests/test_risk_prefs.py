"""tests/test_risk_prefs.py
錢包主人自選風控（2026-07-30）：偏好的驗證／落檔／變成 env 行，以及三條
「壞資料不得靜默變成無保護」的路徑。
"""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from spark.filet.risk_prefs import (RISK_ENV_KEYS, RiskPrefsError, canonical_prefs,
                                    default_prefs, prefs_summary, risk_env_lines,
                                    safe_fallback_prefs)
from spark.publicapi.pending import load_pending, set_pending_risk, write_pending_entry
from tests.publicapi_helpers import login, make_app

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


# ── 1. 預設與邊界 ────────────────────────────────────────────────────
def test_product_default_is_no_risk_controls():
    """2026-07-30 使用者裁決：新錢包預設不啟用任何風控。"""
    assert default_prefs()["enabled"] is False


def test_out_of_range_is_rejected_not_clamped():
    """夾取會讓客戶送了 A、系統套用 B，而且沒有人會知道（沿 capital_settings 裁決）。"""
    with pytest.raises(RiskPrefsError) as e:
        canonical_prefs({"enabled": True, "max_drawdown_pct": "0.01"})
    assert e.value.reason == "max_drawdown_pct_out_of_range"
    with pytest.raises(RiskPrefsError):
        canonical_prefs({"enabled": True, "max_drawdown_pct": "0.90"})


def test_details_are_validated_even_when_disabled():
    """⭐ enabled=False 也驗細項：現在收下超界值，等於埋一顆客戶啟用當下才炸的雷。"""
    with pytest.raises(RiskPrefsError):
        canonical_prefs({"enabled": False, "max_drawdown_pct": "0.99"})


def test_unknown_field_is_rejected():
    """打錯字不該被靜默忽略——客戶會以為自己設了某個東西。"""
    with pytest.raises(RiskPrefsError) as e:
        canonical_prefs({"enabled": True, "max_drawdwon_pct": "0.3"})
    assert e.value.reason == "unknown_field"


def test_non_bool_flag_rejected():
    with pytest.raises(RiskPrefsError):
        canonical_prefs({"enabled": "yes"})


def test_zero_total_drawdown_is_legal_meaning_that_gate_is_off():
    """累計回撤 0 ＝ 停用那一道（引擎既有語意，config.py:137）。"""
    assert canonical_prefs({"enabled": True,
                            "max_total_drawdown_pct": "0"})["max_total_drawdown_pct"] == "0"


# ── 2. env 行 ────────────────────────────────────────────────────────
def test_env_lines_cover_exactly_the_watcher_owned_keys():
    """env 行的鍵集必須與 GENERATED_KEYS 那一側用的常數一致，否則會漏寫一個鍵
    而讓它落回引擎預設（＝有保護），與客戶的選擇不符。"""
    keys = [ln.split("=", 1)[0] for ln in risk_env_lines(None)]
    assert tuple(keys) == RISK_ENV_KEYS


def test_env_lines_off_by_default_and_on_when_chosen():
    assert "COPY_RISK_CONTROLS_ENABLED=false" in risk_env_lines(None)
    on = risk_env_lines({"enabled": True, "max_drawdown_pct": "0.3",
                         "max_total_drawdown_pct": "0.5",
                         "flatten_on_breach": False})
    assert "COPY_RISK_CONTROLS_ENABLED=true" in on
    assert "COPY_MAX_DRAWDOWN_PCT=0.3" in on
    assert "COPY_MAX_TOTAL_DRAWDOWN_PCT=0.5" in on
    assert "COPY_FLATTEN_ON_BREACH=false" in on   # bool 必須是小寫 true/false


def test_safe_fallback_turns_protection_on():
    """⭐ 讀不懂客戶偏好時的方向是**開啟**保護：「資料壞了」與「客戶不要保護」
    必須產生不同結果，否則一次資料損壞就是一顆無保護的實盤引擎。"""
    assert safe_fallback_prefs()["enabled"] is True


def test_env_lines_revalidate_persisted_values():
    """落檔後被手改成超界值 → 上拋（呼叫端據此走安全側），不得原樣寫進 env。"""
    with pytest.raises(RiskPrefsError):
        risk_env_lines({"enabled": True, "max_drawdown_pct": "5"})


# ── 3. pending 條目 ─────────────────────────────────────────────────
def test_set_pending_risk_targets_only_my_account(tmp_path):
    p = tmp_path / "pending.json"
    for acct, addr in (("f" + "a" * 40, "0x" + "a" * 40), ("f" + "b" * 40, "0x" + "b" * 40)):
        write_pending_entry(p, account_id=acct, user_address=addr,
                            builder_address="0x" + "c" * 40, network="mainnet",
                            agent_address="0x" + "d" * 40)
    prefs = canonical_prefs({"enabled": True})
    assert set_pending_risk(p, "f" + "a" * 40, prefs) is True
    entries = {e["account_id"]: e for e in load_pending(p)}
    assert entries["f" + "a" * 40]["risk"]["enabled"] is True
    assert "risk" not in entries["f" + "b" * 40], "不得動到別人的條目"


def test_set_pending_risk_reports_missing_account(tmp_path):
    """⭐ 回 False 而不是靜默成功：已啟用的帳號 env 不會被覆寫，回 200 等於騙客戶。"""
    p = tmp_path / "pending.json"
    p.write_text(json.dumps({"pending": []}))
    assert set_pending_risk(p, "f" + "e" * 40, default_prefs()) is False


# ── 4. API ──────────────────────────────────────────────────────────
def _make_pending(cfg, address: str) -> str:
    account_id = "f" + address.lower()[2:]
    write_pending_entry(cfg.pending_path, account_id=account_id, user_address=address,
                        builder_address=cfg.builder_address, network=cfg.network,
                        agent_address="0x" + "d" * 40)
    return account_id


def test_get_risk_ships_specs_so_the_form_hardcodes_nothing(tmp_path):
    app, cfg, *_ = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    _make_pending(cfg, wallet.address)
    r = client.get("/api/me/risk").json()
    assert r["prefs"]["enabled"] is False          # 預設關
    assert r["editable"] is True
    names = {s["name"] for s in r["specs"]}
    assert names == {"max_drawdown_pct", "max_total_drawdown_pct", "flatten_on_breach"}
    for s in r["specs"]:
        assert s["label"] and s["help"]           # 前端不必自己編文案
    # 成本熔斷器刻意不開放給客戶調（見 risk_prefs.py 的論證）
    assert not any("cost" in n for n in names)


def test_post_risk_persists_and_reads_back(tmp_path):
    app, cfg, *_ = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = _make_pending(cfg, wallet.address)
    r = client.post("/api/me/risk", json={"prefs": {
        "enabled": True, "max_drawdown_pct": "0.25",
        "max_total_drawdown_pct": "0.5", "flatten_on_breach": False}})
    assert r.status_code == 200, r.text
    assert r.json()["effective"] == "at_activation"
    assert client.get("/api/me/risk").json()["prefs"] == {
        "enabled": True, "max_drawdown_pct": "0.25",
        "max_total_drawdown_pct": "0.5", "flatten_on_breach": False}
    entry = next(e for e in load_pending(cfg.pending_path)
                 if e["account_id"] == account_id)
    assert entry["risk"]["enabled"] is True


def test_post_risk_out_of_range_is_400(tmp_path):
    app, cfg, *_ = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    _make_pending(cfg, wallet.address)
    r = client.post("/api/me/risk",
                    json={"prefs": {"enabled": True, "max_drawdown_pct": "0.001"}})
    assert r.status_code == 400
    assert "0.05" in r.json()["detail"]      # 訊息要說得出合法區間


def test_post_risk_when_not_pending_is_409_not_a_silent_success(tmp_path):
    """⭐ 已啟用（或尚未綁定）的帳號：明確 409。靜默 200 會讓客戶以為改好了，
    而引擎的 env 從頭到尾沒被動過——沉默的無效比不給選更糟。"""
    app, cfg, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    r = client.post("/api/me/risk", json={"prefs": {"enabled": True}})
    assert r.status_code == 409
    g = client.get("/api/me/risk").json()
    assert g["editable"] is False and g["not_editable_reason"] == "not_pending"
    assert "聯絡我們" in g["not_editable_note"]


def test_risk_prefs_are_isolated_between_users(tmp_path):
    """紅線：account 由 session 衍生——沒有任何請求參數能指到別人的帳號。"""
    app, cfg, *_ = make_app(tmp_path)
    c1, c2 = _client(app), _client(app)
    w1 = login(c1)
    w2 = login(c2)
    _make_pending(cfg, w1.address)
    _make_pending(cfg, w2.address)
    c1.post("/api/me/risk", json={"prefs": {"enabled": True}})
    assert c2.get("/api/me/risk").json()["prefs"]["enabled"] is False


def test_risk_endpoints_require_session(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    assert client.get("/api/me/risk").status_code == 401
    assert client.post("/api/me/risk", json={"prefs": {}}).status_code == 401


def test_summary_never_leaks_extra_keys():
    """回應的 prefs 只有宣告過的鍵（前端據此比對「已儲存 vs 表單現值」）。"""
    assert set(prefs_summary(None)["prefs"]) == {
        "enabled", "max_drawdown_pct", "max_total_drawdown_pct", "flatten_on_breach"}
