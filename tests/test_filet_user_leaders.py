"""tests/test_filet_user_leaders.py — user-sourced leader registry（spark.filet.user_leaders）。

盯住四件事：
(1) 載入沿用精選白名單的 fail-fast 慣例，並額外強制 `source:"user"` 與 `added_by` 稽核欄位；
(2) 合併優先序：**精選條目一律優先**，同位址的 user 條目被忽略（operator 的撤銷
    不能被自訂路徑蓋掉）；
(3) 寫入原子且冪等（同位址已存在 → 跳過，不動既有檔）；
(4) 引擎述詞 `is_still_permitted` 看得到合併後的 user 條目（合法自訂 leader 不會
    在引擎層被拒）。

純檔案操作，離線。
"""
import json

import pytest

from spark.filet.leaders import LeaderRef, is_still_permitted
from spark.filet.user_leaders import (load_user_leaders, merge_leaders,
                                      record_user_leader, user_leaders_path_for)

_U = "0x" + "a1" * 20      # user-sourced leader
_CURATED = "0x" + "b2" * 20  # 精選 leader
_ACCT = "f" + "c3" * 20      # 加入者的 account_id


def _registry(tmp_path, entries):
    p = tmp_path / "user_leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return p


def _user_entry(address=_U, **over):
    base = {"address": address, "name": address, "description": "",
            "enabled": True, "accepting_new": True,
            "source": "user", "added_by": _ACCT}
    base.update(over)
    return base


# ── 載入 ────────────────────────────────────────────────────────────────

def test_missing_registry_is_an_empty_list(tmp_path):
    """檔案不存在 = 尚無任何用戶自訂 leader，是合法狀態（沿 load_leaders 慣例）。"""
    assert load_user_leaders(tmp_path / "user_leaders.json") == []


def test_loads_a_user_entry_with_flags(tmp_path):
    """條目沿 leaders.json 形狀（address/name/description/enabled/accepting_new）。"""
    p = _registry(tmp_path, [_user_entry(enabled=False)])
    (ref,) = load_user_leaders(p)
    assert ref.address == _U
    assert ref.enabled is False and ref.accepting_new is True


def test_entry_without_added_by_fails_fast(tmp_path):
    """`added_by` 是稽核線索（出事時誰加入的）——缺漏代表檔案被手改壞或寫入邏輯
    出錯，靜默容忍會讓稽核線索無聲蒸發 → fail-fast。"""
    entry = _user_entry()
    del entry["added_by"]
    p = _registry(tmp_path, [entry])
    with pytest.raises(ValueError, match="added_by"):
        load_user_leaders(p)


def test_entry_with_wrong_source_fails_fast(tmp_path):
    """registry 只該裝 source:"user" 的條目——出現別的值代表有人把精選條目手搬進來
    （繞過人工所有權的分界）→ fail-fast。"""
    p = _registry(tmp_path, [_user_entry(source="curated")])
    with pytest.raises(ValueError, match="source"):
        load_user_leaders(p)


# ── ⭐ 合併優先序：精選條目一律優先 ─────────────────────────────────────

def test_curated_entry_beats_user_entry_for_the_same_address(tmp_path):
    """⭐ operator 在精選檔把某位址標成 enabled=false（安全撤銷）之後，user registry
    對同位址的 enabled=true 條目**必須被忽略**——否則自訂路徑就是繞過撤銷的後門。
    引擎述詞在合併清單上必須回 False。"""
    curated = [LeaderRef(_U, "Compromised", "", False, True)]
    user = [LeaderRef(_U, _U, "", True, True)]
    merged = merge_leaders(curated, user)
    assert len(merged) == 1
    assert merged[0].enabled is False
    assert is_still_permitted(_U, merged) is False


def test_user_only_entry_is_permitted_by_the_engine_predicate(tmp_path):
    """⭐ 合法准入的自訂 leader 不會在引擎層被拒：`is_still_permitted` 在合併清單上
    看得到 user 條目（語義不變，仍只看 enabled）。"""
    curated = [LeaderRef(_CURATED, "Alpha", "", True, True)]
    user = [LeaderRef(_U, _U, "", True, True)]
    merged = merge_leaders(curated, user)
    assert is_still_permitted(_U, merged) is True
    assert is_still_permitted(_CURATED, merged) is True


def test_user_entry_killswitch_still_works_after_merge(tmp_path):
    """operator 對 user-sourced 條目保有同等 kill-switch：registry 內 enabled=false
    的條目在合併清單上一樣觸發撤銷語義。"""
    user = [LeaderRef(_U, _U, "", False, True)]
    assert is_still_permitted(_U, merge_leaders([], user)) is False


# ── 路徑推導（API 寫端與引擎讀端共用同一份定義） ────────────────────────

def test_registry_path_is_a_sibling_of_the_curated_allowlist():
    got = user_leaders_path_for("/opt/filet/spark/var/filet/leaders.json")
    assert got == "/opt/filet/spark/var/filet/user_leaders.json"


# ── 寫入：原子、冪等、壞檔不得被覆寫 ────────────────────────────────────

def test_recording_creates_a_loadable_registry_entry(tmp_path):
    """首次寫入：位址正規化小寫、預設 enabled/accepting_new=true、帶 source 與
    added_by 稽核欄位——寫出去的檔必須通過本模組自己的 fail-fast 載入。"""
    p = tmp_path / "user_leaders.json"
    wrote = record_user_leader(p, address="0x" + "A1" * 20, added_by=_ACCT)
    assert wrote is True
    (ref,) = load_user_leaders(p)
    assert ref.address == _U                      # "0xa1a1…"（小寫正規化）
    assert ref.enabled is True and ref.accepting_new is True
    raw = json.loads(p.read_text())["leaders"][0]
    assert raw["source"] == "user" and raw["added_by"] == _ACCT


def test_recording_the_same_address_again_is_a_no_op(tmp_path):
    """⭐ 冪等：同位址（含大小寫變體）第二次寫入 → 跳過，檔案一個位元組都不動。
    客戶重送同一個 POST 不得產生第二筆，也不得覆寫第一筆的 added_by 稽核欄位。"""
    p = tmp_path / "user_leaders.json"
    assert record_user_leader(p, address=_U, added_by=_ACCT) is True
    before = p.read_text()
    assert record_user_leader(p, address="0x" + "A1" * 20,
                              added_by="f" + "99" * 20) is False
    assert p.read_text() == before
    assert len(load_user_leaders(p)) == 1


def test_recording_a_second_address_keeps_the_first(tmp_path):
    p = tmp_path / "user_leaders.json"
    record_user_leader(p, address=_U, added_by=_ACCT)
    record_user_leader(p, address=_CURATED, added_by=_ACCT)
    assert [r.address for r in load_user_leaders(p)] == [_U, _CURATED]


def test_recording_refuses_to_clobber_a_corrupt_registry(tmp_path):
    """⭐ 既有檔壞掉 → raise 且**原檔原封不動**：覆寫等於把先前所有已准入的自訂
    leader 靜默清空，引擎側會把那讀成集體撤銷（真實的收尾成本）。"""
    p = tmp_path / "user_leaders.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError):
        record_user_leader(p, address=_U, added_by=_ACCT)
    assert p.read_text() == "{ not json"


def test_recording_write_failure_raises_loudly(tmp_path):
    """寫入失敗（目錄不存在）→ OSError 原樣上拋，不吞（呼叫端據此回 5xx 且
    不記錄換 leader）。"""
    with pytest.raises(OSError):
        record_user_leader(tmp_path / "nope" / "user_leaders.json",
                           address=_U, added_by=_ACCT)
