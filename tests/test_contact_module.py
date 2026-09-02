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
    ("Jim Bcc: x@y.z", "a@b.co", "x" * 20),   # U+2028 分隔字元
    ("Jim\x85x", "a@b.co", "x" * 20),               # NEL
    ("Jim", "測試@例え.jp", "x" * 20),               # 非 ASCII email
    ("Jim", "a@b", "x" * 20),                        # 無 TLD
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
