"""tests/test_close_all_result_marker.py
Opus 審查 Critical 2(a)：「平倉並撤銷」請求過期時，引擎過去只 `logger.info`——
客戶簽了章、引擎離線或逾時沒處理，兩端都沒有任何大聲的失敗訊號（違反工程原則 3）。
本檔測試新行為：expired 發**一次** critical＋落 result 標記檔（同一筆請求不重發，
新請求會再發一次）；成功收尾也落 completed 標記，供 API／RUNBOOK 判讀「已處理過的
殘留」與「從未處理過」。

全離線（tests/conftest.py 的 autouse socket-ban；本檔不碰網路）。
"""
import json

from eth_account import Account

from spark.copytrade.notifier import RecordingNotifier
from spark.filet.close_all import (close_all_result_path_for,
                                   read_close_all_result, write_close_all_request,
                                   write_close_all_result)
from spark.filet.close_all_apply import (CloseAllApplier,
                                         resolve_close_all_result_path)
from tests.test_kill_switch import (_acct, _at, _manifest, _NOW, _sign_close_all,
                                    _wind_down_recorder)


def _applier(tmp_path, *, account_id, manifest_path, notifier=None, now_s=_NOW
            ) -> tuple[CloseAllApplier, RecordingNotifier]:
    notifier = notifier or RecordingNotifier()
    return CloseAllApplier(account_id=account_id, manifest_path=manifest_path,
                           request_path=tmp_path / "owner_close.json",
                           notifier=notifier, now_fn=lambda: now_s), notifier


# ── result 標記：路徑、讀寫原語 ──────────────────────────────────────────

def test_result_path_derives_from_exchange_dir_and_account_id(tmp_path):
    p = close_all_result_path_for(str(tmp_path), "fabc")
    assert p == tmp_path / "engine" / "close_all_result" / "fabc.json"


def test_write_then_read_result_roundtrip(tmp_path):
    p = close_all_result_path_for(str(tmp_path), "fabc")
    write_close_all_result(p, status="expired", request_issued_at=_at(),
                           now_s=_NOW)
    got = read_close_all_result(p)
    assert got["status"] == "expired"
    assert got["request_issued_at"] == _at()
    assert got["ts"] == _NOW


def test_read_result_missing_file_is_none(tmp_path):
    p = close_all_result_path_for(str(tmp_path), "fabc")
    assert read_close_all_result(p) is None


def test_result_marker_never_contains_signature_material(tmp_path):
    """result 標記刻意窄——不含 signature/nonce/message（沿 engine_health 的
    FORBIDDEN_KEY_PARTS 同一理由）。"""
    p = close_all_result_path_for(str(tmp_path), "fabc")
    write_close_all_result(p, status="completed", request_issued_at=_at(),
                           now_s=_NOW)
    raw = json.loads(p.read_text())
    for forbidden in ("signature", "nonce", "message"):
        assert forbidden not in raw


# ── CloseAllApplier：expired 發一次 critical，同一筆請求不重發 ────────────

def test_expired_request_alerts_once_and_writes_result(tmp_path):
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    issued_at = _at(-700)  # > 600s 上限
    rec, _ = _sign_close_all(wallet, account_id=account_id, issued_at=issued_at)
    write_close_all_request(tmp_path / "owner_close.json", rec)

    applier, notifier = _applier(tmp_path, account_id=account_id, manifest_path=manifest)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is False
    assert calls == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    assert crits[0][3] == "close_all_expired"

    result_path = resolve_close_all_result_path(
        account_id, env={"FILET_EXCHANGE_DIR": str(tmp_path)})
    stored = read_close_all_result(result_path)
    assert stored["status"] == "expired"
    assert stored["request_issued_at"] == issued_at


def test_expired_request_same_request_does_not_realert(tmp_path):
    """同一筆請求（同 issued_at）連續兩輪都過期 → 第二輪不再 critical（防洗版）。"""
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    issued_at = _at(-700)
    rec, _ = _sign_close_all(wallet, account_id=account_id, issued_at=issued_at)
    write_close_all_request(tmp_path / "owner_close.json", rec)

    applier, notifier = _applier(tmp_path, account_id=account_id, manifest_path=manifest)
    wind_down, _ = _wind_down_recorder()
    applier.consume(tmp_path, wind_down)
    applier.consume(tmp_path, wind_down)  # 第二輪：同一筆請求仍然過期

    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1


def test_expired_then_new_request_alerts_again(tmp_path):
    """客戶重新簽了一筆新的請求（issued_at 不同）→ 即使又過期，也要再發一次。"""
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    rec1, _ = _sign_close_all(wallet, account_id=account_id, nonce="n1",
                              issued_at=_at(-700))
    write_close_all_request(tmp_path / "owner_close.json", rec1)
    applier, notifier = _applier(tmp_path, account_id=account_id, manifest_path=manifest)
    wind_down, _ = _wind_down_recorder()
    applier.consume(tmp_path, wind_down)

    rec2, _ = _sign_close_all(wallet, account_id=account_id, nonce="n2",
                              issued_at=_at(-800))
    write_close_all_request(tmp_path / "owner_close.json", rec2)
    applier.consume(tmp_path, wind_down)

    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 2


# ── 成功收尾也落 completed 標記 ──────────────────────────────────────────

def test_successful_trigger_writes_completed_result(tmp_path):
    wallet = Account.create()
    account_id = _acct(wallet)
    manifest = _manifest(tmp_path, account_id=account_id, user_address=wallet.address)
    issued_at = _at()
    rec, _ = _sign_close_all(wallet, account_id=account_id, issued_at=issued_at)
    write_close_all_request(tmp_path / "owner_close.json", rec)

    applier, notifier = _applier(tmp_path, account_id=account_id, manifest_path=manifest)
    wind_down, calls = _wind_down_recorder()
    triggered = applier.consume(tmp_path, wind_down)

    assert triggered is True
    assert calls == [True]
    result_path = resolve_close_all_result_path(
        account_id, env={"FILET_EXCHANGE_DIR": str(tmp_path)})
    stored = read_close_all_result(result_path)
    assert stored["status"] == "completed"
    assert stored["request_issued_at"] == issued_at
