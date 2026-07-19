"""tests/test_leader_resolve.py
引擎側 leader 解析 ＋ **白名單二次驗證**（spark.filet.leader_resolve）。

⭐ 這裡的測試守的是資安防線：manifest 的 leader_address 可能經由被打穿的 filet-api
流入，引擎必須自己拿白名單再驗一次才放行。純檔案操作，離線。
"""
import json
import logging

import pytest

from spark.copytrade.notifier import RecordingNotifier
from spark.filet.leader_resolve import (
    SOURCE_ENV_DEFAULT,
    SOURCE_MANIFEST,
    LeaderResolution,
    LeaderResolutionError,
    LeaderRevokedError,
    LeaderWatch,
    resolve_leader,
)

_ACCT = "alice"
_ME = "0x" + "11" * 20
_BUILDER = "0x" + "22" * 20
_LEADER = "0x" + "d4" * 20
_OTHER = "0x" + "e5" * 20
_ENV_DEFAULT = "0x" + "f6" * 20


def _manifest(tmp_path, *, leader=None, account_id=_ACCT, me=_ME):
    p = tmp_path / "followers.json"
    entry = {"account_id": account_id, "user_address": me,
             "builder_address": _BUILDER, "network": "mainnet", "label": ""}
    if leader is not None:
        entry["leader_address"] = leader
    p.write_text(json.dumps({"followers": [entry]}))
    return p


def _leaders(tmp_path, entries):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return p


def _resolve(tmp_path, *, manifest=None, leaders=None, env_default=_ENV_DEFAULT,
             account_id=_ACCT, self_address=_ME):
    return resolve_leader(
        account_id=account_id,
        manifest_path=manifest if manifest is not None else tmp_path / "nope.json",
        leaders_path=leaders if leaders is not None else tmp_path / "no-leaders.json",
        env_default=env_default, self_address=self_address)


# ── 快樂路徑：manifest 指定 / env 回退 ──────────────────────────────────

def test_uses_manifest_leader_when_allowlisted(tmp_path):
    """manifest 有 leader 且在白名單 → 用 manifest 的，來源標記 manifest。"""
    res = _resolve(tmp_path,
                   manifest=_manifest(tmp_path, leader=_LEADER),
                   leaders=_leaders(tmp_path, [{"address": _LEADER, "name": "Alpha"}]))
    assert res == LeaderResolution(_LEADER, SOURCE_MANIFEST)


def test_falls_back_to_env_when_manifest_leader_absent(tmp_path):
    """manifest 沒指定 leader → 回退 env COPY_LEADER_ADDRESS（向後相容）。"""
    res = _resolve(tmp_path,
                   manifest=_manifest(tmp_path, leader=None),
                   leaders=_leaders(tmp_path, [{"address": _ENV_DEFAULT, "name": "Env"}]))
    assert res == LeaderResolution(_ENV_DEFAULT, SOURCE_ENV_DEFAULT)


def test_manifest_leader_case_insensitive(tmp_path):
    """位址大小寫不敏感，結果一律正規化小寫（同基準比較，工程原則 1）。"""
    upper = _LEADER.upper().replace("0X", "0x")
    res = _resolve(tmp_path, manifest=_manifest(tmp_path, leader=upper),
                   leaders=_leaders(tmp_path, [{"address": upper, "name": "Alpha"}]))
    assert res.address == _LEADER


# ── ⭐ 資安核心：白名單二次驗證 ─────────────────────────────────────────

def test_rejects_manifest_leader_not_in_allowlist(tmp_path):
    """⭐ manifest 的 leader 不在白名單 → 拒絕（啟動時即拒絕啟動）。

    威脅模型：filet-api 被打穿 → 攻擊者把 follower 指向惡意 leader（對敲榨 builder
    fee／反向交易）。白名單只有管理端能寫，這道驗證獨立於 API，故不得省略。
    """
    with pytest.raises(LeaderResolutionError, match="不在白名單"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_OTHER),
                 leaders=_leaders(tmp_path, [{"address": _LEADER, "name": "Alpha"}]))


def test_rejects_disabled_leader(tmp_path):
    """⭐ 撤銷（enabled=False）的 leader 一律拒絕——條目保留只為歷史。"""
    with pytest.raises(LeaderResolutionError, match="不在白名單|已被撤銷"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER),
                 leaders=_leaders(tmp_path, [
                     {"address": _LEADER, "name": "Alpha", "enabled": False}]))


# ── ⭐⭐ 失敗分類：撤銷（semantic）vs 暫時失敗（transient）────────────────
# 這兩類的正確反應完全相反（收尾 vs 沿用），所以型別必須分得開——
# 「沿用上一個 leader」套在撤銷上，等於管理端下架了但跟單永遠不停。

@pytest.mark.parametrize("entry", [
    {"address": _LEADER, "name": "Alpha", "enabled": False},   # 明確撤銷
    {"address": _OTHER, "name": "Other"},                       # 整筆被移除
])
def test_allowlist_rejection_is_revocation(tmp_path, entry):
    """⭐ 白名單載入成功卻拒絕這個 leader → LeaderRevokedError（semantic）。"""
    with pytest.raises(LeaderRevokedError):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER),
                 leaders=_leaders(tmp_path, [entry]))


def test_env_default_rejection_is_revocation(tmp_path):
    """env 回退路徑的白名單拒絕同樣是撤銷（預設值不因為是預設就換一套語意）。"""
    with pytest.raises(LeaderRevokedError):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None),
                 leaders=_leaders(tmp_path, [{"address": _LEADER, "name": "Alpha"}]))


def test_missing_allowlist_is_not_revocation(tmp_path):
    """⭐ 白名單檔不存在 → transient，**不得**升級成撤銷。

    誤判的代價：一次 IO 打嗝／掛載失敗會讓所有 follower 平倉並鎖死 kill switch。
    """
    with pytest.raises(LeaderResolutionError) as e:
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER))
    assert not isinstance(e.value, LeaderRevokedError)


def test_malformed_allowlist_is_not_revocation(tmp_path):
    """白名單格式壞掉 → transient（fail-fast 拒絕，但不是「這個 leader 被撤銷」）。"""
    p = tmp_path / "leaders.json"
    p.write_text("{not json")
    with pytest.raises(LeaderResolutionError) as e:
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER), leaders=p)
    assert not isinstance(e.value, LeaderRevokedError)


def test_accepting_new_false_does_not_affect_running_follower(tmp_path):
    """⭐⭐ 例行下架（accepting_new=False、enabled 仍 True）→ **正在跟的 follower
    完全不受影響**，解析照常成功。

    這條與上面的撤銷測試成對：兩種「下架」走兩條完全不同的路，白名單編輯者選錯
    旗標的後果（該止血沒止／不該平倉卻平）在這裡被釘住。
    """
    res = _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER),
                   leaders=_leaders(tmp_path, [
                       {"address": _LEADER, "name": "Alpha", "accepting_new": False}]))
    assert res == LeaderResolution(_LEADER, SOURCE_MANIFEST)


def test_rejects_env_default_not_in_allowlist(tmp_path):
    """env 預設也要過白名單——不因為是預設值就豁免（一致性）。"""
    with pytest.raises(LeaderResolutionError, match="env 預設 leader"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None),
                 leaders=_leaders(tmp_path, [{"address": _LEADER, "name": "Alpha"}]))


def test_rejects_env_default_when_allowlist_present_but_empty(tmp_path):
    """白名單檔存在但為空 = 管理端明確表態「目前沒有可選 leader」→ 拒絕。
    與「檔案不存在」（既有部署尚未策劃，享有回退豁免）是兩種語意。"""
    with pytest.raises(LeaderResolutionError):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None),
                 leaders=_leaders(tmp_path, []))


# ── 白名單檔不存在：env 回退放行、manifest 明確指定則拒絕 ────────────────

def test_missing_allowlist_allows_env_fallback_with_warning(tmp_path, caplog):
    """白名單檔不存在 → env 回退路徑放行＋warning（否則既有部署升級後全數停擺）。"""
    with caplog.at_level(logging.WARNING):
        res = _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None))
    assert res == LeaderResolution(_ENV_DEFAULT, SOURCE_ENV_DEFAULT)
    assert any("白名單檔不存在" in r.getMessage() for r in caplog.records)


def test_missing_allowlist_still_rejects_explicit_manifest_leader(tmp_path):
    """⭐ manifest 明確指定 leader 卻無白名單可驗 → 拒絕，不享有回退豁免
    （明確指定卻驗不了，正是被竄改的樣子）。"""
    with pytest.raises(LeaderResolutionError):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER))


# ── leader == follower 自己 ─────────────────────────────────────────────

def test_rejects_leader_equal_to_self_from_env(tmp_path):
    """自己跟自己無意義，且形成回饋迴圈（本方下的單下一輪被當 leader 目標放大）。"""
    with pytest.raises(LeaderResolutionError, match="自己的位址"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None),
                 env_default=_ME,
                 leaders=_leaders(tmp_path, [{"address": _ME, "name": "Self"}]))


def test_rejects_leader_equal_to_self_from_manifest(tmp_path):
    with pytest.raises(LeaderResolutionError, match="自己的位址"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_ME),
                 leaders=_leaders(tmp_path, [{"address": _ME, "name": "Self"}]))


def test_rejects_leader_equal_to_manifest_user_address(tmp_path):
    """env 的 SPARK_USER_ADDR 與 manifest 漂移時，對任一方比中都要拒絕。"""
    with pytest.raises(LeaderResolutionError, match="自己的位址"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_ME, me=_ME),
                 self_address=_OTHER,
                 leaders=_leaders(tmp_path, [{"address": _ME, "name": "Self"}]))


# ── manifest 缺席／查無自己／壞掉 ───────────────────────────────────────

def test_missing_manifest_falls_back_to_env(tmp_path):
    """manifest 檔不存在（M1 單實例／本機開發）→ env 回退，不是錯誤。"""
    res = _resolve(tmp_path,
                   leaders=_leaders(tmp_path, [{"address": _ENV_DEFAULT, "name": "Env"}]))
    assert res == LeaderResolution(_ENV_DEFAULT, SOURCE_ENV_DEFAULT)


def test_no_account_id_falls_back_to_env(tmp_path):
    """dry/shadow 無 SPARK_ACCOUNT_ID → 沒有身分可查，走 env 回退。"""
    res = _resolve(tmp_path, account_id=None, manifest=_manifest(tmp_path, leader=_LEADER),
                   leaders=_leaders(tmp_path, [{"address": _ENV_DEFAULT, "name": "Env"}]))
    assert res == LeaderResolution(_ENV_DEFAULT, SOURCE_ENV_DEFAULT)


def test_manifest_without_my_account_is_rejected(tmp_path):
    """manifest 存在卻查無自己 → 拒絕，不得靜默降級成 env 回退。"""
    with pytest.raises(LeaderResolutionError, match="找不到 account_id"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None, account_id="bob"),
                 leaders=_leaders(tmp_path, [{"address": _ENV_DEFAULT, "name": "Env"}]))


def test_malformed_manifest_is_rejected(tmp_path):
    p = tmp_path / "followers.json"
    p.write_text("{not json")
    with pytest.raises(LeaderResolutionError, match="manifest 無法載入"):
        _resolve(tmp_path, manifest=p,
                 leaders=_leaders(tmp_path, [{"address": _ENV_DEFAULT, "name": "Env"}]))


def test_malformed_allowlist_is_rejected(tmp_path):
    """白名單壞掉 → fail-fast，絕不當成空清單或放行（leaders.py 的 fail-fast 語意）。"""
    p = tmp_path / "leaders.json"
    p.write_text("{not json")
    with pytest.raises(LeaderResolutionError, match="白名單無法載入"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=_LEADER), leaders=p)


@pytest.mark.parametrize("bad", ["", "0xshort", "not-an-address", "0x" + "z" * 40])
def test_malformed_env_default_is_rejected(tmp_path, bad):
    with pytest.raises(LeaderResolutionError, match="env 預設 leader 不合法"):
        _resolve(tmp_path, manifest=_manifest(tmp_path, leader=None), env_default=bad)


def test_malformed_self_address_is_rejected(tmp_path):
    """SPARK_USER_ADDR 壞掉時自我比對會靜默失效——寧可炸也不讓閘門無聲關掉。"""
    with pytest.raises(ValueError):
        _resolve(tmp_path, self_address="0xnope",
                 manifest=_manifest(tmp_path, leader=_LEADER),
                 leaders=_leaders(tmp_path, [{"address": _LEADER, "name": "Alpha"}]))


# ── LeaderWatch：執行中的失敗處理與變更告警 ─────────────────────────────

_A = LeaderResolution(_LEADER, SOURCE_MANIFEST)
_B = LeaderResolution(_OTHER, SOURCE_MANIFEST)


def _crits(n: RecordingNotifier):
    return [r for r in n.records if r[0] == "critical"]


def _raise(exc):
    """回一個「一呼叫就拋 exc」的 resolve 閉包。"""
    def _f():
        raise exc
    return _f


def test_watch_keeps_last_leader_on_transient_failure():
    """⭐ 執行中**transient** 解析失敗（白名單檔暫時讀不到）→ 沿用上一個已驗證
    leader ＋ critical 告警，跟單不中斷、**不得誤觸收尾**。

    ⚠️ 本測試只涵蓋 transient。撤銷（LeaderRevokedError）走完全相反的路，
    見 test_watch_winds_down_on_revocation——兩者不可混為一談。
    """
    n = RecordingNotifier()
    wound_down = []

    def boom():
        raise LeaderResolutionError("白名單檔暫時讀不到")

    w = LeaderWatch(_A, boom, n, on_revoked=lambda: wound_down.append(1))
    assert w.refresh() == _A          # 沿用，不中斷、不靜默切換
    assert w.current == _A
    assert wound_down == []           # ⭐ transient 絕不觸發收尾
    crits = _crits(n)
    assert len(crits) == 1            # 告警真的被呼叫（不只是沒 crash）
    assert "沿用上一個已驗證的 leader" in crits[0][2] and _LEADER in crits[0][2]


def test_watch_survives_unexpected_exception():
    """未預期的例外（IO/權限）同樣不得炸掉 cycle——大聲＋沿用，且不觸發收尾。"""
    n = RecordingNotifier()
    wound_down = []

    def boom():
        raise OSError("permission denied")

    w = LeaderWatch(_A, boom, n, on_revoked=lambda: wound_down.append(1))
    assert w.refresh() == _A
    assert len(_crits(n)) == 1
    assert wound_down == []


# ── ⭐⭐ 撤銷：受控收尾（停止開新倉 → flatten → trip kill switch）────────

def test_watch_winds_down_on_revocation():
    """⭐ 執行中偵測到撤銷 → refresh() 回 None（本輪起不得交易）＋呼叫收尾機具
    ＋critical 明說已被撤銷、正在收尾。

    這是本次修復的核心：撤銷若走 transient 的「沿用舊 leader」路徑，管理端把出事的
    leader 標成 enabled:false 之後，跟他的 follower 會永遠繼續跟下去，而操作者以為
    已經止血了——這正是最危險的地方。
    """
    n = RecordingNotifier()
    wound_down = []
    w = LeaderWatch(_A, _raise(LeaderRevokedError("leader 已被撤銷")), n,
                    on_revoked=lambda: wound_down.append(1))

    assert w.refresh() is None        # ⭐ 停止開新倉
    assert wound_down == [1]          # ⭐ 收尾（flatten + trip）確實被呼叫
    crits = _crits(n)
    assert len(crits) == 1
    assert "撤銷" in crits[0][2] and "收尾" in crits[0][2] and _LEADER in crits[0][2]


def test_watch_wind_down_failure_is_loud_but_still_stops_trading():
    """收尾機具本身炸掉（例如 ARM 檔寫不進去）→ 仍然回 None（不交易）＋第二則
    critical 說明需人工確認。絕不因為收尾失敗就恢復跟單。"""
    n = RecordingNotifier()

    def bad_wind_down():
        raise OSError("ARM 檔寫入失敗")

    w = LeaderWatch(_A, _raise(LeaderRevokedError("撤銷")), n,
                    on_revoked=bad_wind_down)
    assert w.refresh() is None
    crits = _crits(n)
    assert len(crits) == 2
    assert "收尾失敗" in crits[1][2] and "人工" in crits[1][2]


def test_watch_keeps_retrying_wind_down_while_revoked():
    """撤銷是持續狀態：每一輪都重新判定為撤銷、重新嘗試收尾
    （第一次收尾失敗不會因為喊過一次就算了）。"""
    n = RecordingNotifier()
    calls = []
    w = LeaderWatch(_A, _raise(LeaderRevokedError("撤銷")), n,
                    on_revoked=lambda: calls.append(1))
    assert w.refresh() is None and w.refresh() is None
    assert calls == [1, 1]
    assert w.current == _A   # 撤銷不是「換 leader」，current 不動


def test_watch_alerts_critical_on_leader_change():
    """⭐ leader 變更 → critical，訊息須含舊 leader、新 leader、來源
    （換 leader ＝ 平舊開新，有實際 taker 成本，屬重大事件必須留痕）。"""
    n = RecordingNotifier()
    w = LeaderWatch(_A, lambda: _B, n)
    assert w.refresh() == _B
    assert w.current == _B
    crits = _crits(n)
    assert len(crits) == 1
    text = crits[0][2]
    assert _LEADER in text and _OTHER in text and SOURCE_MANIFEST in text


def test_watch_silent_when_leader_unchanged():
    n = RecordingNotifier()
    w = LeaderWatch(_A, lambda: _A, n)
    assert w.refresh() == _A
    assert n.records == []


def test_watch_source_only_change_is_not_critical():
    """位址不變、只有來源變 → 交易行為不變、無收斂成本，不吵（只 log）。"""
    n = RecordingNotifier()
    w = LeaderWatch(_A, lambda: LeaderResolution(_LEADER, SOURCE_ENV_DEFAULT), n)
    assert w.refresh().source == SOURCE_ENV_DEFAULT
    assert n.records == []


def test_watch_recovers_after_transient_failure(tmp_path):
    """失敗一輪沿用舊值，下一輪解析成功即恢復——失敗不是終態。"""
    n = RecordingNotifier()
    calls = {"i": 0}

    def flaky():
        calls["i"] += 1
        if calls["i"] == 1:
            raise LeaderResolutionError("manifest 暫時讀不到")
        return _B

    w = LeaderWatch(_A, flaky, n)
    assert w.refresh() == _A   # 第一輪失敗：沿用
    assert w.refresh() == _B   # 第二輪成功：切換（並告警）
    assert len(_crits(n)) == 2
