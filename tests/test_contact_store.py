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
