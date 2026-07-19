"""tests/test_api_me_capital.py — GET /api/me/capital（客戶查自己目前生效的資金設定）。

盯住四件事：
(1) ⭐⭐ **「已提交」與「已生效」分得開**——把記錄當成生效值回傳，會讓一個把使用比例
    從 1.0 調到 0.2 的客戶以為曝險已經降下來了，而實際上一點都沒變。
(2) ⭐ 生效值的唯一來源是**引擎發布的健康心跳**（引擎真正拿去乘部位大小的那組值）；
    心跳缺席／過期 → 明確的「未知」，絕不退回一個看起來合理的預設值。
(3) ⭐ **只查得到自己的**（session 隔離，且結構上沒有 account 參數）。
(4) signature／待簽原文不外流。

沿 tests/test_api_me_leader.py 的形狀。全離線。
"""
import json
import socket

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.capital_settings import (build_capital_settings_record,
                                          capital_settings_path_for,
                                          write_capital_settings)
from spark.filet.engine_health import (HEARTBEAT_STALE_S, build_heartbeat,
                                       heartbeat_path_for, write_heartbeat)
from tests.publicapi_helpers import login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


BUILDER = "0x" + "b1" * 20
_LEADER = "0x" + "d4" * 20
# 132 字元的假簽章：長度與形狀比照真簽章，這樣「有沒有外流」才驗得出來。
_FAKE_SIG = "0x" + "ab" * 65


def acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def make_me_app(tmp_path, *, followers=None, manifest=None):
    cfg = make_cfg(
        tmp_path,
        followers_path=manifest if manifest is not None else str(
            _write_manifest(tmp_path, followers or [])))
    app, *_ = make_app(tmp_path, cfg=cfg)
    return app, cfg


def _write_manifest(tmp_path, followers):
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": followers}))
    return p


def follower(wallet, **over):
    f = {"account_id": acct(wallet), "user_address": wallet.address.lower(),
         "builder_address": BUILDER, "network": "testnet", "label": "t",
         "leader_address": _LEADER}
    f.update(over)
    return f


def write_hb(cfg, account_id, *, alloc="1000.00", util="0.5000", full=False,
             source="customer_signed", changed_at="2026-07-19T03:00:00+00:00",
             age_s=5.0):
    """以引擎的**同一個產生器**寫一份心跳（不自己拼 JSON——自己拼的話，寫端改了
    欄位這裡不會紅，端點測試就變成一份與現實無關的自證）。"""
    import time

    payload = build_heartbeat(
        account_id=account_id, now_s=time.time() - age_s, killswitch_tripped=False,
        coverage=None, leader_address=_LEADER, leader_source="manifest",
        allocated_capital=alloc, capital_utilization=util, use_full_equity=full,
        capital_source=source, capital_changed_at=changed_at,
        cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, account_id), payload)


def write_record(cfg, account_id, *, alloc="5000.00", util="0.2000", full=False,
                 issued_at="2026-07-19T04:00:00Z", nonce="c1"):
    """客戶已提交（API 已收）但引擎未必套用的一筆記錄——含一段假簽章供外流測試。"""
    rec = build_capital_settings_record(
        account_id=account_id, allocated_capital=alloc, capital_utilization=util,
        nonce=nonce, issued_at=issued_at, signature=_FAKE_SIG,
        message="Filet: update copy-trading capital allocation\n...",
        use_full_equity=full)
    write_capital_settings(capital_settings_path_for(cfg.exchange_dir), rec)


def _logged_in(tmp_path, *, activated=True, extra_followers=()):
    """回 (client, cfg, wallet)；`activated=False` ＝ manifest 裡沒有這個帳號。"""
    wallet = Account.create()
    rows = list(extra_followers) + ([follower(wallet)] if activated else [])
    app, cfg = make_me_app(tmp_path, followers=rows)
    client = _client(app)
    login(client, wallet=wallet)
    return client, cfg, wallet


# ── 授權 ──────────────────────────────────────────────────────────────

def test_requires_session(tmp_path):
    app, _cfg = make_me_app(tmp_path)
    assert _client(app).get("/api/me/capital").status_code == 401


def test_has_no_account_parameter_to_query_someone_else(tmp_path):
    """⭐ 結構性：端點沒有 account 參數，多送的 query string 一律被忽略。

    「只回自己的」不是靠一行 filter，是靠 account_id 只可能來自 session。
    """
    other = Account.create()
    client, cfg, wallet = _logged_in(tmp_path, extra_followers=[follower(other)])
    write_hb(cfg, acct(wallet), alloc="1000.00")
    write_hb(cfg, acct(other), alloc="99999.00")

    body = client.get("/api/me/capital",
                      params={"account_id": acct(other)}).json()
    assert body["account_id"] == acct(wallet)
    assert body["effective"]["allocated_capital"] == "1000.00"
    assert "99999.00" not in json.dumps(body)


def test_two_customers_see_only_their_own(tmp_path):
    """兩個帳號的心跳與記錄都在，各自只看得到自己的那一份。"""
    a, b = Account.create(), Account.create()
    app, cfg = make_me_app(tmp_path, followers=[follower(a), follower(b)])
    write_hb(cfg, acct(a), alloc="1000.00", util="0.5000")
    write_hb(cfg, acct(b), alloc="7777.00", util="0.9000")
    write_record(cfg, acct(b), alloc="8888.00", nonce="cb")

    ca, cb = _client(app), _client(app)
    login(ca, wallet=a)
    login(cb, wallet=b)

    ba, bb = ca.get("/api/me/capital").json(), cb.get("/api/me/capital").json()
    assert ba["effective"]["allocated_capital"] == "1000.00"
    assert ba["pending"] is None                    # B 的待套用記錄不在 A 的回應裡
    assert "8888.00" not in json.dumps(ba) and "7777.00" not in json.dumps(ba)
    assert bb["effective"]["allocated_capital"] == "7777.00"
    assert bb["pending"]["allocated_capital"] == "8888.00"


# ── 生效值：來源是心跳，來源標記要說得出來 ────────────────────────────

def test_reports_effective_settings_and_source(tmp_path):
    """⭐ 生效值 ＋ 來源（客戶簽章授權）＋ 上次變更時刻，一次到位。

    這三格正是 `/capital` 頁「前後對照」的左半邊；缺了它，客戶只能在不知道現況的
    情況下按下一次改變曝險倍數的簽名。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="1000.00", util="0.5000",
             source="customer_signed", changed_at="2026-07-19T03:00:00+00:00")

    body = client.get("/api/me/capital").json()
    assert body["status"] == "effective"
    assert body["effective"]["allocated_capital"] == "1000.00"
    assert body["effective"]["capital_utilization"] == "0.5000"
    assert body["effective"]["use_full_equity"] is False
    assert body["effective"]["source"] == "customer_signed"
    assert body["effective"]["changed_at"] == "2026-07-19T03:00:00+00:00"
    assert body["effective"]["as_of"] is not None      # 這組值是何時回報的
    assert body["heartbeat"]["status"] == "ok"
    assert body["heartbeat"]["stale_after_s"] == HEARTBEAT_STALE_S


def test_env_default_source_is_distinguished_from_customer_signed(tmp_path):
    """⭐ 「沿用部署預設」與「你自己簽過」必須分得開，且前者沒有變更時刻。

    分不開的話，客戶無從知道他到底簽過沒有——而「我以為我簽過了」正是這個頁面
    存在要解決的問題。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), source="env_default", changed_at=None)

    body = client.get("/api/me/capital").json()
    assert body["status"] == "effective"
    assert body["effective"]["source"] == "env_default"
    assert body["effective"]["changed_at"] is None


def test_full_equity_mode_is_reported_as_a_flag(tmp_path):
    """全部權益模式：旗標為 True 且金額為 0（兩者是配對的，不是「金額被忽略」）。"""
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="0.00", util="1.0000", full=True)

    eff = client.get("/api/me/capital").json()["effective"]
    assert eff["use_full_equity"] is True and eff["allocated_capital"] == "0.00"


# ── ⭐⭐ 已提交 vs 已生效 ─────────────────────────────────────────────

def test_pending_and_effective_are_both_shown_and_distinguishable(tmp_path):
    """⭐⭐ 本檔最重要的一條：提交值與生效值不同時，**兩者同時呈現且標示清楚**。

    拿掉 `_capital_pending` 的指紋比對，本測試會紅——而正式環境的後果是二選一：
    - 提交值被當成生效值回傳 ⇒ 一個把使用比例從 0.5 調到 0.2 的客戶以為曝險已經
      降下來了，實際上一點都沒變（他可能因此在錯誤的安全感下加碼）。
    - 或已生效的那筆被永遠標成「處理中」⇒ 客戶學會忽略這個提示。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="1000.00", util="0.5000")     # 引擎在用的
    write_record(cfg, acct(wallet), alloc="5000.00", util="0.2000",  # 客戶剛簽的
                 issued_at="2026-07-19T04:00:00Z")

    body = client.get("/api/me/capital").json()

    # 生效值不受待套用記錄影響
    assert body["status"] == "effective"
    assert body["effective"]["allocated_capital"] == "1000.00"
    assert body["effective"]["capital_utilization"] == "0.5000"
    # 待套用的那一筆同時呈現，且明確標示尚未生效
    assert body["pending"] is not None
    assert body["pending"]["allocated_capital"] == "5000.00"
    assert body["pending"]["capital_utilization"] == "0.2000"
    assert body["pending"]["state"] == "not_yet_applied"
    assert body["pending"]["submitted_at"] == "2026-07-19T04:00:00Z"
    assert body["pending"]["effective_when"] == "next_engine_cycle"


def test_applied_record_is_not_reported_as_pending(tmp_path):
    """⭐ 引擎已套用之後，殘留的記錄檔**不得**繼續顯示成「處理中」。

    `write_capital_settings` 是同 account 覆蓋而非流水帳，記錄套用後仍留在檔案裡；
    照單全收會讓客戶永遠看到一個早就生效的「處理中」，久了他會學會忽略它。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="5000.00", util="0.2000")
    write_record(cfg, acct(wallet), alloc="5000.00", util="0.2000")

    body = client.get("/api/me/capital").json()
    assert body["pending"] is None
    assert body["effective"]["allocated_capital"] == "5000.00"


def test_only_the_flag_differs_still_counts_as_pending(tmp_path):
    """⭐ 金額與比例相同、只有 `use_full_equity` 不同 ⇒ 仍是待套用。

    旗標決定曝險基準（固定本金 vs 整個帳戶）。指紋若漏掉它，一次基準的變更會被
    當成「已生效」而消失在畫面上。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="0.00", util="1.0000", full=False)
    write_record(cfg, acct(wallet), alloc="0.00", util="1.0000", full=True)

    body = client.get("/api/me/capital").json()
    assert body["pending"] is not None
    assert body["pending"]["use_full_equity"] is True
    assert body["effective"]["use_full_equity"] is False


# ── ⭐ 生效值不可知時，提交值絕不冒充生效值 ──────────────────────────

def test_stale_heartbeat_makes_the_pending_unconfirmed_not_effective(tmp_path):
    """⭐⭐ 心跳過期 → 生效值「未知」，提交值歸入 pending 且標成 `unconfirmed`。

    這裡有兩個絕不能發生的錯：把過期心跳的值當成生效值（謊報現況），或把提交值
    當成生效值（謊報「你改的已經生效了」）。兩者都會讓客戶對自己的曝險做出錯誤判斷。
    `unconfirmed` 與 `not_yet_applied` 刻意分開：前者代表引擎那邊可能出了事，
    後者可以安心等下一個 cycle——處置完全不同。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="1000.00", util="0.5000",
             age_s=HEARTBEAT_STALE_S + 60)
    write_record(cfg, acct(wallet), alloc="5000.00", util="0.2000")

    body = client.get("/api/me/capital").json()
    assert body["status"] == "unknown"
    assert body["effective"] is None                      # 過期的值不是現況
    assert body["heartbeat"]["status"] == "stale"
    assert body["heartbeat"]["age_s"] > HEARTBEAT_STALE_S
    assert body["pending"]["state"] == "unconfirmed"      # 也不是「已生效」
    assert body["pending"]["allocated_capital"] == "5000.00"
    assert "1000.00" not in json.dumps(body), "過期心跳的值不得出現在回應裡"


def test_missing_heartbeat_is_unknown_not_a_plausible_default(tmp_path):
    """⭐ 沒有心跳 → `unknown` ＋ effective=null，**不是**一組看起來合理的預設值。"""
    client, cfg, wallet = _logged_in(tmp_path)

    body = client.get("/api/me/capital").json()
    assert body["status"] == "unknown"
    assert body["effective"] is None
    assert body["heartbeat"]["status"] == "missing"
    assert body["pending"] is None                        # 沒提交過就沒有 pending
    assert body["note"]


def test_engine_reporting_unavailable_capital_is_unknown(tmp_path):
    """引擎本輪無法判定資金設定（帳本遺失／超界）→ 心跳是新的，但生效值仍是未知。

    心跳新鮮**不等於**生效值可知；把 `source="unavailable"` 當成一組值讀出來，
    等於回報一組本輪根本沒被拿去下單的數字。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc=None, util=None, source="unavailable",
             changed_at=None)

    body = client.get("/api/me/capital").json()
    assert body["heartbeat"]["status"] == "ok"
    assert body["status"] == "unknown" and body["effective"] is None


# ── 查不到時的語意 ────────────────────────────────────────────────────

def test_not_activated_has_explicit_semantics(tmp_path):
    """尚未活化 → `not_activated` ＋ 說明，不回 null 讓前端猜。"""
    client, cfg, wallet = _logged_in(tmp_path, activated=False)

    body = client.get("/api/me/capital").json()
    assert body["status"] == "not_activated"
    assert body["effective"] is None and body["heartbeat"] is None
    assert "尚未啟用" in body["note"]


def test_not_activated_still_surfaces_an_already_submitted_setting(tmp_path):
    """尚未活化但已簽過一筆（POST 不要求活化）→ 照實回報為 pending／unconfirmed。

    假裝沒有這筆記錄比較「乾淨」，但客戶確實簽了，他有權知道它還躺在那裡。
    """
    client, cfg, wallet = _logged_in(tmp_path, activated=False)
    write_record(cfg, acct(wallet), alloc="5000.00", util="0.2000")

    body = client.get("/api/me/capital").json()
    assert body["status"] == "not_activated"
    assert body["pending"]["state"] == "unconfirmed"
    assert body["pending"]["allocated_capital"] == "5000.00"


def test_indeterminate_when_the_manifest_has_unparsable_entries(tmp_path):
    """⭐ 帳號不在 manifest **且** manifest 有壞條目 → `indeterminate`。

    回 `not_activated` 會讓一個正在跟單的客戶以為自己沒在跟單——壞掉的那筆
    可能就是他自己的（危險方向的誤讀）。
    """
    wallet = Account.create()
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": [{"account_id": "broken"}]}))
    app, cfg = make_me_app(tmp_path, manifest=str(p))
    client = _client(app)
    login(client, wallet=wallet)

    body = client.get("/api/me/capital").json()
    assert body["status"] == "indeterminate"
    assert "不要當作" in body["note"]


def test_corrupt_capital_record_is_not_reported_as_pending(tmp_path):
    """記錄格式壞（引擎也會拒絕它）→ 不宣稱有一筆處理中的變更。

    宣稱有，客戶就會一直等一個永遠不會來的生效。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet))
    p = capital_settings_path_for(cfg.exchange_dir)
    from pathlib import Path
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps({"settings": [
        {"account_id": acct(wallet), "allocated_capital": "not-a-number",
         "capital_utilization": "0.2"}]}))

    assert client.get("/api/me/capital").json()["pending"] is None


# ── ⭐ 機密不外流 ────────────────────────────────────────────────────

def test_response_never_contains_signature_material(tmp_path):
    """⭐ 記錄裡的 signature／原文／nonce 一個都不得出現在回應裡。

    `_load_own_capital_record` 只投影安全欄位，所以這些東西**結構上**到不了回應
    ——但這條測試要能在有人把投影改成「原樣回傳整筆記錄」時立刻紅。
    """
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet))
    write_record(cfg, acct(wallet), nonce="secret-nonce-xyz")

    raw = client.get("/api/me/capital").text
    assert _FAKE_SIG not in raw
    assert "secret-nonce-xyz" not in raw
    for key in ("signature", "message", "nonce"):
        assert f'"{key}"' not in raw


def test_real_signed_submission_round_trips_without_leaking(tmp_path):
    """走完一次**真實簽章**的 POST 之後再 GET：待套用的值看得到，簽章看不到。"""
    client, cfg, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), alloc="1000.00", util="0.5000")

    r = client.get("/api/me/capital/message",
                   params={"allocated_capital": "5000", "capital_utilization": "0.2"})
    assert r.status_code == 200, r.text
    m = r.json()
    sig = wallet.sign_message(encode_defunct(text=m["message"])).signature.hex()
    r = client.post("/api/me/capital", json={
        "account_id": m["account_id"], "allocated_capital": m["allocated_capital"],
        "capital_utilization": m["capital_utilization"],
        "use_full_equity": m["use_full_equity"], "nonce": m["nonce"],
        "issued_at": m["issued_at"], "signature": sig, "message": m["message"]})
    assert r.status_code == 200, r.text

    body = client.get("/api/me/capital")
    assert body.json()["pending"]["allocated_capital"] == "5000.00"
    assert body.json()["pending"]["state"] == "not_yet_applied"
    assert body.json()["effective"]["allocated_capital"] == "1000.00"
    assert sig not in body.text and m["nonce"] not in body.text
