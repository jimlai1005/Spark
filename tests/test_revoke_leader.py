"""tests/test_revoke_leader.py — operator 撤銷 leader 的 CLI（scripts/revoke_leader.py）。

存在的理由（2026-07-27 Finding 2 的結構性修法，工程原則的 meta-rule：與其再加一條
checklist，不如給一個不會用錯的工具）：合併語義是「精選優先、缺則由 registry 遞補」，
所以**刪掉精選條目不是撤銷**——registry 的同位址 enabled:true 條目會遞補上來。恆定
有效的撤銷只有一種：確保精選檔裡存在一筆 `enabled:false` 的條目。

盯住四件事：
(1) 四種起始狀態（只在精選／只在 registry／兩檔都在／兩檔都不在）跑完都讓
    `is_still_permitted` 為 False；
(2) 冪等：重跑不改變檔案內容，結論仍為 False；
(3) 自我驗收是**真的**驗（載入兩檔重新合併），驗不過就拋，不會報 OK；
(4) 不越權破壞：其他條目與稽核欄位原封不動、壞檔不覆寫、registry 讀寫取得 flock。

純檔案操作，離線。
"""
import fcntl
import json
import time

import pytest

from scripts.revoke_leader import revoke
from spark.filet.leaders import is_still_permitted, load_leaders
from spark.filet.user_leaders import (load_user_leaders, merge_leaders,
                                      record_user_leader,
                                      registry_init_marker_path,
                                      registry_lock_path)

_TARGET = "0x" + "a1" * 20     # 要撤銷的位址
_OTHER = "0x" + "b2" * 20      # 無關的旁觀者（不得被動到）
_ACCT = "f" + "c3" * 20


def _paths(tmp_path):
    return tmp_path / "leaders.json", tmp_path / "exchange" / "user_leaders.json"


def _write_curated(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"leaders": entries}))


def _permitted(curated_path, registry_path, address=_TARGET) -> bool:
    """引擎眼中的最終答案：兩檔各自載入 → 合併 → 引擎述詞。"""
    return is_still_permitted(
        address, merge_leaders(load_leaders(curated_path),
                               load_user_leaders(registry_path)))


# ── (1) 四種起始狀態，撤銷後一律 False ──────────────────────────────────

def test_revokes_an_address_listed_only_in_the_curated_file(tmp_path):
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"},
                             {"address": _OTHER, "name": "Bravo"}])
    assert _permitted(curated, registry) is True
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert _permitted(curated, registry) is False
    # 旁觀者不受影響（撤銷是點狀操作，不是全清單停用）。
    assert is_still_permitted(_OTHER, load_leaders(curated)) is True


def test_revokes_an_address_listed_only_in_the_registry(tmp_path):
    """⭐ 承重點：user-sourced 條目也必須撤得掉。精選檔補一筆 enabled:false 就贏過
    registry（合併優先權），registry 那筆同時被停用（縱深防禦）。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _OTHER, "name": "Bravo"}])
    record_user_leader(registry, address=_TARGET, added_by=_ACCT)
    assert _permitted(curated, registry) is True
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert _permitted(curated, registry) is False
    assert [r.enabled for r in load_user_leaders(registry)] == [False]


def test_revokes_an_address_present_in_both_files(tmp_path):
    """⭐⭐ Finding 2 的正題：兩檔都有時，**刪精選條目**會讓 registry 那筆遞補上來
    （撤銷失效）。本工具改寫成 enabled:false（而非刪除），所以刪不掉的縫也就不存在。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    record_user_leader(registry, address=_TARGET, added_by=_ACCT)
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert _permitted(curated, registry) is False
    # 兩檔都停用 → 就算有人事後刪掉精選那筆，registry 遞補上來的也是 enabled:false。
    curated_entry = next(r for r in load_leaders(curated) if r.address == _TARGET)
    assert curated_entry.enabled is False
    assert [r.enabled for r in load_user_leaders(registry)] == [False]
    _write_curated(curated, [])                      # 模擬事後誤刪精選條目
    assert _permitted(curated, registry) is False


def test_revokes_an_address_absent_from_both_files(tmp_path):
    """兩檔都沒有 → 仍落一筆 enabled:false 的精選條目（預防性封鎖：日後有人把該位址
    加回來、或客戶經自訂路徑輸入它，都會被這一筆擋下）。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _OTHER, "name": "Bravo"}])
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert _permitted(curated, registry) is False
    entry = next(r for r in load_leaders(curated) if r.address == _TARGET)
    assert entry.enabled is False and entry.accepting_new is False
    assert entry.name.strip()                        # name 非空（load_leaders 硬性要求）


def test_works_when_the_curated_file_does_not_exist_yet(tmp_path):
    curated, registry = _paths(tmp_path)
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert _permitted(curated, registry) is False


# ── (2) 冪等 ────────────────────────────────────────────────────────────

def test_running_twice_is_idempotent(tmp_path):
    """重跑不得產生第二筆條目、不得改動檔案內容（operator 重跑是常態：不確定上次
    有沒有跑成功時，正確的反應應該是再跑一次，而不是先去讀檔）。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    record_user_leader(registry, address=_TARGET, added_by=_ACCT)
    first = revoke(_TARGET, leaders_path=curated, registry_path=registry)
    curated_after, registry_after = curated.read_text(), registry.read_text()
    second = revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert curated.read_text() == curated_after      # 一個位元組都不動
    assert registry.read_text() == registry_after
    assert first.changed is True and second.changed is False
    assert len(load_leaders(curated)) == 1
    assert _permitted(curated, registry) is False


def test_address_case_variants_hit_the_same_entry(tmp_path):
    """大小寫變體不得產生第二筆（位址比較一律正規化小寫，工程原則 1）。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    revoke("0x" + "A1" * 20, leaders_path=curated, registry_path=registry)
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert len(load_leaders(curated)) == 1
    assert _permitted(curated, registry) is False


# ── (3) 自我驗收是真的驗 ────────────────────────────────────────────────

def test_self_check_failure_raises_instead_of_reporting_ok(tmp_path):
    """⭐ 自我驗收若只是印一行「OK」就毫無價值。把驗收用的合併結果動手腳（monkeypatch
    成永遠 True）→ 必須拋，不得回報成功：這支工具唯一的賣點就是「它自己確認過了」。"""
    import scripts.revoke_leader as mod
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "is_still_permitted", lambda *_a, **_k: True)
        with pytest.raises(SystemExit):
            revoke(_TARGET, leaders_path=curated, registry_path=registry)


def test_unreadable_registry_fails_loudly(tmp_path):
    """⭐ registry 壞掉 → 引擎每輪的合併載入也會壞（LeaderResolutionError＝transient
    ＝**沿用上一個 leader**），撤銷等於沒生效。所以這裡必須大聲失敗，讓 operator 知道
    「精選檔改好了但引擎仍在跟」，不能報 OK 就走人。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{ not json")
    with pytest.raises(SystemExit):
        revoke(_TARGET, leaders_path=curated, registry_path=registry)


def test_missing_registry_with_init_marker_fails_loudly(tmp_path):
    """registry 遺失＋init-marker 在＝已初始化後被刪（load_user_leaders 既有語意：
    raise）。同上：引擎也載入不了，撤銷不生效 → 大聲失敗。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry_init_marker_path(registry).touch()
    with pytest.raises(SystemExit):
        revoke(_TARGET, leaders_path=curated, registry_path=registry)


def test_corrupt_curated_file_is_not_clobbered(tmp_path):
    """精選檔壞掉 → 拒絕覆寫（沿 record_user_leader 的同一政策：覆寫等於把整份白名單
    靜默清空，那是全體 follower 的集體撤銷）。"""
    curated, registry = _paths(tmp_path)
    curated.write_text("{ not json")
    with pytest.raises(SystemExit):
        revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert curated.read_text() == "{ not json"


# ── (4) 不越權破壞 ──────────────────────────────────────────────────────

def test_registry_audit_fields_survive_the_revocation(tmp_path):
    """只翻 enabled，其餘欄位（source／added_by 稽核線索）原封不動——改壞了 registry
    連載入都過不了（fail-fast），撤銷反而變成把引擎打成 transient 失敗。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [])
    record_user_leader(registry, address=_TARGET, added_by=_ACCT)
    record_user_leader(registry, address=_OTHER, added_by=_ACCT)
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    raw = {e["address"]: e for e in json.loads(registry.read_text())["leaders"]}
    assert raw[_TARGET]["added_by"] == _ACCT and raw[_TARGET]["source"] == "user"
    assert raw[_TARGET]["enabled"] is False
    assert raw[_OTHER]["enabled"] is True            # 旁觀者不受影響
    assert len(load_user_leaders(registry)) == 2


def test_existing_curated_fields_are_preserved(tmp_path):
    """既有精選條目只被翻 enabled：name／description 是人工維護的內容，工具不得改寫。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha",
                              "description": "旗艦策略", "accepting_new": False}])
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    entry = json.loads(curated.read_text())["leaders"][0]
    assert entry["name"] == "Alpha" and entry["description"] == "旗艦策略"
    assert entry["enabled"] is False


def test_writes_are_atomic_and_leave_no_tmp(tmp_path):
    """原子換檔（tmp + os.replace）：兩個檔的目錄都不得留下 tmp 殘骸——引擎每輪在讀
    這兩個檔，半份 JSON 會被讀成「白名單壞掉」。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    record_user_leader(registry, address=_TARGET, added_by=_ACCT)
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert list(curated.parent.glob("*.tmp")) == []
    assert list(registry.parent.glob("*.tmp")) == []


def test_registry_edit_takes_the_same_flock_as_the_api_writer(tmp_path):
    """⭐ registry 的 read-modify-write 必須取 **API 寫端同一把鎖**（registry_lock_path）：
    不取的話，operator 的停用位會被下一次 API 寫入拿舊快照無聲蓋掉（撤銷失效）。
    測法：外部持鎖 → revoke 必須失敗（有界等待逾時），而不是無視鎖直接改檔。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    record_user_leader(registry, address=_TARGET, added_by=_ACCT)
    before = registry.read_text()
    with open(registry_lock_path(registry), "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        t0 = time.monotonic()
        with pytest.raises(SystemExit):
            revoke(_TARGET, leaders_path=curated, registry_path=registry)
        assert time.monotonic() - t0 < 10.0          # 有界等待，不是無限期掛死
    assert registry.read_text() == before            # registry 未被繞過鎖改寫


def test_never_touches_a_registry_that_was_never_created(tmp_path):
    """registry 從未建立（無檔、無標記、無 lock）→ 完全不碰交換目錄：不建目錄、不建
    lock 檔。理由是權限——交換目錄是 `filet-api:filet-engine`，operator（root）在那裡
    建出的檔會變成 root 所有，之後 API 寫端可能就開不了它了。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    revoke(_TARGET, leaders_path=curated, registry_path=registry)
    assert not registry.parent.exists()
    assert _permitted(curated, registry) is False


def test_rejects_a_malformed_address(tmp_path):
    """位址不合法 → 拒絕執行（不落一筆載入不了的垃圾條目讓整份白名單 fail-fast）。"""
    curated, registry = _paths(tmp_path)
    _write_curated(curated, [{"address": _TARGET, "name": "Alpha"}])
    with pytest.raises(SystemExit):
        revoke("0xnope", leaders_path=curated, registry_path=registry)
    assert load_leaders(curated)[0].enabled is True
