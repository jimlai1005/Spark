"""tests/test_contact_module.py — /contact 寄信模組純邏輯（驗證＋組信）與 config 成對檢查。
全離線：本檔不建 SmtpMailer 連線（autouse socket-ban）。

Task 6（2026-09-02，設計稿改版）：欄位改為 topic／email／wallet／message，移除 name；
新增工單編號 `FLT-XXXX-XXXX`。"""
import re

import pytest

from spark.publicapi.contact import (
    ContactInput, ContactValidationError, build_contact_email, new_ticket_id, validate_contact,
)
from spark.publicapi.config import ApiConfig
from tests.publicapi_helpers import make_cfg


def test_validate_ok_strips_whitespace():
    ci = validate_contact(topic=" copytrade ", email=" a@b.co ", wallet="",
                          message="  hello there, ten+ chars ")
    assert ci == ContactInput(topic="copytrade", email="a@b.co", wallet="",
                              message="hello there, ten+ chars")


def test_validate_ok_with_valid_wallet():
    wallet = "0x" + "ab" * 20
    ci = validate_contact(topic="billing", email="a@b.co", wallet=f" {wallet} ",
                          message="x" * 20)
    assert ci.wallet == wallet


@pytest.mark.parametrize("topic,email,wallet,message", [
    ("nope", "a@b.co", "", "x" * 20),                 # topic 不在清單
    ("", "a@b.co", "", "x" * 20),                      # topic 空
    ("copytrade", "not-an-email", "", "x" * 20),
    ("copytrade", "a@b.co\nX", "", "x" * 20),
    ("copytrade", "a" * 250 + "@b.co", "", "x" * 20),  # email 太長
    ("copytrade", "a@b.co", "", "short"),              # message 太短
    ("copytrade", "a@b.co", "", "x" * 2001),           # message 太長
    ("copytrade", "a@b.co\u2028X", "", "x" * 20),      # U+2028 分隔字元
    ("copytrade", "a\x85@b.co", "", "x" * 20),         # NEL
    ("copytrade", "測試@例え.jp", "", "x" * 20),         # 非 ASCII email
    ("copytrade", "a@b", "", "x" * 20),                # 無 TLD
    ("copytrade", "a@b.co", "0x123", "x" * 20),        # wallet 太短
    ("copytrade", "a@b.co", "0x" + "zz" * 20, "x" * 20),  # wallet 非十六進位
    ("copytrade", "a@b.co", "ab" * 21, "x" * 20),      # wallet 缺 0x 前綴
])
def test_validate_rejects(topic, email, wallet, message):
    with pytest.raises(ContactValidationError):
        validate_contact(topic=topic, email=email, wallet=wallet, message=message)


def test_build_email_headers_and_body():
    ci = ContactInput(topic="copytrade", email="a@b.co", wallet="",
                      message="line1\nline2 long enough")
    msg = build_contact_email(ci, ticket="FLT-AAAA-BBBB", sender="site@gmail.com",
                              to="owner@gmail.com", client_ip="1.2.3.4",
                              now_iso="2026-09-02T00:00:00Z")
    assert msg["From"] == "site@gmail.com"
    assert msg["To"] == "owner@gmail.com"
    assert msg["Reply-To"] == "a@b.co"
    assert msg["Subject"] == "[FLT-AAAA-BBBB] Filet 聯絡表單：跟單問題"
    body = msg.get_content()
    assert "FLT-AAAA-BBBB" in body and "a@b.co" in body and "1.2.3.4" in body
    assert "（未提供）" in body
    assert "line1\nline2 long enough" in body
    assert msg.get_content_type() == "text/plain"


def test_build_email_security_topic_subject_prefix():
    ci = ContactInput(topic="security", email="a@b.co", wallet="0x" + "ab" * 20,
                      message="x" * 20)
    msg = build_contact_email(ci, ticket="FLT-AAAA-BBBB", sender="site@gmail.com",
                              to="owner@gmail.com", client_ip="1.2.3.4",
                              now_iso="2026-09-02T00:00:00Z")
    assert msg["Subject"] == "[FLT-AAAA-BBBB] 【安全回報】Filet 聯絡表單：安全回報"
    assert ("0x" + "ab" * 20) in msg.get_content()


def test_new_ticket_id_format_and_uniqueness():
    pattern = re.compile(r"^FLT-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
    tickets = {new_ticket_id() for _ in range(50)}
    assert len(tickets) == 50
    for t in tickets:
        assert pattern.match(t), t


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
