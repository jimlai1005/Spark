"""tests/test_keysvc_client.py
KeysvcClient：public API 用來呼叫 key-service 的 client，只有 generate 一個 method。

serve_forever 測試需要真的建立 AF_UNIX socket（本機 IPC，不連網、不出機器）。
tests/conftest.py 的 autouse _no_network fixture 對 socket.socket 全家族擋下（含 AF_UNIX）
——這是刻意的結構性斷網保證，屬專案紅線（CLAUDE.md 第 6 條，動之前必問），不在此檔
繞過或修改該全域 fixture。改用範圍最小的做法（沿用 test_keysvc_server.py 的既有模式）：
在本檔 import 期（fixture 尚未跑、socket.socket 還是原生類別）先存一份真身，只在下面
的測試內用 monkeypatch 換回真身（測試結束自動還原），其餘所有測試完全不受影響、網路仍被擋。"""
import socket
import threading
import uuid
from pathlib import Path

import pytest

from spark.keysvc.client import KeysvcClient
from spark.keysvc.server import serve_forever
from spark.keystore.envfile import EnvFileKeyStore

_REAL_SOCKET_CTOR = socket.socket  # 捕捉於 import 期，早於 autouse fixture 的 patch


def _connect_when_ready(sock_path):
    """重試 connect 直到 server 就緒。bind()（建檔）與 listen()（可接受）之間有窗口，
    僅靠 sock_path.exists() 判就緒會有 TOCTOU race（connect 落在窗口內→ConnectionRefusedError）。
    每次用新的 AF_UNIX socket（失敗的 AF_UNIX socket 不宜重連）；用 import 期存下的真 socket
    建構子，繞過 autouse 斷網 fixture（見檔頭說明），不動 conftest。"""
    import time
    for _ in range(100):
        c = _REAL_SOCKET_CTOR(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(str(sock_path))
            c.close()
            return
        except (FileNotFoundError, ConnectionRefusedError):
            c.close()
            time.sleep(0.02)
    raise RuntimeError("keysvc 測試: server 未就緒")


def _start_server(sock_path, ks, authorize=lambda s: True):
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, authorize, stop), daemon=True)
    t.start()
    _connect_when_ready(sock_path)  # 重試 connect 確認就緒，避開 bind/listen 之間的 race
    return t, stop


def test_client_generate_returns_address(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    sock_path = Path(f"/tmp/spark-keysvc-cli-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    t, stop = _start_server(sock_path, ks)
    try:
        client = KeysvcClient(str(sock_path))
        address = client.generate("alice")
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)
    assert address == ks.get_agent_signer("alice").address


def test_client_generate_already_exists_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    sock_path = Path(f"/tmp/spark-keysvc-cli-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    t, stop = _start_server(sock_path, ks)
    try:
        client = KeysvcClient(str(sock_path))
        client.generate("alice")
        with pytest.raises(RuntimeError):
            client.generate("alice")  # O_EXCL 已存在 → server ok=False → client raise
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)
