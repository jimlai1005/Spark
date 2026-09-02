"""tests/test_api_contact.py — POST /api/public/contact 對齊設計稿 R3-02～R3-05：
honeypot decoy、422、503、in-flight、IP 5/小時、email 10/日、工單 FLT-YYMM-NNNN 落 DB、
寄信主旨、TG 通知（每筆 info；security critical 🚨）、寄信失敗 502＋mailed=0。全離線。"""
import re
import socket
import threading
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
    {**GOOD, "email": "a@b.co X"},
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
    # 順序：寄信（失敗告警）在 in-flight 名額內，📩 通知在名額釋放後
    assert [x[1] for x in notifier.records] == ["contact_mail_failure", "contact_ticket"]
    assert "FLT-2609-0001" in notifier.records[0][2]
    clock.t += 1000
    client.post("/api/public/contact", json=GOOD)          # 冷卻內：📩 照推，失敗告警不再推
    assert [x[1] for x in notifier.records] == ["contact_mail_failure", "contact_ticket", "contact_ticket"]
    clock.t += 3600
    client.post("/api/public/contact", json=GOOD)
    assert [x[1] for x in notifier.records].count("contact_mail_failure") == 2


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
