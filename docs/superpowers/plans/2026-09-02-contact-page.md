# /contact 聯絡頁 Implementation Plan

> **For agentic workers:** 逐 task 執行，每個 task 標 `@inline`（builder）或 `@sdd`（impl-worker）。
> **實作 agent 不得 commit**（主線程統一 commit）；驗收指令要親跑並貼輸出末尾。
> 步驟用 checkbox（`- [ ]`）追蹤。

**Goal:** 新增公開頁 `/contact`（Name / Email / Message → Send），送出後由後端用
`goldwisetw@gmail.com` 的 Gmail SMTP 寄一封信到 `goldwisetw@gmail.com`（Reply-To 帶填表人
email），站主人工回覆。

**Architecture:** 後端 FastAPI 新增 `POST /api/public/contact`（無需登入），寄信邏輯獨立成
`src/spark/publicapi/contact.py`（純函式：驗證＋組信；副作用：`SmtpMailer`），透過
`create_app(..., mailer=None)` 注入（同 `notifier` 慣例），測試注入 FakeMailer 全離線。
前端 `web/src/app/contact/page.tsx`（use client）走既有 `lib/api.ts` 的 `post` helper，
文案進 `lib/copy.ts`（zh/en 對稱），footer 「聯絡我們」改連 `/contact`。

**Tech Stack:** Python 3.11 stdlib `smtplib` + `email.message.EmailMessage`（不加套件）、
FastAPI、Next.js 15 app router、vitest。

**設計稿對照**（Claude Design 截圖）：標題「CONTACT US」、一段說明文、三欄位
（Name / Email / Message，placeholder：Your name / Your email / Your message goes here）、
右下角紅色主按鈕「Send」。以現行站的 token 與 `.card` / `.btn-primary` 呈現，不照抄設計稿配色。

---

## 主線程裁決（實作 agent 照此執行，不重新討論）

| 項目 | 裁決 |
|---|---|
| 寄信通道 | Gmail SMTP `smtp.gmail.com:465`（`smtplib.SMTP_SSL`，timeout 20s），Google 帳號**應用程式密碼**（需 2FA）。不加第三方套件。 |
| 環境變數 | `FILET_CONTACT_SMTP_USER`、`FILET_CONTACT_SMTP_PASS`（secret，`repr=False`）、`FILET_CONTACT_TO`（選填，預設＝SMTP_USER）。USER/PASS **成對**：只設一個 → `from_env` 拒絕啟動（沿 Stripe trio 慣例）；都不設 → 端點回 503（表單頁仍可開、顯示直接來信的 mailto）。 |
| 收件／寄件 | From = SMTP_USER，To = CONTACT_TO，Reply-To = 填表人 email。Subject `[Filet 聯絡表單] <name>`。純文字 body。 |
| 驗證 | name 1–80 字、email ≤254 且符合 `^[^@\s]+@[^@\s]+\.[^@\s]+$`、message 10–2000 字；三者 `strip()` 後檢查；name/email 不得含 `\r`/`\n`（header injection）。不合法 → 422，detail 為固定中文句（不回顯輸入）。 |
| 反濫用 | (a) honeypot 欄位 `website`：非空 → 直接回 `{"ok": true}` 不寄信（log info）；(b) per client IP sliding window：`CONTACT_RATELIMIT_WINDOW_S = 600`、`CONTACT_RATELIMIT_MAX = 3`，超限 429。獨立 dict，不與 probe 限流共用。 |
| 失敗路徑 | 寄信例外 → `logger.error`（含例外類型，**不含**信件內容與密碼）→ 502 detail「寄送失敗，請稍後再試或直接來信」。**不重試**（send 非冪等，工程原則 2）。 |
| 導覽 | 只放 footer「聯絡我們」（`/contact`）＋ sitemap；不進 header nav。 |
| 對外揭露 email | 頁面文案明示「也可直接來信 goldwisetw@gmail.com」（mailto），這是使用者要的「讓用戶找到我」的保底路徑。 |
| 部署 | `deploy/filet-api.service` 加註解範例三行（不出貨真值）；RUNBOOK 新增 §「聯絡表單 SMTP」（申請應用程式密碼＋驗收 curl）。 |

---

### Task 1 `@inline`：後端純邏輯 `contact.py` ＋ config 欄位

**Files:**
- Create: `src/spark/publicapi/contact.py`
- Modify: `src/spark/publicapi/config.py`（`ApiConfig` 欄位＋`from_env`＋`__post_init__` 成對檢查）
- Test: `tests/test_contact_module.py`

- [ ] **Step 1: 寫失敗測試 `tests/test_contact_module.py`**

```python
"""tests/test_contact_module.py — /contact 寄信模組純邏輯（驗證＋組信）與 config 成對檢查。
全離線：本檔不建 SmtpMailer 連線（autouse socket-ban）。"""
import pytest

from spark.publicapi.contact import (
    ContactInput, ContactValidationError, build_contact_email, validate_contact,
)
from spark.publicapi.config import ApiConfig
from tests.publicapi_helpers import make_cfg


def test_validate_ok_strips_whitespace():
    ci = validate_contact(name="  Jim ", email=" a@b.co ", message="  hello there, ten+ chars ")
    assert ci == ContactInput(name="Jim", email="a@b.co", message="hello there, ten+ chars")


@pytest.mark.parametrize("name,email,message", [
    ("", "a@b.co", "x" * 20),                 # name 空
    ("n" * 81, "a@b.co", "x" * 20),           # name 太長
    ("Jim\r\nBcc: x@y.z", "a@b.co", "x" * 20),  # header injection
    ("Jim", "not-an-email", "x" * 20),
    ("Jim", "a@b.co\nX", "x" * 20),
    ("Jim", "a" * 250 + "@b.co", "x" * 20),   # email 太長
    ("Jim", "a@b.co", "short"),               # message 太短
    ("Jim", "a@b.co", "x" * 2001),            # message 太長
])
def test_validate_rejects(name, email, message):
    with pytest.raises(ContactValidationError):
        validate_contact(name=name, email=email, message=message)


def test_build_email_headers_and_body():
    ci = ContactInput(name="Jim", email="a@b.co", message="line1\nline2 long enough")
    msg = build_contact_email(ci, sender="site@gmail.com", to="owner@gmail.com",
                              client_ip="1.2.3.4", now_iso="2026-09-02T00:00:00Z")
    assert msg["From"] == "site@gmail.com"
    assert msg["To"] == "owner@gmail.com"
    assert msg["Reply-To"] == "a@b.co"
    assert msg["Subject"] == "[Filet 聯絡表單] Jim"
    body = msg.get_content()
    assert "Jim" in body and "a@b.co" in body and "1.2.3.4" in body
    assert "line1\nline2 long enough" in body
    assert msg.get_content_type() == "text/plain"


def test_cfg_defaults_unconfigured(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.contact_smtp_user is None
    assert cfg.contact_enabled is False
    assert "contact_smtp_pass" not in repr(cfg)


def test_cfg_pair_required(tmp_path):
    with pytest.raises(ValueError, match="FILET_CONTACT_SMTP"):
        make_cfg(tmp_path, contact_smtp_user="site@gmail.com")   # 缺 pass


def test_cfg_to_defaults_to_user(tmp_path):
    cfg = make_cfg(tmp_path, contact_smtp_user="site@gmail.com", contact_smtp_pass="app-pw")
    assert cfg.contact_enabled is True
    assert cfg.contact_to == "site@gmail.com"
    assert "app-pw" not in repr(cfg)


def test_from_env_reads_contact_vars(tmp_path, monkeypatch):
    base = make_cfg(tmp_path)
    env = {
        "FILET_API_NETWORK": "testnet", "FILET_BUILDER_ADDR": base.builder_address,
        "FILET_SIWE_DOMAIN": "d", "FILET_SIWE_URI": "https://d",
        "FILET_API_DB": base.db_path, "FILET_KEYSVC_SOCK": base.keysvc_sock,
        "FILET_PENDING_PATH": base.pending_path, "FILET_EXCHANGE_DIR": base.exchange_dir,
        "FILET_STATE_BASE": base.state_base, "FILET_LEADERS_PATH": base.leaders_path,
        "FILET_CONTACT_SMTP_USER": "site@gmail.com", "FILET_CONTACT_SMTP_PASS": "pw",
        "FILET_CONTACT_TO": "owner@gmail.com",
    }
    cfg = ApiConfig.from_env(env)
    assert cfg.contact_smtp_user == "site@gmail.com"
    assert cfg.contact_smtp_pass == "pw"
    assert cfg.contact_to == "owner@gmail.com"
```

⚠️ `test_from_env_reads_contact_vars` 的 env 鍵名請對照 `config.py:229-305` 的 `from_env`
required 清單（若有其他必填鍵，補上 `make_cfg` 對應值；`FILET_EXPLORE_CACHE_PATH`、
`FILET_ACCRUED_HISTORY_PATH` 等若為必填也一併補）。目標是這個測試只驗 contact 三鍵。

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_contact_module.py -q`
Expected: ImportError（`spark.publicapi.contact` 不存在）。

- [ ] **Step 3: 建 `src/spark/publicapi/contact.py`**

```python
"""publicapi/contact.py — /contact 聯絡表單的驗證、組信與 SMTP 寄送。

分層：`validate_contact` / `build_contact_email` 是純函式（可離線測）；`SmtpMailer`
是唯一副作用點（Gmail SMTP_SSL），透過 `create_app(mailer=...)` 注入，測試給 FakeMailer。
寄信**非冪等**：任何層都不得自動重試（工程原則 2）；失敗由路由層 log + 502。
"""
from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)

NAME_MAX = 80
EMAIL_MAX = 254
MESSAGE_MIN = 10
MESSAGE_MAX = 2000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465
_SMTP_TIMEOUT_S = 20.0


class ContactValidationError(ValueError):
    """detail 為可安全外顯的固定字串（不回顯輸入）。"""


@dataclass(frozen=True)
class ContactInput:
    name: str
    email: str
    message: str


def validate_contact(*, name: str, email: str, message: str) -> ContactInput:
    name = (name or "").strip()
    email = (email or "").strip()
    message = (message or "").strip()
    if not name or len(name) > NAME_MAX or "\r" in name or "\n" in name:
        raise ContactValidationError("姓名為必填，且不得超過 80 字")
    if (not email or len(email) > EMAIL_MAX or "\r" in email or "\n" in email
            or not _EMAIL_RE.match(email)):
        raise ContactValidationError("Email 格式不正確")
    if len(message) < MESSAGE_MIN or len(message) > MESSAGE_MAX:
        raise ContactValidationError("訊息長度需介於 10 到 2000 字")
    return ContactInput(name=name, email=email, message=message)


def build_contact_email(ci: ContactInput, *, sender: str, to: str,
                        client_ip: str, now_iso: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Reply-To"] = ci.email
    msg["Subject"] = f"[Filet 聯絡表單] {ci.name}"
    msg.set_content(
        f"姓名：{ci.name}\nEmail：{ci.email}\n來源 IP：{client_ip}\n時間：{now_iso}\n\n"
        f"訊息：\n{ci.message}\n"
    )
    return msg


class Mailer(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


class SmtpMailer:
    """Gmail SMTP_SSL。密碼只存在實例屬性，不進 repr／log。"""

    def __init__(self, user: str, password: str) -> None:
        self._user = user
        self._password = password

    def __repr__(self) -> str:
        return f"SmtpMailer(user={self._user!r})"

    def send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=_SMTP_TIMEOUT_S) as s:
            s.login(self._user, self._password)
            s.send_message(msg)
```

- [ ] **Step 4: 改 `src/spark/publicapi/config.py`**

在 `ApiConfig` 的 stripe 欄位附近（`config.py:108-114`）新增：

```python
    # ⭐ /contact 聯絡表單 SMTP（2026-09-02）。user/pass 成對（同 stripe trio 慣例）；
    # 都不設 → 端點回 503（表單頁仍可開）。pass 是 secret → repr=False（紅線 2）。
    contact_smtp_user: str | None = None
    contact_smtp_pass: str | None = field(default=None, repr=False)
    contact_to: str | None = None          # 未設 → 預設等於 contact_smtp_user
```

在 `__post_init__` 內（stripe trio 檢查旁）新增：

```python
        if (self.contact_smtp_user is None) != (self.contact_smtp_pass is None):
            raise ValueError("FILET_CONTACT_SMTP_USER 與 FILET_CONTACT_SMTP_PASS 必須成對設定")
        if self.contact_smtp_user is not None and self.contact_to is None:
            object.__setattr__(self, "contact_to", self.contact_smtp_user)
```

（`ApiConfig` 若不是 frozen dataclass，直接 `self.contact_to = ...` 即可。）

新增 property（放 `stripe_enabled` 附近，約 `config.py:218`）：

```python
    @property
    def contact_enabled(self) -> bool:
        return self.contact_smtp_user is not None
```

`from_env` 的 `cls(...)` 建構加三個 kwargs：

```python
                   contact_smtp_user=env.get("FILET_CONTACT_SMTP_USER") or None,
                   contact_smtp_pass=env.get("FILET_CONTACT_SMTP_PASS") or None,
                   contact_to=env.get("FILET_CONTACT_TO") or None,
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_contact_module.py -q` → 全綠。
Run: `uv run pytest tests/test_api_config*.py -q`（若存在）→ 仍綠。
Run: `uv run ruff check src tests` → 無錯。

---

### Task 2 `@inline`：路由 `POST /api/public/contact` ＋ API 測試

**Files:**
- Modify: `src/spark/publicapi/app.py`
  - 模組常數區（`app.py:105-106` 旁）加 `CONTACT_RATELIMIT_WINDOW_S = 600.0`、`CONTACT_RATELIMIT_MAX = 3`
  - `create_app` 簽名（`app.py:1273`）加 `mailer=None`
  - `/api/public/status` 路由（`app.py:2025`）之後加新路由
- Modify: `tests/publicapi_helpers.py:213` `make_app` 加 `mailer=None` 透傳
- Test: `tests/test_api_contact.py`

- [ ] **Step 1: 寫失敗測試 `tests/test_api_contact.py`**

```python
"""tests/test_api_contact.py — POST /api/public/contact：無需登入；驗證→honeypot→IP 限流
→寄信；寄信失敗 502、未設定 503。全離線（FakeMailer；loopback 放行供 TestClient）。"""
import socket
import time
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from spark.publicapi.app import CONTACT_RATELIMIT_MAX, CONTACT_RATELIMIT_WINDOW_S
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class FakeMailer:
    def __init__(self, fail: bool = False):
        self.sent: list[EmailMessage] = []
        self.fail = fail

    def send(self, msg: EmailMessage) -> None:
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(msg)


GOOD = {"name": "Jim", "email": "jim@example.com", "message": "Hello, I have a question about fees."}


def _make(tmp_path, *, configured=True, fail=False):
    over = {"contact_smtp_user": "site@gmail.com", "contact_smtp_pass": "pw",
            "contact_to": "owner@gmail.com"} if configured else {}
    cfg = make_cfg(tmp_path, **over)
    clock = Clock()
    mailer = FakeMailer(fail=fail)
    app, *_ = make_app(tmp_path, cfg=cfg, now_fn=clock, mailer=mailer)
    return TestClient(app, base_url="https://testserver"), clock, mailer


def test_happy_path_sends_mail(tmp_path):
    client, _, mailer = _make(tmp_path)
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(mailer.sent) == 1
    m = mailer.sent[0]
    assert m["To"] == "owner@gmail.com" and m["From"] == "site@gmail.com"
    assert m["Reply-To"] == "jim@example.com"
    assert m["Subject"] == "[Filet 聯絡表單] Jim"
    assert "question about fees" in m.get_content()


def test_honeypot_silently_accepts_without_sending(tmp_path):
    client, _, mailer = _make(tmp_path)
    r = client.post("/api/public/contact", json={**GOOD, "website": "http://spam"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert mailer.sent == []


@pytest.mark.parametrize("bad", [
    {**GOOD, "name": ""}, {**GOOD, "email": "nope"}, {**GOOD, "message": "short"},
    {**GOOD, "name": "a\r\nBcc: x@y.z"},
])
def test_validation_422_does_not_echo_input(tmp_path, bad):
    client, _, mailer = _make(tmp_path)
    r = client.post("/api/public/contact", json=bad)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert "Bcc" not in r.json()["detail"] and "nope" not in r.json()["detail"]
    assert mailer.sent == []


def test_missing_fields_422(tmp_path):
    client, _, mailer = _make(tmp_path)
    assert client.post("/api/public/contact", json={"name": "Jim"}).status_code == 422
    assert mailer.sent == []


def test_ip_ratelimit_429_then_recovers(tmp_path):
    client, clock, mailer = _make(tmp_path)
    for _ in range(CONTACT_RATELIMIT_MAX):
        assert client.post("/api/public/contact", json=GOOD).status_code == 200
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 429
    assert len(mailer.sent) == CONTACT_RATELIMIT_MAX
    clock.t += CONTACT_RATELIMIT_WINDOW_S + 1
    assert client.post("/api/public/contact", json=GOOD).status_code == 200


def test_ratelimit_counts_only_sendable_requests(tmp_path):
    """422 不消耗額度（限流在驗證之後、寄信之前）。"""
    client, _, _ = _make(tmp_path)
    for _ in range(CONTACT_RATELIMIT_MAX + 2):
        assert client.post("/api/public/contact", json={**GOOD, "email": "nope"}).status_code == 422
    assert client.post("/api/public/contact", json=GOOD).status_code == 200


def test_mailer_failure_502_no_retry(tmp_path):
    client, _, mailer = _make(tmp_path, fail=True)
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 502
    assert "來信" in r.json()["detail"]
    assert mailer.sent == []


def test_unconfigured_503(tmp_path):
    client, _, mailer = _make(tmp_path, configured=False)
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 503
    assert mailer.sent == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_api_contact.py -q`
Expected: ImportError（`CONTACT_RATELIMIT_MAX` 不存在）。

- [ ] **Step 3: 改 `tests/publicapi_helpers.py` `make_app`**

簽名加 `mailer=None`；`if mailer is not None: kw["mailer"] = mailer`。

- [ ] **Step 4: 改 `src/spark/publicapi/app.py`**

模組常數（`PROBE_RATELIMIT_MAX` 之後）：

```python
# ⭐ /api/public/contact 的 per-client-IP sliding window（2026-09-02）：無需登入、
# 每次成功呼叫都寄一封信到站主信箱（外送放大面）。獨立於 probe 限流。
CONTACT_RATELIMIT_WINDOW_S = 600.0
CONTACT_RATELIMIT_MAX = 3
```

`create_app` 簽名加 `mailer=None`。函式體內（notifier 預設建構旁）：

```python
    # /contact 寄信通道：未注入 → cfg 有 SMTP 設定則建 SmtpMailer，否則 None（端點 503）。
    if mailer is None and cfg.contact_enabled:
        mailer = SmtpMailer(cfg.contact_smtp_user, cfg.contact_smtp_pass)
```

（檔頭 import：`from spark.publicapi.contact import ContactValidationError, SmtpMailer, build_contact_email, validate_contact`；`from pydantic import BaseModel` 若尚未 import。）

限流（放 `_enforce_probe_ratelimit` 之後，同款 reap 邏輯、獨立 dict/lock）：

```python
    _contact_hits: dict[str, list[float]] = {}
    _contact_lock = threading.Lock()

    def _enforce_contact_ratelimit(client_ip: str) -> None:
        now = now_fn()
        cutoff = now - CONTACT_RATELIMIT_WINDOW_S
        with _contact_lock:
            for k in list(_contact_hits.keys()):
                kept = [t for t in _contact_hits[k] if t > cutoff]
                if kept:
                    _contact_hits[k] = kept
                else:
                    del _contact_hits[k]
            hits = _contact_hits.get(client_ip, [])
            if len(hits) >= CONTACT_RATELIMIT_MAX:
                raise HTTPException(status_code=429, detail="送出過於頻繁，請稍後再試")
            hits.append(now)
            _contact_hits[client_ip] = hits
```

路由（`/api/public/status` 之後）：

```python
    class ContactBody(BaseModel):
        name: str
        email: str
        message: str
        website: str = ""    # honeypot：真人看不到、機器人會填

    @app.post("/api/public/contact")
    def public_contact_endpoint(body: ContactBody, request: Request):
        """/contact 表單：驗證 → honeypot → IP 限流 → 寄信。無需登入。
        寄信非冪等：失敗只 log + 502，不重試（工程原則 2）。"""
        if body.website.strip():
            logger.info("/api/public/contact honeypot 命中，靜默接受")
            return {"ok": True}
        try:
            ci = validate_contact(name=body.name, email=body.email, message=body.message)
        except ContactValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if mailer is None:
            raise HTTPException(status_code=503,
                                detail="聯絡表單暫時無法使用，請直接來信")
        client_ip = request.client.host if request.client else "unknown"
        _enforce_contact_ratelimit(client_ip)
        now_iso = datetime.fromtimestamp(now_fn(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = build_contact_email(ci, sender=cfg.contact_smtp_user, to=cfg.contact_to,
                                  client_ip=client_ip, now_iso=now_iso)
        try:
            mailer.send(msg)
        except Exception as e:  # noqa: BLE001 — 失敗必須外顯（原則 3），但不得洩露信件內容
            logger.error("/api/public/contact 寄信失敗 (%s): %s", type(e).__name__, e)
            raise HTTPException(status_code=502,
                                detail="寄送失敗，請稍後再試或直接來信") from e
        logger.info("/api/public/contact 已寄出（reply-to 網域 %s）", ci.email.rsplit("@", 1)[-1])
        return {"ok": True}
```

⚠️ 如果 `create_app` 內已有其他路由以 `Request` 為參數，沿用其 import。`ContactBody` 若依本檔慣例應放模組層（與其他 BaseModel 同區），照慣例放。

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_api_contact.py tests/test_contact_module.py -q` → 全綠。
Run: `uv run pytest -q` → 全綠（確認 `create_app` 新參數沒弄壞既有 fixture）。
Run: `uv run ruff check src tests scripts` → 無錯。

---

### Task 3 `@inline`：前端 `/contact` 頁、文案、API client、footer、sitemap

**Files:**
- Create: `web/src/app/contact/page.tsx`、`web/src/app/contact/layout.tsx`
- Create: `web/src/app/contact/page.test.tsx`
- Modify: `web/src/lib/copy.ts`（COPY_ZH 加 `contact` 區，COPY_EN 鏡射；`footer` 註解與 `legalContact` 不動）
- Modify: `web/src/lib/api.ts`（加 `postContact`）＋ `web/src/lib/api.test.ts`（若有逐函式測試慣例則補一條）
- Modify: `web/src/components/Footer.tsx:81`（`<a href="https://filet.app/#/contact" …>` → `<Link href="/contact">`）＋ `Footer.test.tsx:40-48`
- Modify: `web/src/app/sitemap.ts`（`SITEMAP_ROUTES` 加 `"/contact"`，放 `/status` 之後）＋ `sitemap.test.ts` 期望清單
- Modify: `web/src/styles/globals.css`（加 `.contact-*` 樣式）

前置：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH"`；測試 `cd web && npm test`。

- [ ] **Step 1: 文案 `copy.ts`**

COPY_ZH（放 `status` 區之後）：

```ts
  /**
   * `/contact` 聯絡頁（2026-09-02）：表單送到 POST /api/public/contact，由站主人工回覆。
   * fallbackEmail 是「表單壞了也找得到人」的保底路徑，錯誤訊息一律附上。
   */
  contact: {
    heading: "聯絡我們",
    sub: "感謝你的關注。任何問題或合作洽詢都歡迎透過下方表單送出，我們會盡快回覆到你留下的 Email。",
    fallbackPrefix: "也可以直接來信：",
    fallbackEmail: "goldwisetw@gmail.com",
    nameLabel: "姓名",
    namePlaceholder: "你的姓名",
    emailLabel: "Email",
    emailPlaceholder: "你的 Email",
    messageLabel: "訊息",
    messagePlaceholder: "想說的話寫在這裡",
    send: "送出",
    sending: "送出中…",
    successTitle: "已送出",
    successBody: "我們已收到你的訊息，會盡快回覆到你留下的 Email。",
    sendAnother: "再送一則",
    errNameRequired: "請填寫姓名（最多 80 字）",
    errEmailInvalid: "請填寫正確的 Email",
    errMessageLength: "訊息長度需介於 10 到 2000 字",
    errNetwork: "無法連線到伺服器，請稍後再試。",
    errGeneric: "送出失敗，請稍後再試。",
  },
```

COPY_EN 鏡射（同 key）：

```ts
  contact: {
    heading: "Contact us",
    sub: "Thank you for your interest. Send any questions or inquiries through the form below and we will reply to the email you leave as promptly as possible.",
    fallbackPrefix: "You can also email us directly: ",
    fallbackEmail: "goldwisetw@gmail.com",
    nameLabel: "Name",
    namePlaceholder: "Your name",
    emailLabel: "Email",
    emailPlaceholder: "Your email",
    messageLabel: "Message",
    messagePlaceholder: "Your message goes here",
    send: "Send",
    sending: "Sending…",
    successTitle: "Message sent",
    successBody: "We have received your message and will reply to the email you provided.",
    sendAnother: "Send another",
    errNameRequired: "Please enter your name (up to 80 characters)",
    errEmailInvalid: "Please enter a valid email address",
    errMessageLength: "Message must be between 10 and 2000 characters",
    errNetwork: "Could not reach the server. Please try again later.",
    errGeneric: "Sending failed. Please try again later.",
  },
```

- [ ] **Step 2: `api.ts` 加**

```ts
// ---------- /contact（無需登入）----------
export interface ContactBody { name: string; email: string; message: string; website?: string }
export function postContact(body: ContactBody): Promise<{ ok: boolean }> {
  return post("/api/public/contact", body);
}
```

- [ ] **Step 3: 寫失敗測試 `web/src/app/contact/page.test.tsx`**

參考 `web/src/app/settings/page.test.tsx` 的 render／mock 慣例（LangProvider 包法、`vi.mock("@/lib/api")`）。必測案例：

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ContactPage from "./page";
import { COPY_ZH } from "@/lib/copy";
import { ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (orig) => {
  const mod = await orig<typeof import("@/lib/api")>();
  return { ...mod, postContact: vi.fn() };
});
import { postContact } from "@/lib/api";
const mocked = vi.mocked(postContact);

// renderPage(): 依 settings/page.test.tsx 的 provider 包法渲染 <ContactPage />
function fill(name: string, email: string, message: string) {
  fireEvent.change(screen.getByLabelText(COPY_ZH.contact.nameLabel), { target: { value: name } });
  fireEvent.change(screen.getByLabelText(COPY_ZH.contact.emailLabel), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(COPY_ZH.contact.messageLabel), { target: { value: message } });
}

describe("/contact", () => {
  beforeEach(() => mocked.mockReset());

  it("渲染標題、三欄位、送出鈕與保底 mailto", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: COPY_ZH.contact.heading })).toBeInTheDocument();
    expect(screen.getByLabelText(COPY_ZH.contact.nameLabel)).toBeInTheDocument();
    expect(screen.getByLabelText(COPY_ZH.contact.emailLabel)).toBeInTheDocument();
    expect(screen.getByLabelText(COPY_ZH.contact.messageLabel)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY_ZH.contact.fallbackEmail }))
      .toHaveAttribute("href", "mailto:goldwisetw@gmail.com");
  });

  it("客端驗證：空姓名／壞 email／短訊息不打 API", () => {
    renderPage();
    fill("", "nope", "short");
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    expect(mocked).not.toHaveBeenCalled();
    expect(screen.getByText(COPY_ZH.contact.errNameRequired)).toBeInTheDocument();
  });

  it("成功送出 → 顯示成功卡、表單消失", async () => {
    mocked.mockResolvedValue({ ok: true });
    renderPage();
    fill("Jim", "jim@example.com", "Hello, a question about fees.");
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    await waitFor(() => expect(screen.getByText(COPY_ZH.contact.successTitle)).toBeInTheDocument());
    expect(mocked).toHaveBeenCalledWith({
      name: "Jim", email: "jim@example.com", message: "Hello, a question about fees.", website: "",
    });
    expect(screen.queryByLabelText(COPY_ZH.contact.nameLabel)).not.toBeInTheDocument();
  });

  it("後端 detail（422/429/502）原樣顯示；network 顯示 errNetwork", async () => {
    mocked.mockRejectedValueOnce(new ApiError("client", "送出過於頻繁，請稍後再試", 429));
    renderPage();
    fill("Jim", "jim@example.com", "Hello, a question about fees.");
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    await waitFor(() => expect(screen.getByText("送出過於頻繁，請稍後再試")).toBeInTheDocument());
    // 表單仍在，可重試
    expect(screen.getByLabelText(COPY_ZH.contact.nameLabel)).toBeInTheDocument();

    mocked.mockRejectedValueOnce(new ApiError("network", "x"));
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    await waitFor(() => expect(screen.getByText(COPY_ZH.contact.errNetwork)).toBeInTheDocument());
  });
});
```

（`ApiError` 建構子簽名見 `web/src/lib/api.ts:62`，照實際簽名調整。）

- [ ] **Step 4: 建 `layout.tsx`**（照 `web/src/app/risk/layout.tsx`）

```tsx
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "聯絡我們",
  description: "透過表單聯絡 Filet 團隊，我們會盡快回覆。",
  alternates: { canonical: canonicalUrl("/contact") },
};

export default function ContactLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
```

- [ ] **Step 5: 建 `page.tsx`**

```tsx
"use client";
/**
 * `/contact` — 聯絡表單（2026-09-02）。POST /api/public/contact（無需登入），後端寄信到
 * 站主信箱、人工回覆。未登入可直接開啟，不掛登入 guard（同 /terms /privacy /risk）。
 * honeypot 欄位 `website`：視覺隱藏＋tabIndex=-1，真人不會填；後端見非空即靜默接受。
 */
import { useState, type FormEvent } from "react";
import { useCopy } from "@/lib/lang";
import { ApiError, postContact } from "@/lib/api";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
type Phase = "idle" | "sending" | "sent";

export default function ContactPage() {
  const c = useCopy().contact;
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  function validate(): string | null {
    const n = name.trim(), e = email.trim(), m = message.trim();
    if (!n || n.length > 80) return c.errNameRequired;
    if (!e || e.length > 254 || !EMAIL_RE.test(e)) return c.errEmailInvalid;
    if (m.length < 10 || m.length > 2000) return c.errMessageLength;
    return null;
  }

  async function onSubmit(ev: FormEvent) {
    ev.preventDefault();
    const v = validate();
    if (v) { setError(v); return; }
    setError(null);
    setPhase("sending");
    try {
      await postContact({ name: name.trim(), email: email.trim(), message: message.trim(), website });
      setPhase("sent");
    } catch (e) {
      setPhase("idle");
      if (e instanceof ApiError) setError(e.kind === "network" ? c.errNetwork : (e.detail ?? c.errGeneric));
      else setError(c.errGeneric);
    }
  }

  function reset() {
    setName(""); setEmail(""); setMessage(""); setWebsite(""); setError(null); setPhase("idle");
  }

  return (
    <main className="page contact-page">
      <header className="contact-head">
        <h1>{c.heading}</h1>
        <p className="section-sub">{c.sub}</p>
        <p className="hint">
          {c.fallbackPrefix}
          <a href={`mailto:${c.fallbackEmail}`}>{c.fallbackEmail}</a>
        </p>
      </header>

      {phase === "sent" ? (
        <section className="card contact-card contact-success" role="status">
          <h2>{c.successTitle}</h2>
          <p>{c.successBody}</p>
          <button type="button" className="btn btn-secondary" onClick={reset}>{c.sendAnother}</button>
        </section>
      ) : (
        <form className="card contact-card" onSubmit={onSubmit} noValidate>
          <label className="addr-field" htmlFor="contact-name">
            <span className="addr-field-label">{c.nameLabel}</span>
            <input id="contact-name" className="addr-input" type="text" value={name}
              maxLength={80} autoComplete="name" placeholder={c.namePlaceholder}
              onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="addr-field" htmlFor="contact-email">
            <span className="addr-field-label">{c.emailLabel}</span>
            <input id="contact-email" className="addr-input" type="email" value={email}
              maxLength={254} autoComplete="email" placeholder={c.emailPlaceholder}
              onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label className="addr-field" htmlFor="contact-message">
            <span className="addr-field-label">{c.messageLabel}</span>
            <textarea id="contact-message" className="addr-input contact-textarea" value={message}
              maxLength={2000} rows={8} placeholder={c.messagePlaceholder}
              onChange={(e) => setMessage(e.target.value)} />
          </label>
          <input className="visually-hidden" tabIndex={-1} autoComplete="off" aria-hidden="true"
            name="website" value={website} onChange={(e) => setWebsite(e.target.value)} />
          {error && <p className="addr-input-error" role="alert">{error}</p>}
          <div className="contact-actions">
            <button type="submit" className="btn btn-primary" disabled={phase === "sending"}>
              {phase === "sending" ? c.sending : c.send}
            </button>
          </div>
        </form>
      )}
    </main>
  );
}
```

`ApiError` 的 `kind` / `detail` 屬性名以 `api.ts:62` 實際定義為準。

- [ ] **Step 6: CSS `globals.css`**（加在 `.addr-input-error` 區塊之後）

```css
/* ---------- /contact ---------- */
.contact-page { max-width: 760px; }
.contact-head { margin-bottom: 20px; }
.contact-card { padding: 24px; display: grid; gap: 16px; }
.contact-textarea { resize: vertical; min-height: 160px; font-family: inherit; }
textarea.addr-input:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.contact-actions { display: flex; justify-content: flex-end; }
.contact-success h2 { margin: 0 0 8px; }
```

（若 `main.page` 已有 max-width 且與 760 不衝突就省略 `.contact-page` 那行。）

- [ ] **Step 7: Footer、sitemap、對應測試**

`Footer.tsx:81` 改為 `<Link href="/contact">{c.legalContact}</Link>`；`Footer.test.tsx:40-48` 的
測試名改「法務欄連向 /terms /privacy /risk /contact」、斷言 href `"/contact"`。
`copy.ts:100` footer 註解裡的 `filet.app/#/contact` 改成 `/contact`。
`sitemap.ts` `SITEMAP_ROUTES` 在 `"/status"` 後加 `"/contact"`；`sitemap.test.ts:6` 的期望清單同步。

- [ ] **Step 8: 驗收**

Run: `cd web && npm test` → 全綠（含 copy.test 的 zh/en 對稱＋en 無 CJK、禁詞零命中）。
Run: `cd web && npx tsc --noEmit` → 無錯。
Run: `cd web && npm run lint` → 無錯（若 script 存在）。

---

### Task 4 `@inline`：部署文件與回歸清單

**Files:**
- Modify: `scripts/filet_regression_check.py:67-69` `FRONTEND_ROUTES` 在 `"/status"` 後加 `"/contact"`
- Modify: `deploy/filet-api.service`（Stripe 註解區之後）
- Modify: `deploy/RUNBOOK.md`（§5 之下新增小節；位置選在 §5.8a 附近或現有「選配」段）
- Modify: `CLAUDE.md` 慣例段「公開路由」清單加 `/contact`

- [ ] **Step 1: service 註解範例**

```
# /contact 聯絡表單（2026-09-02，選配）：Gmail SMTP 應用程式密碼（需帳號 2FA）。
# USER/PASS 成對，只設一個會拒絕啟動；都不設 → 表單頁仍可開、送出回 503。
# 申請與驗收見 RUNBOOK §「聯絡表單 SMTP」。
#Environment=FILET_CONTACT_SMTP_USER=goldwisetw@gmail.com
#Environment=FILET_CONTACT_SMTP_PASS=REPLACE_WITH_GMAIL_APP_PASSWORD
#Environment=FILET_CONTACT_TO=goldwisetw@gmail.com
```

- [ ] **Step 2: RUNBOOK 新小節**（照現有小節格式，含以下內容）

1. 申請：Google 帳號 → 安全性 → 兩步驟驗證開啟 → 「應用程式密碼」→ 建一組（16 碼），只出現一次。
2. 落地：**不要**寫進 repo 的 service 檔；用 `sudo systemctl edit filet-api`（drop-in override）貼三行
   `Environment=`，然後 `sudo systemctl daemon-reload && sudo systemctl restart filet-api`。
3. 驗收：
```bash
curl -s -X POST https://app.filet.trade/api/public/contact \
  -H 'Content-Type: application/json' \
  -d '{"name":"runbook-check","email":"goldwisetw@gmail.com","message":"deploy verification message"}'
# 期望 {"ok":true}，且 goldwisetw@gmail.com 收件匣出現「[Filet 聯絡表單] runbook-check」
# （Gmail 自己寄給自己有時歸入「寄件備份」或被合併為同一串；搜尋主旨確認）
```
4. 限流：同一 IP 10 分鐘 3 次，超過 429；驗收後不要連打。
5. 未設定時的症狀：送出回 503「聯絡表單暫時無法使用」，頁面照常顯示 mailto 保底。

- [ ] **Step 3: 驗收**

Run: `uv run ruff check scripts`；`grep -n '"/contact"' scripts/filet_regression_check.py` 有命中；
`grep -n FILET_CONTACT deploy/filet-api.service deploy/RUNBOOK.md` 各有命中。

---

### Task 5 `@inline`：reviewer 修正（2026-09-02 opus 審查後）

**Files:**
- Modify: `src/spark/publicapi/contact.py`（驗證強化）
- Modify: `src/spark/publicapi/app.py`（in-flight 上限、寄信失敗告警）
- Modify: `tests/test_contact_module.py`、`tests/test_api_contact.py`
- Modify: `deploy/filet-api.service`、`deploy/RUNBOOK.md`

**5-1 驗證強化（Warning：U+2028 等分隔字元 → 500；非 ASCII email → Reply-To 壞掉）**

`contact.py`：

```python
# str.splitlines() 視為換行的全部字元。EmailMessage 對含這些字元的 header 會 raise
# ValueError（落在路由 try 之外 → 500）；在驗證層先擋成 422。
_LINEBREAK_CHARS = frozenset("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
# 只收 ASCII addr-spec：非 ASCII 位址會被 EmailMessage 編成 RFC 2047 encoded-word 塞進
# Reply-To（=非法位址，站主按回覆會退信）。國際化位址（EAI）本站不支援。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")


def _has_linebreak(s: str) -> bool:
    return any(ch in _LINEBREAK_CHARS for ch in s)
```

`validate_contact` 的 name/email 檢查改用 `_has_linebreak(name)`／`_has_linebreak(email)` 取代 `"\r" in … or "\n" in …`。

`tests/test_contact_module.py` 的 `test_validate_rejects` parametrize 追加：

```python
    ("Jim\u2028Bcc: x@y.z", "a@b.co", "x" * 20),   # U+2028 分隔字元
    ("Jim\x85x", "a@b.co", "x" * 20),               # NEL
    ("Jim", "測試@例え.jp", "x" * 20),               # 非 ASCII email
    ("Jim", "a@b", "x" * 20),                        # 無 TLD
```

`tests/test_api_contact.py` 的 `test_validation_422_does_not_echo_input` parametrize 追加
`{**GOOD, "name": "a\u2028Bcc: x@y.z"}`（斷言 422，非 500）。

**5-2 in-flight 上限（Warning：sync 端點阻塞 threadpool 拖垮整個 API）**

`app.py` 模組常數（`CONTACT_RATELIMIT_MAX` 之後）：

```python
# 同時在寄信中的請求上限：smtplib 阻塞在 starlette threadpool（40 執行緒，全 API 共用），
# Gmail 變慢時不得讓 /contact 佔滿執行緒拖垮 dashboard／explore。超限回 503。
CONTACT_MAX_INFLIGHT = 2
```

`create_app` 內（`_contact_lock` 旁）：`_contact_inflight = threading.BoundedSemaphore(CONTACT_MAX_INFLIGHT)`。

路由：在 `_enforce_contact_ratelimit(client_ip)` **之前**：

```python
        if not _contact_inflight.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="系統忙碌中，請稍後再試")
        try:
            _enforce_contact_ratelimit(client_ip)
            ...（組信、mailer.send、log）...
            return {"ok": True}
        finally:
            _contact_inflight.release()
```

（限流 429 與寄信 502 都在 try 內 raise，finally 保證釋放。）

測試 `tests/test_api_contact.py` 追加：

```python
from spark.publicapi.app import CONTACT_MAX_INFLIGHT
import threading


class BlockingMailer:
    """send 卡在 Event 上，讓測試把 in-flight 佔滿。"""
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self.sent = 0
    def send(self, msg):
        self.entered.release()
        assert self.release.wait(timeout=10)
        self.sent += 1


def test_inflight_cap_503_and_recovers(tmp_path):
    cfg = make_cfg(tmp_path, contact_smtp_user="site@gmail.com", contact_smtp_pass="pw")
    mailer = BlockingMailer()
    app, *_ = make_app(tmp_path, cfg=cfg, mailer=mailer)
    results = []
    def worker():
        c = TestClient(app, base_url="https://testserver")
        results.append(c.post("/api/public/contact", json=GOOD).status_code)
    threads = [threading.Thread(target=worker) for _ in range(CONTACT_MAX_INFLIGHT)]
    for t in threads:
        t.start()
    for _ in range(CONTACT_MAX_INFLIGHT):
        assert mailer.entered.acquire(timeout=10)   # 全部卡在 send 內
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/public/contact", json=GOOD).status_code == 503
    mailer.release.set()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == [200] * CONTACT_MAX_INFLIGHT
    # 釋放後恢復（同 IP 額度 3 次：前面 2 次成功 +1 次 503 不計額度 → 這次仍在額度內）
    assert c.post("/api/public/contact", json=GOOD).status_code == 200
    assert mailer.sent == CONTACT_MAX_INFLIGHT + 1
```

**5-3 寄信失敗告警（Suggestion 採納：失敗要外顯給能修的人）**

路由的 `except Exception as e:` 區塊，在 `logger.error` 之後、raise 之前，用既有注入的 `notifier`
（呼叫形狀照 `app.py:1723` 附近的 `notifier.critical(...)`）送一則告警，**1 小時冷卻**（避免
SMTP 全斷時每次送出都推一則）：

```python
    _contact_alert_last = [0.0]     # create_app 內，與 _contact_lock 同層

            now = now_fn()
            if now - _contact_alert_last[0] >= 3600:
                _contact_alert_last[0] = now
                try:
                    notifier.critical(<照既有呼叫形狀：標題「/contact 寄信失敗」＋ type(e).__name__>)
                except Exception as ne:  # noqa: BLE001 — 告警失敗不得蓋掉原錯誤
                    logger.error("/contact 失敗告警送出失敗: %r", ne)
```

告警內容只含例外型別，不含信件內容與密碼。測試：`make_app(..., notifier=RecordingNotifier)`
（既有 helper，見 `tests/publicapi_helpers.py` 的 notifier 注入說明）——mailer fail 兩次
→ notifier 只收到 1 則；假時鐘推 3600s 後再 fail → 收到第 2 則。

**5-4 部署（Critical：應用程式密碼不得走 drop-in 行內 `Environment=`）**

`deploy/filet-api.service`：把 Task 4 加的三行 `#Environment=FILET_CONTACT_*` 註解改成說明
「密碼走 `/etc/filet/contact.env`（0640 root:filet-api，絕不進 repo）」，並在 `Environment=` 區
之後加一行真的 `EnvironmentFile=-/etc/filet/contact.env`（`-`＝檔不存在不擋啟動，此時端點回 503）。

`deploy/RUNBOOK.md` §5.8b 第 2 點改寫，照 §「Telegram 憑證檔」（RUNBOOK.md:1831-1841）同款：

```bash
# 內容格式（密碼為佔位，換成 Google 應用程式密碼；不要把真值寫進任何 repo 檔案或 commit）：
#   FILET_CONTACT_SMTP_USER=goldwisetw@gmail.com
#   FILET_CONTACT_SMTP_PASS=REPLACE_WITH_GMAIL_APP_PASSWORD
#   FILET_CONTACT_TO=goldwisetw@gmail.com
sudo install -m 640 -o root -g filet-api /dev/null /etc/filet/contact.env
sudo $EDITOR /etc/filet/contact.env      # 貼上面三行
sudo systemctl daemon-reload && sudo systemctl restart filet-api
# 驗收：systemctl show filet-api -p EnvironmentFiles 含 /etc/filet/contact.env；
#       ls -l /etc/filet/contact.env → -rw-r----- root filet-api
```

並明寫「**不要**用 `systemctl edit` drop-in 行內 `Environment=` 放密碼（0644、`systemctl cat` 對所有帳號可讀）」。
RUNBOOK.md:2163 §8 驗收 4 的「66 條」改「67 條」（2406 行是歷史部署記錄，不動）。

**驗收**：`uv run pytest tests/test_contact_module.py tests/test_api_contact.py -q` 全綠；
`uv run pytest -q` 全綠；`uv run ruff check src tests scripts`；
`grep -n "EnvironmentFile=-/etc/filet/contact.env" deploy/filet-api.service` 命中；
`grep -n "systemctl edit" deploy/RUNBOOK.md` 在 §5.8b 範圍內只出現在「不要」句；
`grep -n "67 條" deploy/RUNBOOK.md` 命中 §8。

---

## Task 6：照 Claude Design 設計稿重做（2026-09-02 使用者退版）

> 第一版照錯了參考圖（淺色「CONTACT US」三欄位表單）。真正的設計稿是**雙欄深色**版，且**沒有姓名欄**。
> 寄信通道（SmtpMailer／限流／in-flight／告警／部署憑證）全部保留；改的是**欄位**與**頁面**。

### 設計稿規格（截圖逐項對照，實作以此為準）

**版面**：`main.page` 內雙欄 grid（左 1fr、右 1.4fr，gap 40px；<900px 折成單欄）。

**左欄**：
1. 眉標 `CONTACT`（`.eyebrow`，主色綠）。
2. `h1` 聯絡我們（大、粗）。
3. 說明段：「跟單問題、費用疑義、安全回報或合作提案都從這裡送出。我們會以你留下的 Email 回覆，通常在 1 個工作日內。」
4. 卡片「送出前請確認」（小標，dim），三列：
   - `01` 跟單或帳務問題請附上錢包地址，我們才能對到鏈上紀錄。
   - `02` 回信主旨會帶工單編號 FLT-XXXX-XXXX，請以此辨識是否為我們的回覆。
   - `!`（`--neg` 紅）Filet 團隊**永遠不會**向你索取私鑰、助記詞或要求轉帳。收到此類訊息一律視為詐騙。（「永遠不會」粗體）
   - 編號用 mono 主色綠；`!` 用 mono 紅。
5. 卡片下方一行、前面琥珀色圓點：「安全漏洞請選擇「安全回報」，將進入優先處理佇列。」

**右欄**（一張 `.card`，padding 32px，欄位垂直 gap 24px）：
1. **主題**（label）＋五顆單選 chip：跟單問題／費用與帳務／安全回報／合作提案／其他。選中＝主色綠邊框＋綠字＋淡綠底；未選＝一般邊框。預設選「跟單問題」。用 `<button type="button" role="radio" aria-checked>` 包在 `role="radiogroup"`。
2. **Email** `*`（必填星號紅）；label 列右側 dim 小字「回覆會寄到這裡」；placeholder `you@example.com`。
3. **錢包地址**；label 列右側 dim 小字「選填 · 已登入時自動帶入」。
   - 已登入（`useMe().data?.address` 有值）且使用者未手動改過：input **readOnly**、mono、顯示 `shortAddr(address)`，右側綠色小字徽章「已連結錢包」；旁邊一個小文字按鈕「改填其他地址」按下後變成空白可編輯 input（badge 消失）。送出時送**完整**地址。
   - 未登入：空白可編輯 input，mono，placeholder `0x…`。
4. **訊息** `*`；label 列右側 mono dim 計數 `N / 2000` 即時更新；textarea rows=7，placeholder：「請描述發生什麼事、大約時間（UTC）、以及你預期的結果。若是跟單問題，附上策略名稱或跟隨的地址會更快。」
5. 底列：左側 dim 兩行同意文字「送出即表示你同意我們使用此 Email 回覆你的問題，不會用於其他用途。」；右側主色按鈕「送出訊息」（送出中「送出中…」＋ disabled）。
6. 錯誤：在底列上方紅字 `role=alert`（後端 detail 原樣；network → errNetwork；502/503 → detail ＋「或直接來信 goldwisetw@gmail.com」）。
7. **成功態**（取代右卡內容）：標題「已送出」、mono 大字工單編號 `FLT-XXXX-XXXX`、說明「我們會回覆到 {email}，回信主旨會帶上這個編號。」、按鈕「再送一則」。

**沒有的東西**：姓名欄、頁面上的 mailto 保底句（只在錯誤訊息裡出現）。

### 主線程裁決（Task 6）

| 項目 | 裁決 |
|---|---|
| 主題 enum | 後端 `topic` ∈ `copytrade / billing / security / partnership / other`；後端保存中文標籤對照表 `CONTACT_TOPIC_LABELS`（信件主旨用）。 |
| 工單編號 | 後端產生 `FLT-XXXX-XXXX`，字元集 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（去 0/O/1/I），`secrets.choice`。回應 `{"ok": true, "ticket": "FLT-…"}`。 |
| 信件主旨 | `[FLT-XXXX-XXXX] Filet 聯絡表單：<主題中文>`；security 主題前面再加 `【安全回報】`。站主回信時 Gmail 會保留主旨 → 用戶看到編號。 |
| 安全回報優先 | topic=security 成功寄出後**另外**呼叫 `notifier.critical("contact_security_report", f"/contact 安全回報 {ticket}", dedup_key=ticket)`——這就是「優先處理佇列」的實作（TG 是站主唯一即時通道）。告警失敗只 log。 |
| 錢包地址 | 選填；非空時必須符合 `^0x[0-9a-fA-F]{40}$`（strip 後），否則 422「錢包地址格式不正確」。信件 body 印原值；空則印「（未提供）」。 |
| 欄位驗證 | email 與 message 規則不變（ASCII email、10–2000 字、分隔字元擋掉）。`name` 欄位**移除**（`ContactInput` 改為 `topic / email / wallet / message`）。 |
| honeypot／限流／in-flight／不重試／失敗告警 | 全部不變。 |
| 前端 API | `postContact({topic, email, wallet, message, website})` → `{ok, ticket}`。 |

### Task 6A `@inline`：後端欄位改版

**Files:** `src/spark/publicapi/contact.py`、`src/spark/publicapi/app.py`（`ContactBody`＋路由）、`tests/test_contact_module.py`、`tests/test_api_contact.py`

`contact.py` 新增／修改：

```python
import secrets

CONTACT_TOPICS = ("copytrade", "billing", "security", "partnership", "other")
CONTACT_TOPIC_LABELS = {
    "copytrade": "跟單問題", "billing": "費用與帳務", "security": "安全回報",
    "partnership": "合作提案", "other": "其他",
}
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TICKET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_ticket_id() -> str:
    part = lambda: "".join(secrets.choice(_TICKET_ALPHABET) for _ in range(4))  # noqa: E731
    return f"FLT-{part()}-{part()}"


@dataclass(frozen=True)
class ContactInput:
    topic: str
    email: str
    wallet: str          # "" ＝ 未提供
    message: str


def validate_contact(*, topic: str, email: str, wallet: str, message: str) -> ContactInput:
    topic = (topic or "").strip()
    email = (email or "").strip()
    wallet = (wallet or "").strip()
    message = (message or "").strip()
    if topic not in CONTACT_TOPICS:
        raise ContactValidationError("請選擇主題")
    if (not email or len(email) > EMAIL_MAX or _has_linebreak(email)
            or not _EMAIL_RE.match(email)):
        raise ContactValidationError("Email 格式不正確")
    if wallet and not WALLET_RE.match(wallet):
        raise ContactValidationError("錢包地址格式不正確")
    if len(message) < MESSAGE_MIN or len(message) > MESSAGE_MAX:
        raise ContactValidationError("訊息長度需介於 10 到 2000 字")
    return ContactInput(topic=topic, email=email, wallet=wallet, message=message)


def build_contact_email(ci: ContactInput, *, ticket: str, sender: str, to: str,
                        client_ip: str, now_iso: str) -> EmailMessage:
    label = CONTACT_TOPIC_LABELS[ci.topic]
    prefix = "【安全回報】" if ci.topic == "security" else ""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Reply-To"] = ci.email
    msg["Subject"] = f"[{ticket}] {prefix}Filet 聯絡表單：{label}"
    msg.set_content(
        f"工單：{ticket}\n主題：{label}\nEmail：{ci.email}\n"
        f"錢包地址：{ci.wallet or '（未提供）'}\n來源 IP：{client_ip}\n時間：{now_iso}\n\n"
        f"訊息：\n{ci.message}\n"
    )
    return msg
```

`NAME_MAX` 與 name 相關程式碼刪除。`app.py`：`ContactBody` 改為 `topic: str; email: str; wallet: str = ""; message: str; website: str = ""`；路由呼叫 `validate_contact(topic=..., email=..., wallet=..., message=...)`；寄信前 `ticket = new_ticket_id()`，`build_contact_email(ci, ticket=ticket, ...)`；成功後若 `ci.topic == "security"` 走裁決表的 `notifier.critical`（try/except 只 log）；回傳 `{"ok": True, "ticket": ticket}`；成功 log 加上 ticket（不含 email）。

測試改寫要點（`tests/test_contact_module.py`）：
- 驗證通過案例改用 `topic="copytrade"`, `wallet=""`；wallet 合法 `"0x" + "ab"*20` 通過、`"0x123"` 拒絕、`"0x"+"zz"*20` 拒絕；topic 不在清單拒絕；email／message 既有拒絕案例保留（去掉 name 相關）。
- `test_build_email_headers_and_body`：Subject == `"[FLT-AAAA-BBBB] Filet 聯絡表單：跟單問題"`（ticket 由參數傳入）；security 主題 Subject 含 `【安全回報】`；body 含工單、錢包（空 → 「（未提供）」）。
- `new_ticket_id()`：符合 `^FLT-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$`，連取 50 次無重複。

`tests/test_api_contact.py`：`GOOD = {"topic": "copytrade", "email": "jim@example.com", "message": "..."}`；happy path 斷言 `r.json()["ok"] is True` 且 `ticket` 符合正則、`mailer.sent[0]["Subject"].startswith(f"[{ticket}]")`；422 案例改為壞 topic／壞 wallet／缺 email；新增 `test_security_topic_alerts_notifier`（RecordingNotifier 收到 1 則、dedup_key == ticket）與 `test_non_security_topic_no_alert`；其餘（honeypot、限流、in-flight、502、503、失敗告警冷卻）改 payload 後保留。

驗收：`uv run pytest tests/test_contact_module.py tests/test_api_contact.py -q` 全綠；`uv run pytest -q` 全綠；`uv run ruff check src tests`；`grep -n "name" src/spark/publicapi/contact.py` 只剩與欄位無關的命中（例如 `__name__`）。

### Task 6B `@inline`：前端照設計稿重做

**Files:** `web/src/app/contact/page.tsx`（重寫）、`web/src/app/contact/page.test.tsx`（重寫）、`web/src/lib/copy.ts`（`contact` 區整段換掉，zh 1632 / en 3125 附近）、`web/src/lib/api.ts`（`ContactBody`／`postContact` 回傳型別）、`web/src/lib/api.test.ts`（契約測試同步）、`web/src/styles/globals.css`（`.contact-*` 整段換掉）

既有可沿用：`useMe()`（`web/src/lib/hooks.ts:17`，react-query 結果，`me.data?.address`）、`shortAddr`（`web/src/lib/format.ts:2`）、`.eyebrow`（globals.css:342）、`.card`、`.addr-input`／`.addr-field`／`.addr-field-label`／`.addr-input-error`、`.btn.btn-primary`／`.btn-secondary`、`.mono`、`.neg`、`.hint`、`--primary`／`--primary-rgb`／`--neg`／`--text-dim`／`--border` tokens；雙欄範例 `.strategy-detail-grid`（globals.css:1233）。

**copy.ts `contact` 區（zh；en 鏡射、無 CJK）：**

```ts
  contact: {
    eyebrow: "CONTACT",
    heading: "聯絡我們",
    sub: "跟單問題、費用疑義、安全回報或合作提案都從這裡送出。我們會以你留下的 Email 回覆，通常在 1 個工作日內。",
    checklistTitle: "送出前請確認",
    check1: "跟單或帳務問題請附上錢包地址，我們才能對到鏈上紀錄。",
    check2: "回信主旨會帶工單編號 FLT-XXXX-XXXX，請以此辨識是否為我們的回覆。",
    checkWarnPrefix: "Filet 團隊",
    checkWarnStrong: "永遠不會",
    checkWarnSuffix: "向你索取私鑰、助記詞或要求轉帳。收到此類訊息一律視為詐騙。",
    securityNote: "安全漏洞請選擇「安全回報」，將進入優先處理佇列。",
    topicLabel: "主題",
    topics: { copytrade: "跟單問題", billing: "費用與帳務", security: "安全回報", partnership: "合作提案", other: "其他" },
    emailLabel: "Email",
    emailHint: "回覆會寄到這裡",
    emailPlaceholder: "you@example.com",
    walletLabel: "錢包地址",
    walletHint: "選填 · 已登入時自動帶入",
    walletPlaceholder: "0x…",
    walletConnected: "已連結錢包",
    walletUseOther: "改填其他地址",
    messageLabel: "訊息",
    messagePlaceholder: "請描述發生什麼事、大約時間（UTC）、以及你預期的結果。若是跟單問題，附上策略名稱或跟隨的地址會更快。",
    consent: "送出即表示你同意我們使用此 Email 回覆你的問題，不會用於其他用途。",
    send: "送出訊息",
    sending: "送出中…",
    successTitle: "已送出",
    successBody: "我們會回覆到 {email}，回信主旨會帶上這個編號。",
    sendAnother: "再送一則",
    fallbackEmail: "goldwisetw@gmail.com",
    fallbackPrefix: "或直接來信 ",
    errEmailInvalid: "請填寫正確的 Email",
    errWalletInvalid: "錢包地址格式不正確（0x 開頭 + 40 位十六進位）",
    errMessageLength: "訊息長度需介於 10 到 2000 字",
    errNetwork: "無法連線到伺服器，請稍後再試。",
    errGeneric: "送出失敗，請稍後再試。",
  },
```

（`{email}` 用 `.replace("{email}", email)` 代入；`topics` 是巢狀物件，copy.test 的深層對稱檢查會涵蓋。）

**api.ts：**

```ts
export type ContactTopic = "copytrade" | "billing" | "security" | "partnership" | "other";
export interface ContactBody { topic: ContactTopic; email: string; wallet: string; message: string; website?: string }
export interface ContactResp { ok: boolean; ticket: string }
export function postContact(body: ContactBody): Promise<ContactResp> {
  return post("/api/public/contact", body);
}
```

**page.tsx 結構：**

```tsx
<main className="page contact-page">
  <div className="contact-grid">
    <section className="contact-intro">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1 className="contact-title">{c.heading}</h1>
      <p className="contact-sub">{c.sub}</p>
      <div className="card contact-checklist">
        <p className="contact-checklist-title">{c.checklistTitle}</p>
        <ol className="contact-checklist-list">
          <li><span className="contact-check-num mono">01</span><span>{c.check1}</span></li>
          <li><span className="contact-check-num mono">02</span><span>{c.check2}</span></li>
          <li><span className="contact-check-num mono neg">!</span>
              <span>{c.checkWarnPrefix}<strong>{c.checkWarnStrong}</strong>{c.checkWarnSuffix}</span></li>
        </ol>
      </div>
      <p className="contact-security-note"><span className="contact-dot" aria-hidden />{c.securityNote}</p>
    </section>

    <section className="card contact-card">
      {phase === "sent" ? <成功態/> : (
        <form onSubmit={onSubmit} noValidate className="contact-form">
          <div className="contact-field">
            <div className="contact-label-row"><span className="contact-label">{c.topicLabel}</span></div>
            <div className="contact-chips" role="radiogroup" aria-label={c.topicLabel}>
              {TOPICS.map(t => <button key={t} type="button" role="radio" aria-checked={topic===t}
                 className={"contact-chip" + (topic===t ? " is-active" : "")} onClick={() => setTopic(t)}>{c.topics[t]}</button>)}
            </div>
          </div>
          <div className="contact-field">
            <div className="contact-label-row">
              <label className="contact-label" htmlFor="contact-email">{c.emailLabel} <span className="neg">*</span></label>
              <span className="contact-hint">{c.emailHint}</span>
            </div>
            <input id="contact-email" className="addr-input" type="email" ... />
          </div>
          <div className="contact-field">
            <div className="contact-label-row">
              <label className="contact-label" htmlFor="contact-wallet">{c.walletLabel}</label>
              <span className="contact-hint">{c.walletHint}</span>
            </div>
            <div className="contact-wallet-wrap">
              <input id="contact-wallet" className="addr-input mono" readOnly={autofilled}
                     value={autofilled ? shortAddr(meAddress) : wallet} ... />
              {autofilled && <span className="contact-wallet-badge">{c.walletConnected}</span>}
            </div>
            {autofilled && <button type="button" className="contact-link-btn" onClick={useOther}>{c.walletUseOther}</button>}
          </div>
          <div className="contact-field">
            <div className="contact-label-row">
              <label className="contact-label" htmlFor="contact-message">{c.messageLabel} <span className="neg">*</span></label>
              <span className="contact-hint mono">{message.length} / 2000</span>
            </div>
            <textarea id="contact-message" className="addr-input contact-textarea" rows={7} maxLength={2000} ... />
          </div>
          <input className="visually-hidden" tabIndex={-1} autoComplete="off" aria-hidden="true" name="website" ... />
          {error && <p className="addr-input-error" role="alert">{error}</p>}
          <div className="contact-footer-row">
            <p className="contact-consent">{c.consent}</p>
            <button type="submit" className="btn btn-primary" disabled={phase==="sending"}>{phase==="sending" ? c.sending : c.send}</button>
          </div>
        </form>
      )}
    </section>
  </div>
</main>
```

狀態：`topic`（預設 `"copytrade"`）、`email`、`wallet`（使用者手填）、`walletOverride: boolean`（按了「改填其他地址」）、`message`、`website`、`phase`、`error`、`ticket`。
`meAddress = useMe().data?.address ?? ""`；`autofilled = !!meAddress && !walletOverride`；送出的 wallet ＝ `autofilled ? meAddress : wallet.trim()`。
客端驗證：email 正則；wallet 非空時 `/^0x[0-9a-fA-F]{40}$/`；message 10–2000。
錯誤映射：`ApiError.kind === "network"` → errNetwork；`kind === "upstream"`（502/503）→ `(detail ?? errGeneric) + fallbackPrefix + fallbackEmail`（mailto 連結）；其他 → `detail ?? errGeneric`。

**globals.css `.contact-*`（換掉第一版那段）：**

```css
/* ---------- /contact（雙欄，照 Claude Design 設計稿）---------- */
.contact-grid { display: grid; grid-template-columns: 1fr 1.4fr; gap: 40px; align-items: start; }
@media (max-width: 900px) { .contact-grid { grid-template-columns: 1fr; } }
.contact-title { font-size: 40px; font-weight: 800; margin: 8px 0 12px; letter-spacing: -.01em; }
.contact-sub { color: var(--text-dim); font-size: 16px; line-height: 1.8; margin: 0 0 28px; }
.contact-checklist { padding: 22px 24px; }
.contact-checklist-title { color: var(--text-dim); font-size: 12px; letter-spacing: .08em; margin: 0 0 14px; }
.contact-checklist-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }
.contact-checklist-list li { display: grid; grid-template-columns: 28px 1fr; gap: 10px; line-height: 1.7; }
.contact-check-num { color: var(--primary); font-size: 14px; }
.contact-check-num.neg { color: var(--neg); }
.contact-security-note { display: flex; align-items: center; gap: 10px; color: var(--text-dim); margin: 22px 0 0; font-size: 14px; }
.contact-dot { width: 8px; height: 8px; border-radius: 50%; background: #e0b35a; flex: 0 0 auto; }
.contact-card { padding: 32px; }
.contact-form { display: grid; gap: 24px; }
.contact-field { display: grid; gap: 10px; }
.contact-label-row { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.contact-label { font-size: 15px; font-weight: 500; }
.contact-hint { color: var(--text-dim); font-size: 13px; }
.contact-chips { display: flex; flex-wrap: wrap; gap: 10px; }
.contact-chip { padding: 10px 16px; border-radius: 12px; border: 1px solid var(--border); background: transparent;
  color: var(--text); font-size: 15px; cursor: pointer; }
.contact-chip:hover { border-color: rgba(var(--text-rgb), .3); }
.contact-chip.is-active { border-color: var(--primary); color: var(--primary); background: rgba(var(--primary-rgb), .08); }
.contact-wallet-wrap { position: relative; }
.contact-wallet-wrap .addr-input { padding-right: 120px; }
.contact-wallet-badge { position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
  color: var(--primary); font-size: 13px; }
.contact-link-btn { background: none; border: 0; padding: 0; color: var(--text-dim); font-size: 12px;
  cursor: pointer; text-decoration: underline; justify-self: start; }
.contact-textarea { resize: vertical; min-height: 180px; font-family: inherit; line-height: 1.7; }
textarea.addr-input:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.contact-footer-row { display: flex; justify-content: space-between; align-items: center; gap: 24px; }
.contact-consent { color: var(--text-dim); font-size: 13px; line-height: 1.7; margin: 0; max-width: 60%; }
.contact-success { display: grid; gap: 12px; }
.contact-ticket { font-size: 28px; letter-spacing: .04em; color: var(--primary); }
@media (max-width: 600px) { .contact-footer-row { flex-direction: column; align-items: stretch; } .contact-consent { max-width: none; } }
```

（`--text-rgb`／`--primary-rgb` 若 tokens.css 沒有就改用既有等價變數。）

**page.test.tsx 必測**（mock `@/lib/api` 的 `postContact`，並 mock `@/lib/hooks` 的 `useMe`）：
1. 渲染：眉標、h1、五顆 chip（`role=radio`，預設「跟單問題」`aria-checked=true`）、Email／錢包／訊息欄位、送出訊息鈕、三列確認清單、安全提示。
2. 未登入：錢包欄可編輯、無「已連結錢包」徽章。
3. 已登入（`useMe` 回 `{data:{address:"0x"+"ab"*20}}`）：錢包欄 readOnly、值為 `shortAddr`、徽章顯示；送出時 `postContact` 收到**完整**地址；按「改填其他地址」後欄位可編輯且徽章消失。
4. 客端驗證：壞 email／壞錢包／短訊息各自顯示對應錯誤且不打 API。
5. 字數計數：輸入 12 字後顯示 `12 / 2000`。
6. 成功：`postContact` 回 `{ok:true, ticket:"FLT-AB12-CD34"}` → 顯示「已送出」與工單編號、表單消失；「再送一則」回到表單且主題重置。
7. 錯誤：`ApiError("client", "...", 429, "...")` detail 原樣顯示；`ApiError("upstream", "...", 503, "...")` 顯示 detail 並含 mailto 連結；network 顯示 errNetwork。

驗收：`cd web && npm test` 全綠；`npx tsc --noEmit` 對本次檔案零錯；`npm run lint` 乾淨；`grep -c "nameLabel" web/src/lib/copy.ts` 為 0。

### Task 6C：部署（主線程）

同 §3.2／§4.2／§9.2：rsync → uv sync → chown → build → restart api＋dashboard → DEPLOYED_VERSION → 真實送出一封（topic=other）驗證回傳 ticket 且信件主旨帶 ticket。

## 執行順序與狀態

- Task 1 → Task 2（後端，同一 builder 連做）與 Task 3（前端）互不相依，可平行派工；Task 4 最後。
- 主線程親跑：`uv run pytest -q`、`uv run ruff check src tests scripts`、`cd web && npm test && npx tsc --noEmit`。
- reviewer（opus）審 `git diff` 對照本檔；重點：header injection、密碼不入 log/repr、不重試、限流位置。

| Task | 狀態 |
|---|---|
| 1 | 完成（builder；主線程親驗 25 passed） |
| 2 | 完成（同上） |
| 3 | 完成（builder；主線程另改 `legal.ts` 四處舊 URL → `https://trade.filet.app/contact`） |
| 4 | 完成（builder；主線程修 RUNBOOK 驗收 curl 主機名） |
| 5 | 完成（opus 審查 1C/4W 全修；主線程親驗 32 passed、全量 2773 passed、探針三種輸入皆 422） |
| 6A/6B | 完成（使用者退版後照設計稿重做；opus 審查 PASS 0C/3W，W1 載入競態、W2 honeypot 缺 ticket、W3 裸 U+2028 皆已修；主線程親驗後端 40 passed、全量 2781、前端 674、本機截圖三態對照設計稿） |
| 6C | 見 RUNBOOK 附錄 B 部署記錄 |

未採納（Task 6 審查建議）：chip 方向鍵 roving tabindex（Tab＋Enter 可操作）；字數計數 UTF-16 vs code point 差異（只影響 emoji 的下限判定，上限方向安全）；security 告警佔 in-flight 名額（與既有失敗告警同形狀）。

未採納的審查建議：`legal.ts` 改相對路徑（法務頁只把 `http` 開頭段落渲染成連結，改相對路徑要動三個頁面渲染器，超出範圍）。
待使用者：Google 應用程式密碼申請＋落地 `/etc/filet/contact.env`（RUNBOOK §5.8b）；未落地前端點回 503、頁面顯示 mailto 保底。
