"""tests/test_filet_followers.py"""
import json
import pytest
from spark.filet.followers import (
    FollowerRef,
    load_followers,
    load_followers_tolerant,
    validate_account_id,
)

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


# ── M2 Task 10：account_id 路徑安全（validate_account_id） ──────────────

@pytest.mark.parametrize("bad_id", ["a/b", "..", "a:b", "", "x" * 65])
def test_validate_account_id_rejects_unsafe(bad_id):
    with pytest.raises(ValueError):
        validate_account_id(bad_id)


def test_validate_account_id_accepts_safe():
    validate_account_id("alice_1-2")  # 不 raise 即通過


def test_unsafe_account_id_rejected_via_manifest(tmp_path):
    """load_followers（fail-fast）的 _parse_one 呼叫點：不合法 account_id 擋在
    載入邊界，不待流入檔案路徑（EnvFileKeyStore／狀態目錄）才爆。"""
    bad = {"followers": [{"account_id": "../evil", "user_address": "0x"+"a"*40,
        "builder_address": "0x"+"b"*40, "network": "mainnet"}]}
    with pytest.raises(ValueError):
        load_followers(_w(tmp_path, bad))


def test_tolerant_seen_set_not_polluted_by_bad_entry_before_good(tmp_path):
    """T2 reviewer 迴歸：同 account_id「先壞後好再重複」——
    entry0 account_id=x 但位址不合法（壞）→ 不得進 seen；
    entry1 account_id=x 位址合法（好）→ 應成功載入（seen 未被壞條目污染）；
    entry2 account_id=x 重複（好位址再來一次）→ 應被擋（重複）。"""
    mixed = {"followers": [
        {"account_id": "x", "user_address": "0xshort",
         "builder_address": "0x"+"b"*40, "network": "mainnet"},  # 壞：位址不合法
        {"account_id": "x", "user_address": "0x"+"a"*40,
         "builder_address": "0x"+"b"*40, "network": "mainnet"},  # 好
        {"account_id": "x", "user_address": "0x"+"c"*40,
         "builder_address": "0x"+"b"*40, "network": "mainnet"},  # 重複
    ]}
    refs, errors = load_followers_tolerant(_w(tmp_path, mixed))
    assert [r.account_id for r in refs] == ["x"]
    assert len(errors) == 2
    assert "重複" in errors[1]


# ── M2 Task 10：位址小寫正規化（T6 reviewer Important） ─────────────────

def test_addresses_normalized_to_lowercase(tmp_path):
    """以太坊位址大小寫不敏感；load boundary 統一存小寫，避免北極星去重／
    跨 follower 比對因大小寫不同而重複計。"""
    mixed = {"followers": [
        {"account_id": "alice", "user_address": "0x" + "A" * 40,
         "builder_address": "0x" + "B" * 40, "network": "mainnet"},
    ]}
    refs = load_followers(_w(tmp_path, mixed))
    assert refs[0].user_address == "0x" + "a" * 40
    assert refs[0].builder_address == "0x" + "b" * 40
