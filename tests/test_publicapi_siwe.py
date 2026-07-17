"""tests/test_publicapi_siwe.py
SIWE 用真密碼學（eth_account 本地運算，不觸網）。"""
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.publicapi.config import normalize_address
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer

_NONCE = "abcd1234deadbeef" * 2
_ISSUED = "2026-07-17T00:00:00Z"


def _msg(addr, domain="filet.example"):
    return build_siwe_message(domain=domain, uri="https://filet.example",
                              address=addr, chain_id=42161,
                              nonce=_NONCE, issued_at=_ISSUED)


def test_siwe_roundtrip_recovers_signer():
    acct = Account.create()
    msg = _msg(acct.address)
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    assert normalize_address(recover_siwe_signer(msg, sig)) == acct.address.lower()


def test_siwe_wrong_signer_detected():
    a, b = Account.create(), Account.create()
    msg = _msg(a.address)
    sig = b.sign_message(encode_defunct(text=msg)).signature.hex()
    assert normalize_address(recover_siwe_signer(msg, sig)) != a.address.lower()


def test_siwe_message_eip4361_shape():
    m = _msg("0x" + "ab" * 20)
    assert m.startswith(
        "filet.example wants you to sign in with your Ethereum account:\n")
    for line in ("URI: https://filet.example", "Version: 1", "Chain ID: 42161",
                 f"Nonce: {_NONCE}", f"Issued At: {_ISSUED}"):
        assert line in m


def test_siwe_message_binds_domain():
    """不同 domain → 不同訊息 → 他站簽名在本站必然驗不過（防跨站釣魚重放）。"""
    assert _msg("0x" + "ab" * 20) != _msg("0x" + "ab" * 20, domain="evil.example")


def test_siwe_message_uses_checksum_address():
    acct = Account.create()
    assert f"\n{acct.address}\n" in _msg(acct.address.lower())
