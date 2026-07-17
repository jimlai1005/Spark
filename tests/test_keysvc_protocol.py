"""tests/test_keysvc_protocol.py"""
import pytest
from spark.keysvc.protocol import (
    encode_request, decode_request, encode_response, decode_response,
    GenerateRequest, Response, AddressRequest)


def test_request_roundtrip():
    line = encode_request(GenerateRequest(account_id="alice"))
    assert line.endswith(b"\n")
    req = decode_request(line)
    assert req.account_id == "alice"


def test_response_ok_roundtrip():
    line = encode_response(Response(ok=True, agent_address="0x" + "a" * 40))
    resp = decode_response(line)
    assert resp.ok and resp.agent_address == "0x" + "a" * 40 and resp.error is None


def test_response_err_roundtrip():
    resp = decode_response(encode_response(Response(ok=False, error="boom")))
    assert resp.ok is False and resp.error == "boom" and resp.agent_address is None


def test_decode_bad_op_rejected():
    import json
    with pytest.raises(ValueError):
        decode_request((json.dumps({"op": "read", "account_id": "x"}) + "\n").encode())


def test_decode_missing_account_rejected():
    import json
    with pytest.raises(ValueError):
        decode_request((json.dumps({"op": "generate"}) + "\n").encode())


def test_decode_non_object_rejected():
    with pytest.raises(ValueError):
        decode_request(b"[1, 2, 3]\n")


def test_address_request_roundtrip():
    line = encode_request(AddressRequest(account_id="alice"))
    assert line.endswith(b"\n")
    req = decode_request(line)
    assert isinstance(req, AddressRequest) and req.account_id == "alice"


def test_generate_request_type_preserved():
    req = decode_request(encode_request(GenerateRequest(account_id="alice")))
    assert isinstance(req, GenerateRequest)


def test_response_code_roundtrip():
    resp = decode_response(encode_response(Response(ok=False, error="x", code="exists")))
    assert resp.ok is False and resp.error == "x" and resp.code == "exists"


def test_response_code_absent_is_none():
    resp = decode_response(encode_response(Response(ok=True, agent_address="0x" + "a" * 40)))
    assert resp.code is None
