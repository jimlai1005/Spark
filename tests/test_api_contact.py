"""tests/test_api_contact.py — POST /api/public/contact：無需登入；驗證→honeypot→IP 限流
→寄信；寄信失敗 502、未設定 503。全離線（FakeMailer；loopback 放行供 TestClient）。"""
import socket
import threading
import time
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from spark.copytrade.notifier import RecordingNotifier
from spark.publicapi.app import (
    CONTACT_MAX_INFLIGHT, CONTACT_RATELIMIT_MAX, CONTACT_RATELIMIT_WINDOW_S,
)
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
    {**GOOD, "name": "a Bcc: x@y.z"},
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


def test_mailer_failure_alerts_notifier_with_cooldown(tmp_path):
    cfg = make_cfg(tmp_path, contact_smtp_user="site@gmail.com", contact_smtp_pass="pw")
    clock = Clock()
    mailer = FakeMailer(fail=True)
    notifier = RecordingNotifier()
    app, *_ = make_app(tmp_path, cfg=cfg, now_fn=clock, mailer=mailer, notifier=notifier)
    client = TestClient(app, base_url="https://testserver")

    assert client.post("/api/public/contact", json=GOOD).status_code == 502
    assert client.post("/api/public/contact", json=GOOD).status_code == 502
    critical = [r for r in notifier.records if r[0] == "critical"]
    assert len(critical) == 1

    clock.t += 3600
    assert client.post("/api/public/contact", json=GOOD).status_code == 502
    critical = [r for r in notifier.records if r[0] == "critical"]
    assert len(critical) == 2
