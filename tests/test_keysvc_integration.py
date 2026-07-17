"""tests/test_keysvc_integration.py
key-service 端到端整合測試：client → unix socket → server → keystore 落檔。
守住非託管不變量：(1) 引擎側能讀到落檔的 agent key、(2) 私鑰絕不出現在 socket 傳輸
的任何 bytes 裡（⭐ 核心不變量）、(3) 落檔權限 600、(4) O_EXCL 不覆寫、帳號互相獨立。

tests/conftest.py 的 autouse _no_network fixture 對 socket.socket 全家族擋下（含 AF_UNIX）
——這是刻意的結構性斷網保證，屬專案紅線（CLAUDE.md 第 6 條，動之前必問），本檔不繞過或
修改該全域 fixture。沿用 test_keysvc_client.py / test_keysvc_server.py 的既有模式：在本檔
import 期（fixture 尚未跑、socket.socket 還是原生類別）先存一份真身，只在下面的測試內用
monkeypatch 換回真身（測試結束自動還原），其餘所有測試完全不受影響、網路仍被擋。"""
import socket
import stat
import threading
import uuid
from pathlib import Path

import pytest

from spark.keysvc.client import KeysvcClient
from spark.keysvc.protocol import GenerateRequest, decode_response, encode_request
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
            return c
        except (FileNotFoundError, ConnectionRefusedError):
            c.close()
            time.sleep(0.02)
    raise RuntimeError("keysvc 整合測試: server 未就緒")


def _start_server(ks, authorize=lambda s: True):
    """起 serve_forever thread，回傳 (sock_path, thread, stop_event)。socket 檔放 /tmp 下
    短路徑（AF_UNIX sun_path 在 macOS 約 104 bytes 上限，pytest tmp_path 常超過）；
    keystore 目錄不受此限，由呼叫端傳入（通常在 tmp_path 底下）。"""
    sock_path = Path(f"/tmp/spark-keysvc-it-{uuid.uuid4().hex[:8]}.sock")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, authorize, stop), daemon=True)
    t.start()
    _connect_when_ready(sock_path).close()  # 只探測就緒，這條探測連線本身即關閉
    return sock_path, t, stop


def _stop_server(sock_path, t, stop):
    stop.set()
    t.join(timeout=2)  # 等執行緒真正跑完 finally（unlink socket 檔），避免遺留檔案
    sock_path.unlink(missing_ok=True)  # 安全網：萬一執行緒未及時清乾淨


def test_end_to_end_generate_and_engine_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    ks = EnvFileKeyStore(tmp_path / "keys")
    account_id = "f" + "a" * 40
    sock_path, t, stop = _start_server(ks)
    try:
        client = KeysvcClient(str(sock_path))
        addr = client.generate(account_id)
    finally:
        _stop_server(sock_path, t, stop)
    # 引擎側（獨立於 client/server，直接用 keystore）讀得到剛生成的 agent key
    signer = ks.get_agent_signer(account_id)
    assert signer.address == addr
    # 落檔權限必須是 600（僅擁有者可讀寫，不可群組/其他人存取）
    path = tmp_path / "keys" / account_id / "agent.key"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_private_key_never_crosses_socket(tmp_path, monkeypatch):
    """⭐ 非託管核心不變量：私鑰只在 daemon 進程、落檔 600，永不跨 socket 傳出。
    直接用還原後的原生 socket 連 daemon、送 request、抓「原始回應 bytes」，
    確認落檔私鑰（含去 0x 前綴版本）完全不出現在這些 bytes 裡。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    ks = EnvFileKeyStore(tmp_path / "keys")
    sock_path, t, stop = _start_server(ks)
    try:
        c = _REAL_SOCKET_CTOR(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock_path))
        c.sendall(encode_request(GenerateRequest("alice")))
        raw = c.makefile("rb").readline()  # 原始回應 bytes，未經任何 decode
        c.close()
    finally:
        _stop_server(sock_path, t, stop)
    resp = decode_response(raw)
    assert resp.ok and resp.agent_address == ks.get_agent_signer("alice").address
    pk = (tmp_path / "keys" / "alice" / "agent.key").read_text().strip()
    assert pk.encode() not in raw
    assert pk.removeprefix("0x").encode() not in raw  # 保險：即便落檔含 0x 前綴也要涵蓋


def test_private_key_never_crosses_socket_on_error(tmp_path, monkeypatch):
    """⭐ 回歸護欄：私鑰不出現在 socket 傳輸的任何 bytes——含錯誤回應路徑。
    成功回應已由 test_private_key_never_crosses_socket 涵蓋；本測試補上錯誤分支：
    對同一帳號第二次 generate 會走 O_EXCL 已存在→ok=False 的錯誤路徑，斷言該錯誤回應
    的原始 bytes 不含落檔私鑰。今天靠 server.py 各 except 不外洩私鑰，但若未來有人把某個
    except 改成 error=str(e) 而 e 挾帶 key，這條斷言會抓到。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    ks = EnvFileKeyStore(tmp_path / "keys")
    sock_path, t, stop = _start_server(ks)
    try:
        KeysvcClient(str(sock_path)).generate("alice")  # 第一次成功落檔
        pk = (tmp_path / "keys" / "alice" / "agent.key").read_text().strip()
        c = _REAL_SOCKET_CTOR(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock_path))
        c.sendall(encode_request(GenerateRequest("alice")))  # 第二次 → O_EXCL 錯誤分支
        raw = c.makefile("rb").readline()  # 錯誤回應的原始 bytes，未經任何 decode
        c.close()
    finally:
        _stop_server(sock_path, t, stop)
    assert decode_response(raw).ok is False  # 確認確實走到錯誤分支，避免假綠
    assert pk.encode() not in raw
    assert pk.removeprefix("0x").encode() not in raw  # 保險：即便落檔含 0x 前綴也涵蓋


def test_second_generate_same_account_rejected_key_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    ks = EnvFileKeyStore(tmp_path / "keys")
    sock_path, t, stop = _start_server(ks)
    try:
        client = KeysvcClient(str(sock_path))
        a1 = client.generate("alice")
        with pytest.raises(RuntimeError):
            client.generate("alice")  # O_EXCL 已存在 → server ok=False → client raise
    finally:
        _stop_server(sock_path, t, stop)
    assert ks.get_agent_signer("alice").address == a1  # 未被第二次呼叫覆寫


def test_two_accounts_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    ks = EnvFileKeyStore(tmp_path / "keys")
    sock_path, t, stop = _start_server(ks)
    try:
        client = KeysvcClient(str(sock_path))
        a1 = client.generate("alice")
        a2 = client.generate("bob")
    finally:
        _stop_server(sock_path, t, stop)
    assert a1 != a2
    assert ks.get_agent_signer("alice").address == a1
    assert ks.get_agent_signer("bob").address == a2
