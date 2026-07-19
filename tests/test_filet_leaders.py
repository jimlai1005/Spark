"""tests/test_filet_leaders.py
策劃 leader 白名單（客戶可選 leader 的唯一合法來源）。純檔案操作，離線。"""
import json

import pytest

from spark.filet.leaders import LeaderRef, is_allowed_leader, load_leaders

_A = "0x" + "a1" * 20
_B = "0x" + "b2" * 20

_GOOD = {"leaders": [
    {"address": _A, "name": "Alpha", "description": "穩健趨勢", "enabled": True},
    {"address": _B, "name": "Beta"},
]}


def _w(tmp_path, obj):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps(obj))
    return p


def test_load_two(tmp_path):
    leaders = load_leaders(_w(tmp_path, _GOOD))
    assert [x.address for x in leaders] == [_A, _B]
    assert leaders[0].name == "Alpha" and leaders[0].description == "穩健趨勢"
    assert leaders[1].description == "" and leaders[1].enabled is True  # 預設值
    assert isinstance(leaders[0], LeaderRef)


def test_frozen(tmp_path):
    x = load_leaders(_w(tmp_path, _GOOD))[0]
    with pytest.raises(Exception):
        x.address = "0x" + "0" * 40


def test_missing_file_returns_empty(tmp_path):
    """檔案不存在不是錯誤：尚未策劃任何 leader 是合法狀態。"""
    assert load_leaders(tmp_path / "nope.json") == []


def test_address_normalized_to_lowercase(tmp_path):
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": "0x" + "A1" * 20, "name": "Alpha"}]}))
    assert leaders[0].address == _A


@pytest.mark.parametrize("entry", [
    {"address": "0xshort", "name": "Alpha"},                   # 位址太短
    {"address": "0x" + "z" * 40, "name": "Alpha"},             # 非 hex
    {"name": "Alpha"},                                          # 缺 address
    {"address": _A},                                            # 缺 name
    {"address": _A, "name": "  "},                              # name 空白
    {"address": _A, "name": "Alpha", "enabled": "false"},       # enabled 非 bool
    {"address": _A, "name": "Alpha", "description": 123},       # description 非字串
])
def test_bad_entry_raises(tmp_path, entry):
    """fail-fast：白名單壞掉寧可停，也不放行未經審核的 leader。"""
    with pytest.raises(ValueError):
        load_leaders(_w(tmp_path, {"leaders": [entry]}))


def test_duplicate_address_raises(tmp_path):
    """重複條目 = 兩筆可能矛盾的 enabled 狀態，靜默取一等於讓下架失效。"""
    dup = {"leaders": [_GOOD["leaders"][0],
                       {"address": _A.upper().replace("0X", "0x"), "name": "Alpha 2"}]}
    with pytest.raises(ValueError):
        load_leaders(_w(tmp_path, dup))


def test_malformed_json_raises(tmp_path):
    p = tmp_path / "leaders.json"
    p.write_text("{not json")
    with pytest.raises(ValueError):
        load_leaders(p)


@pytest.mark.parametrize("obj", [[], {"leaders": {"a": 1}}, {"leaders": ["x"]}])
def test_bad_shape_raises(tmp_path, obj):
    with pytest.raises(ValueError):
        load_leaders(_w(tmp_path, obj))


def test_empty_leaders_key_ok(tmp_path):
    assert load_leaders(_w(tmp_path, {"leaders": []})) == []


# ── is_allowed_leader：閘門語意 ──────────────────────────────────────────

def test_is_allowed_true_for_listed(tmp_path):
    assert is_allowed_leader(_A, load_leaders(_w(tmp_path, _GOOD))) is True


def test_is_allowed_false_for_unlisted(tmp_path):
    assert is_allowed_leader("0x" + "cc" * 20, load_leaders(_w(tmp_path, _GOOD))) is False


def test_is_allowed_false_when_disabled(tmp_path):
    """⭐ 下架（enabled=False）的 leader 不得再被選——條目保留只為歷史。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "enabled": False}]}))
    assert leaders[0].enabled is False       # 條目仍在
    assert is_allowed_leader(_A, leaders) is False


def test_is_allowed_case_insensitive(tmp_path):
    """位址大小寫不敏感；比較前兩側同基準正規化（工程原則 1）。"""
    leaders = load_leaders(_w(tmp_path, _GOOD))
    assert is_allowed_leader(_A.upper().replace("0X", "0x"), leaders) is True


def test_is_allowed_false_on_empty_allowlist():
    assert is_allowed_leader(_A, []) is False


@pytest.mark.parametrize("bad", ["", "0xshort", "not-an-address", "0x" + "z" * 40])
def test_is_allowed_false_on_malformed_address(tmp_path, bad):
    """閘門語意：不合法位址回 False（不放行），不是 raise。"""
    assert is_allowed_leader(bad, load_leaders(_w(tmp_path, _GOOD))) is False
