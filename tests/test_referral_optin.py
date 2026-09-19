"""tests/test_referral_optin.py
客戶簽章的 Hyperliquid 推薦碼 opt-in 記錄（spark.filet.referral_optin）——
格式、驗證原語、域分隔（risk_settings 的姊妹檔，同一套信任錨）。

用真密碼學（eth_account 本地運算，不觸網，沿 test_risk_settings.py 慣例）。
"""
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.filet.referral_optin import (ACTION_REFERRAL_OPTIN,
                                        REFERRAL_OPTIN_FIELDS,
                                        REFERRAL_OPTIN_MAX_AGE_S,
                                        ReferralOptinError,
                                        build_referral_optin_message,
                                        build_referral_optin_record,
                                        load_referral_optins,
                                        normalize_referral_code,
                                        referral_optin_path_for,
                                        verify_referral_optin,
                                        write_referral_optin)

_NOW = 1_800_000_000.0


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _always(_nonce: str) -> bool:
    return True


def _never(_nonce: str) -> bool:
    return False


def _sign(wallet, message: str) -> str:
    return wallet.sign_message(encode_defunct(text=message)).signature.hex()


def _record(wallet, *, account_id=None, code="FILET123", nonce="n1",
            issued_at=None, signer=None, tamper=None) -> dict:
    """簽一筆合法的推薦碼 opt-in 記錄。`tamper` 在簽完之後改動欄位（重放／竄改）。"""
    account_id = account_id or _acct(wallet)
    issued_at = issued_at or _at()
    msg = build_referral_optin_message(account_id=account_id, code=code,
                                       nonce=nonce, issued_at=issued_at)
    rec = build_referral_optin_record(account_id=account_id, code=code, nonce=nonce,
                                      issued_at=issued_at,
                                      signature=_sign(signer or wallet, msg),
                                      message=msg)
    if tamper:
        rec.update(tamper)
    return rec


@pytest.fixture
def wallet():
    return Account.create()


# ── 快樂路徑 ──────────────────────────────────────────────────────────

def test_verifies_valid_record_and_returns_code(wallet):
    v = verify_referral_optin(_record(wallet), account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always)
    assert v.account_id == _acct(wallet)
    assert v.user_address == wallet.address.lower()
    assert v.code == "FILET123"
    assert v.nonce == "n1"
    assert v.issued_at_s == pytest.approx(_NOW)


def test_record_field_set_is_pinned_by_the_constant(wallet):
    assert tuple(_record(wallet).keys()) == REFERRAL_OPTIN_FIELDS


def test_action_is_written_by_the_builder_not_the_caller(wallet):
    assert _record(wallet)["action"] == ACTION_REFERRAL_OPTIN


# ── 域分隔與帳號 ──────────────────────────────────────────────────────

def test_action_mismatch_is_rejected(wallet):
    rec = {**_record(wallet), "action": "risk_settings"}
    with pytest.raises(ReferralOptinError) as e:
        verify_referral_optin(rec, account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always)
    assert e.value.reason == "action_mismatch"


def test_account_mismatch_is_rejected(wallet):
    with pytest.raises(ReferralOptinError) as e:
        verify_referral_optin(_record(wallet), account_id="f" + "b2" * 20,
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always)
    assert e.value.reason == "account_mismatch"


# ── 推薦碼正規化 ──────────────────────────────────────────────────────

def test_lowercase_code_is_normalized_to_uppercase_in_the_message(wallet):
    """⭐ code 小寫進來被 normalize 成大寫，且原文含 `Referral Code: XXX`。"""
    msg = build_referral_optin_message(account_id=_acct(wallet), code="filet123",
                                       nonce="n1", issued_at=_at())
    assert "Referral Code: FILET123" in msg
    v = verify_referral_optin(_record(wallet, code="filet123"),
                              account_id=_acct(wallet), user_address=wallet.address,
                              now_s=_NOW, consume_nonce=_always)
    assert v.code == "FILET123"


def test_code_with_hyphen_is_malformed():
    with pytest.raises(ReferralOptinError) as e:
        normalize_referral_code("FI-LET")
    assert e.value.reason == "malformed"


def test_code_too_long_is_malformed():
    with pytest.raises(ReferralOptinError) as e:
        normalize_referral_code("A" * 33)
    assert e.value.reason == "malformed"


def test_malformed_code_in_record_is_rejected(wallet):
    rec = {**_record(wallet), "code": "BAD-CODE"}
    with pytest.raises(ReferralOptinError) as e:
        verify_referral_optin(rec, account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always)
    assert e.value.reason == "malformed"


# ── 驗章：偽造 ────────────────────────────────────────────────────────

def test_someone_elses_signature_is_rejected(wallet):
    other = Account.create()
    with pytest.raises(ReferralOptinError) as e:
        verify_referral_optin(_record(wallet, signer=other), account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always)
    assert e.value.reason == "signer_mismatch"


# ── 時效語意 ──────────────────────────────────────────────────────────

def test_api_mode_enforces_expiry(wallet):
    """API 端強制 `max_age_s=REFERRAL_OPTIN_MAX_AGE_S`：客戶剛剛按下的那一次。"""
    old = _record(wallet, issued_at=_at(-REFERRAL_OPTIN_MAX_AGE_S - 60))
    with pytest.raises(ReferralOptinError) as e:
        verify_referral_optin(old, account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always,
                              max_age_s=REFERRAL_OPTIN_MAX_AGE_S)
    assert e.value.reason == "expired"


def test_engine_mode_ignores_expiry_by_default(wallet):
    """⭐⭐ 引擎端 `max_age_s=None`：持續意圖，舊記錄照過（同 risk_settings 放行理由）。"""
    old = _record(wallet, issued_at=_at(-30 * 86400))
    v = verify_referral_optin(old, account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_always)
    assert v.code == "FILET123"


# ── nonce：最後才消耗 ──────────────────────────────────────────────────

def test_nonce_consumption_is_the_last_step(wallet):
    """副作用擺最後：簽章錯時就燒掉 nonce ＝任何人送垃圾就能作廢客戶的授權。"""
    burned = []

    def _consume(n):
        burned.append(n)
        return True

    with pytest.raises(ReferralOptinError):
        verify_referral_optin(_record(wallet, signer=Account.create()),
                              account_id=_acct(wallet), user_address=wallet.address,
                              now_s=_NOW, consume_nonce=_consume)
    assert burned == []


def test_unusable_nonce_is_rejected(wallet):
    with pytest.raises(ReferralOptinError) as e:
        verify_referral_optin(_record(wallet), account_id=_acct(wallet),
                              user_address=wallet.address, now_s=_NOW,
                              consume_nonce=_never)
    assert e.value.reason == "nonce_unusable"


# ── 落檔：同 account 覆蓋、mode 0644 ──────────────────────────────────

def test_write_overwrites_the_same_account(tmp_path, wallet):
    path = referral_optin_path_for(tmp_path)
    write_referral_optin(path, _record(wallet, nonce="n1"))
    write_referral_optin(path, _record(wallet, nonce="n2"))
    entries = load_referral_optins(path)
    assert [e["nonce"] for e in entries] == ["n2"]


def test_write_keeps_other_accounts(tmp_path, wallet):
    other = Account.create()
    path = referral_optin_path_for(tmp_path)
    write_referral_optin(path, _record(wallet))
    write_referral_optin(path, _record(other))
    assert {e["account_id"] for e in load_referral_optins(path)} == {
        _acct(wallet), _acct(other)}


def test_written_files_are_readable_by_the_engine_user(tmp_path, wallet):
    """0644：讀端 filet-engine 是另一個 user（交換目錄無 setgid）。"""
    import os
    path = referral_optin_path_for(tmp_path)
    write_referral_optin(path, _record(wallet))
    assert os.stat(path).st_mode & 0o777 == 0o644


def test_load_missing_file_returns_empty_list(tmp_path):
    path = referral_optin_path_for(tmp_path)
    assert load_referral_optins(path) == []
