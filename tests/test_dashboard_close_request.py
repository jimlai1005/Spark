"""tests/test_dashboard_close_request.py
Opus 審查 Critical 2(b)：`/api/me/dashboard` 的 `status.close_request` 欄位
——讓前端能分辨「從未提出過平倉並撤銷請求」／「提出了，引擎還沒處理」／
「引擎判定過期」／「已完成」，不必無限輪詢猜測。

全離線（tests/conftest.py 的 autouse socket-ban；本檔 TestClient 段落沿
test_me_dashboard.py 的既有 `_allow_local_sockets` fixture）。
"""
import socket
from datetime import datetime, timezone

import pytest

from spark.filet.close_all import (close_all_path_for, close_all_result_path_for,
                                   write_close_all_request, write_close_all_result)
from tests.test_me_dashboard import _logged_in

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _iso(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(1_800_000_000.0 + offset_s,
                                  timezone.utc).isoformat()


def _write_request(cfg, account_id: str, *, issued_at: str, nonce="n1") -> None:
    write_close_all_request(close_all_path_for(cfg.exchange_dir), {
        "action": "close_all", "account_id": account_id, "nonce": nonce,
        "issued_at": issued_at, "signature": "0xdead", "message": "irrelevant",
    })


def test_no_request_is_none(tmp_path):
    client, cfg, _hl, wallet = _logged_in(tmp_path)
    body = client.get("/api/me/dashboard").json()
    assert body["status"]["close_request"] is None


def test_request_without_result_is_pending(tmp_path):
    client, cfg, _hl, wallet = _logged_in(tmp_path)
    account_id = "f" + wallet.address[2:].lower()
    _write_request(cfg, account_id, issued_at=_iso())

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["close_request"] == {"state": "pending"}


def test_request_with_matching_expired_result(tmp_path):
    client, cfg, _hl, wallet = _logged_in(tmp_path)
    account_id = "f" + wallet.address[2:].lower()
    issued_at = _iso(-700)
    _write_request(cfg, account_id, issued_at=issued_at)
    write_close_all_result(close_all_result_path_for(cfg.exchange_dir, account_id),
                           status="expired", request_issued_at=issued_at,
                           now_s=1_800_000_000.0)

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["close_request"] == {"state": "expired"}


def test_request_with_matching_completed_result(tmp_path):
    client, cfg, _hl, wallet = _logged_in(tmp_path)
    account_id = "f" + wallet.address[2:].lower()
    issued_at = _iso()
    _write_request(cfg, account_id, issued_at=issued_at)
    write_close_all_result(close_all_result_path_for(cfg.exchange_dir, account_id),
                           status="completed", request_issued_at=issued_at,
                           now_s=1_800_000_000.0)

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["close_request"] == {"state": "completed"}


def test_new_request_after_stale_result_is_pending_again(tmp_path):
    """客戶重新簽了一筆新的請求（issued_at 不同）——舊的 expired/completed 標記
    不能冒充新請求已經有結果。"""
    client, cfg, _hl, wallet = _logged_in(tmp_path)
    account_id = "f" + wallet.address[2:].lower()
    old_issued_at = _iso(-1000)
    write_close_all_result(close_all_result_path_for(cfg.exchange_dir, account_id),
                           status="expired", request_issued_at=old_issued_at,
                           now_s=1_800_000_000.0)
    new_issued_at = _iso()
    _write_request(cfg, account_id, issued_at=new_issued_at, nonce="n2")

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["close_request"] == {"state": "pending"}
