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
# str.splitlines() 視為換行的全部字元。EmailMessage 對含這些字元的 header 會 raise
# ValueError（落在路由 try 之外 → 500）；在驗證層先擋成 422。
_LINEBREAK_CHARS = frozenset("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
# 只收 ASCII addr-spec：非 ASCII 位址會被 EmailMessage 編成 RFC 2047 encoded-word 塞進
# Reply-To（=非法位址，站主按回覆會退信）。國際化位址（EAI）本站不支援。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465
_SMTP_TIMEOUT_S = 20.0


def _has_linebreak(s: str) -> bool:
    return any(ch in _LINEBREAK_CHARS for ch in s)


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
    if not name or len(name) > NAME_MAX or _has_linebreak(name):
        raise ContactValidationError("姓名為必填，且不得超過 80 字")
    if (not email or len(email) > EMAIL_MAX or _has_linebreak(email)
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
