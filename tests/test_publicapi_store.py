"""tests/test_publicapi_store.py"""
from spark.publicapi.store import ApiStore

_ADDR = "0x" + "ab" * 20


def _store(tmp_path):
    return ApiStore(tmp_path / "api.db")


def test_nonce_single_use(tmp_path):
    st = _store(tmp_path)
    n = st.issue_nonce(_ADDR, 42161, "2026-07-17T00:00:00Z", now_s=1000.0, ttl_s=300)
    rec = st.consume_nonce(n, now_s=1001.0)
    assert rec is not None
    assert rec.address == _ADDR and rec.chain_id == 42161
    assert rec.issued_at == "2026-07-17T00:00:00Z"
    # 第二次一定 None：原子 UPDATE 單次使用（防有效期內重放）
    assert st.consume_nonce(n, now_s=1002.0) is None


def test_nonce_expired_not_consumable(tmp_path):
    st = _store(tmp_path)
    n = st.issue_nonce(_ADDR, 1, "2026-07-17T00:00:00Z", now_s=1000.0, ttl_s=300)
    assert st.consume_nonce(n, now_s=1301.0) is None


def test_nonce_unknown_none(tmp_path):
    assert _store(tmp_path).consume_nonce("nope", now_s=0.0) is None


def test_session_roundtrip_expiry_delete(tmp_path):
    st = _store(tmp_path)
    sid = st.create_session(_ADDR, now_s=1000.0, ttl_s=3600)
    assert st.get_session_address(sid, now_s=2000.0) == _ADDR
    assert st.get_session_address(sid, now_s=4601.0) is None      # 過期
    assert st.get_session_address("nope", now_s=1000.0) is None   # 不存在
    st.delete_session(sid)
    assert st.get_session_address(sid, now_s=1001.0) is None


def test_onboarding_agent_address(tmp_path):
    st = _store(tmp_path)
    acct = "f" + "ab" * 20
    assert st.get_agent_address(acct) is None
    st.ensure_onboarding(acct, _ADDR)
    st.ensure_onboarding(acct, _ADDR)  # 冪等
    assert st.get_agent_address(acct) is None
    st.set_agent_address(acct, "0x" + "cd" * 20)
    assert st.get_agent_address(acct) == "0x" + "cd" * 20


def test_onboarding_rows_isolated(tmp_path):
    st = _store(tmp_path)
    a, b = "f" + "ab" * 20, "f" + "cd" * 20
    st.ensure_onboarding(a, _ADDR)
    st.ensure_onboarding(b, "0x" + "cd" * 20)
    st.set_agent_address(a, "0x" + "ee" * 20)
    assert st.get_agent_address(b) is None  # 各 account 進度獨立


# ---------- billing（M3 計費骨幹） ----------

def _mkstore(tmp_path):
    from spark.publicapi.store import ApiStore
    return ApiStore(tmp_path / "api.db")


def test_billing_get_missing_returns_none(tmp_path):
    store = _mkstore(tmp_path)
    assert store.get_billing("f" + "ab" * 20) is None


def test_billing_upsert_and_get_roundtrip(tmp_path):
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="active", stripe_customer_id="cus_1",
                         stripe_subscription_id="sub_1", now_s=1000.0, event_created=500)
    rec = store.get_billing(acct)
    assert rec.account_id == acct
    assert rec.stripe_customer_id == "cus_1"
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.status == "active"
    assert rec.updated_at == 1000.0
    assert rec.last_event_created == 500


def test_billing_upsert_is_idempotent_and_keeps_ids_on_none(tmp_path):
    """webhook 事件可能重送：upsert 冪等；後續事件未帶 id（None）不得清掉已存 id。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="active", stripe_customer_id="cus_1",
                         stripe_subscription_id="sub_1", now_s=1000.0, event_created=500)
    store.upsert_billing(acct, status="past_due", now_s=2000.0, event_created=600)  # id 未帶
    rec = store.get_billing(acct)
    assert rec.status == "past_due"
    assert rec.stripe_customer_id == "cus_1"          # COALESCE 保留
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.updated_at == 2000.0


def test_billing_upsert_monotonic_guard_rejects_stale(tmp_path):
    """⭐ 亂序守衛（opus 必改 1）：event_created 較舊的 upsert 整筆 no-op——已取消的
    訂閱不因晚到的舊 active 事件復活；`>=` 允許同值＝重放（同 event）冪等仍成立。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="canceled", now_s=2.0, event_created=200)
    store.upsert_billing(acct, status="active", now_s=3.0, event_created=100)  # 舊事件晚到
    rec = store.get_billing(acct)
    assert rec.status == "canceled"                   # 不復活
    assert rec.last_event_created == 200
    assert rec.updated_at == 2.0                      # 整筆 no-op，非只擋 status
    store.upsert_billing(acct, status="active", now_s=4.0, event_created=200)  # 同值允許
    assert store.get_billing(acct).status == "active"


def test_billing_rejects_unknown_status(tmp_path):
    import pytest
    store = _mkstore(tmp_path)
    with pytest.raises(ValueError):
        store.upsert_billing("f" + "ab" * 20, status="paid", now_s=1.0)


def test_billing_upsert_rejects_bad_account_id(tmp_path):
    """account_id 在 store 邊界驗證（單一邊界，工程原則 5）——會流進檔案路徑，
    路徑穿越字元一律拒收，所有呼叫端都繞不開。"""
    import pytest
    store = _mkstore(tmp_path)
    with pytest.raises(ValueError):
        store.upsert_billing("../evil", status="active", now_s=1.0)
    assert store.get_billing("../evil") is None


def test_billing_lookup_by_subscription(tmp_path):
    store = _mkstore(tmp_path)
    acct = "f" + "cd" * 20
    store.upsert_billing(acct, status="active", stripe_subscription_id="sub_9", now_s=1.0)
    assert store.get_billing_by_subscription("sub_9").account_id == acct
    assert store.get_billing_by_subscription("sub_none") is None


def test_billing_migration_is_additive_on_existing_db(tmp_path):
    """對「舊 schema DB」重開 store → billing 表自動出現，舊表資料不動。"""
    from spark.publicapi.store import ApiStore
    db = tmp_path / "api.db"
    s1 = ApiStore(db)
    s1.ensure_onboarding("f" + "ab" * 20, "0x" + "ab" * 20)
    s2 = ApiStore(db)  # 重開＝migration 路徑
    assert s2.get_agent_address("f" + "ab" * 20) is None  # 舊表仍在、資料不動
    assert s2.get_billing("f" + "ab" * 20) is None        # 新表可用


def test_billing_table_has_no_sensitive_columns(tmp_path):
    """⭐ 紅線 7 結構性斷言：billing 表欄位集合精確等於白名單——
    無金額、無卡號、無 email；新增欄位必須回這裡改白名單（強迫審視）。"""
    store = _mkstore(tmp_path)
    cols = {row[1] for row in store._db.execute("PRAGMA table_info(billing)")}
    assert cols == {"account_id", "stripe_customer_id", "stripe_subscription_id",
                    "status", "updated_at", "last_event_created"}
