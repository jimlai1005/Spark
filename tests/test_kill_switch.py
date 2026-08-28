"""tests/test_kill_switch.py
Owner kill switch（Task 15）：暫停（第一級）＋簽章「平倉並撤銷」（第二級）。

四層各自的測試群：
(A) `spark.filet.pause_flag`——路徑慣例、寫入、引擎側 fail-safe 讀取（IO/格式
    失敗視為暫停 ＋ critical）。
(B) `spark.copytrade.loop.run_cycle`——`settings.paused` 併入既有 `no_new_exposure`
    旗標（與成本熔斷器同一個「只減不開」語意），跳過開倉、放行減倉。
(C) `spark.filet.close_all`——待簽原文、驗章（含域分隔、時效、偽造 nonce）。
(D) `spark.filet.close_all_apply.CloseAllApplier`——引擎每輪消化請求，命中即觸發
    既有 `killswitch.trip`（reason=owner_close），冪等靠 `is_tripped`。
(E) `src/spark/publicapi/app.py`——`POST /api/me/pause`、
    `GET/POST /api/me/close-all(/message)` 端點。
(F) `scripts/run_copytrade.py`——接線版：簽章請求真的一路走到 `killswitch.trip`
    寫下 ARM 檔；壞簽章不觸發。

全離線（tests/conftest.py 的 autouse socket-ban；TestClient 段落沿
test_api_risk_settings.py 的既有 `_allow_local_sockets` fixture 放行 loopback）。
"""
import json
import socket
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import spark.copytrade.loop as loop_mod
import scripts.run_copytrade as rc
from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor
from spark.copytrade.killswitch import ARM_FILE_RELPATH, is_tripped
from spark.copytrade.loop import run_cycle
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import CycleReport, ReconcileResult, ReconcileState
from spark.exchange.base import AccountSnapshot, BuilderCode, EquityView
from spark.exchange.fakes import FakeAdapter
from spark.filet.close_all import (CloseAllError, build_close_all_message,
                                   build_close_all_record, close_all_path_for,
                                   load_close_all_requests, verify_close_all,
                                   write_close_all_request)
from spark.filet.close_all_apply import CloseAllApplier
from spark.filet.pause_flag import (pause_flag_path_for,
                                    read_pause_flag_for_engine, write_pause_flag)
from spark.filet.leader_change import build_leader_change_message

_LEADER = "0x" + "a1" * 20
_BUILDER = "0x" + "22" * 20
_NOW = 1_800_000_000.0


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _account(value="1000", ntl="0") -> AccountSnapshot:
    return AccountSnapshot(account_value=Decimal(value), total_margin_used=Decimal("0"),
                           withdrawable=Decimal(value), total_ntl_pos=Decimal(ntl))


def _healthy_equity() -> EquityView:
    return EquityView(current=Decimal("1000"), recent_peak=Decimal("1000"))


# ══════════════════════════════════════════════════════════════════════
# (A) pause_flag：路徑慣例、寫入、引擎側 fail-safe 讀取
# ══════════════════════════════════════════════════════════════════════

def test_pause_flag_write_then_read_true(tmp_path):
    path = pause_flag_path_for(str(tmp_path), "0xabc")
    write_pause_flag(path, paused=True, now_s=_NOW)
    n = RecordingNotifier()
    assert read_pause_flag_for_engine(str(tmp_path), "0xabc", n) is True
    assert n.records == []  # 正常讀取不告警


def test_pause_flag_write_then_read_false(tmp_path):
    path = pause_flag_path_for(str(tmp_path), "0xabc")
    write_pause_flag(path, paused=False, now_s=_NOW)
    n = RecordingNotifier()
    assert read_pause_flag_for_engine(str(tmp_path), "0xabc", n) is False


def test_pause_flag_missing_file_reads_as_not_paused(tmp_path):
    n = RecordingNotifier()
    assert read_pause_flag_for_engine(str(tmp_path), "0xabc", n) is False
    assert n.records == []


def test_pause_flag_malformed_json_is_fail_safe_paused_with_critical(tmp_path):
    p = Path(pause_flag_path_for(str(tmp_path), "0xabc"))
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    n = RecordingNotifier()
    assert read_pause_flag_for_engine(str(tmp_path), "0xabc", n) is True
    crits = [r for r in n.records if r[0] == "critical"]
    assert len(crits) == 1
    assert crits[0][3] == "pause_flag_unreadable"


def test_pause_flag_non_dict_json_is_fail_safe_paused_with_critical(tmp_path):
    p = Path(pause_flag_path_for(str(tmp_path), "0xabc"))
    p.parent.mkdir(parents=True)
    p.write_text("[1, 2, 3]")
    n = RecordingNotifier()
    assert read_pause_flag_for_engine(str(tmp_path), "0xabc", n) is True
    crits = [r for r in n.records if r[0] == "critical"]
    assert crits[0][3] == "pause_flag_malformed"


def test_pause_flag_path_is_keyed_by_address_not_account_id(tmp_path):
    """路徑約定就是 `<exchange_dir>/<user_address>/pause.json`（寫端 app.py 與
    讀端引擎必須用同一個推導——本測試釘住形狀本身，防止兩端各拼一份而漂移）。"""
    assert pause_flag_path_for(str(tmp_path), "0xDEAD") == str(
        tmp_path / "0xDEAD" / "pause.json")


# ══════════════════════════════════════════════════════════════════════
# (B) run_cycle：settings.paused 併入 no_new_exposure（跳開倉、放行減倉）
# ══════════════════════════════════════════════════════════════════════

def _settings(**kw) -> CopySettings:
    kw.setdefault("volatility_weight_enabled", False)
    return CopySettings(**kw)


def _executor(adapter, *, live=False) -> ActionExecutor:
    return ActionExecutor(adapter, "SIGNER" if live else None,
                          BuilderCode(b=_BUILDER, f=20), live=live,
                          my_address="0xme", settings=_settings())


def _run_cycle(adapter, root, *, settings=None, notifier=None, ex=None):
    n = notifier or RecordingNotifier()
    e = ex or _executor(adapter)
    report = run_cycle(adapter, e, settings or _settings(), n, ReconcileState(), root)
    return report, n, e


def test_paused_sets_no_new_exposure_on_both_gates(tmp_path, monkeypatch):
    """⭐⭐ `settings.paused=True`（成本熔斷器未觸發）→ 掛單對帳與部位安全網
    兩處都收到 `no_new_exposure=True`——與成本熔斷器共用同一個「只減不開」語意
    （engine 級證據：不是分別各自兩套判斷）。"""
    captured_orders, captured_positions = {}, {}

    def fake_sync_open_orders(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured_orders.update(kw)
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net=kw["safety_net"](), scale=scale)

    def fake_sync_positions(ex, leader_positions, my_positions, scale, **kw):
        captured_positions.update(kw)
        return {"opened": [], "flattened": [], "failed": []}

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync_open_orders)
    monkeypatch.setattr(loop_mod, "sync_positions", fake_sync_positions)

    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"))
    _run_cycle(fa, tmp_path, settings=_settings(paused=True))

    assert captured_orders["no_new_exposure"] is True
    assert captured_positions["no_new_exposure"] is True


def test_not_paused_leaves_both_gates_open(tmp_path, monkeypatch):
    """對照組：`paused=False` 且成本熔斷器未觸發 → 兩處皆 `no_new_exposure=False`
    （沒有暫停旗標時，行為與 Task 15 之前完全相同）。"""
    captured = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured.update(kw)
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net={}, scale=scale)

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"))
    _run_cycle(fa, tmp_path, settings=_settings(paused=False))
    assert captured["no_new_exposure"] is False


def test_paused_does_not_trip_or_write_arm_file(tmp_path):
    """暫停不是熔斷：不寫 ARM 檔、不進入 tripped 狀態（純粹跳開倉，不是鎖死交易）。"""
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"))
    report, _n, _ex = _run_cycle(fa, tmp_path, settings=_settings(paused=True))
    assert report.tripped is False
    assert not is_tripped(tmp_path)


# ══════════════════════════════════════════════════════════════════════
# (C) close_all：待簽原文、驗章（域分隔／時效／偽造 nonce）
# ══════════════════════════════════════════════════════════════════════

def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _sign_close_all(wallet, *, account_id, nonce="n1", issued_at=None):
    issued_at = issued_at or _at()
    msg = build_close_all_message(account_id=account_id, nonce=nonce,
                                  issued_at=issued_at)
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    return build_close_all_record(account_id=account_id, nonce=nonce,
                                  issued_at=issued_at, signature=sig, message=msg), msg


def test_message_domain_literal_is_unique_and_irreversible():
    """第一行是與另外四個既有模板（換 leader／資金／風控／解除熔斷）都不同的
    固定字面量——域分隔的結構性基礎。"""
    msg = build_close_all_message(account_id="fabc", nonce="n1", issued_at=_at())
    assert msg.startswith("Filet: close all positions and revoke copy-trading")
    other_first_lines = {
        build_leader_change_message(account_id="fabc", leader_address=_LEADER,
                                    nonce="n1", issued_at=_at()).splitlines()[0],
        "Filet: update copy-trading capital allocation",
        "Filet: update copy-trading risk settings",
        "Filet: resume copy-trading after a risk halt",
    }
    assert msg.splitlines()[0] not in other_first_lines
    assert "irreversible" in msg
    assert "Hyperliquid" in msg  # 明講不代發鏈上撤銷，需自行至官方介面


def test_verify_close_all_happy_path():
    wallet = Account.create()
    account_id = _acct(wallet)
    rec, _msg = _sign_close_all(wallet, account_id=account_id)
    verified = verify_close_all(rec, account_id=account_id,
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=lambda n: True)
    assert verified.account_id == account_id
    assert verified.user_address == wallet.address.lower()


def test_verify_close_all_wrong_action_rejected():
    wallet = Account.create()
    account_id = _acct(wallet)
    rec, _ = _sign_close_all(wallet, account_id=account_id)
    rec["action"] = "risk_settings"  # 挪用其他域的動作標籤
    with pytest.raises(CloseAllError) as e:
        verify_close_all(rec, account_id=account_id, user_address=wallet.address,
                         now_s=_NOW, consume_nonce=lambda n: True)
    assert e.value.reason == "action_mismatch"


def test_verify_close_all_expired_rejected():
    wallet = Account.create()
    account_id = _acct(wallet)
    rec, _ = _sign_close_all(wallet, account_id=account_id,
                             issued_at=_at(-700))  # > 600s 上限
    with pytest.raises(CloseAllError) as e:
        verify_close_all(rec, account_id=account_id, user_address=wallet.address,
                         now_s=_NOW, consume_nonce=lambda n: True)
    assert e.value.reason == "expired"


def test_verify_close_all_wrong_signer_rejected():
    wallet = Account.create()
    someone_else = Account.create()
    account_id = _acct(wallet)
    rec, _ = _sign_close_all(someone_else, account_id=account_id)
    with pytest.raises(CloseAllError) as e:
        verify_close_all(rec, account_id=account_id, user_address=wallet.address,
                         now_s=_NOW, consume_nonce=lambda n: True)
    assert e.value.reason == "signer_mismatch"


def test_verify_close_all_forged_nonce_rejected():
    """他域 nonce（偽造、從未由伺服器發放過）→ `consume_nonce` 回 False →
    `nonce_unusable`。模擬「拿別的端點發的 nonce 硬套進來」的攻擊：consume_nonce
    的呼叫端（API 層）只認自己核發、屬同一位址同一 chain_id 的 nonce。"""
    wallet = Account.create()
    account_id = _acct(wallet)
    rec, _ = _sign_close_all(wallet, account_id=account_id, nonce="forged-nonce")
    with pytest.raises(CloseAllError) as e:
        verify_close_all(rec, account_id=account_id, user_address=wallet.address,
                         now_s=_NOW, consume_nonce=lambda n: False)
    assert e.value.reason == "nonce_unusable"


def test_verify_close_all_account_mismatch_rejected():
    wallet = Account.create()
    account_id = _acct(wallet)
    rec, _ = _sign_close_all(wallet, account_id=account_id)
    with pytest.raises(CloseAllError) as e:
        verify_close_all(rec, account_id="fdeadbeef", user_address=wallet.address,
                         now_s=_NOW, consume_nonce=lambda n: True)
    assert e.value.reason == "account_mismatch"


def test_write_close_all_request_same_account_overwrites(tmp_path):
    """同 account_id 覆蓋而非附加（檔案代表「目前有沒有一筆待處理的請求」，
    不是流水帳，同 `write_risk_settings` 的既有慣例）。"""
    path = tmp_path / "owner_close.json"
    wallet = Account.create()
    account_id = _acct(wallet)
    rec1, _ = _sign_close_all(wallet, account_id=account_id, nonce="n1")
    write_close_all_request(path, rec1)
    rec2, _ = _sign_close_all(wallet, account_id=account_id, nonce="n2")
    write_close_all_request(path, rec2)
    entries = load_close_all_requests(path)
    assert len(entries) == 1
    assert entries[0]["nonce"] == "n2"


# ══════════════════════════════════════════════════════════════════════
# (D) CloseAllApplier：引擎每輪消化請求 → 觸發既有收尾路徑
# ══════════════════════════════════════════════════════════════════════

class _Ex:
    my_address = "0xme"

    def __init__(self):
        self.calls = []

    def get_open_orders(self):
        return []

    def cancel(self, coin, oid):
        self.calls.append(("cancel", coin, oid))
        return True

    def close_reduce_only(self, coin, is_buy, size, *, emergency=False):
        from spark.exchange.base import OrderResult
        self.calls.append(("close", coin, is_buy, size, emergency))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("100"), raw={})


class _Adapter:
    def get_positions(self, address):
        return []


def _manifest(tmp_path, *, account_id, user_address):
    m = tmp_path / "followers.json"
    m.write_text(json.dumps({"followers": [
        {"account_id": account_id, "user_address": user_address,
         "builder_address": _BUILDER, "network": "mainnet", "label": ""}]}))
    return m


def _applier(tmp_path, *, account_id, manifest_path, now_s=_NOW) -> CloseAllApplier:
    return CloseAllApplier(account_id=account_id, manifest_path=manifest_path,
                           request_path=tmp_path / "owner_close.json",
                           notifier=RecordingNotifier(), now_fn=lambda: now_s)


def _wind_down_recorder():
    calls = []

    def _wind_down():
        calls.append(True)

    return _wind_down, calls


def test_close_all_applier_valid_request_triggers_wind_down(tmp_path):
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    rec, _ = _sign_close_all(wallet, account_id=account_id)
    write_close_all_request(tmp_path / "owner_close.json", rec)

    applier = _applier(tmp_path, account_id=account_id, manifest_path=manifest)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is True
    assert calls == [True]


def test_close_all_applier_already_tripped_is_idempotent(tmp_path):
    """已經 tripped（不論原因）→ 不重複觸發（`is_tripped` 短路，見 docstring）。"""
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    rec, _ = _sign_close_all(wallet, account_id=account_id)
    write_close_all_request(tmp_path / "owner_close.json", rec)
    arm = tmp_path / ARM_FILE_RELPATH
    arm.parent.mkdir(parents=True)
    arm.write_text(json.dumps({"tripped_at": _at(), "reason": "drawdown"}))

    applier = _applier(tmp_path, account_id=account_id, manifest_path=manifest)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is False
    assert calls == []


def test_close_all_applier_bad_signature_does_not_trigger_and_alerts(tmp_path):
    wallet = Account.create()
    forger = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    rec, _ = _sign_close_all(forger, account_id=account_id)  # 別人簽的
    write_close_all_request(tmp_path / "owner_close.json", rec)

    notifier = RecordingNotifier()
    applier = CloseAllApplier(account_id=account_id, manifest_path=manifest,
                              request_path=tmp_path / "owner_close.json",
                              notifier=notifier, now_fn=lambda: _NOW)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is False
    assert calls == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert any("close_all_verify_failed" in (r[3] or "") for r in crits)


def test_close_all_applier_expired_request_no_critical_only_quiet_skip(tmp_path):
    """過期是預期會發生的常態（記錄沒人清）——只 log，不 critical 洗版。"""
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    rec, _ = _sign_close_all(wallet, account_id=account_id, issued_at=_at(-700))
    write_close_all_request(tmp_path / "owner_close.json", rec)

    notifier = RecordingNotifier()
    applier = CloseAllApplier(account_id=account_id, manifest_path=manifest,
                              request_path=tmp_path / "owner_close.json",
                              notifier=notifier, now_fn=lambda: _NOW)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is False
    assert calls == []
    assert notifier.records == []


def test_close_all_applier_no_manifest_entry_alerts_and_skips(tmp_path):
    wallet = Account.create()
    account_id = _acct(wallet)
    other_manifest = _manifest(tmp_path, account_id="fother",
                               user_address=Account.create().address)
    rec, _ = _sign_close_all(wallet, account_id=account_id)
    write_close_all_request(tmp_path / "owner_close.json", rec)

    notifier = RecordingNotifier()
    applier = CloseAllApplier(account_id=account_id, manifest_path=other_manifest,
                              request_path=tmp_path / "owner_close.json",
                              notifier=notifier, now_fn=lambda: _NOW)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is False
    assert calls == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert any(r[3] == "close_all_no_trusted_user" for r in crits)


def test_close_all_applier_no_request_is_a_quiet_noop(tmp_path):
    """最常見路徑：帳號沒有請求 → 完全安靜、不觸發。"""
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    notifier = RecordingNotifier()
    applier = CloseAllApplier(account_id=account_id, manifest_path=manifest,
                              request_path=tmp_path / "owner_close.json",
                              notifier=notifier, now_fn=lambda: _NOW)
    wind_down, calls = _wind_down_recorder()
    assert applier.consume(tmp_path, wind_down) is False
    assert calls == []
    assert notifier.records == []


# ══════════════════════════════════════════════════════════════════════
# (E) publicapi/app.py：POST /api/me/pause、GET/POST /api/me/close-all
# ══════════════════════════════════════════════════════════════════════

from fastapi.testclient import TestClient  # noqa: E402 — 沿 test_api_risk_settings 慣例

from tests.publicapi_helpers import login, make_app  # noqa: E402

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


@pytest.fixture
def client_wallet(tmp_path):
    app, cfg, _store, _keysvc, _hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)
    return client, cfg, wallet


def test_pause_requires_session(tmp_path):
    app, _cfg, _store, _keysvc, _hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/api/me/pause", json={"action": "pause"})
    assert r.status_code == 401


def test_pause_invalid_action_rejected(client_wallet):
    client, _cfg, _wallet = client_wallet
    r = client.post("/api/me/pause", json={"action": "bogus"})
    assert r.status_code == 400


def test_pause_then_resume_roundtrip_via_engine_reader(client_wallet):
    """端點寫下的旗標必須是引擎讀端（同一個路徑推導）讀得懂的那份——寫端與讀端
    不可能各拼一份路徑（工程原則 1，見 pause_flag.py 檔頭）。"""
    client, cfg, wallet = client_wallet
    r = client.post("/api/me/pause", json={"action": "pause"})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True
    n = RecordingNotifier()
    assert read_pause_flag_for_engine(cfg.exchange_dir, wallet.address.lower(), n) is True

    r = client.post("/api/me/pause", json={"action": "resume"})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is False
    assert read_pause_flag_for_engine(cfg.exchange_dir, wallet.address.lower(), n) is False


def test_close_all_message_requires_session(tmp_path):
    app, _cfg, _store, _keysvc, _hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/me/close-all/message").status_code == 401


def test_close_all_submit_requires_session(tmp_path):
    app, _cfg, _store, _keysvc, _hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/api/me/close-all", json={
        "account_id": "fabc", "nonce": "n1", "issued_at": _at(),
        "signature": "0x00", "message": "x"})
    assert r.status_code == 401


def _api_close_all_submit(client, wallet, *, account_id=None, signer=None, tamper=None):
    r = client.get("/api/me/close-all/message")
    assert r.status_code == 200, r.text
    m = r.json()
    sig = (signer or wallet).sign_message(
        encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": account_id or m["account_id"], "nonce": m["nonce"],
            "issued_at": m["issued_at"], "signature": sig, "message": m["message"]}
    if tamper:
        body.update(tamper)
    return client.post("/api/me/close-all", json=body)


def test_close_all_submit_happy_path_writes_request_file(client_wallet):
    client, cfg, wallet = client_wallet
    r = _api_close_all_submit(client, wallet)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["effective"] == "next_engine_cycle"
    account_id = body["account_id"]
    requests = load_close_all_requests(close_all_path_for(cfg.exchange_dir))
    assert any(req["account_id"] == account_id for req in requests)


def test_close_all_submit_bad_signature_rejected(client_wallet):
    client, _cfg, wallet = client_wallet
    forger = Account.create()
    r = _api_close_all_submit(client, wallet, signer=forger)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]


def test_close_all_submit_wrong_account_rejected(client_wallet):
    client, _cfg, wallet = client_wallet
    r = _api_close_all_submit(client, wallet, account_id="fdeadbeef" + "0" * 32)
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════
# (F) scripts/run_copytrade.py：接線版，簽章請求一路走到 killswitch.trip
# ══════════════════════════════════════════════════════════════════════

def _exchange_dir(tmp_path):
    d = tmp_path / "exchange"
    d.mkdir(exist_ok=True)
    return d


def _wire_signed(monkeypatch, tmp_path, *, wallet=None):
    wallet = wallet or Account.create()
    m = tmp_path / "followers.json"
    m.write_text(json.dumps({"followers": [
        {"account_id": "alice", "user_address": wallet.address,
         "builder_address": _BUILDER, "network": "mainnet", "label": "",
         "leader_address": _LEADER}]}))
    lp = tmp_path / "leaders.json"
    lp.write_text(json.dumps({"leaders": [{"address": _LEADER, "name": "Alpha"}]}))
    monkeypatch.setenv("FILET_FOLLOWERS", str(m))
    monkeypatch.setenv("FILET_LEADERS_PATH", str(lp))
    monkeypatch.setenv("SPARK_USER_ADDR", wallet.address)
    monkeypatch.setenv("SPARK_BUILDER_ADDR", _BUILDER)
    monkeypatch.setenv("SPARK_ACCOUNT_ID", "alice")
    monkeypatch.setenv("SPARK_NETWORK", "mainnet")
    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FILET_EXCHANGE_DIR", str(_exchange_dir(tmp_path)))
    monkeypatch.delenv("COPY_LIVE_TRADING", raising=False)
    monkeypatch.delenv("COPY_TG_BOT_TOKEN", raising=False)
    return wallet


def _stub_network(monkeypatch):
    import hyperliquid.info

    import spark.exchange.hyperliquid as hl

    class _Info:
        def __init__(self, *a, **k):
            pass

    class _AdapterStub:
        def __init__(self, *a, **k):
            pass

        def get_positions(self, address):
            return []

    monkeypatch.setattr(hyperliquid.info, "Info", _Info)
    monkeypatch.setattr(hl, "HyperliquidAdapter", _AdapterStub)


def test_signed_close_all_request_reaches_trip_via_engine_cycle(monkeypatch, tmp_path):
    """⭐⭐ 接縫測試：簽章請求真的從交換目錄一路走到 `killswitch.trip`，寫下
    ARM 檔（reason=owner_close），且 run_cycle 本輪不再下任何交易動作。"""
    # ⭐ 引擎端 `make_close_all_applier` 用真時鐘（`time.time`，未注入假時鐘）
    # 驗時效——issued_at 必須是**當下**，不能沿用其餘章節固定的 `_NOW` 錨（那會
    # 被 600 秒時效判成 expired，症狀是「簽章對但靜默不觸發」）。
    wallet = _wire_signed(monkeypatch, tmp_path)
    account_id = "alice"
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = build_close_all_message(account_id=account_id, nonce="n1", issued_at=issued_at)
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    rec = build_close_all_record(account_id=account_id, nonce="n1", issued_at=issued_at,
                                 signature=sig, message=msg)
    write_close_all_request(close_all_path_for(_exchange_dir(tmp_path)), rec)
    _stub_network(monkeypatch)

    rc.main(["--once"])

    root = tmp_path / "state"
    assert is_tripped(root)
    payload = json.loads((root / ARM_FILE_RELPATH).read_text())
    assert payload["reason"] == "owner_close"


def test_forged_close_all_signature_does_not_trip(monkeypatch, tmp_path):
    """壞簽章的請求檔（偽造）不得觸發收尾——引擎照常跑正常的一輪。"""
    _wire_signed(monkeypatch, tmp_path)
    forger = Account.create()
    account_id = "alice"
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = build_close_all_message(account_id=account_id, nonce="n1", issued_at=issued_at)
    sig = forger.sign_message(encode_defunct(text=msg)).signature.hex()  # 別人簽的
    rec = build_close_all_record(account_id=account_id, nonce="n1", issued_at=issued_at,
                                 signature=sig, message=msg)
    write_close_all_request(close_all_path_for(_exchange_dir(tmp_path)), rec)
    _stub_network(monkeypatch)
    # 不觸發時 run_cycle 照常跑完一輪——沿其餘接縫測試的既有慣例（見
    # test_run_copytrade_wiring.py `_record_leaders`），用替身避免還要餵一整套
    # 完整的 adapter（本測試只在意「有沒有 tripped」，不在意實際交易細節）。
    monkeypatch.setattr(rc, "run_cycle",
                        lambda adapter, ex, settings, notifier, state, root: "report")

    rc.main(["--once"])

    assert not is_tripped(tmp_path / "state")
