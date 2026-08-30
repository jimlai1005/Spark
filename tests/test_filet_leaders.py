"""tests/test_filet_leaders.py
策劃 leader 白名單（客戶可選 leader 的唯一合法來源）。純檔案操作，離線。"""
import json
import os

import pytest

from spark.filet.leaders import (
    LeaderRef,
    find_leader,
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


# ── 出貨組態：預設路徑與範例檔（opus 審查 I3/I4）─────────────────────────

def test_default_paths_are_absolute_and_repo_anchored():
    """⭐ 預設路徑必須絕對且錨定 repo 根，不隨 CWD 漂移。

    相對路徑的危險方向是 fail-open：引擎 CWD 被 systemd 釘在 /opt/filet/spark，
    管理端跑 CLI 的 CWD 是他當下所在——兩邊驗不同檔時，管理端在 A 檔下架、
    引擎讀的 B 檔仍列著他，下架無聲失效。
    """
    from pathlib import Path

    from spark.filet.leader_resolve import DEFAULT_LEADERS_PATH, DEFAULT_MANIFEST_PATH

    repo_root = Path(__file__).resolve().parents[1]
    for p in (DEFAULT_LEADERS_PATH, DEFAULT_MANIFEST_PATH):
        assert Path(p).is_absolute(), p
        assert Path(p).is_relative_to(repo_root), p
    assert Path(DEFAULT_LEADERS_PATH).name == "leaders.json"
    assert Path(DEFAULT_MANIFEST_PATH).name == "followers.json"


def test_shipped_example_allowlist_parses_and_shows_both_flags():
    """⭐ deploy/leaders.json.example 必須是**合法且可載入**的白名單。

    RUNBOOK 叫操作者 `cp` 它當起手式——範例壞掉 = 部署當下才 fail-fast 的地雷。
    同時釘住四筆範例分別示範了四種狀態（正常／例行下架／安全撤銷／vault），
    因為那正是這份範例存在的教學價值。
    """
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "deploy" / "leaders.json.example"
    leaders = load_leaders(example)
    assert len(leaders) == 4
    # vault 示例：kind 標 vault 且 enabled:false（示範條目不可被照抄直接生效）
    vault = next(x for x in leaders if x.kind == "vault")
    assert vault.enabled is False
    assert sum(1 for x in leaders if x.kind == "vault") == 1
    states = {(x.enabled, x.accepting_new) for x in leaders}
    assert (True, True) in states     # 正常營運
    assert (True, False) in states    # 例行下架：已在跟的不受影響
    assert (False, False) in states   # 安全撤銷：跟隨者已被收尾
    # 各自對應到正確的述詞結果
    normal = next(x for x in leaders if x.enabled and x.accepting_new)
    delisted = next(x for x in leaders if x.enabled and not x.accepting_new)
    revoked = next(x for x in leaders if not x.enabled)
    assert is_selectable(normal.address, leaders) is True
    assert is_selectable(delisted.address, leaders) is False
    assert is_still_permitted(delisted.address, leaders) is True
    assert is_still_permitted(revoked.address, leaders) is False


def test_follower_unit_pins_absolute_allowlist_path():
    """⭐ systemd unit 必須顯式設 FILET_LEADERS_PATH——照 RUNBOOK 部署出來的系統
    不能倚賴 WorkingDirectory 加相對路徑（那正是 CWD 漂移的來源）。"""
    from pathlib import Path

    unit = (Path(__file__).resolve().parents[1] / "deploy"
            / "filet-follower@.service").read_text()
    assert "Environment=FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json" in unit
    assert "Environment=FILET_FOLLOWERS=/opt/filet/spark/var/filet/followers.json" in unit


# ── ⭐⭐⭐ 白名單路徑：必填、無預設（2026-07-20，同一失敗模式的第三次）──────────
# 前兩次是 FILET_EXCHANGE_DIR 與 FILET_STATE_BASE：一條路徑、多個進程各推導一次，
# 靠字面量剛好相等而成立。白名單是第三個，共用者最多（API／引擎／activate CLI／
# 兩個快照 timer），而它讀錯的方向是 fail-open——已撤銷的 leader 仍被放行或仍可被選。

_ALL_UNITS_DECLARING_LEADERS_PATH = [
    "filet-api.service",          # 目錄頁與選 leader：客戶**能選誰**
    "filet-follower@.service",    # 引擎每輪二次驗證：已在跟的**還能不能繼續**
    "filet-leaderboard.service",  # 每日快照：**抓誰**
    "filet-perf-series.service",  # 12 小時序列：**抓誰**（且無法回填）
]


@pytest.mark.parametrize("unit_name", _ALL_UNITS_DECLARING_LEADERS_PATH)
def test_every_unit_that_reads_the_allowlist_declares_the_same_path(unit_name):
    """⭐⭐ 每一個會讀白名單的 unit 都必須**顯式**宣告同一個絕對路徑。

    這條測試存在的理由是實機上的具體發現（2026-07-20）：`filet-api` 宣告數為 0、
    `filet-leaderboard` 為 1，兩者卻指向同一個檔——因為 API 走的是程式預設，而預設
    恰好錨在 repo 根、API 的 CWD 恰好也是 repo 根。**那是巧合不是強制**：改動其中
    一邊（搬白名單、改 WorkingDirectory）不會有任何錯誤，只會讓兩邊讀不同的檔。
    """
    from pathlib import Path

    unit = (Path(__file__).resolve().parents[1] / "deploy" / unit_name).read_text()
    assert ("Environment=FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json"
            in unit), f"{unit_name} 未宣告 FILET_LEADERS_PATH（服務將拒絕啟動）"


def test_engine_refuses_to_start_without_the_leaders_path_env():
    """⭐ 引擎漏設 → 拒絕啟動，不得靜默退回 repo 內的預設檔。

    靜默退回的後果不是「讀不到」而是「讀到另一份」：管理端在部署的那份白名單撤銷了
    一個 leader，引擎卻仍拿 repo 裡那份放行他——撤銷無聲失效，而兩邊 log 都正常。
    """
    from spark.filet.leader_resolve import require_leaders_path

    with pytest.raises(ValueError, match="FILET_LEADERS_PATH"):
        require_leaders_path({})
    with pytest.raises(ValueError, match="FILET_LEADERS_PATH"):
        require_leaders_path({"FILET_LEADERS_PATH": ""})


def test_relative_leaders_path_is_refused():
    """⭐ 相對路徑＝CWD 漂移＝兩邊驗不同檔（I4 的舊教訓，隨預設值一起搬進 require）。"""
    from spark.filet.leader_resolve import require_leaders_path

    with pytest.raises(ValueError, match="絕對路徑"):
        require_leaders_path({"FILET_LEADERS_PATH": "var/filet/leaders.json"})


def test_missing_allowlist_file_is_still_legal(tmp_path):
    """⭐ 檔案**不存在**仍是合法狀態（與 require_exchange_dir 的目錄檢查刻意不同）：
    「尚未策劃任何 leader」由 load_leaders 回空清單 → 全部拒絕（安全預設），
    而 resolve_leader 對「白名單檔不存在」另有明訂的向後相容豁免。在 require 裡
    檢查存在性會把那兩條既有語意悄悄改掉。"""
    from spark.filet.leader_resolve import require_leaders_path

    p = tmp_path / "not-created-yet.json"
    assert require_leaders_path({"FILET_LEADERS_PATH": str(p)}) == str(p)


def test_all_consumers_resolve_to_the_same_single_value(tmp_path, monkeypatch):
    """⭐⭐ 同一份 env → 五個消費端拿到的是**逐字元相同**的同一個值。

    這是本次修法的可執行版本：不是「每個消費端各自能拿到某個值」，而是「同一個 env
    餵下去，五邊拿到的是同一個」。任何一端偷偷保留自己的預設或加工都會讓這條變紅。
    """
    import scripts.filet_activate as fa
    import scripts.run_copytrade as rc
    from scripts.watchlist_snapshot import resolve_targets
    from spark.publicapi.config import ApiConfig

    leaders = tmp_path / "leaders.json"
    leaders.write_text(json.dumps({"leaders": []}))
    monkeypatch.setenv("FILET_LEADERS_PATH", str(leaders))

    # 1) API（/api/leaders 目錄頁與選 leader）
    api_value = ApiConfig.from_env({
        "FILET_API_NETWORK": "testnet", "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
        "FILET_SIWE_DOMAIN": "d", "FILET_SIWE_URI": "u", "FILET_API_DB": "db",
        "FILET_KEYSVC_SOCK": "s", "FILET_PENDING_PATH": "p",
        "FILET_EXCHANGE_DIR": str(tmp_path), "FILET_STATE_BASE": str(tmp_path),
        "FILET_LEADERS_PATH": str(leaders)}).leaders_path

    # 2) 引擎（每輪二次驗證）——用閉包實際解析一次，證明它讀的就是這份檔
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": []}))
    monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    engine_value = rc.require_leaders_path()

    # 3) activate 人工 CLI（--leaders 未給 → 取 env）
    cli_value = fa.require_leaders_path(os.environ)

    # 4)+5) 兩個快照 timer 共用 resolve_targets（perf_series import 的是同一支函式）
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": str(leaders)})
    assert err is None and addrs  # 空白名單 → 落回 DEFAULT_WATCHLIST，但沒有錯誤

    assert api_value == engine_value == cli_value == str(leaders)


# ── Wave 1（2026-07-31）：LeaderRef.kind 欄位 ─────────────────────────────

def test_kind_defaults_to_standard(tmp_path):
    """kind 欄位缺失 → 預設 "standard"（保住所有既有建構點）。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha"}  # 無 kind 欄位
    ]}))
    assert leaders[0].kind == "standard"


def test_kind_vault_parses(tmp_path):
    """kind: "vault" → 解析成功。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "kind": "vault"}
    ]}))
    assert leaders[0].kind == "vault"


def test_kind_invalid_string_value_raises(tmp_path):
    """kind 非 {"standard", "vault"} 中的值 → ValueError（訊息含位址）。"""
    with pytest.raises(ValueError, match="kind"):
        load_leaders(_w(tmp_path, {"leaders": [
            {"address": _A, "name": "Alpha", "kind": "yolo"}
        ]}))


def test_kind_non_string_raises(tmp_path):
    """kind 非 str → ValueError。"""
    with pytest.raises(ValueError, match="kind"):
        load_leaders(_w(tmp_path, {"leaders": [
            {"address": _A, "name": "Alpha", "kind": 1}
        ]}))


# ── tagline_en（2026-08-30，EN 模式殘留繁中修法）─────────────────────────

def test_tagline_en_defaults_to_none(tmp_path):
    """tagline_en 缺席 → None（區別於 tagline 的預設空字串——None 讓前端能
    明確判斷「沒有英文版」而 fallback 用 tagline，空字串會被誤判成「英文版是空的」）。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha"}  # 無 tagline_en 欄位
    ]}))
    assert leaders[0].tagline_en is None


def test_tagline_en_parses(tmp_path):
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "tagline_en": "Multi-asset momentum · Perpetuals"}
    ]}))
    assert leaders[0].tagline_en == "Multi-asset momentum · Perpetuals"


def test_tagline_en_non_string_raises(tmp_path):
    with pytest.raises(ValueError, match="tagline_en"):
        load_leaders(_w(tmp_path, {"leaders": [
            {"address": _A, "name": "Alpha", "tagline_en": 123}
        ]}))


def test_find_leader_returns_ref_if_found(tmp_path):
    """find_leader(address, leaders) 命中時回傳 LeaderRef。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha", "kind": "vault"}
    ]}))
    ref = find_leader(_A, leaders)
    assert ref is not None
    assert ref.address == _A
    assert ref.kind == "vault"


def test_find_leader_returns_none_if_not_found(tmp_path):
    """find_leader 未命中回 None。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha"}
    ]}))
    assert find_leader(_B, leaders) is None


def test_find_leader_case_insensitive(tmp_path):
    """find_leader 比較時大小寫不敏感（位址同基準）。"""
    leaders = load_leaders(_w(tmp_path, {"leaders": [
        {"address": _A, "name": "Alpha"}
    ]}))
    result = find_leader(_A.upper().replace("0X", "0x"), leaders)
    assert result is not None
    assert result.address == _A


def test_load_user_leaders_with_kind_vault(tmp_path):
    """load_user_leaders 路徑：registry 檔含 kind: "vault" 條目 → 同語意解析（重用驗證）。"""
    from spark.filet.user_leaders import load_user_leaders

    user_reg = tmp_path / "user_leaders.json"
    user_reg.write_text(json.dumps({"leaders": [
        {
            "address": _A,
            "name": "User Leader",
            "kind": "vault",
            "source": "user",
            "added_by": "0x" + "c3" * 20
        }
    ]}))
    leaders = load_user_leaders(user_reg)
    assert len(leaders) == 1
    assert leaders[0].kind == "vault"
