"""tests/test_publicapi_store.py"""
import sqlite3

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
    """⭐ 亂序守衛：event_created 較舊的 upsert 整筆 no-op——已取消的訂閱不因晚到的
    舊 active 事件復活。同秒平手不等於「允許」——偏向低權益（opus 總審 F1）：
    active 不得覆寫同秒已存在的 canceled，維持 no-op。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="canceled", now_s=2.0, event_created=200)
    store.upsert_billing(acct, status="active", now_s=3.0, event_created=100)  # 舊事件晚到
    rec = store.get_billing(acct)
    assert rec.status == "canceled"                   # 不復活
    assert rec.last_event_created == 200
    assert rec.updated_at == 2.0                      # 整筆 no-op，非只擋 status
    # 同秒平手：active（rank2）不得覆寫既有 canceled（rank0）——opus 總審 F1 修復點
    store.upsert_billing(acct, status="active", now_s=4.0, event_created=200)
    rec2 = store.get_billing(acct)
    assert rec2.status == "canceled"                  # 仍未復活
    assert rec2.updated_at == 2.0                      # 整筆 no-op


def test_billing_upsert_same_second_tie_favors_lower_entitlement(tmp_path):
    """⭐ opus 總審 F1：同秒平手偏向低權益——降級允許、升級拒絕。
    active@200 之後同秒收到 canceled@200 → 套用（降級）；
    上一個測試已驗證反方向（canceled@200 後同秒 active@200 → 拒絕/no-op）。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="active", now_s=1.0, event_created=200)
    store.upsert_billing(acct, status="canceled", now_s=2.0, event_created=200)  # 同秒降級
    rec = store.get_billing(acct)
    assert rec.status == "canceled"
    assert rec.updated_at == 2.0                       # 有套用，非 no-op


def test_billing_upsert_replay_by_event_id_is_full_noop(tmp_path):
    """⭐ opus 總審 F1：重放冪等靠 event_id——同一 event_id 再次送達，即使 status／
    event_created／now_s 都不同（病態重放）也整筆 no-op，連 updated_at 都不動。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="active", now_s=1.0, event_created=100,
                         event_id="evt_1")
    store.upsert_billing(acct, status="canceled", now_s=99.0, event_created=999,
                         event_id="evt_1")  # 同 id、狀態與時間都不同——仍 no-op
    rec = store.get_billing(acct)
    assert rec.status == "active"
    assert rec.updated_at == 1.0
    assert rec.last_event_created == 100
    assert rec.last_event_id == "evt_1"


def test_billing_upsert_strict_newer_created_applies(tmp_path):
    """嚴格較新 event_created（非平手）一律套用，不受 rank 限制——即使是升級。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="canceled", now_s=1.0, event_created=100)
    store.upsert_billing(acct, status="active", now_s=2.0, event_created=200)  # 嚴格較新
    rec = store.get_billing(acct)
    assert rec.status == "active"
    assert rec.updated_at == 2.0


def test_billing_migration_adds_last_event_id_to_existing_table(tmp_path):
    """真正的欄位級 additive migration（opus 總審 F1）：`CREATE TABLE IF NOT EXISTS`
    不會幫已存在的舊 billing 表（此欄新增前建立）補新欄——舊表重開必須自動補上，
    不炸、預設空字串（等同「從未記錄過重放水位」），既有資料原地不動。"""
    db = tmp_path / "api.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE billing ("
        "  account_id TEXT PRIMARY KEY,"
        "  stripe_customer_id TEXT,"
        "  stripe_subscription_id TEXT,"
        "  status TEXT NOT NULL DEFAULT 'none',"
        "  updated_at REAL NOT NULL DEFAULT 0,"
        "  last_event_created INTEGER NOT NULL DEFAULT 0"
        ");")
    acct = "f" + "ab" * 20
    conn.execute("INSERT INTO billing (account_id, status, updated_at, last_event_created) "
                "VALUES (?, 'active', 1.0, 100)", (acct,))
    conn.commit()
    conn.close()
    store = _mkstore(tmp_path)
    rec = store.get_billing(acct)
    assert rec.status == "active"                      # 舊資料不動
    assert rec.last_event_created == 100
    assert rec.last_event_id == ""                      # 新欄預設值
    store.upsert_billing(acct, status="canceled", now_s=2.0, event_created=200,
                         event_id="evt_x")
    rec2 = store.get_billing(acct)
    assert rec2.status == "canceled"
    assert rec2.last_event_id == "evt_x"


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
                    "status", "updated_at", "last_event_created", "last_event_id"}


# ---------- pending checkout（重複結帳擋板；opus 對抗審查 Important） ----------

def test_claim_pending_checkout_blocks_second_claim(tmp_path):
    """⭐ 佔位是原子的單一判準：第二次 claim 在 TTL 內一律 False（呼叫端 409）。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    assert store.claim_pending_checkout(acct, now_s=100.0, ttl_s=900) is True
    assert store.claim_pending_checkout(acct, now_s=100.0, ttl_s=900) is False
    assert store.claim_pending_checkout(acct, now_s=999.0, ttl_s=900) is False


def test_claim_pending_checkout_expires_and_is_per_account(tmp_path):
    """逾時後可重新佔位（放棄付款者不被永久卡死）；不同 account 互不影響。"""
    store = _mkstore(tmp_path)
    a, b = "f" + "ab" * 20, "f" + "cd" * 20
    assert store.claim_pending_checkout(a, now_s=100.0, ttl_s=900) is True
    assert store.claim_pending_checkout(b, now_s=100.0, ttl_s=900) is True
    assert store.claim_pending_checkout(a, now_s=1000.0, ttl_s=900) is True  # 100+900
    assert store.get_pending_checkout(a) == 1000.0   # 佔位時刻就地更新


def test_clear_pending_checkout_is_idempotent(tmp_path):
    """clear 是 DELETE：重複呼叫無害（webhook 重放與失敗補償都可能重複觸發）。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.claim_pending_checkout(acct, now_s=100.0, ttl_s=900)
    store.clear_pending_checkout(acct)
    store.clear_pending_checkout(acct)
    assert store.get_pending_checkout(acct) is None
    assert store.claim_pending_checkout(acct, now_s=101.0, ttl_s=900) is True


def test_claim_pending_checkout_validates_account_id(tmp_path):
    """account_id 會流進檔案路徑，驗證在 store 這層（單一邊界，工程原則 5）。"""
    import pytest
    store = _mkstore(tmp_path)
    with pytest.raises(ValueError):
        store.claim_pending_checkout("../evil", now_s=1.0, ttl_s=900)


def test_pending_checkouts_table_has_no_sensitive_columns(tmp_path):
    """⭐ 紅線 7 結構性斷言（同 billing 表）：新表只有 account_id 與時刻，
    無金額、無 Stripe session/secret——新增欄位必須回這裡改白名單。"""
    store = _mkstore(tmp_path)
    cols = {row[1] for row in store._db.execute("PRAGMA table_info(pending_checkouts)")}
    assert cols == {"account_id", "created_at"}


def test_pending_checkouts_table_appears_on_existing_db(tmp_path):
    """對舊 schema DB 重開 → 新表自動出現（CREATE TABLE IF NOT EXISTS 每次都跑）。"""
    from spark.publicapi.store import ApiStore
    db = tmp_path / "api.db"
    ApiStore(db).ensure_onboarding("f" + "ab" * 20, "0x" + "ab" * 20)
    assert ApiStore(db).claim_pending_checkout("f" + "ab" * 20, now_s=1.0,
                                               ttl_s=900) is True
