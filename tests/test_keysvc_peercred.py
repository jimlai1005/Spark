"""tests/test_keysvc_peercred.py"""
import struct
from unittest.mock import MagicMock
from spark.keysvc.peercred import make_peercred_authorizer

def test_authorizer_allows_configured_uid():
    authz = make_peercred_authorizer(allowed_uids={1001})
    sock = MagicMock()
    sock.getsockopt.return_value = struct.pack("3i", 42, 1001, 1001)
    assert authz(sock) is True

def test_authorizer_rejects_other_uid():
    authz = make_peercred_authorizer(allowed_uids={1001})
    sock = MagicMock()
    sock.getsockopt.return_value = struct.pack("3i", 42, 9999, 9999)
    assert authz(sock) is False
