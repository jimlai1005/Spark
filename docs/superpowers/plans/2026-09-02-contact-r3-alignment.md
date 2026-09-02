# /contact 對齊設計稿 R3-01～R3-08 Implementation Plan

> **For agentic workers:** 本 plan 全部 task 標 `@sdd`（haiku `impl-worker` 機械執行）。每個 task 有確切檔案、
> 完整程式碼、一行驗收指令。**不得 commit**（主線程統一）。做到一半需要判斷 → 停下回報，不要猜。
> 設計稿本體：`docs/superpowers/research/2026-09-02-contact-design-r3.html`（R3 區段原始 HTML）與
> `…-r3-rules.txt`（R3-01～R3-08 原文）。前置：`docs/superpowers/plans/2026-09-02-contact-page.md` Task 1–6 已上線。

**Goal:** 讓已上線的 `/contact`（commit 4281d06）完全符合設計稿 R3-01～R3-08 與成功卡規格。

**使用者裁決（2026-09-02，七點）：** (1) API 路徑保留 `/api/public/contact`（偏離 R3-02 的 `/api/contact`，repo 慣例＋nginx 對應）；(2) 工單落 sqlite，**存完整內容**（主題／Email／錢包／訊息／page_url／UA／IP／時間／bot）；(3) 安全回報用同一 TG 頻道加 `🚨 URGENT` 前綴；(4) 每筆送出推 TG（編號、主題、前 200 字）**且**保留 Email 寄到站主（回信載體）；(5) 全站移除信箱字串；(6) **DB 寫入＋TG 推送＝送出成功（回 200）**，Email 失敗或 SMTP 未設定只告警、`mailed=0`，不叫用戶重送——5xx 只剩 DB 寫入失敗與 in-flight 滿；(7) honeypot 命中且欄位合法 → **照樣落 DB／寄信／推 TG**，但 DB `bot=1`、主旨與 TG 標「🤖 疑似機器人」；欄位不合法（無法安全組信）才回假工單靜默丟棄。

---

## R3 規則 → Task 對照（驗收依此逐條）

| 規則 | 要求 | 落點 |
|---|---|---|
| R3-01 | footer＋設定頁「需要協助？」連 /contact；全站無 mailto／信箱字串 | 7B-3（設定頁）、7B-1（copy 移除 fallbackEmail）、驗收 `grep -rn "mailto:\|goldwisetw" web/src` = 0 |
| R3-02 | body `{topic,email,wallet?,message,page_url,user_agent}`；驗證規則 | 7A-2（ContactBody）、7B-2（api.ts） |
| R3-03 | honeypot（依裁決 7：合法欄位照送並標機器人，不合法才靜默假工單）；IP 5/小時、email 10/日；429「送出太頻繁，請稍後再試」；無 CAPTCHA | 7A-2 常數＋`count_contact_by_email_since` |
| R3-04 | `FLT-YYMM-NNNN` 月份流水、寫 DB、回傳；TG 通知帶編號／主題／前 200 字；安全回報 urgent | 7A-1（store）、7A-2（route） |
| R3-05 | 回信主旨「[Filet FLT-2609-0412] Re: 跟單問題」；頁面不預告寄件地址 | 7A-3 寄出主旨 `[Filet FLT-…] 跟單問題`（站主按回覆即成 `Re:`）；7B-1 文案 |
| R3-06 | 登入自動帶入＋「已連結錢包」＋可清除；email 不自動帶入 | 7B-4（按鈕改「清除」） |
| R3-07 | 送出中 disabled「送出中…」；5xx／網路紅字「送出失敗，請稍後再試」保留內容；成功同位置取代 | 7B-4 |
| R3-08 | <900px 上下堆疊、確認區塊移到表單下方；輸入框 ≥44px | 7B-5（grid-template-areas＋min-height） |
| 成功卡 | ✓ 已收到你的訊息／回覆會寄到 {email}…請留意垃圾郵件匣／工單編號／複製／← 回到首頁 | 7B-1、7B-4 |

---

## Task 7A-1 `@sdd`：sqlite 工單表與流水號

**Files:** `src/spark/publicapi/store.py`、`tests/test_contact_store.py`（新建）

- [ ] **Step 1** `store.py` 的 `_SCHEMA` 字串（`billing` 表之後、結尾 `"""` 之前）追加：

```sql
CREATE TABLE IF NOT EXISTS contact_tickets (
    ticket TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    seq INTEGER NOT NULL,
    topic TEXT NOT NULL,
    email TEXT NOT NULL,
    wallet TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    page_url TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    mailed INTEGER NOT NULL DEFAULT 0,
    bot INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS contact_tickets_email_created ON contact_tickets (email, created_at);
```

- [ ] **Step 2** `store.py` 檔頭 import 加 `from datetime import datetime, timezone`（若已存在則略）。在 `class ApiStore` 的 `create_session` 方法之前插入：

```python
    # --- /contact 工單（設計稿 R3-04：FLT-YYMM-NNNN 月份流水，落 DB）---
    def create_contact_ticket(self, *, topic: str, email: str, wallet: str, message: str,
                              page_url: str, user_agent: str, client_ip: str,
                              now_s: float, bot: bool = False) -> str:
        """同一把鎖內取 MAX(seq)+1 並 INSERT：流水號不會因並發重複。bot=True ＝ honeypot 命中
        （使用者裁決：照樣落 DB／寄信／推 TG，但標明疑似機器人）。"""
        month = datetime.fromtimestamp(now_s, tz=timezone.utc).strftime("%y%m")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM contact_tickets WHERE month = ?",
                (month,)).fetchone()
            seq = int(row[0]) + 1
            ticket = f"FLT-{month}-{seq:04d}"
            self._db.execute(
                "INSERT INTO contact_tickets (ticket, month, seq, topic, email, wallet, message, "
                "page_url, user_agent, client_ip, created_at, bot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket, month, seq, topic, email, wallet, message, page_url, user_agent,
                 client_ip, now_s, 1 if bot else 0))
        return ticket

    def mark_contact_mailed(self, ticket: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE contact_tickets SET mailed = 1 WHERE ticket = ?", (ticket,))

    def count_contact_by_email_since(self, email: str, since_s: float) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM contact_tickets WHERE email = ? AND created_at >= ?",
                (email, since_s)).fetchone()
        return int(row[0])

    def get_contact_ticket(self, ticket: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT ticket, month, seq, topic, email, wallet, message, page_url, user_agent, "
                "client_ip, created_at, mailed, bot FROM contact_tickets WHERE ticket = ?",
                (ticket,)).fetchone()
        if row is None:
            return None
        keys = ("ticket", "month", "seq", "topic", "email", "wallet", "message", "page_url",
                "user_agent", "client_ip", "created_at", "mailed", "bot")
        return dict(zip(keys, row))
```

- [ ] **Step 3** 新建 `tests/test_contact_store.py`：

```python
"""tests/test_contact_store.py — /contact 工單表：FLT-YYMM-NNNN 月份流水、落 DB、email 計數。"""
import threading
from datetime import datetime, timezone

from spark.publicapi.store import ApiStore

T_2609 = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()
T_2610 = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()


def _store(tmp_path):
    return ApiStore(str(tmp_path / "api.db"))


def _create(store, now_s=T_2609, email="a@b.co", **over):
    kw = dict(topic="copytrade", email=email, wallet="", message="x" * 20,
              page_url="https://trade.filet.app/contact", user_agent="ua", client_ip="1.2.3.4",
              now_s=now_s)
    kw.update(over)
    return store.create_contact_ticket(**kw)


def test_ticket_format_and_monthly_sequence(tmp_path):
    s = _store(tmp_path)
    assert _create(s) == "FLT-2609-0001"
    assert _create(s) == "FLT-2609-0002"
    assert _create(s, now_s=T_2610) == "FLT-2610-0001"   # 換月歸零
    assert _create(s) == "FLT-2609-0003"                 # 舊月份續號


def test_ticket_row_persisted_with_full_content(tmp_path):
    s = _store(tmp_path)
    t = _create(s, wallet="0x" + "ab" * 20, message="hello there, ten+ chars")
    row = s.get_contact_ticket(t)
    assert row["topic"] == "copytrade" and row["email"] == "a@b.co"
    assert row["wallet"] == "0x" + "ab" * 20
    assert row["message"] == "hello there, ten+ chars"
    assert row["page_url"].endswith("/contact") and row["user_agent"] == "ua"
    assert row["client_ip"] == "1.2.3.4" and row["mailed"] == 0 and row["bot"] == 0
    s.mark_contact_mailed(t)
    assert s.get_contact_ticket(t)["mailed"] == 1
    assert s.get_contact_ticket("FLT-0000-0000") is None
    tb = _create(s, bot=True)
    assert s.get_contact_ticket(tb)["bot"] == 1


def test_count_by_email_since(tmp_path):
    s = _store(tmp_path)
    for _ in range(3):
        _create(s, now_s=T_2609)
    _create(s, now_s=T_2609 - 90000)          # 超過一天前
    _create(s, email="other@b.co")
    assert s.count_contact_by_email_since("a@b.co", T_2609 - 86400) == 3
    assert s.count_contact_by_email_since("other@b.co", T_2609 - 86400) == 1


def test_sequence_unique_under_threads(tmp_path):
    s = _store(tmp_path)
    out: list[str] = []
    lock = threading.Lock()

    def w():
        t = _create(s)
        with lock:
            out.append(t)

    th = [threading.Thread(target=w) for _ in range(20)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert len(out) == 20 and len(set(out)) == 20
    assert sorted(out)[-1] == "FLT-2609-0020"
```

- [ ] **Step 4 驗收**：`uv run pytest tests/test_contact_store.py tests/test_publicapi_store.py -q` 全綠；`uv run ruff check src tests`。

---

## Task 7A-2 `@sdd`：contact.py 主旨／decoy ticket＋路由對齊 R3-02～R3-05

**Files:** `src/spark/publicapi/contact.py`、`src/spark/publicapi/app.py`

- [ ] **Step 1** `contact.py`：刪除 `_TICKET_ALPHABET` 與 `new_ticket_id`，改為：

```python
def ticket_month(now_s: float) -> str:
    """FLT-YYMM-NNNN 的 YYMM 段（UTC）。"""
    return datetime.fromtimestamp(now_s, tz=timezone.utc).strftime("%y%m")


def decoy_ticket(now_s: float) -> str:
    """honeypot 命中時回給機器人的假工單：格式同真工單、不落 DB（R3-03「靜默丟棄但仍回成功」）。"""
    return f"FLT-{ticket_month(now_s)}-{secrets.randbelow(9000) + 1000:04d}"
```

檔頭 import 加 `from datetime import datetime, timezone`。新增常數：

```python
PAGE_URL_MAX = 512
USER_AGENT_MAX = 512
NOTIFY_PREVIEW_CHARS = 200


def clip(s: str, n: int) -> str:
    return (s or "").strip()[:n]
```

`build_contact_email` 改為（主旨對齊 R3-05；body 加 page_url／UA）：

```python
BOT_TAG = "🤖 疑似機器人"


def build_contact_email(ci: ContactInput, *, ticket: str, sender: str, to: str,
                        client_ip: str, now_iso: str, page_url: str = "",
                        user_agent: str = "", bot: bool = False) -> EmailMessage:
    """bot=True（honeypot 命中）：主旨與 body 首行標明疑似機器人（使用者裁決：照樣寄）。"""
    label = CONTACT_TOPIC_LABELS[ci.topic]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Reply-To"] = ci.email
    msg["Subject"] = f"[Filet {ticket}] {BOT_TAG}｜{label}" if bot else f"[Filet {ticket}] {label}"
    bot_line = f"⚠️ {BOT_TAG}：honeypot 欄位有值，此訊息由自動程式送出的機率很高。\n\n" if bot else ""
    msg.set_content(
        f"{bot_line}工單：{ticket}\n主題：{label}\nEmail：{ci.email}\n"
        f"錢包地址：{ci.wallet or '（未提供）'}\n來源 IP：{client_ip}\n時間：{now_iso}\n"
        f"頁面：{page_url or '（未提供）'}\nUA：{user_agent or '（未提供）'}\n\n"
        f"訊息：\n{ci.message}\n"
    )
    return msg


def notify_text(ci: ContactInput, ticket: str, *, bot: bool = False) -> str:
    """TG 內部通知（R3-04）：編號、主題、前 200 字。security 加 🚨 URGENT（使用者裁決 3）；
    bot 加 🤖 疑似機器人 前綴（使用者裁決）。"""
    label = CONTACT_TOPIC_LABELS[ci.topic]
    head = f"🚨 URGENT {ticket}｜{label}" if ci.topic == "security" else f"📩 {ticket}｜{label}"
    if bot:
        head = f"{BOT_TAG}（honeypot）{head}"
    return f"{head}\n{ci.message[:NOTIFY_PREVIEW_CHARS]}"
```

- [ ] **Step 2** `app.py` 模組常數改為（找 `CONTACT_RATELIMIT_WINDOW_S` 那段整段替換）：

```python
# ⭐ /api/public/contact 防濫用（設計稿 R3-03）：同 IP 每小時 5 次（sliding window，記憶體）、
# 同 email 每日 10 次（查 contact_tickets 表，重啟不歸零）。超限 429。不上 CAPTCHA。
CONTACT_RATELIMIT_WINDOW_S = 3600.0
CONTACT_RATELIMIT_MAX = 5
CONTACT_EMAIL_DAILY_MAX = 10
CONTACT_EMAIL_WINDOW_S = 86400.0
CONTACT_RATELIMIT_DETAIL = "送出太頻繁，請稍後再試"
```

`_enforce_contact_ratelimit` 內的 429 detail 改用 `CONTACT_RATELIMIT_DETAIL`。
import 行 `new_ticket_id` 改為 `clip, decoy_ticket, notify_text, PAGE_URL_MAX, USER_AGENT_MAX`（保留 `ContactValidationError, SmtpMailer, build_contact_email, validate_contact`）。

- [ ] **Step 3** `app.py` 的 `ContactBody` 與 `public_contact_endpoint` **整段替換**為：

```python
    class ContactBody(BaseModel):
        topic: str
        email: str
        wallet: str = ""       # 選填；已登入時前端自動帶入（R3-06）
        message: str
        page_url: str = ""     # R3-02
        user_agent: str = ""   # R3-02（空則取 header）
        website: str = ""      # honeypot：真人看不到、機器人會填（R3-03）

    @app.post("/api/public/contact")
    def public_contact_endpoint(body: ContactBody, request: Request):
        """/contact 表單（設計稿 R3-02～R3-05＋使用者裁決 2026-09-02）：
        驗證 → in-flight 上限 → IP 限流 → email 日限 → **工單落 DB（FLT-YYMM-NNNN）＝送出成功**
        → TG 通知（一律推）→ 寄信（失敗只告警、mailed=0，仍回 200）。無需登入。
        「送出成功」的定義是 DB＋TG 這條內部佇列收到，Email 只是站主回信的載體，SMTP 壞掉不叫
        用戶重送。honeypot 命中（website 有值）：欄位合法就照常走完整流程但標 bot=1、主旨與 TG
        加「🤖 疑似機器人」；欄位不合法（無法安全組信）才回假工單靜默丟棄。
        寄信非冪等：不重試（工程原則 2）。"""
        now = now_fn()
        is_bot = bool(body.website.strip())
        try:
            ci = validate_contact(topic=body.topic, email=body.email,
                                  wallet=body.wallet, message=body.message)
        except ContactValidationError as e:
            if is_bot:
                logger.info("/api/public/contact honeypot 命中且欄位不合法，回假工單靜默丟棄")
                return {"ok": True, "ticket": decoy_ticket(now)}
            raise HTTPException(status_code=422, detail=str(e)) from e
        client_ip = request.client.host if request.client else "unknown"
        page_url = clip(body.page_url, PAGE_URL_MAX)
        user_agent = clip(body.user_agent or request.headers.get("user-agent", ""), USER_AGENT_MAX)
        if not _contact_inflight.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="系統忙碌中，請稍後再試")
        try:
            _enforce_contact_ratelimit(client_ip)
            if store.count_contact_by_email_since(
                    ci.email, now - CONTACT_EMAIL_WINDOW_S) >= CONTACT_EMAIL_DAILY_MAX:
                raise HTTPException(status_code=429, detail=CONTACT_RATELIMIT_DETAIL)
            ticket = store.create_contact_ticket(
                topic=ci.topic, email=ci.email, wallet=ci.wallet, message=ci.message,
                page_url=page_url, user_agent=user_agent, client_ip=client_ip, now_s=now,
                bot=is_bot)
            # R3-04 內部通知：每筆都推、與寄信成敗無關；security 走 critical＋🚨 URGENT（裁決 3）。
            try:
                text = notify_text(ci, ticket, bot=is_bot)
                if ci.topic == "security":
                    notifier.critical("contact_security_report", text, dedup_key=ticket)
                else:
                    notifier.info("contact_ticket", text, dedup_key=ticket)
            except Exception as ne:  # noqa: BLE001 — 通知失敗不得蓋掉成功回應
                logger.error("/contact TG 通知送出失敗 工單 %s: %r", ticket, ne)
            # Email：站主回信載體。未設定 SMTP 或寄失敗 → mailed 留 0、告警（1 小時冷卻），仍回 200。
            if mailer is None or not cfg.contact_enabled:
                logger.warning("/api/public/contact SMTP 未設定，工單 %s 只落 DB＋TG（mailed=0）", ticket)
            else:
                now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                msg = build_contact_email(ci, ticket=ticket, sender=cfg.contact_smtp_user,
                                          to=cfg.contact_to, client_ip=client_ip, now_iso=now_iso,
                                          page_url=page_url, user_agent=user_agent, bot=is_bot)
                try:
                    mailer.send(msg)
                    store.mark_contact_mailed(ticket)
                except Exception as e:  # noqa: BLE001 — 失敗必須外顯（原則 3），但不得洩露信件內容
                    logger.error("/api/public/contact 寄信失敗 工單 %s (%s): %s",
                                 ticket, type(e).__name__, e)
                    if now - _contact_alert_last[0] >= 3600:
                        _contact_alert_last[0] = now
                        try:
                            notifier.critical(
                                "contact_mail_failure",
                                f"/contact 寄信失敗：{type(e).__name__}（工單 {ticket} 已落 DB，mailed=0）",
                                dedup_key="contact_mail_failure")
                        except Exception as ne:  # noqa: BLE001 — 告警失敗不得蓋掉原錯誤
                            logger.error("/contact 失敗告警送出失敗: %r", ne)
            logger.info("/api/public/contact 已受理（工單 %s，bot=%s）", ticket, is_bot)
            return {"ok": True, "ticket": ticket}
        finally:
            _contact_inflight.release()
```

- [ ] **Step 4 驗收**：`uv run ruff check src`；`uv run python -c "import spark.publicapi.app"`。（測試在 7A-3。）

---

## Task 7A-3 `@sdd`：後端測試重寫

**Files:** `tests/test_contact_module.py`（整檔覆寫）、`tests/test_api_contact.py`（整檔覆寫）

- [ ] **Step 1** `tests/test_contact_module.py` 整檔內容：

```python
"""tests/test_contact_module.py — /contact 純邏輯：驗證、主旨（R3-05）、decoy ticket、TG 文案（R3-04）。"""
import re
from datetime import datetime, timezone

import pytest

from spark.publicapi.contact import (
    BOT_TAG, CONTACT_TOPICS, ContactInput, ContactValidationError, build_contact_email, clip,
    decoy_ticket, notify_text, ticket_month, validate_contact,
)
from tests.publicapi_helpers import make_cfg

T_2609 = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()


def test_validate_ok_strips_and_allows_empty_wallet():
    ci = validate_contact(topic=" copytrade ", email=" a@b.co ", wallet="  ", message="  hello there, ten+ chars ")
    assert ci == ContactInput(topic="copytrade", email="a@b.co", wallet="", message="hello there, ten+ chars")


def test_validate_ok_with_valid_wallet():
    ci = validate_contact(topic="billing", email="a@b.co", wallet="0x" + "Ab" * 20, message="x" * 20)
    assert ci.wallet == "0x" + "Ab" * 20


@pytest.mark.parametrize("topic,email,wallet,message", [
    ("nope", "a@b.co", "", "x" * 20),                  # topic 不在白名單
    ("", "a@b.co", "", "x" * 20),
    ("copytrade", "not-an-email", "", "x" * 20),
    ("copytrade", "a@b", "", "x" * 20),                 # 無 TLD
    ("copytrade", "測試@例え.jp", "", "x" * 20),         # 非 ASCII
    ("copytrade", "a@b.co\nX", "", "x" * 20),
    ("copytrade", "a@b.co X", "", "x" * 20),       # U+2028 分隔字元
    ("copytrade", "a" * 250 + "@b.co", "", "x" * 20),
    ("copytrade", "a@b.co", "0x123", "x" * 20),          # wallet 太短
    ("copytrade", "a@b.co", "0x" + "zz" * 20, "x" * 20),  # 非 hex
    ("copytrade", "a@b.co", "", "short"),
    ("copytrade", "a@b.co", "", "x" * 2001),
])
def test_validate_rejects(topic, email, wallet, message):
    with pytest.raises(ContactValidationError):
        validate_contact(topic=topic, email=email, wallet=wallet, message=message)


def test_topics_whitelist():
    assert CONTACT_TOPICS == ("copytrade", "billing", "security", "partnership", "other")


def test_build_email_subject_r3_05_and_body():
    ci = ContactInput(topic="copytrade", email="a@b.co", wallet="", message="line1\nline2 long enough")
    msg = build_contact_email(ci, ticket="FLT-2609-0412", sender="site@gmail.com", to="owner@gmail.com",
                              client_ip="1.2.3.4", now_iso="2026-09-02T00:00:00Z",
                              page_url="https://trade.filet.app/contact", user_agent="UA/1.0")
    assert msg["Subject"] == "[Filet FLT-2609-0412] 跟單問題"
    assert msg["From"] == "site@gmail.com" and msg["To"] == "owner@gmail.com"
    assert msg["Reply-To"] == "a@b.co"
    body = msg.get_content()
    for needle in ("FLT-2609-0412", "跟單問題", "a@b.co", "（未提供）", "1.2.3.4",
                   "https://trade.filet.app/contact", "UA/1.0", "line1\nline2 long enough"):
        assert needle in body
    assert msg.get_content_type() == "text/plain"


def test_build_email_security_subject_has_no_extra_prefix():
    ci = ContactInput(topic="security", email="a@b.co", wallet="0x" + "ab" * 20, message="x" * 20)
    msg = build_contact_email(ci, ticket="FLT-2609-0001", sender="s@g.com", to="t@g.com",
                              client_ip="1.1.1.1", now_iso="t")
    assert msg["Subject"] == "[Filet FLT-2609-0001] 安全回報"
    assert "0x" + "ab" * 20 in msg.get_content()


def test_notify_text_r3_04():
    ci = ContactInput(topic="billing", email="a@b.co", wallet="", message="m" * 300)
    t = notify_text(ci, "FLT-2609-0002")
    assert t.startswith("📩 FLT-2609-0002｜費用與帳務\n")
    assert len(t.split("\n", 1)[1]) == 200
    sec = notify_text(ContactInput(topic="security", email="a@b.co", wallet="", message="x" * 20), "FLT-2609-0003")
    assert sec.startswith("🚨 URGENT FLT-2609-0003｜安全回報\n")


def test_bot_flag_marks_subject_body_and_notify():
    ci = ContactInput(topic="other", email="a@b.co", wallet="", message="x" * 20)
    msg = build_contact_email(ci, ticket="FLT-2609-0009", sender="s@g.com", to="t@g.com",
                              client_ip="1.1.1.1", now_iso="t", bot=True)
    assert msg["Subject"] == f"[Filet FLT-2609-0009] {BOT_TAG}｜其他"
    assert msg.get_content().startswith(f"⚠️ {BOT_TAG}")
    assert notify_text(ci, "FLT-2609-0009", bot=True).startswith(f"{BOT_TAG}（honeypot）📩 FLT-2609-0009｜其他\n")


def test_ticket_month_and_decoy():
    assert ticket_month(T_2609) == "2609"
    assert re.fullmatch(r"FLT-2609-\d{4}", decoy_ticket(T_2609))


def test_clip():
    assert clip("  abc  ", 2) == "ab"
    assert clip(None, 5) == ""


def test_cfg_defaults_unconfigured(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.contact_smtp_user is None and cfg.contact_enabled is False
    assert "contact_smtp_pass" not in repr(cfg)


def test_cfg_pair_required(tmp_path):
    with pytest.raises(ValueError, match="FILET_CONTACT_SMTP"):
        make_cfg(tmp_path, contact_smtp_user="site@gmail.com")


def test_cfg_to_defaults_to_user(tmp_path):
    cfg = make_cfg(tmp_path, contact_smtp_user="site@gmail.com", contact_smtp_pass="app-pw")
    assert cfg.contact_enabled is True and cfg.contact_to == "site@gmail.com"
    assert "app-pw" not in repr(cfg)
```

- [ ] **Step 2** `tests/test_api_contact.py` 整檔內容：

```python
"""tests/test_api_contact.py — POST /api/public/contact 對齊設計稿 R3-02～R3-05：
honeypot decoy、422、503、in-flight、IP 5/小時、email 10/日、工單 FLT-YYMM-NNNN 落 DB、
寄信主旨、TG 通知（每筆 info；security critical 🚨）、寄信失敗 502＋mailed=0。全離線。"""
import re
import socket
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from spark.copytrade.notifier import RecordingNotifier
from spark.publicapi.app import (
    CONTACT_EMAIL_DAILY_MAX, CONTACT_MAX_INFLIGHT, CONTACT_RATELIMIT_DETAIL,
    CONTACT_RATELIMIT_MAX, CONTACT_RATELIMIT_WINDOW_S,
)
from spark.publicapi.contact import BOT_TAG
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket
TICKET_RE = re.compile(r"^FLT-\d{4}-\d{4}$")


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()

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


class BlockingMailer:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self.sent = 0

    def send(self, msg):
        self.entered.release()
        assert self.release.wait(timeout=10)
        self.sent += 1


GOOD = {"topic": "copytrade", "email": "jim@example.com",
        "message": "Hello, I have a question about fees.",
        "page_url": "https://trade.filet.app/contact", "user_agent": "UA/1.0"}


def _make(tmp_path, *, configured=True, fail=False, mailer=None):
    over = {"contact_smtp_user": "site@gmail.com", "contact_smtp_pass": "pw",
            "contact_to": "owner@gmail.com"} if configured else {}
    cfg = make_cfg(tmp_path, **over)
    clock = Clock()
    mailer = mailer or FakeMailer(fail=fail)
    notifier = RecordingNotifier()
    app, _cfg, store, *_ = make_app(tmp_path, cfg=cfg, now_fn=clock, mailer=mailer, notifier=notifier)
    return TestClient(app, base_url="https://testserver"), clock, mailer, notifier, store


def test_happy_path_ticket_db_mail_notify(tmp_path):
    client, _, mailer, notifier, store = _make(tmp_path)
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 200
    ticket = r.json()["ticket"]
    assert r.json()["ok"] is True and ticket == "FLT-2609-0001"
    m = mailer.sent[0]
    assert m["Subject"] == "[Filet FLT-2609-0001] 跟單問題"
    assert m["Reply-To"] == "jim@example.com" and m["To"] == "owner@gmail.com"
    assert "UA/1.0" in m.get_content() and "trade.filet.app/contact" in m.get_content()
    row = store.get_contact_ticket(ticket)
    assert row["email"] == "jim@example.com" and row["mailed"] == 1
    assert row["message"] == GOOD["message"] and row["client_ip"] == "testclient"
    assert notifier.records == [("info", "contact_ticket",
                                 "📩 FLT-2609-0001｜跟單問題\n" + GOOD["message"], ticket)]
    assert client.post("/api/public/contact", json=GOOD).json()["ticket"] == "FLT-2609-0002"


def test_user_agent_falls_back_to_header(tmp_path):
    client, _, mailer, _, store = _make(tmp_path)
    body = {**GOOD, "user_agent": ""}
    r = client.post("/api/public/contact", json=body, headers={"User-Agent": "HeaderUA/2"})
    assert store.get_contact_ticket(r.json()["ticket"])["user_agent"] == "HeaderUA/2"


def test_security_topic_critical_urgent(tmp_path):
    client, _, _, notifier, _ = _make(tmp_path)
    r = client.post("/api/public/contact", json={**GOOD, "topic": "security"})
    t = r.json()["ticket"]
    assert notifier.records[0][0] == "critical"
    assert notifier.records[0][1] == "contact_security_report"
    assert notifier.records[0][2].startswith(f"🚨 URGENT {t}｜安全回報\n")
    assert notifier.records[0][3] == t


def test_honeypot_valid_fields_flagged_bot_but_still_delivered(tmp_path):
    """使用者裁決：honeypot 命中仍寄信＋推 TG＋落 DB，但主旨／通知／DB 都標疑似機器人。"""
    client, _, mailer, notifier, store = _make(tmp_path)
    r = client.post("/api/public/contact", json={**GOOD, "website": "http://spam"})
    assert r.status_code == 200 and r.json()["ticket"] == "FLT-2609-0001"
    assert store.get_contact_ticket("FLT-2609-0001")["bot"] == 1
    assert mailer.sent[0]["Subject"] == f"[Filet FLT-2609-0001] {BOT_TAG}｜跟單問題"
    assert notifier.records[0][2].startswith(f"{BOT_TAG}（honeypot）📩 FLT-2609-0001｜跟單問題\n")


def test_honeypot_invalid_fields_returns_decoy_without_db_or_mail(tmp_path):
    client, _, mailer, notifier, store = _make(tmp_path)
    r = client.post("/api/public/contact", json={**GOOD, "email": "nope", "website": "http://spam"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert TICKET_RE.match(r.json()["ticket"])
    assert mailer.sent == [] and notifier.records == []
    assert store.get_contact_ticket(r.json()["ticket"]) is None


@pytest.mark.parametrize("bad", [
    {**GOOD, "topic": "nope"}, {**GOOD, "email": "nope"}, {**GOOD, "message": "short"},
    {**GOOD, "wallet": "0x123"}, {**GOOD, "email": "a@b.co\nBcc: x@y.z"},
    {**GOOD, "email": "a@b.co X"},
])
def test_validation_422_does_not_echo_input(tmp_path, bad):
    client, _, mailer, _, _ = _make(tmp_path)
    r = client.post("/api/public/contact", json=bad)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert "Bcc" not in r.json()["detail"] and "nope" not in r.json()["detail"]
    assert mailer.sent == []


def test_missing_fields_422(tmp_path):
    client, _, mailer, _, _ = _make(tmp_path)
    assert client.post("/api/public/contact", json={"topic": "other"}).status_code == 422
    assert mailer.sent == []


def test_ip_ratelimit_5_per_hour_then_recovers(tmp_path):
    client, clock, mailer, _, _ = _make(tmp_path)
    assert CONTACT_RATELIMIT_MAX == 5 and CONTACT_RATELIMIT_WINDOW_S == 3600.0
    for _ in range(CONTACT_RATELIMIT_MAX):
        assert client.post("/api/public/contact", json=GOOD).status_code == 200
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 429 and r.json()["detail"] == CONTACT_RATELIMIT_DETAIL
    assert len(mailer.sent) == CONTACT_RATELIMIT_MAX
    clock.t += CONTACT_RATELIMIT_WINDOW_S + 1
    assert client.post("/api/public/contact", json=GOOD).status_code == 200


def test_email_daily_limit_10(tmp_path):
    client, clock, _, _, store = _make(tmp_path)
    assert CONTACT_EMAIL_DAILY_MAX == 10
    for i in range(CONTACT_EMAIL_DAILY_MAX):
        clock.t += 1000     # 每筆間隔，避免撞 IP 每小時 5 次
        if i % 4 == 3:
            clock.t += 3600
        assert client.post("/api/public/contact", json=GOOD).status_code == 200, i
    clock.t += 3600
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 429 and r.json()["detail"] == CONTACT_RATELIMIT_DETAIL
    # 換一個 email 不受影響
    assert client.post("/api/public/contact", json={**GOOD, "email": "z@example.com"}).status_code == 200
    clock.t += 86400
    assert client.post("/api/public/contact", json=GOOD).status_code == 200


def test_mailer_failure_still_200_ticket_unmailed_alert_cooldown(tmp_path):
    """使用者裁決 2：DB＋TG 收到＝送出成功；Email 失敗只告警（1 小時冷卻）、mailed=0。"""
    client, clock, mailer, notifier, store = _make(tmp_path, fail=True)
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 200 and r.json()["ticket"] == "FLT-2609-0001"
    assert store.get_contact_ticket("FLT-2609-0001")["mailed"] == 0
    assert [x[1] for x in notifier.records] == ["contact_ticket", "contact_mail_failure"]
    assert "FLT-2609-0001" in notifier.records[1][2]
    clock.t += 1000
    client.post("/api/public/contact", json=GOOD)          # 冷卻內：📩 照推，失敗告警不再推
    assert [x[1] for x in notifier.records] == ["contact_ticket", "contact_mail_failure", "contact_ticket"]
    clock.t += 3600
    client.post("/api/public/contact", json=GOOD)
    assert [x[1] for x in notifier.records][-1] == "contact_mail_failure"


def test_unconfigured_smtp_still_accepts_via_db_and_tg(tmp_path):
    client, _, mailer, notifier, store = _make(tmp_path, configured=False)
    r = client.post("/api/public/contact", json=GOOD)
    assert r.status_code == 200 and r.json()["ticket"] == "FLT-2609-0001"
    assert mailer.sent == []
    assert store.get_contact_ticket("FLT-2609-0001")["mailed"] == 0
    assert [x[1] for x in notifier.records] == ["contact_ticket"]


def test_inflight_cap_503_and_recovers(tmp_path):
    mailer = BlockingMailer()
    cfg = make_cfg(tmp_path, contact_smtp_user="site@gmail.com", contact_smtp_pass="pw")
    app, *_ = make_app(tmp_path, cfg=cfg, mailer=mailer)
    results: list[int] = []

    def worker():
        c = TestClient(app, base_url="https://testserver")
        results.append(c.post("/api/public/contact", json=GOOD).status_code)

    threads = [threading.Thread(target=worker) for _ in range(CONTACT_MAX_INFLIGHT)]
    for t in threads:
        t.start()
    for _ in range(CONTACT_MAX_INFLIGHT):
        assert mailer.entered.acquire(timeout=10)
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/public/contact", json=GOOD).status_code == 503
    mailer.release.set()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == [200] * CONTACT_MAX_INFLIGHT
    assert c.post("/api/public/contact", json=GOOD).status_code == 200
    assert mailer.sent == CONTACT_MAX_INFLIGHT + 1
```

⚠️ `row["client_ip"] == "testclient"`：Starlette TestClient 的 `request.client.host` 固定為 `"testclient"`；若實跑得到其他值，把斷言改成實際值並在回報說明。

- [ ] **Step 3 驗收**：`uv run pytest tests/test_contact_module.py tests/test_api_contact.py tests/test_contact_store.py -q` 全綠；`uv run pytest -q` 全綠；`uv run ruff check src tests`。

---

## Task 7B-1 `@sdd`：文案 copy.ts（contact 區整段替換＋settings.leader 兩個 key）

**Files:** `web/src/lib/copy.ts`

- [ ] **Step 1** COPY_ZH 的 `contact: { … },`（從 `  contact: {` 起到 `errGeneric: "送出失敗，請稍後再試。",\n  },` 止）整段替換為：

```ts
  /**
   * `/contact`（設計稿 R3-01～R3-08）：站內唯一聯絡入口，**不出現任何信箱字串**（R3-01）。
   * 成功卡文案照設計稿；錯誤只分「後端 4xx detail 原樣」與「5xx／網路：送出失敗，請稍後再試」（R3-07）。
   */
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
    walletClear: "清除",
    messageLabel: "訊息",
    messagePlaceholder: "請描述發生什麼事、大約時間（UTC）、以及你預期的結果。若是跟單問題，附上策略名稱或跟隨的地址會更快。",
    consent: "送出即表示你同意我們使用此 Email 回覆你的問題，不會用於其他用途。",
    send: "送出訊息",
    sending: "送出中…",
    successTitle: "已收到你的訊息",
    successBody: "回覆會寄到 {email}，通常在 1 個工作日內。回信主旨會帶下方工單編號，請留意垃圾郵件匣。",
    ticketLabel: "工單編號",
    copy: "複製",
    copied: "已複製",
    backHome: "← 回到首頁",
    errEmailInvalid: "請填寫正確的 Email",
    errWalletInvalid: "錢包地址格式不正確（0x 開頭 + 40 位十六進位）",
    errMessageLength: "訊息長度需介於 10 到 2000 字",
    errSendFailed: "送出失敗，請稍後再試",
  },
```

- [ ] **Step 2** COPY_EN 的 `contact: { … },` 整段替換為：

```ts
  contact: {
    eyebrow: "CONTACT",
    heading: "Contact us",
    sub: "Copy-trading questions, fee inquiries, security reports and partnership proposals all start here. We reply to the email you leave, usually within 1 business day.",
    checklistTitle: "Before you send",
    check1: "For copy-trading or billing questions, include your wallet address so we can match on-chain records.",
    check2: "Our reply subject carries a ticket number FLT-XXXX-XXXX. Use it to verify the reply is from us.",
    checkWarnPrefix: "The Filet team will ",
    checkWarnStrong: "never",
    checkWarnSuffix: " ask for your private key or seed phrase, or ask you to transfer funds. Treat any such message as a scam.",
    securityNote: "For security vulnerabilities choose \"Security report\"; it goes to the priority queue.",
    topicLabel: "Topic",
    topics: { copytrade: "Copy trading", billing: "Fees & billing", security: "Security report", partnership: "Partnership", other: "Other" },
    emailLabel: "Email",
    emailHint: "Replies go here",
    emailPlaceholder: "you@example.com",
    walletLabel: "Wallet address",
    walletHint: "Optional · filled automatically when signed in",
    walletPlaceholder: "0x…",
    walletConnected: "Connected wallet",
    walletClear: "Clear",
    messageLabel: "Message",
    messagePlaceholder: "Describe what happened, roughly when (UTC), and what you expected. For copy-trading issues, the strategy name or followed address speeds things up.",
    consent: "By sending you agree that we use this email to reply to your question and for nothing else.",
    send: "Send message",
    sending: "Sending…",
    successTitle: "We received your message",
    successBody: "A reply will go to {email}, usually within 1 business day. The reply subject will carry the ticket number below; please check your spam folder too.",
    ticketLabel: "Ticket number",
    copy: "Copy",
    copied: "Copied",
    backHome: "← Back to home",
    errEmailInvalid: "Please enter a valid email address",
    errWalletInvalid: "Invalid wallet address (0x followed by 40 hex characters)",
    errMessageLength: "Message must be between 10 and 2000 characters",
    errSendFailed: "Sending failed. Please try again later",
  },
```

- [ ] **Step 3** `settings.leader` 區：zh 的 `advancedModeBtn: "進階模式",` 之後加一行 `helpPrompt: "需要協助？", helpLink: "聯絡我們 →",`；en 的 `advancedModeBtn: "Advanced mode",` 之後加 `helpPrompt: "Need help?", helpLink: "Contact us →",`。

- [ ] **Step 4 驗收**：`grep -c "fallbackEmail\|goldwisetw\|walletUseOther\|errNetwork\|errGeneric" web/src/lib/copy.ts` → 0；`grep -c "helpPrompt" web/src/lib/copy.ts` → 2。

---

## Task 7B-2 `@sdd`：api.ts 契約

**Files:** `web/src/lib/api.ts`、`web/src/lib/api.test.ts`

- [ ] **Step 1** `api.ts` 的 `ContactBody` 改為：

```ts
export interface ContactBody {
  topic: ContactTopic; email: string; wallet: string; message: string;
  page_url: string; user_agent: string; website?: string;
}
```

- [ ] **Step 2** `api.test.ts` 既有的 `postContact` 契約測試：body 物件補 `page_url: "https://x/contact", user_agent: "UA"` 並斷言送出的 JSON 含這兩鍵。

- [ ] **Step 3 驗收**：`cd web && npx tsc --noEmit 2>&1 | grep -c "lib/api"` → 0。

---

## Task 7B-3 `@sdd`：設定頁「需要協助？」入口（R3-01）

**Files:** `web/src/app/settings/page.tsx`

- [ ] **Step 1** `LeaderSection` 內 `<div className="step-actions">…</div>` 之後（`</section>` 之前）插入：

```tsx
      <p className="hint settings-help">
        {c.helpPrompt} <Link href="/contact">{c.helpLink}</Link>
      </p>
```

（`Link` 與 `useCopy` 該檔已 import。）

- [ ] **Step 2 驗收**：`grep -n 'href="/contact"' web/src/app/settings/page.tsx` 命中一行；`cd web && npx tsc --noEmit 2>&1 | grep -c "settings/page.tsx"` → 0。

---

## Task 7B-4 `@sdd`：page.tsx 整檔覆寫（成功卡、清除、錯誤映射、page_url／UA）

**Files:** `web/src/app/contact/page.tsx`（整檔覆寫）

- [ ] **Step 1** 檔案全文：

```tsx
"use client";
/**
 * `/contact` — 聯絡表單（設計稿 R3-01～R3-08，2026-09-02 Task 7）。
 * POST /api/public/contact（無需登入）；後端落工單 FLT-YYMM-NNNN、寄信到站主、推 TG。
 * 頁面上**沒有任何信箱字串**（R3-01）。錢包地址登入時自動帶入、可「清除」（R3-06）。
 * 狀態（R3-07）：送出中 disabled；5xx／網路 → 紅字「送出失敗，請稍後再試」保留內容；
 * 4xx → 後端 detail 原樣；成功 → 同位置卡片取代表單，不跳頁。
 * honeypot 欄位 `website`：視覺隱藏＋tabIndex=-1，真人不會填；後端見非空即靜默接受。
 * 窄版（R3-08）：grid-template-areas 讓「送出前請確認」移到表單下方（見 globals.css）。
 */
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { useCopy } from "@/lib/lang";
import { ApiError, postContact, type ContactTopic } from "@/lib/api";
import { useMe } from "@/lib/hooks";
import { shortAddr } from "@/lib/format";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const WALLET_RE = /^0x[0-9a-fA-F]{40}$/;
const TOPICS: ContactTopic[] = ["copytrade", "billing", "security", "partnership", "other"];
type Phase = "idle" | "sending" | "sent";

export default function ContactPage() {
  const c = useCopy().contact;
  const me = useMe();
  const meAddress = me.data?.address ?? "";

  const [topic, setTopic] = useState<ContactTopic>("copytrade");
  const [email, setEmail] = useState("");
  const [wallet, setWallet] = useState("");
  const [walletCleared, setWalletCleared] = useState(false);
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState("");
  const [sentEmail, setSentEmail] = useState("");
  const [copied, setCopied] = useState(false);

  // `wallet === ""` 是載入競態的守門：/api/me 回來之前使用者若已手動輸入地址，不得被自動帶入蓋掉。
  const autofilled = !!meAddress && !walletCleared && wallet === "";

  function clearWallet() {
    setWalletCleared(true);
    setWallet("");
  }

  function validate(): string | null {
    const e = email.trim();
    if (!EMAIL_RE.test(e)) return c.errEmailInvalid;
    const w = autofilled ? meAddress : wallet.trim();
    if (w && !WALLET_RE.test(w)) return c.errWalletInvalid;
    const m = message.trim();
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
      const resp = await postContact({
        topic,
        email: email.trim(),
        wallet: autofilled ? meAddress : wallet.trim(),
        message: message.trim(),
        page_url: typeof window === "undefined" ? "" : window.location.href,
        user_agent: typeof navigator === "undefined" ? "" : navigator.userAgent,
        website,
      });
      setTicket(resp.ticket);
      setSentEmail(email.trim());
      setPhase("sent");
    } catch (e) {
      setPhase("idle");
      if (e instanceof ApiError && (e.kind === "client" || e.kind === "auth") && e.detail) {
        setError(e.detail);                 // 4xx：後端固定字串（422／429）
      } else {
        setError(c.errSendFailed);          // 5xx／網路（R3-07）
      }
    }
  }

  async function copyTicket() {
    try {
      await navigator.clipboard.writeText(ticket);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <main className="page contact-page">
      <div className="contact-grid">
        <header className="contact-head">
          <p className="eyebrow">{c.eyebrow}</p>
          <h1 className="contact-title">{c.heading}</h1>
          <p className="contact-sub">{c.sub}</p>
        </header>

        <aside className="contact-aside">
          <div className="card contact-checklist">
            <p className="contact-checklist-title">{c.checklistTitle}</p>
            <ol className="contact-checklist-list">
              <li>
                <span className="contact-check-num mono">01</span>
                <span>{c.check1}</span>
              </li>
              <li>
                <span className="contact-check-num mono">02</span>
                <span>{c.check2}</span>
              </li>
              <li>
                <span className="contact-check-num mono neg">!</span>
                <span>
                  {c.checkWarnPrefix}
                  <strong>{c.checkWarnStrong}</strong>
                  {c.checkWarnSuffix}
                </span>
              </li>
            </ol>
          </div>
          <p className="contact-security-note">
            <span className="contact-dot" aria-hidden />
            {c.securityNote}
          </p>
        </aside>

        <section className="card contact-card">
          {phase === "sent" ? (
            <div className="contact-success" role="status">
              <div className="contact-success-check" aria-hidden>✓</div>
              <h2 className="contact-success-title">{c.successTitle}</h2>
              <p className="contact-success-body">{c.successBody.replace("{email}", sentEmail)}</p>
              <div className="inset contact-ticket-box">
                <span className="contact-ticket-label">{c.ticketLabel}</span>
                <span className="contact-ticket mono">{ticket}</span>
                <button type="button" className="btn btn-secondary contact-copy-btn" onClick={copyTicket}>
                  {copied ? c.copied : c.copy}
                </button>
              </div>
              <Link href="/" className="contact-back-link">{c.backHome}</Link>
            </div>
          ) : (
            <form onSubmit={onSubmit} noValidate className="contact-form">
              <div className="contact-field">
                <div className="contact-label-row">
                  <span className="contact-label">{c.topicLabel}</span>
                </div>
                <div className="contact-chips" role="radiogroup" aria-label={c.topicLabel}>
                  {TOPICS.map((t) => (
                    <button
                      key={t}
                      type="button"
                      role="radio"
                      aria-checked={topic === t}
                      className={"contact-chip" + (topic === t ? " is-active" : "")}
                      onClick={() => setTopic(t)}
                    >
                      {c.topics[t]}
                    </button>
                  ))}
                </div>
              </div>

              <div className="contact-field">
                <div className="contact-label-row">
                  <label className="contact-label" htmlFor="contact-email">
                    {c.emailLabel} <span className="neg">*</span>
                  </label>
                  <span className="contact-hint">{c.emailHint}</span>
                </div>
                <input
                  id="contact-email"
                  className="addr-input"
                  type="email"
                  value={email}
                  maxLength={254}
                  autoComplete="email"
                  placeholder={c.emailPlaceholder}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>

              <div className="contact-field">
                <div className="contact-label-row">
                  <label className="contact-label" htmlFor="contact-wallet">
                    {c.walletLabel}
                  </label>
                  <span className="contact-hint">{c.walletHint}</span>
                </div>
                <div className="contact-wallet-wrap">
                  <input
                    id="contact-wallet"
                    className="addr-input mono"
                    readOnly={autofilled}
                    value={autofilled ? shortAddr(meAddress) : wallet}
                    placeholder={c.walletPlaceholder}
                    onChange={(e) => {
                      if (!autofilled) setWallet(e.target.value);
                    }}
                  />
                  {autofilled && (
                    <span className="contact-wallet-tools">
                      <span className="contact-wallet-badge">{c.walletConnected}</span>
                      <button type="button" className="contact-link-btn" onClick={clearWallet}>
                        {c.walletClear}
                      </button>
                    </span>
                  )}
                </div>
              </div>

              <div className="contact-field">
                <div className="contact-label-row">
                  <label className="contact-label" htmlFor="contact-message">
                    {c.messageLabel} <span className="neg">*</span>
                  </label>
                  <span className="contact-hint mono">{message.length} / 2000</span>
                </div>
                <textarea
                  id="contact-message"
                  className="addr-input contact-textarea"
                  rows={7}
                  maxLength={2000}
                  value={message}
                  placeholder={c.messagePlaceholder}
                  onChange={(e) => setMessage(e.target.value)}
                />
              </div>

              <input
                className="visually-hidden"
                tabIndex={-1}
                autoComplete="off"
                aria-hidden="true"
                name="website"
                value={website}
                onChange={(e) => setWebsite(e.target.value)}
              />

              {error && <p className="addr-input-error" role="alert">{error}</p>}

              <div className="contact-footer-row">
                <p className="contact-consent">{c.consent}</p>
                <button type="submit" className="btn btn-primary" disabled={phase === "sending"}>
                  {phase === "sending" ? c.sending : c.send}
                </button>
              </div>
            </form>
          )}
        </section>
      </div>
    </main>
  );
}
```

- [ ] **Step 2 驗收**：`grep -c "mailto\|fallback" web/src/app/contact/page.tsx` → 0；`cd web && npx tsc --noEmit 2>&1 | grep -c "contact/page.tsx"` → 0。

---

## Task 7B-5 `@sdd`：CSS（R3-08 窄版順序＋44px＋成功卡）

**Files:** `web/src/styles/globals.css`

- [ ] **Step 1** 把 `/* ---------- /contact（雙欄，照 Claude Design 設計稿）---------- */` 起、到 `@media (max-width: 600px) { .contact-footer-row … }` 那一行止的整段替換為：

```css
/* ---------- /contact（設計稿 R3-01～R3-08）---------- */
/* R3-08：<900px 上下堆疊，且「送出前請確認」(aside) 移到表單下方 → grid-template-areas 決定順序 */
.contact-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); gap: 40px; align-items: start;
  grid-template-areas: "head form" "aside form";
  grid-template-rows: auto 1fr;   /* 主線程截圖後補：多出的高度給第二列，head 與 aside 不留空白 */
}
.contact-head { grid-area: head; }
.contact-aside { grid-area: aside; }
.contact-card { grid-area: form; padding: 32px; }
@media (max-width: 900px) {
  .contact-grid { grid-template-columns: minmax(0, 1fr); grid-template-areas: "head" "form" "aside"; gap: 24px; }
}
.contact-title { font-size: 36px; font-weight: 800; margin: 8px 0 12px; letter-spacing: -.01em; }
.contact-sub { color: var(--text-dim); font-size: 15px; line-height: 1.8; margin: 0; }
.contact-checklist { padding: 22px 24px; }
.contact-checklist-title { color: var(--text-dim); font-size: 12px; letter-spacing: .08em; margin: 0 0 14px; }
.contact-checklist-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }
.contact-checklist-list li { display: grid; grid-template-columns: 28px 1fr; gap: 10px; line-height: 1.7; font-size: 14px; }
.contact-check-num { color: var(--primary); font-size: 14px; }
.contact-check-num.neg { color: var(--neg); }
.contact-security-note { display: flex; align-items: center; gap: 10px; color: var(--text-dim); margin: 22px 0 0; font-size: 14px; }
.contact-dot { width: 8px; height: 8px; border-radius: 50%; background: #e9b872; flex: 0 0 auto; }
.contact-form { display: grid; gap: 24px; }
.contact-field { display: grid; gap: 10px; }
.contact-label-row { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.contact-label { font-size: 15px; font-weight: 500; }
.contact-hint { color: var(--text-dim); font-size: 13px; }
.contact-chips { display: flex; flex-wrap: wrap; gap: 10px; }
.contact-chip { min-height: 44px; padding: 8px 14px; border-radius: 12px; border: 1px solid var(--border); background: transparent;
  color: var(--text); font-size: 15px; cursor: pointer; }
.contact-chip:hover { border-color: rgba(var(--text-dim-rgb), .5); }
.contact-chip.is-active { border-color: var(--primary); color: var(--primary); background: rgba(var(--primary-rgb), .08); }
/* R3-08：輸入框高度 ≥ 44px */
.contact-form .addr-input { min-height: 44px; padding: 12px 14px; }
.contact-wallet-wrap { position: relative; }
.contact-wallet-wrap .addr-input { padding-right: 160px; }
.contact-wallet-tools { position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
  display: inline-flex; align-items: center; gap: 12px; }
.contact-wallet-badge { color: var(--primary); font-size: 13px; }
.contact-link-btn { background: none; border: 0; padding: 0; color: var(--text-dim); font-size: 13px;
  cursor: pointer; text-decoration: underline; }
.contact-textarea { resize: vertical; min-height: 180px; font-family: inherit; line-height: 1.7; }
textarea.addr-input:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.contact-footer-row { display: flex; justify-content: space-between; align-items: center; gap: 24px; }
.contact-consent { color: var(--text-dim); font-size: 13px; line-height: 1.7; margin: 0; max-width: 60%; }
/* 成功卡（同位置取代表單，R3-07） */
.contact-success { display: grid; gap: 14px; justify-items: start; }
.contact-success-check { width: 44px; height: 44px; border-radius: 50%; display: grid; place-items: center;
  background: rgba(var(--primary-rgb), .12); color: var(--primary); font-size: 22px; font-weight: 700; }
.contact-success-title { margin: 0; font-size: 20px; }
.contact-success-body { margin: 0; color: var(--text-dim); line-height: 1.8; }
.contact-ticket-box { display: flex; align-items: center; gap: 16px; padding: 14px 18px; width: 100%; box-sizing: border-box; flex-wrap: wrap; }
.contact-ticket-label { color: var(--text-dim); font-size: 12px; letter-spacing: .08em; }
.contact-ticket { font-size: 24px; letter-spacing: .04em; color: var(--primary); flex: 1; }
.contact-copy-btn { padding: 8px 14px; }
.contact-back-link { color: var(--text-dim); font-size: 14px; }
@media (max-width: 600px) { .contact-footer-row { flex-direction: column; align-items: stretch; } .contact-consent { max-width: none; } }
```

（若 `--text-dim-rgb`／`--primary-rgb` 在 tokens.css 不存在，改用 `rgba(255,255,255,.3)`／既有等價變數並回報。）

- [ ] **Step 2 驗收**：`grep -c "grid-template-areas" web/src/styles/globals.css` ≥ 2；`grep -n "min-height: 44px" web/src/styles/globals.css` 命中。

---

## Task 7B-6 `@sdd`：前端測試整檔覆寫

**Files:** `web/src/app/contact/page.test.tsx`（整檔覆寫）

- [ ] **Step 1** 檔案全文：

```tsx
/**
 * `/contact` 頁測試（設計稿 R3-01～R3-08，Task 7）。無需登入、不掛 LangProvider；
 * `useMe` 走 `@/lib/hooks` mock。涵蓋：渲染；R3-01 無信箱字串；R3-02 送出 body 含 page_url／user_agent；
 * R3-06 錢包自動帶入＋徽章＋清除；R3-07 送出中／4xx detail／5xx 與網路統一文案／成功卡（工單、複製、回首頁）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ContactPage from "./page";
import { COPY_ZH } from "@/lib/copy";
import { ApiError } from "@/lib/api";

let mockMe: { data: { address: string; account_id: string } | null };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
}));

vi.mock("@/lib/api", async (orig) => {
  const mod = await orig<typeof import("@/lib/api")>();
  return { ...mod, postContact: vi.fn() };
});
import { postContact } from "@/lib/api";
const mocked = vi.mocked(postContact);

const c = COPY_ZH.contact;
const ADDR = "0x" + "ab".repeat(20);
const MSG = "Hello, a question about fees.";

function renderPage() {
  return render(<ContactPage />);
}
function fillEmail(value: string) {
  fireEvent.change(screen.getByLabelText(c.emailLabel, { exact: false }), { target: { value } });
}
function fillMessage(value: string) {
  fireEvent.change(screen.getByLabelText(c.messageLabel, { exact: false }), { target: { value } });
}
function walletInput() {
  return screen.getByLabelText(c.walletLabel) as HTMLInputElement;
}
function submit() {
  fireEvent.click(screen.getByRole("button", { name: c.send }));
}

describe("/contact", () => {
  beforeEach(() => {
    mocked.mockReset();
    mockMe = { data: null };
  });

  it("渲染：眉標、h1、五顆 chip（預設跟單問題）、三欄位、送出鈕、確認清單、安全提示", () => {
    renderPage();
    expect(screen.getByText(c.eyebrow)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: c.heading })).toBeInTheDocument();
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(5);
    expect(screen.getByRole("radio", { name: c.topics.copytrade })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText(c.emailLabel, { exact: false })).toBeInTheDocument();
    expect(walletInput()).toBeInTheDocument();
    expect(screen.getByLabelText(c.messageLabel, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: c.send })).toBeInTheDocument();
    expect(screen.getByText(c.check1)).toBeInTheDocument();
    expect(screen.getByText(c.securityNote)).toBeInTheDocument();
  });

  it("R3-01：頁面上沒有 mailto 與任何 @ 信箱字串", () => {
    const { container } = renderPage();
    expect(container.querySelector('a[href^="mailto:"]')).toBeNull();
    expect(container.textContent).not.toMatch(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  });

  it("未登入：錢包欄可編輯、無徽章", () => {
    renderPage();
    expect(walletInput()).not.toHaveAttribute("readonly");
    expect(screen.queryByText(c.walletConnected)).toBeNull();
  });

  it("R3-06 已登入：readOnly＋短地址＋徽章；送出帶完整地址；「清除」後可編輯且徽章消失", async () => {
    mockMe = { data: { address: ADDR, account_id: "x" } };
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-2609-0001" });
    renderPage();
    expect(walletInput()).toHaveAttribute("readonly");
    expect(walletInput().value).toContain("…");
    expect(screen.getByText(c.walletConnected)).toBeInTheDocument();
    fillEmail("jim@example.com");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
    expect(mocked.mock.calls[0][0].wallet).toBe(ADDR);
  });

  it("R3-06 清除鈕", () => {
    mockMe = { data: { address: ADDR, account_id: "x" } };
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: c.walletClear }));
    expect(walletInput()).not.toHaveAttribute("readonly");
    expect(walletInput().value).toBe("");
    expect(screen.queryByText(c.walletConnected)).toBeNull();
  });

  it("R3-02 送出 body 含 topic/email/wallet/message/page_url/user_agent/website", async () => {
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-2609-0001" });
    renderPage();
    fireEvent.click(screen.getByRole("radio", { name: c.topics.billing }));
    fillEmail(" jim@example.com ");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
    const body = mocked.mock.calls[0][0];
    expect(body.topic).toBe("billing");
    expect(body.email).toBe("jim@example.com");
    expect(body.wallet).toBe("");
    expect(body.message).toBe(MSG);
    expect(typeof body.page_url).toBe("string");
    expect(typeof body.user_agent).toBe("string");
    expect(body.website).toBe("");
  });

  it("客端驗證：壞 email／壞錢包／短訊息各自顯示錯誤且不打 API", () => {
    renderPage();
    fillEmail("nope");
    fillMessage(MSG);
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent(c.errEmailInvalid);
    fillEmail("jim@example.com");
    fireEvent.change(walletInput(), { target: { value: "0x123" } });
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent(c.errWalletInvalid);
    fireEvent.change(walletInput(), { target: { value: "" } });
    fillMessage("short");
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent(c.errMessageLength);
    expect(mocked).not.toHaveBeenCalled();
  });

  it("字數計數：輸入 12 字後顯示 12 / 2000", () => {
    renderPage();
    fillMessage("123456789012");
    expect(screen.getByText("12 / 2000")).toBeInTheDocument();
  });

  it("R3-07 成功：同位置成功卡（標題、工單、{email} 代入、複製、回首頁），表單消失", async () => {
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-2609-0412" });
    renderPage();
    fillEmail("jim@example.com");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(screen.getByText(c.successTitle)).toBeInTheDocument());
    expect(screen.getByText("FLT-2609-0412")).toBeInTheDocument();
    expect(screen.getByText(c.successBody.replace("{email}", "jim@example.com"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: c.copy })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: c.backHome })).toHaveAttribute("href", "/");
    expect(screen.queryByLabelText(c.emailLabel, { exact: false })).toBeNull();
  });

  it("R3-07 錯誤：4xx detail 原樣；5xx 與網路皆顯示 errSendFailed，內容保留", async () => {
    mocked.mockRejectedValueOnce(new ApiError("client", "送出太頻繁，請稍後再試", 429, "送出太頻繁，請稍後再試"));
    renderPage();
    fillEmail("jim@example.com");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("送出太頻繁，請稍後再試"));
    expect((screen.getByLabelText(c.messageLabel, { exact: false }) as HTMLTextAreaElement).value).toBe(MSG);

    mocked.mockRejectedValueOnce(new ApiError("upstream", "x", 502, "x"));
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(c.errSendFailed));

    mocked.mockRejectedValueOnce(new ApiError("network", "x"));
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(c.errSendFailed));
  });
});
```

- [ ] **Step 2 驗收**：`cd web && npm test` 全綠；`npm run lint`；`npx tsc --noEmit 2>&1 | grep -c "contact\|settings/page.tsx\|lib/api"` → 0；`grep -rn "mailto:\|goldwisetw" web/src` → 0 命中。

---

## Task 7C（主線程）：截圖對照、審查、部署

1. dev server 截圖三態（1440 未登入、1440 模擬登入＋填寫、480 窄版）＋成功態（route mock `/api/public/contact` 回 `{ok:true,ticket:"FLT-2609-0412"}`），逐項對照設計稿 R3 區段。
2. reviewer（opus）對照本 plan 的「R3 規則 → Task 對照」表逐條審。
3. 部署照 RUNBOOK §3.2／§4.2／§9.2；sqlite 新表由 `CREATE TABLE IF NOT EXISTS` 於 API 啟動時自建（無需人工 migration）；驗證：真實送出一筆 topic=other → 回應 `FLT-2609-0001`、信件主旨 `[Filet FLT-2609-0001] 其他`、TG 收到 `📩 FLT-2609-0001｜其他`。
4. RUNBOOK 附錄 B 部署記錄；§5.8b 補「工單表 contact_tickets 在 /var/lib/filet-api/api.db」。

## Task 7D（主線程，reviewer 修正 2026-09-02）

opus 審查 1C/5W：C（TG parse_mode=HTML 未 escape、回 False 靜默）→ `notify_text` 用 `html.escape`＋路由對 False 記 warning；
W1 email 大小寫繞過日限 → `validate_contact` 小寫正規化；W2 textarea 180px 被 44px 規則蓋掉 → 選擇器同級；
W4 TG 呼叫佔 in-flight 名額 → 移到 `finally` 釋放之後；S1 殘留 JSDoc 刪除；S3 R3 原文檔重抽（8 條）。
未採納：W5 日限檢查與 INSERT 非同一交易（最多多 1 筆）；S2 複製失敗無提示；S4 `settings-help` 無 CSS；S5 sending 態測試。
**待使用者裁決 W3**：隱私政策未提及聯絡表單會記錄 IP／UA／頁面網址（`web/src/content/legal.ts:125-135`），屬法務文字，主線程不擅改。

## 狀態

| Task | 狀態 |
|---|---|
| 7A-1 / 7A-2 / 7A-3 | 待使用者確認 plan |
| 7B-1～7B-6 | 待使用者確認 plan |
| 7C | 待 7A/7B |
