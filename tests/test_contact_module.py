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
    ("copytrade", "a@b.co X", "", "x" * 20),       # U+2028 分隔字元
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


def test_notify_text_escapes_html_for_telegram():
    """TelegramNotifier 固定 parse_mode=HTML：`<` 不 escape 會讓通知靜默 400（reviewer Critical）。"""
    ci = ContactInput(topic="other", email="a@b.co", wallet="", message="價格 <b>低於</b> 100 & 上漲")
    t = notify_text(ci, "FLT-2609-0001")
    assert "&lt;b&gt;低於&lt;/b&gt;" in t and "&amp;" in t and "<b>" not in t


def test_validate_lowercases_email():
    """email 日限用 DB 等值比對；不正規化就能用大小寫變體繞過（reviewer W1）。"""
    assert validate_contact(topic="other", email="JIM@Example.COM", wallet="", message="x" * 20).email == "jim@example.com"


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
