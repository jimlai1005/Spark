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
