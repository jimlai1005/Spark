"""publicapi/contact.py — /contact 聯絡表單的驗證、組信與 SMTP 寄送。

分層：`validate_contact` / `build_contact_email` 是純函式（可離線測）；`SmtpMailer`
是唯一副作用點（Gmail SMTP_SSL），透過 `create_app(mailer=...)` 注入，測試給 FakeMailer。
寄信**非冪等**：任何層都不得自動重試（工程原則 2）；失敗由路由層 log + 502。

Task 6（2026-09-02，設計稿改版）：欄位改為 主題／Email／錢包地址（選填）／訊息，
移除姓名；新增工單編號 `FLT-XXXX-XXXX`（進信件主旨、也回給前端顯示給使用者）。
"""
from __future__ import annotations

import html
import logging
import re
import secrets
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)

EMAIL_MAX = 254
MESSAGE_MIN = 10
MESSAGE_MAX = 2000
# str.splitlines() 視為換行的全部字元。EmailMessage 對含這些字元的 header 會 raise
# ValueError（落在路由 try 之外 → 500）；在驗證層先擋成 422。
_LINEBREAK_CHARS = frozenset("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
# 只收 ASCII addr-spec：非 ASCII 位址會被 EmailMessage 編成 RFC 2047 encoded-word 塞進
# Reply-To（=非法位址，站主按回覆會退信）。國際化位址（EAI）本站不支援。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465
_SMTP_TIMEOUT_S = 20.0

CONTACT_TOPICS = ("copytrade", "billing", "security", "partnership", "other")
CONTACT_TOPIC_LABELS = {
    "copytrade": "跟單問題", "billing": "費用與帳務", "security": "安全回報",
    "partnership": "合作提案", "other": "其他",
}
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

PAGE_URL_MAX = 512
USER_AGENT_MAX = 512
NOTIFY_PREVIEW_CHARS = 200


def clip(s: str, n: int) -> str:
    return (s or "").strip()[:n]


def ticket_month(now_s: float) -> str:
    """FLT-YYMM-NNNN 的 YYMM 段（UTC）。"""
    return datetime.fromtimestamp(now_s, tz=timezone.utc).strftime("%y%m")


def decoy_ticket(now_s: float) -> str:
    """honeypot 命中時回給機器人的假工單：格式同真工單、不落 DB（R3-03「靜默丟棄但仍回成功」）。"""
    return f"FLT-{ticket_month(now_s)}-{secrets.randbelow(9000) + 1000:04d}"


def _has_linebreak(s: str) -> bool:
    return any(ch in _LINEBREAK_CHARS for ch in s)


class ContactValidationError(ValueError):
    """detail 為可安全外顯的固定字串（不回顯輸入）。"""


@dataclass(frozen=True)
class ContactInput:
    topic: str
    email: str
    wallet: str          # "" ＝ 未提供
    message: str


def validate_contact(*, topic: str, email: str, wallet: str, message: str) -> ContactInput:
    topic = (topic or "").strip()
    # 小寫正規化：email 日限（R3-03）用 DB 等值比對，不正規化就能用大小寫變體繞過（reviewer W1）。
    email = (email or "").strip().lower()
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
    # ⭐ TelegramNotifier 固定 parse_mode=HTML：用戶文字必須 escape，否則一個 `<` 就讓
    #   Telegram 回 400、通知靜默消失（reviewer Critical）。
    return f"{head}\n{html.escape(ci.message[:NOTIFY_PREVIEW_CHARS], quote=False)}"


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
