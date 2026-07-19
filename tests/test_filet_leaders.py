"""tests/test_filet_leaders.py
策劃 leader 白名單（客戶可選 leader 的唯一合法來源）。純檔案操作，離線。"""
import json

import pytest

from spark.filet.leaders import (
    LeaderRef,
    is_selectable,
    is_still_permitted,
    load_leaders,
)

# 兩個述詞在「不在清單／位址壞掉／大小寫」這些面向語意相同——共用參數化，
# 差異面向（enabled vs accepting_new）另外各自明寫。
_BOTH = [is_selectable, is_still_permitted]

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
    assert leaders[1].accepting_new is True                             # 預設收新客戶
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
    {"address": _A, "name": "Alpha", "accepting_new": "false"},  # accepting_new 非 bool
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


# ── 兩個述詞的共同閘門語意（不在清單／壞位址／大小寫）─────────────────

@pytest.mark.parametrize("pred", _BOTH)
def test_true_for_listed(tmp_path, pred):
    assert pred(_A, load_leaders(_w(tmp_path, _GOOD))) is True


@pytest.mark.parametrize("pred", _BOTH)
def test_false_for_unlisted(tmp_path, pred):
    assert pred("0x" + "cc" * 20, load_leaders(_w(tmp_path, _GOOD))) is False


@pytest.mark.parametrize("pred", _BOTH)
def test_case_insensitive(tmp_path, pred):
    """位址大小寫不敏感；比較前兩側同基準正規化（工程原則 1）。"""
    leaders = load_leaders(_w(tmp_path, _GOOD))
    assert pred(_A.upper().replace("0X", "0x"), leaders) is True


@pytest.mark.parametrize("pred", _BOTH)
def test_false_on_empty_allowlist(pred):
    assert pred(_A, []) is False


@pytest.mark.parametrize("pred", _BOTH)
@pytest.mark.parametrize("bad", ["", "0xshort", "not-an-address", "0x" + "z" * 40])
def test_false_on_malformed_address(tmp_path, bad, pred):
    """閘門語意：不合法位址回 False（不放行），不是 raise。"""
    assert pred(bad, load_leaders(_w(tmp_path, _GOOD))) is False


# ── ⭐ 兩個旗標的分工：撤銷 vs 例行下架 ────────────────────────────────

@pytest.mark.parametrize("pred", _BOTH)
def test_disabled_blocks_both_predicates(tmp_path, pred):
    """⭐ enabled=False ＝ **安全撤銷**：新客戶選不到，**且正在跟的人也不得繼續跟**
    （後者交由引擎收尾）。條目保留只為歷史。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "enabled": False}]}))
    assert leaders[0].enabled is False       # 條目仍在
    assert pred(_A, leaders) is False


def test_not_accepting_new_blocks_selection_only(tmp_path):
    """⭐⭐ accepting_new=False ＝ **例行下架**：新客戶選不到，但**正在跟的
    follower 完全不受影響**（continue 跟單）。

    這條是整個雙旗標設計的重點：把「不再收新客戶」和「這個 leader 出事了」分開，
    才不會為了一個行銷決策去付真實的平倉成本，也才不會讓真正的止血無聲失效。
    """
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "accepting_new": False}]}))
    assert leaders[0].enabled is True and leaders[0].accepting_new is False
    assert is_selectable(_A, leaders) is False        # 新客戶：擋
    assert is_still_permitted(_A, leaders) is True    # 已在跟的：放行


def test_disabled_wins_over_accepting_new(tmp_path):
    """兩旗標同時為 False → 兩個述詞都拒（撤銷比較嚴，不會被 accepting_new 蓋過）。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "enabled": False, "accepting_new": False}]}))
    assert is_selectable(_A, leaders) is False
    assert is_still_permitted(_A, leaders) is False
