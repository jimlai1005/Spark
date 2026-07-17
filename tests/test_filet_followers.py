"""tests/test_filet_followers.py"""
import json
import pytest
from spark.filet.followers import FollowerRef, load_followers, load_followers_tolerant

_GOOD = {"followers": [
    {"account_id": "alice", "user_address": "0x"+"a"*40,
     "builder_address": "0x"+"b"*40, "network": "mainnet", "label": "Alice"},
    {"account_id": "bob", "user_address": "0x"+"c"*40,
     "builder_address": "0x"+"b"*40, "network": "testnet"},
]}


def _w(tmp_path, obj):
    p = tmp_path/"followers.json"
    p.write_text(json.dumps(obj))
    return p


def test_load_two(tmp_path):
    refs = load_followers(_w(tmp_path, _GOOD))
    assert [r.account_id for r in refs] == ["alice", "bob"]
    assert refs[0].label == "Alice" and refs[1].label == ""
    assert isinstance(refs[0], FollowerRef)


def test_frozen(tmp_path):
    r = load_followers(_w(tmp_path, _GOOD))[0]
    with pytest.raises(Exception):
        r.account_id = "x"


def test_duplicate_rejected(tmp_path):
    dup = {"followers": _GOOD["followers"] + [_GOOD["followers"][0]]}
    with pytest.raises(ValueError):
        load_followers(_w(tmp_path, dup))


def test_bad_address_rejected(tmp_path):
    bad = {"followers": [{"account_id": "x", "user_address": "0xshort",
        "builder_address": "0x"+"b"*40, "network": "mainnet"}]}
    with pytest.raises(ValueError):
        load_followers(_w(tmp_path, bad))


def test_non_hex_address_rejected(tmp_path):  # opus O11：驗 hex 字元集
    bad = {"followers": [{"account_id": "x", "user_address": "0x"+"z"*40,
        "builder_address": "0x"+"b"*40, "network": "mainnet"}]}
    with pytest.raises(ValueError):
        load_followers(_w(tmp_path, bad))


def test_bad_network_rejected(tmp_path):
    bad = {"followers": [{"account_id": "x", "user_address": "0x"+"a"*40,
        "builder_address": "0x"+"b"*40, "network": "devnet"}]}
    with pytest.raises(ValueError):
        load_followers(_w(tmp_path, bad))


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_followers(tmp_path/"nope.json")


def test_tolerant_skips_bad_entry_keeps_good(tmp_path):
    mixed = {"followers": [
        _GOOD["followers"][0],
        {"account_id": "bad", "user_address": "0xshort",
         "builder_address": "0x"+"b"*40, "network": "mainnet"},
    ]}
    refs, errors = load_followers_tolerant(_w(tmp_path, mixed))
    assert [r.account_id for r in refs] == ["alice"]
    assert len(errors) == 1
