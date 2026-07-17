"""tests/test_keysvc_server.py
handle_generate 純函式：request + keystore → response。私鑰不外洩、O_EXCL 已存在、
account_id 校驗（縱深防禦）。serve_forever：unix socket accept 迴圈 + SO_PEERCRED 授權。

serve_forever 測試需要真的建立 AF_UNIX socket（本機 IPC，不連網、不出機器）。
tests/conftest.py 的 autouse _no_network fixture 對 socket.socket 全家族擋下（含 AF_UNIX）
——這是刻意的結構性斷網保證，屬專案紅線（CLAUDE.md 第 6 條，動之前必問），不在此檔
繞過或修改該全域 fixture。改用範圍最小的做法：在本檔 import 期（fixture 尚未跑、
socket.socket 還是原生類別）先存一份真身，只在下面兩個 serve_forever 測試內用
monkeypatch 換回真身（測試結束自動還原），其餘所有測試完全不受影響、網路仍被擋。"""
import socket

from spark.keysvc.server import handle_generate
from spark.keysvc.protocol import GenerateRequest
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
    raise RuntimeError("keysvc 測試: server 未就緒")


def test_generate_writes_key_returns_address(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    resp = handle_generate(GenerateRequest("alice"), ks)
    assert resp.ok and resp.error is None
    # 回應地址 == keystore 落檔的 agent key 對應地址
    assert ks.get_agent_signer("alice").address == resp.agent_address


def test_generate_private_key_never_in_response(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    resp = handle_generate(GenerateRequest("alice"), ks)
    # 讀出落檔私鑰，確認它不在回應的任何欄位
    pk = (tmp_path / "alice" / "agent.key").read_text().strip()
    blob = f"{resp.ok}{resp.agent_address}{resp.error}"
    assert pk not in blob and pk[2:] not in blob


def test_generate_already_exists_returns_error_not_overwrite(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    first = handle_generate(GenerateRequest("alice"), ks)
    second = handle_generate(GenerateRequest("alice"), ks)  # O_EXCL → 錯誤，不覆寫
    assert second.ok is False and "已" in (second.error or "")
    assert ks.get_agent_signer("alice").address == first.agent_address  # 未被換掉


def test_generate_bad_account_id_rejected(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    resp = handle_generate(GenerateRequest("../evil"), ks)
    assert resp.ok is False and (tmp_path / "..").resolve().joinpath("evil").exists() is False


def test_serve_one_generates_and_responds(tmp_path, monkeypatch):
    import threading
    import uuid
    from pathlib import Path
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    from spark.keysvc.server import serve_forever
    from spark.keysvc.protocol import encode_request, GenerateRequest, decode_response
    from spark.keystore.envfile import EnvFileKeyStore
    # AF_UNIX sun_path 上限約 104 bytes（macOS）；pytest tmp_path 常超過，故 socket 檔另放
    # /tmp 下短路徑，keystore 目錄不受此限、仍用 tmp_path。
    sock_path = Path(f"/tmp/spark-keysvc-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    try:
        c = _connect_when_ready(sock_path)  # 重試 connect，避開 bind/listen 之間的 race
        c.sendall(encode_request(GenerateRequest("alice")))
        resp = decode_response(c.makefile("rb").readline())
        c.close()
    finally:
        stop.set()
        t.join(timeout=2)  # 等執行緒真正跑完 finally（unlink socket 檔），避免遺留檔案
        sock_path.unlink(missing_ok=True)  # 安全網：萬一執行緒未及時清乾淨
    assert resp.ok and ks.get_agent_signer("alice").address == resp.agent_address


def test_serve_rejects_unauthorized_peer(tmp_path, monkeypatch):
    import threading
    import uuid
    from pathlib import Path
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    from spark.keysvc.server import serve_forever
    from spark.keysvc.protocol import encode_request, GenerateRequest
    from spark.keystore.envfile import EnvFileKeyStore
    # AF_UNIX sun_path 上限約 104 bytes（macOS）；pytest tmp_path 常超過，故 socket 檔另放
    # /tmp 下短路徑，keystore 目錄不受此限、仍用 tmp_path。
    sock_path = Path(f"/tmp/spark-keysvc-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: False, stop), daemon=True)
    t.start()
    try:
        c = _connect_when_ready(sock_path)  # 重試 connect，避開 bind/listen 之間的 race
        c.sendall(encode_request(GenerateRequest("alice")))
        c.makefile("rb").readline()
        c.close()
    finally:
        stop.set()
        t.join(timeout=2)  # 等執行緒真正跑完 finally（unlink socket 檔），避免遺留檔案
        sock_path.unlink(missing_ok=True)  # 安全網：萬一執行緒未及時清乾淨
    assert (tmp_path / "keys" / "alice").exists() is False


def test_serve_survives_client_early_disconnect(tmp_path, monkeypatch):
    """Critical 回歸：已授權 client 在收到回應前就斷線，不得弄垮整個 serve 迴圈。
    第一個 client 連上後立刻 close 不送任何 bytes（模擬提前斷線）；斷言 thread 仍活，
    且第二個正常 client 仍能成功生成——證明單一斷線只丟該連線、服務自我續命。"""
    import threading
    import time
    import uuid
    from pathlib import Path
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)  # 見檔頭說明：僅本測試放行 AF_UNIX
    from spark.keysvc.server import serve_forever
    from spark.keysvc.protocol import encode_request, GenerateRequest, decode_response
    from spark.keystore.envfile import EnvFileKeyStore
    sock_path = Path(f"/tmp/spark-keysvc-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    try:
        # 1. 提前斷線的 client：連上→立刻關，不送 bytes，也不讀回應。
        c1 = _connect_when_ready(sock_path)
        c1.close()
        time.sleep(0.1)  # 讓 serve 迴圈跑過這個壞連線
        # 2. 迴圈必須沒被弄垮。
        assert t.is_alive() is True
        # 3. 後續正常 client 仍能成功生成。
        c2 = _connect_when_ready(sock_path)
        c2.sendall(encode_request(GenerateRequest("bob")))
        resp = decode_response(c2.makefile("rb").readline())
        c2.close()
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)
    assert resp.ok and ks.get_agent_signer("bob").address == resp.agent_address


def test_generate_internal_error_no_key_in_log(tmp_path, caplog):
    import logging
    from unittest.mock import MagicMock
    from spark.keysvc.server import handle_generate
    from spark.keysvc.protocol import GenerateRequest
    ks = MagicMock()
    ks.import_agent_key.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.ERROR):
        resp = handle_generate(GenerateRequest("alice"), ks)
    assert resp.ok is False and resp.error == "internal error"
    # 私鑰（傳給 import_agent_key 的第二個位置參數）不得出現在任何 log 文字
    pk = ks.import_agent_key.call_args.args[1]  # 生成的私鑰 hex
    blob = " ".join(r.getMessage() for r in caplog.records) + caplog.text
    assert pk not in blob and pk.removeprefix("0x") not in blob
