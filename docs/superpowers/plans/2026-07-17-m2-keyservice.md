# M2 Key-service（agent 金鑰生成隔離層）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 蓋一個不對外的本機 key-service daemon——onboarding 的 public API 透過本機 unix socket 請它生成 agent keypair、寫入引擎 keystore、只回 agent 地址；agent 私鑰永不出此進程。這是非託管拆分雙層的「金鑰側」，安全等級最高。

**Architecture:** key-service 監聽本機 unix socket（只有 filet-api user 能連，SO_PEERCRED 驗連線者），唯一操作 `generate(account_id)`：`validate_account_id` → 生成 keypair（eth_account）→ `EnvFileKeyStore.import_agent_key`（改為 O_EXCL，絕不覆寫既有金鑰的結構性保證）→ 回 agent 地址。無讀金鑰、無簽名操作——最小攻擊面。

**Tech Stack:** Python 3.11 + uv、eth_account、unix domain socket（stdlib socket）、pytest（離線）、Decimal 無關。

**Spec:** `docs/superpowers/specs/2026-07-17-m2-onboarding-dashboard-design.md`（不變量與元件拓撲以該檔為準）。本計畫是該 spec 實作拆解的**第 1 項（key-service + envfile O_EXCL）**；API、前端、部署各自後續計畫。

---

## 執行狀態（2026-07-17 完成）

**全 8 task 實作 + fresh-context 雙審完成，全套 `uv run pytest -q` = 570 passed, 2 deselected；`ruff check src tests scripts` clean。** 分支 `feat/m2-keyservice`（自 `feat/m2-multiinstance` 分出），未 push、未動 main。

| Task | 主題 | commit | 審查結果 |
|---|---|---|---|
| 1 | envfile O_EXCL ⭐ | `713491c` | APPROVED |
| 2 | protocol（newline-JSON）| `95e4b20` | APPROVED（decode 加 isinstance dict 檢查於 T4）|
| 3 | generate handler ⭐ | `feafb19` | APPROVED（私鑰只在區域變數，三 except 分支不外洩）|
| 4 | SO_PEERCRED + socket 迴圈 ⭐ | `fd21ced` + fix `3cb1f2b` | review 抓 Critical（連線層例外未涵蓋→單一斷線弄垮 daemon），已修（per-connection 守衛）+ 複審 APPROVED |
| 5 | client | `9ea8921` | APPROVED |
| 6 | 入口 + systemd unit | `7bafdcb` | read-back 全符 |
| 7 | 端到端 + 非託管不變量 ⭐ | `52f46e2` + 護欄 `b27f6f6` | **opus 對抗性第二意見 APPROVED**：不變量成立（私鑰只在進程 + 600 落檔、不跨 socket、不進 log），未找到當前會漏私鑰的路徑；補錯誤回應路徑的回歸護欄測試 |

**opus 第二意見留下的觀察（非阻擋，待裁決/後續）：**
- **Minor（理論）**：`test_private_key_never_crosses_socket` 的 pk 比對只涵蓋小寫 hex 形式（實測 `Account.create().key.hex()` 即 64 字元小寫無 0x）；未涵蓋大寫 hex 與 raw 32-byte 形態——線上不產生此兩形式，純理論缺口。
- **Minor（生產 keystore，留使用者裁決）**：`envfile.py` `import_agent_key` 只 chmod 葉層帳號目錄（0o700）+ 檔案（0o600），未 chmod `self._root`；病態 umask（如 000）下 root 目錄可能過寬。**不漏私鑰**（帳號目錄 + 檔案雙重擋），至多洩漏 account_id 目錄名（= builder 位址，本非機密）。是否加固由使用者決定。
- **Minor（client，留下一計畫 Public API）**：`KeysvcClient.generate` 若 daemon 中途斷線導致 `readline()` 回空 bytes，`decode_response` 會拋 `json.JSONDecodeError` 而非乾淨的 RuntimeError（仍大聲失敗、未吞）；Public API 計畫包裝 client 錯誤處理時一併收。

**部署驗收留待 VPS 部署計畫實機驗**：filet-api user 讀不到 `/etc/filet/keys`、SO_PEERCRED 在 Linux 實機生效（macOS 開發機用 stub 授權器，syscall 僅 Linux）。

---

## 全域紅線（每個任務的實作者與 reviewer 都先讀）

1. ⭐ **agent 私鑰永不出 key-service 進程**：不進 socket 回應、不進 log、不進例外訊息。回應只含 agent **地址**。
2. ⭐ **絕不覆寫既有金鑰**（O_EXCL 結構性保證）：`import_agent_key` 存在即 `FileExistsError`，不倚賴「先查再寫」的 TOCTOU。並行/誤呼都不會截斷既有金鑰（否則 keystore key 與鏈上已授權 agent 失聯、引擎死鎖）。
3. ⭐ **連線者授權**：socket 只接受設定允許的 uid（SO_PEERCRED，Linux）。授權器可注入（測試在 macOS 用 stub；生產用 SO_PEERCRED 實作）。
4. `validate_account_id` 在生成前擋（沿元件一，防路徑穿越）。
5. 測試全離線（無真網、無真金鑰服務常駐）；hl-copytrader 唯讀（本計畫不需碰）。內部一律用既有慣例。

## 檔案結構（本計畫鎖定）

```
src/spark/
├── keystore/envfile.py         # Modify：import_agent_key 改 O_EXCL
└── keysvc/
    ├── __init__.py             # Task 2
    ├── protocol.py             # Task 2：request/response 序列化（newline-JSON）
    ├── peercred.py             # Task 4：SO_PEERCRED 授權器（Linux）
    ├── server.py               # Task 3/4：socket daemon + generate handler
    └── client.py               # Task 5：API 用的 client
scripts/run_keysvc.py           # Task 6：daemon 入口
deploy/filet-keysvc.service     # Task 6：systemd unit
tests/
├── test_envfile_keystore.py    # Modify：O_EXCL 測試
├── test_keysvc_protocol.py     # Task 2
├── test_keysvc_server.py       # Task 3
├── test_keysvc_peercred.py     # Task 4
├── test_keysvc_client.py       # Task 5
└── test_keysvc_integration.py  # Task 7：client→server→keystore 端到端
```

## 模型分工與 review gate

| Task | 主題 | 實作 | 驗收 | 加驗 |
|---|---|---|---|---|
| 0 | 分支+基線 | haiku | sonnet read-back | — |
| 1 | envfile O_EXCL ⭐ | sonnet | sonnet fresh | ⭐ 紅線 2 |
| 2 | protocol | sonnet | sonnet | — |
| 3 | server generate ⭐ | sonnet | sonnet | ⭐ 紅線 1（私鑰不外洩）|
| 4 | peercred 授權 ⭐ | sonnet | sonnet | ⭐ 紅線 3 |
| 5 | client | sonnet | sonnet | — |
| 6 | 入口+systemd | haiku | sonnet read-back | — |
| 7 | 端到端 ⭐ | sonnet | sonnet + **opus 第二意見** | ⭐ 非託管不變量整條驗 |

- 每任務：實作 → fresh-context 驗收 → commit。全部 commit 落 `feat/m2-keyservice`（自 `feat/m2-multiinstance` 分出）。不 push、不動 main。

---

### Task 0: 分支、基線

- [ ] **Step 1** `git checkout -b feat/m2-keyservice feat/m2-multiinstance`；`git branch --show-current` 應為 `feat/m2-keyservice`。
- [ ] **Step 2** `uv run pytest -q` → 應與 M2 Phase A 基線一致（`546 passed, 2 deselected`）；`uv run ruff check src tests scripts` 乾淨。不符則停下回報。
- [ ] **Step 3** 無新檔可 commit（分支即起點）；直接進 Task 1。

---

### Task 1: envfile `import_agent_key` 改 O_EXCL ⭐

**Files:** Modify `src/spark/keystore/envfile.py`；Modify `tests/test_envfile_keystore.py`。
先讀 `src/spark/keystore/envfile.py`（現有 `import_agent_key`——M2 Phase A 已加 `validate_account_id`、用 `os.open(..., O_CREAT|O_TRUNC)`）。

- [ ] **Step 1: 失敗測試**（加到既有測試檔）

```python
def test_import_agent_key_refuses_overwrite(tmp_path):
    """O_EXCL：既有金鑰存在時再 import 同一 account → FileExistsError（絕不覆寫）。"""
    ks = EnvFileKeyStore(tmp_path)
    ks.import_agent_key("acct1", _PK)
    other = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    import pytest
    with pytest.raises(FileExistsError):
        ks.import_agent_key("acct1", other)
    # 既有金鑰未被截斷：仍是第一把
    assert ks.get_agent_signer("acct1").address == _ADDR
```

- [ ] **Step 2** `uv run pytest tests/test_envfile_keystore.py::test_import_agent_key_refuses_overwrite -v` → FAIL（目前 O_TRUNC 會覆寫、不拋）。
- [ ] **Step 3: 實作** —— `import_agent_key` 的 `os.open` flags 從 `os.O_WRONLY | os.O_CREAT | os.O_TRUNC` 改為 `os.O_WRONLY | os.O_CREAT | os.O_EXCL`（其餘不變：先 mkdir 700、寫入、chmod 600）。docstring 補一行「O_EXCL：存在即 FileExistsError，絕不覆寫（結構性保證，非 TOCTOU）」。
- [ ] **Step 4** `uv run pytest tests/test_envfile_keystore.py -q`（既有全部 + 新測試全綠）+ `uv run ruff check src/spark/keystore/envfile.py tests/test_envfile_keystore.py`。
- [ ] **Step 5** `git commit -m "harden: import_agent_key O_EXCL — never overwrite existing key (structural)"`（帶 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` footer；以下所有 commit 同）。

---

### Task 2: 協定（protocol.py）

**Files:** Create `src/spark/keysvc/__init__.py`（docstring）、`src/spark/keysvc/protocol.py`、`tests/test_keysvc_protocol.py`。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_keysvc_protocol.py"""
import pytest
from spark.keysvc.protocol import (
    encode_request, decode_request, encode_response, decode_response,
    GenerateRequest, Response)

def test_request_roundtrip():
    line = encode_request(GenerateRequest(account_id="alice"))
    assert line.endswith(b"\n")
    req = decode_request(line)
    assert req.account_id == "alice"

def test_response_ok_roundtrip():
    line = encode_response(Response(ok=True, agent_address="0x" + "a"*40))
    resp = decode_response(line)
    assert resp.ok and resp.agent_address == "0x" + "a"*40 and resp.error is None

def test_response_err_roundtrip():
    resp = decode_response(encode_response(Response(ok=False, error="boom")))
    assert resp.ok is False and resp.error == "boom" and resp.agent_address is None

def test_decode_bad_op_rejected():
    import json
    with pytest.raises(ValueError):
        decode_request((json.dumps({"op": "read", "account_id": "x"}) + "\n").encode())

def test_decode_missing_account_rejected():
    import json
    with pytest.raises(ValueError):
        decode_request((json.dumps({"op": "generate"}) + "\n").encode())
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作**

```python
"""src/spark/keysvc/protocol.py
key-service 的 socket 協定：newline 結尾的 JSON。唯一操作 generate。
私鑰絕不出現在任何訊息——回應只帶 agent 地址或錯誤。"""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerateRequest:
    account_id: str


@dataclass(frozen=True)
class Response:
    ok: bool
    agent_address: str | None = None
    error: str | None = None


def encode_request(req: GenerateRequest) -> bytes:
    return (json.dumps({"op": "generate", "account_id": req.account_id}) + "\n").encode()


def decode_request(line: bytes) -> GenerateRequest:
    d = json.loads(line.decode())
    if d.get("op") != "generate":
        raise ValueError(f"unsupported op: {d.get('op')!r}")
    acct = d.get("account_id")
    if not acct:
        raise ValueError("missing account_id")
    return GenerateRequest(account_id=acct)


def encode_response(resp: Response) -> bytes:
    body = {"ok": resp.ok}
    if resp.agent_address is not None:
        body["agent_address"] = resp.agent_address
    if resp.error is not None:
        body["error"] = resp.error
    return (json.dumps(body) + "\n").encode()


def decode_response(line: bytes) -> Response:
    d = json.loads(line.decode())
    return Response(ok=bool(d.get("ok")), agent_address=d.get("agent_address"),
                    error=d.get("error"))
```

- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: key-service socket protocol (newline-JSON, generate op only)"`。

---

### Task 3: Server generate handler ⭐（私鑰不外洩）

**Files:** Create `src/spark/keysvc/server.py`；Test `tests/test_keysvc_server.py`。
先讀 `src/spark/keystore/envfile.py`（EnvFileKeyStore）、`src/spark/keysvc/protocol.py`。

本任務只做**純處理函式** `handle_generate`（socket 接線在 Task 4）——輸入 request + keystore，輸出 response，含私鑰不外洩與 O_EXCL 已存在的處理。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_keysvc_server.py"""
from eth_account import Account
from spark.keysvc.server import handle_generate
from spark.keysvc.protocol import GenerateRequest, Response
from spark.keystore.envfile import EnvFileKeyStore

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
```

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作**

```python
"""src/spark/keysvc/server.py
key-service 的核心處理與 socket daemon。generate：生成 agent keypair、寫入 keystore、
只回地址。私鑰絕不進回應/log。"""
import logging
import socket
from collections.abc import Callable
from pathlib import Path

from eth_account import Account

from spark.keystore.envfile import EnvFileKeyStore
from spark.keysvc.protocol import (GenerateRequest, Response, decode_request,
                                   encode_response)

logger = logging.getLogger(__name__)


def handle_generate(req: GenerateRequest, ks: EnvFileKeyStore) -> Response:
    """生成 agent keypair → 寫 keystore（O_EXCL）→ 回地址。任何失敗回 Response(ok=False)，
    私鑰絕不進回應/log/例外訊息。"""
    try:
        acct = Account.create()  # os.urandom 亂數；私鑰只存在此區域變數
        ks.import_agent_key(req.account_id, acct.key.hex())  # O_EXCL：存在即 FileExistsError
    except FileExistsError:
        return Response(ok=False, error=f"account {req.account_id} 已有 agent key，不重生")
    except ValueError as e:  # validate_account_id 等——e 不含私鑰
        return Response(ok=False, error=str(e))
    except Exception:  # noqa: BLE001 — 不外洩細節（可能含路徑，不含私鑰）
        logger.exception("keysvc generate 失敗 account=%s", req.account_id)  # 不 log 私鑰
        return Response(ok=False, error="internal error")
    return Response(ok=True, agent_address=acct.address)
```

（socket accept 迴圈在 Task 4 加入，與授權器一起。）

- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: key-service generate handler — keypair to keystore, address-only response"`。

---

### Task 4: SO_PEERCRED 授權 + socket 迴圈 ⭐

**Files:** Create `src/spark/keysvc/peercred.py`；Modify `src/spark/keysvc/server.py`（加 `serve` 迴圈）；Test `tests/test_keysvc_peercred.py`、`tests/test_keysvc_server.py`（補 serve 測試）。

- [ ] **Step 1: 失敗測試（peercred）**

```python
"""tests/test_keysvc_peercred.py"""
import struct, socket
from unittest.mock import MagicMock
from spark.keysvc.peercred import make_peercred_authorizer

def test_authorizer_allows_configured_uid():
    authz = make_peercred_authorizer(allowed_uids={1001})
    sock = MagicMock()
    # SO_PEERCRED 回 (pid, uid, gid) 打包
    sock.getsockopt.return_value = struct.pack("3i", 42, 1001, 1001)
    assert authz(sock) is True

def test_authorizer_rejects_other_uid():
    authz = make_peercred_authorizer(allowed_uids={1001})
    sock = MagicMock()
    sock.getsockopt.return_value = struct.pack("3i", 42, 9999, 9999)
    assert authz(sock) is False
```

- [ ] **Step 2** 跑到失敗。**Step 3: 實作（peercred.py）**

```python
"""src/spark/keysvc/peercred.py
SO_PEERCRED 連線者授權（Linux）。回一個 authorize_peer(sock)->bool。
非 Linux（開發 macOS）沒有 SO_PEERCRED——生產在 Linux 跑，測試用 mock 打包驗邏輯。"""
import socket
import struct
from collections.abc import Callable

_PEERCRED_FMT = "3i"  # pid, uid, gid


def make_peercred_authorizer(allowed_uids: set[int]) -> Callable[[socket.socket], bool]:
    def authorize(sock: socket.socket) -> bool:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize(_PEERCRED_FMT))
        _pid, uid, _gid = struct.unpack(_PEERCRED_FMT, raw)
        return uid in allowed_uids
    return authorize
```

- [ ] **Step 4: 失敗測試（serve 迴圈，用 socketpair 或臨時 socket 檔）**

```python
# 加到 tests/test_keysvc_server.py
def test_serve_one_generates_and_responds(tmp_path):
    import socket, threading
    from spark.keysvc.server import serve_forever
    from spark.keysvc.protocol import encode_request, decode_request, GenerateRequest
    from spark.keystore.envfile import EnvFileKeyStore
    sock_path = tmp_path / "ks.sock"
    ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    # authorize 全放行（測試不驗 peercred，peercred 邏輯在 test_keysvc_peercred 驗）
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    # 等 socket 檔出現
    import time
    for _ in range(50):
        if sock_path.exists(): break
        time.sleep(0.02)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(str(sock_path))
    c.sendall(encode_request(GenerateRequest("alice")))
    resp = decode_response(c.makefile("rb").readline()); c.close()
    stop.set()
    assert resp.ok and ks.get_agent_signer("alice").address == resp.agent_address

def test_serve_rejects_unauthorized_peer(tmp_path):
    import socket, threading, time
    from spark.keysvc.server import serve_forever
    from spark.keysvc.protocol import encode_request, GenerateRequest, decode_response
    from spark.keystore.envfile import EnvFileKeyStore
    sock_path = tmp_path / "ks.sock"; ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: False, stop), daemon=True)  # 全拒
    t.start()
    for _ in range(50):
        if sock_path.exists(): break
        time.sleep(0.02)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(str(sock_path))
    c.sendall(encode_request(GenerateRequest("alice")))
    line = c.makefile("rb").readline(); c.close(); stop.set()
    # 未授權：連線被拒（無回應或錯誤回應），且 keystore 無 alice
    assert (tmp_path / "keys" / "alice").exists() is False
```

- [ ] **Step 5: 實作 `serve_forever`（加到 server.py）**

```python
def serve_forever(sock_path: str, ks: EnvFileKeyStore,
                  authorize_peer: Callable[[socket.socket], bool],
                  stop=None) -> None:
    """監聽 unix socket；每個連線：授權 → 讀一個 request → 處理 → 回一個 response → 關。
    未授權連線直接關閉不處理。stop（threading.Event）供測試/優雅停止。"""
    p = Path(sock_path)
    if p.exists():
        p.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    # socket 檔權限：660 filet-engine:filet-api（部署由 systemd/啟動腳本設 group；此處設 660）
    import os as _os
    _os.chmod(sock_path, 0o660)
    srv.listen(8)
    srv.settimeout(0.5)
    try:
        while stop is None or not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                if not authorize_peer(conn):
                    logger.warning("keysvc 拒絕未授權連線")
                    continue
                line = conn.makefile("rb").readline()
                try:
                    req = decode_request(line)
                    resp = handle_generate(req, ks)
                except ValueError as e:
                    resp = Response(ok=False, error=str(e))
                conn.sendall(encode_response(resp))
    finally:
        srv.close()
        if p.exists():
            p.unlink()
```

- [ ] **Step 6** 全綠（peercred + serve 測試）+ ruff。**Step 7** `git commit -m "feat: key-service socket loop with SO_PEERCRED authorization"`。

---

### Task 5: Client（給 public API 用）

**Files:** Create `src/spark/keysvc/client.py`；Test `tests/test_keysvc_client.py`。

- [ ] **Step 1: 失敗測試**（起一個真 serve_forever，client 連它）

```python
"""tests/test_keysvc_client.py"""
import threading, time
from spark.keysvc.server import serve_forever
from spark.keysvc.client import KeysvcClient
from spark.keystore.envfile import EnvFileKeyStore

def _start(tmp_path):
    sock = tmp_path / "ks.sock"; ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock), ks, lambda s: True, stop), daemon=True)
    t.start()
    for _ in range(50):
        if sock.exists(): break
        time.sleep(0.02)
    return sock, ks, stop

def test_client_generate_returns_address(tmp_path):
    sock, ks, stop = _start(tmp_path)
    addr = KeysvcClient(str(sock)).generate("alice")
    stop.set()
    assert addr == ks.get_agent_signer("alice").address

def test_client_generate_already_exists_raises(tmp_path):
    sock, ks, stop = _start(tmp_path)
    cli = KeysvcClient(str(sock))
    cli.generate("alice")
    import pytest
    with pytest.raises(RuntimeError):
        cli.generate("alice")  # 第二次 O_EXCL → 錯誤 → client raise
    stop.set()
```

- [ ] **Step 2** 跑到失敗。**Step 3: 實作**

```python
"""src/spark/keysvc/client.py
public API 用來呼叫 key-service 的 client。只有 generate。"""
import socket

from spark.keysvc.protocol import (GenerateRequest, decode_response,
                                   encode_request)


class KeysvcClient:
    def __init__(self, sock_path: str):
        self._sock_path = sock_path

    def generate(self, account_id: str) -> str:
        """請 key-service 生成 agent、回 agent 地址。失敗 raise RuntimeError（含錯誤訊息，
        不含私鑰——key-service 本來就不回私鑰）。"""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(self._sock_path)
            c.sendall(encode_request(GenerateRequest(account_id)))
            line = c.makefile("rb").readline()
        resp = decode_response(line)
        if not resp.ok:
            raise RuntimeError(f"keysvc generate 失敗: {resp.error}")
        return resp.agent_address
```

- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: key-service client for public API"`。

---

### Task 6: Daemon 入口 + systemd unit

**Files:** Create `scripts/run_keysvc.py`、`deploy/filet-keysvc.service`。無單元測試（入口/部署檔）；驗收＝read-back + 無 env 執行印用法。

- [ ] **Step 1: `scripts/run_keysvc.py`**

```python
"""key-service daemon 入口。
用法: FILET_KEYSVC_SOCK=/run/filet/keysvc.sock FILET_KEYS_DIR=/etc/filet/keys \\
      FILET_KEYSVC_ALLOWED_UIDS=1002 uv run python -m scripts.run_keysvc
（生產由 systemd 拉起，跑在 filet-engine；FILET_KEYSVC_ALLOWED_UIDS = filet-api 的 uid）"""
import os
import signal
import threading

USAGE = ("用法: FILET_KEYSVC_SOCK=.. FILET_KEYS_DIR=.. FILET_KEYSVC_ALLOWED_UIDS=<uid[,uid]> "
         "uv run python -m scripts.run_keysvc")


def main() -> None:
    sock = os.environ.get("FILET_KEYSVC_SOCK")
    keys_dir = os.environ.get("FILET_KEYS_DIR")
    uids_raw = os.environ.get("FILET_KEYSVC_ALLOWED_UIDS")
    if not sock or not keys_dir or not uids_raw:
        print(USAGE)
        missing = [k for k, v in [("FILET_KEYSVC_SOCK", sock), ("FILET_KEYS_DIR", keys_dir),
                                  ("FILET_KEYSVC_ALLOWED_UIDS", uids_raw)] if not v]
        print(f"缺少環境變數: {', '.join(missing)}")
        raise SystemExit(2)
    allowed = {int(x) for x in uids_raw.split(",") if x.strip()}
    # 網路依賴延後到 main 內 import（import 階段零副作用）
    from spark.keystore.envfile import EnvFileKeyStore
    from spark.keysvc.peercred import make_peercred_authorizer
    from spark.keysvc.server import serve_forever
    ks = EnvFileKeyStore(keys_dir)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve_forever(sock, ks, make_peercred_authorizer(allowed), stop)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: `deploy/filet-keysvc.service`**

```ini
[Unit]
Description=Filet key-service (agent key generation, local socket only)
After=network.target

[Service]
Type=simple
User=filet-engine
Group=filet-engine
RuntimeDirectory=filet
RuntimeDirectoryMode=0750
Environment=FILET_KEYSVC_SOCK=/run/filet/keysvc.sock
Environment=FILET_KEYS_DIR=/etc/filet/keys
# FILET_KEYSVC_ALLOWED_UIDS 由部署填入 filet-api 的實際 uid（見部署文件）
Environment=FILET_KEYSVC_ALLOWED_UIDS=REPLACE_WITH_FILET_API_UID
ExecStart=/opt/filet/spark/.venv/bin/python -m scripts.run_keysvc
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/etc/filet/keys /run/filet
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

（`REPLACE_WITH_FILET_API_UID` 是部署時填實際 uid 的**明確佔位**，非程式 placeholder——部署文件會指示 `id -u filet-api` 填入。）

- [ ] **Step 3** 無 env 跑 `uv run python -m scripts.run_keysvc` → 印用法、exit 2（不觸網、不建 socket）。**Step 4** `git commit -m "feat: key-service daemon entrypoint + systemd unit (filet-engine, local socket)"`。

---

### Task 7: 端到端 + 非託管不變量 ⭐（加 opus 第二意見）

**Files:** Create `tests/test_keysvc_integration.py`。

- [ ] **Step 1: 失敗測試**（整條：client → serve → keystore；私鑰不外洩；O_EXCL；不同 account 各自獨立）

```python
"""tests/test_keysvc_integration.py"""
import socket, threading, time
import pytest
from spark.keysvc.server import serve_forever
from spark.keysvc.client import KeysvcClient
from spark.keystore.envfile import EnvFileKeyStore

def _start(tmp_path, authorize=lambda s: True):
    sock = tmp_path / "ks.sock"; ks = EnvFileKeyStore(tmp_path / "keys")
    stop = threading.Event()
    t = threading.Thread(target=serve_forever, args=(str(sock), ks, authorize, stop), daemon=True)
    t.start()
    for _ in range(50):
        if sock.exists(): break
        time.sleep(0.02)
    return sock, ks, stop

def test_end_to_end_generate_and_engine_reads(tmp_path):
    """client 生成 → keystore 落 600 → 引擎（get_agent_signer）讀得到、地址一致。"""
    sock, ks, stop = _start(tmp_path)
    addr = KeysvcClient(str(sock)).generate("f" + "a"*40)
    stop.set()
    signer = ks.get_agent_signer("f" + "a"*40)  # 引擎側讀取
    assert signer.address == addr
    import stat
    mode = (tmp_path / "keys" / ("f"+"a"*40) / "agent.key").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600

def test_private_key_never_crosses_socket(tmp_path):
    """落檔私鑰不出現在 socket 上傳輸的任何 bytes。"""
    sock, ks, stop = _start(tmp_path)
    # 直接抓 socket 回應 bytes
    from spark.keysvc.protocol import encode_request, GenerateRequest
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(str(sock))
    c.sendall(encode_request(GenerateRequest("alice")))
    raw = c.makefile("rb").readline(); c.close(); stop.set()
    pk = (tmp_path / "keys" / "alice" / "agent.key").read_text().strip()
    assert pk.encode() not in raw and pk[2:].encode() not in raw

def test_second_generate_same_account_rejected_key_unchanged(tmp_path):
    sock, ks, stop = _start(tmp_path)
    cli = KeysvcClient(str(sock))
    a1 = cli.generate("alice")
    with pytest.raises(RuntimeError):
        cli.generate("alice")
    stop.set()
    assert ks.get_agent_signer("alice").address == a1  # 未被覆寫

def test_two_accounts_independent(tmp_path):
    sock, ks, stop = _start(tmp_path)
    cli = KeysvcClient(str(sock))
    a = cli.generate("alice"); b = cli.generate("bob")
    stop.set()
    assert a != b
    assert ks.get_agent_signer("alice").address == a
    assert ks.get_agent_signer("bob").address == b
```

- [ ] **Step 2** 跑到失敗（若前面任務都完成則可能直接綠——那也 OK，此為整合守門）。
- [ ] **Step 3** 若有紅則修對應層；全綠 + ruff。
- [ ] **Step 4** `git commit -m "test: key-service end-to-end + non-custodial invariants (key never crosses socket, O_EXCL, isolation)"`。

---

## 收尾（全計畫完成後）

1. 指揮官親跑 `uv run pytest -q`（全套，M2 Phase A 546 + 本計畫新增全綠）+ `uv run ruff check src tests scripts`。
2. 更新本計畫頂部加「執行狀態」節（任務→commit 對照），commit。
3. 交付：key-service 完成，等**下一個計畫（HL SDK 外部簽名路徑 research → public API）**。**部署驗收**（filet-api user 讀不到 key、SO_PEERCRED 實機生效）留待 VPS 部署計畫實機驗。

## 不在本計畫（各自後續，見 spec 拆解）

- **HL SDK 外部簽名 research**（load-bearing 未知：只建 typed-data 不簽的路徑）——key-service 之後、API 之前。
- **Public API**（SIWE、產 payload、verify、admin 唯讀、activate CLI）。
- **Dashboard 前端**（Next.js，v1 token，wizard 4 階段）。
- **部署**（systemd、反代 TLS、權限實機驗收——filet-api 讀不到 key、SO_PEERCRED）。
