"""tests/test_pause_flag_address_normalization.py
Opus 審查 Critical 1：`pause_flag_path_for` 對混大小寫的真實位址正規化，讓
寫端（`app.py` session 位址，恆小寫）與讀端（引擎 `SPARK_USER_ADDR`，可能是
使用者貼入的 EIP-55 checksum）解出同一條路徑——修復前兩端各拼各的，暫停旗標
fail-open（使用者按了暫停、面板顯示已停，引擎仍在開倉）。

全離線（tests/conftest.py 的 autouse socket-ban；TestClient 段落用既有
`_allow_local_sockets` fixture 放行 loopback，同 test_kill_switch.py 的既有模式）。
"""
import socket

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.copytrade.notifier import RecordingNotifier
from spark.filet.pause_flag import (pause_flag_path_for,
                                    read_pause_flag_for_engine)
from tests.publicapi_helpers import login, make_app

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def test_path_for_normalizes_real_address_case(tmp_path):
    """標準 0x+40hex 位址：checksum 混大小寫與純小寫解出同一條路徑。"""
    mixed = "0x" + "aB" * 20
    assert pause_flag_path_for(str(tmp_path), mixed) == \
        pause_flag_path_for(str(tmp_path), mixed.lower())


def test_path_for_leaves_non_standard_fixture_untouched(tmp_path):
    """非標準格式（既有測試用的短假位址）原樣拼入——不動既有路徑慣例測試。"""
    assert pause_flag_path_for(str(tmp_path), "0xDEAD") == \
        str(tmp_path / "0xDEAD" / "pause.json")


def test_engine_reads_checksum_address_after_lowercase_write(tmp_path):
    """寫端恆小寫（session 位址），讀端直接餵一個真實 checksum 位址（引擎
    SPARK_USER_ADDR 可能是的樣子）——修復前這兩者會落在不同路徑，讀端讀不到
    檔案 → 視為未暫停 → fail-open。"""
    wallet = Account.create()
    lower_addr = wallet.address.lower()
    checksum_addr = wallet.address
    assert lower_addr != checksum_addr  # eth_account 位址本身就是 checksum，兩者必不同

    from spark.filet.pause_flag import write_pause_flag
    path = pause_flag_path_for(str(tmp_path), lower_addr)
    write_pause_flag(path, paused=True, now_s=1_800_000_000.0)

    n = RecordingNotifier()
    assert read_pause_flag_for_engine(str(tmp_path), checksum_addr, n) is True
    assert n.records == []  # 正常讀取（同一路徑命中），不告警


def test_pause_endpoint_readable_by_engine_with_checksum_address(tmp_path):
    """端到端：API 寫下暫停旗標後，引擎用**未小寫**的原始 checksum 位址
    （模擬 SPARK_USER_ADDR 被設成使用者貼入的原始大小寫）也讀得到。"""
    app, cfg, _store, _keysvc, _hl = make_app(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    wallet = login(client)

    r = client.post("/api/me/pause", json={"action": "pause"})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True

    n = RecordingNotifier()
    # 引擎讀端餵原始（checksum）位址，不手動 .lower() ——正是修復前會漂移的情境。
    assert read_pause_flag_for_engine(cfg.exchange_dir, wallet.address, n) is True
    assert n.records == []
