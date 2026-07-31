"""tests/test_engine_health.py
引擎主動發布的健康摘要心跳（`spark.filet.engine_health`）。

本檔釘住三件事，其餘都是周邊：
1. ⭐ **過期的心跳不得被當成目前狀態**——`read_heartbeat` 在過期時結構性地不回傳
   payload，呼叫端就算想拿也拿不到。
2. ⭐ **心跳不含任何密鑰材料**——掃描在寫入邊界上執行，加錯欄位的人在這裡撞牆。
3. ⭐ **最小權限拓撲**——心跳落在交換目錄的 engine→api 子通道裡，不是散在根目錄，
   也不是靠放寬引擎狀態根的權限。
"""
import json

import pytest

from spark.filet.engine_health import (ENGINE_PUBLISH_DIRNAME, HEARTBEAT_FIELDS,
                                       HEARTBEAT_STALE_S, FORBIDDEN_KEY_PARTS,
                                       HeartbeatRejected, build_heartbeat,
                                       engine_publish_dir_for, heartbeat_dir_for,
                                       heartbeat_path_for, publish_heartbeat,
                                       read_heartbeat, write_heartbeat)

ACCT = "f" + "a1" * 20
_NOW = 1_800_000_000.0


class _Cov:
    """`copytrade.equity.SampleCoverage` 的最小替身（只用到四個欄位）。"""

    def __init__(self, count=12, oldest=900.0, newest=30.0, read_error=False):
        self.count, self.oldest_age_s = count, oldest
        self.newest_age_s, self.read_error = newest, read_error

    @property
    def sufficient(self):
        return not self.read_error and self.count >= 2


def _payload(now_s=_NOW, **over):
    base = dict(account_id=ACCT, now_s=now_s, killswitch_tripped=False,
                coverage=_Cov(), alerts_count=0, leader_address="0x" + "d4" * 20,
                leader_source="customer_signed", leader_kind="standard",
                allocated_capital="5000.00",
                capital_utilization="0.4000", use_full_equity=False,
                capital_source="customer_signed",
                capital_changed_at="2026-07-19T00:00:00+00:00",
                risk_controls_enabled=True, risk_source="env_default",
                risk_changed_at=None,
                risk_prefs=None,
                risk_halt=None,
                cycle_result="no_action", cycle_detail=None)
    base.update(over)
    return build_heartbeat(**base)


# ── 拓撲：發布的是一份窄的產物，落在專屬的 engine→api 子通道 ────────────────

def test_heartbeat_lives_in_the_engine_to_api_subchannel(tmp_path):
    """⭐⭐ 心跳落在 `<exchange>/engine/health/<account>.json`，**不是**交換目錄根。

    這一條釘住的是權限拓撲：交換目錄根的 owner 是 filet-api（引擎無寫權，維持
    api→engine 單向），引擎能寫的只有 `engine/` 這一格。若哪天有人把心跳搬回根目錄，
    部署就必須讓引擎對整個交換目錄可寫——那會讓被打穿的引擎能改寫客戶簽章記錄。
    """
    p = heartbeat_path_for(tmp_path, ACCT)
    assert p.parent.parent == engine_publish_dir_for(tmp_path)
    assert p.parent == heartbeat_dir_for(tmp_path)
    assert p.parent.parent.parent == tmp_path      # engine/ 就掛在交換目錄底下
    assert ENGINE_PUBLISH_DIRNAME in p.parts
    assert p.name == f"{ACCT}.json"                # per-follower 一個檔


def test_heartbeat_path_rejects_traversal_in_account_id(tmp_path):
    """account_id 會變成路徑的一段 → 穿越一律拒絕（縱深防禦）。"""
    with pytest.raises(ValueError):
        heartbeat_path_for(tmp_path, "../../etc/cron.d/x")


# ── 內容：欄位齊全、釘住鍵集 ────────────────────────────────────────────

def test_heartbeat_carries_every_summary_field(tmp_path):
    p = heartbeat_path_for(tmp_path, ACCT)
    write_heartbeat(p, _payload())
    data = read_heartbeat(p, _NOW + 1).data

    assert data["account_id"] == ACCT
    assert data["written_at"].startswith("20") and data["written_at"].endswith("+00:00")
    assert data["written_at_s"] == _NOW
    assert data["killswitch_tripped"] is False
    assert data["coverage"] == {"known": True, "count": 12, "oldest_age_s": 900.0,
                                "newest_age_s": 30.0, "sufficient": True}
    assert data["leader"] == {"address": "0x" + "d4" * 20,
                              "source": "customer_signed", "kind": "standard"}
    assert data["capital"]["allocated_capital"] == "5000.00"
    assert data["capital"]["capital_utilization"] == "0.4000"
    assert data["capital"]["use_full_equity"] is False
    assert data["capital"]["source"] == "customer_signed"
    assert data["capital"]["changed_at"] == "2026-07-19T00:00:00+00:00"
    assert data["last_cycle"] == {"result": "no_action", "detail": None}


def test_heartbeat_field_set_is_pinned_by_the_constant():
    """新增／刪除頂層欄位必須主動改 HEARTBEAT_FIELDS（沿 CAPITAL_SETTINGS_FIELDS）。

    釘住鍵集的用意不是潔癖：心跳落在一個權限較低的進程讀得到的目錄裡，「多了一個
    欄位」這件事必須是有人主動做的決定，而不是某次重構的副作用。
    """
    assert tuple(_payload().keys()) == HEARTBEAT_FIELDS


def test_leader_kind_rides_the_heartbeat():
    """⭐ leader 種類（standard／vault）隨心跳發布（2026-07-31 第二批）。

    vault 保護（20x 帽＋流量中性化）是否正在生效，面板唯一的觀測面就是這一格——
    少了它，操作者只能翻引擎 log 才知道引擎當下把 leader 當 vault 還是 standard。
    None ＝本輪沒有 leader（例如已撤銷），與 "standard" 分開：未知不得畫成已知。
    """
    assert _payload(leader_kind="vault")["leader"]["kind"] == "vault"
    assert _payload(leader_kind="standard")["leader"]["kind"] == "standard"
    assert _payload(leader_kind=None)["leader"]["kind"] is None


def test_risk_controls_flag_rides_the_heartbeat(tmp_path):
    """⭐ 「這顆引擎沒有任何風控」必須看得見（2026-07-30）：狀態根對 filet-api
    不可讀，心跳是面板唯一的來源。未知（settings 尚未載入）不得畫成 True。"""
    assert _payload(risk_controls_enabled=False)["risk"]["controls_enabled"] is False
    assert _payload(risk_controls_enabled=True)["risk"]["controls_enabled"] is True
    assert _payload(risk_controls_enabled=None)["risk"]["controls_enabled"] is None


def test_risk_source_and_changed_at_ride_the_heartbeat():
    """⭐ 風控那一格要帶來源與變更時刻（沿 `capital` 的同一個形狀，2026-07-30）。

    少了它，面板分不出「客戶自己把回撤上限調到 50%」與「部署把它設成 50%」——
    前者是客戶的決定，後者是我們該去查的事。
    """
    hb = _payload(risk_source="customer_signed",
                  risk_changed_at="2026-07-30T01:00:00+00:00")["risk"]
    assert hb["source"] == "customer_signed"
    assert hb["changed_at"] == "2026-07-30T01:00:00+00:00"
    env = _payload(risk_source="env_default", risk_changed_at=None)["risk"]
    assert env == {"controls_enabled": True, "source": "env_default",
                   "changed_at": None, "prefs": None, "halt": None}


def test_halt_reason_rides_the_heartbeat_with_resumable(tmp_path):
    """⭐ 熔斷原因與「客戶能不能自助恢復」必須一起發布（2026-07-30）：客戶頁面上的
    「立即恢復跟單」是一次真實簽章，前端不知道原因就只能讓他簽一份注定被拒的請求。
    `resumable` 由引擎端的 `rearm_allowed_for` 導出，前端不自己比對字串。"""
    halt = {"tripped": True, "reason": "leader_revoked",
            "tripped_at": "2026-07-30T02:00:00+00:00", "resumable": False}
    assert _payload(risk_halt=halt)["risk"]["halt"] == halt
    assert _payload(risk_halt=None)["risk"]["halt"] is None


def test_coverage_read_error_reports_unknown_not_zero():
    """⭐ 樣本讀不到 → `known=False` ＋ 全 null，**不是 count=0**。

    0 是「引擎剛啟動、還沒樣本」，讀不到是「有東西在那裡但看不到」——後者在面板上
    必須刺眼。回 0 等於在覆蓋度檔壞掉的當下宣稱「沒有歷史」。
    """
    cov = _payload(coverage=_Cov(read_error=True))["coverage"]
    assert cov["known"] is False
    assert cov["count"] is None and cov["sufficient"] is None


def test_alerts_count_rides_the_heartbeat_with_a_known_flag():
    """⭐ 告警數必須進心跳，而且要帶 `known` 旗標（沿 coverage 的同一個形狀）。

    存在理由：面板直讀告警檔需要讀得到狀態根，而正式部署的狀態根是
    `0700 filet-engine`（面板讀不到），路徑也可能漂移。少了這一格，`alerts` 在
    那些情境下**永遠**是未知——而「0 則告警」正是操作者判斷「不用現在去看」的依據。
    """
    assert _payload(alerts_count=3)["alerts"] == {"known": True, "count": 3}
    # ⭐ 真的 0 則與「引擎自己也讀不到」是兩件事，不得長成同一個樣子
    assert _payload(alerts_count=0)["alerts"] == {"known": True, "count": 0}
    assert _payload(alerts_count=None)["alerts"] == {"known": False, "count": None}


def test_missing_coverage_object_reports_unknown():
    cov = _payload(coverage=None)["coverage"]
    assert cov == {"known": False, "count": None, "oldest_age_s": None,
                   "newest_age_s": None, "sufficient": None}


# ── ⭐⭐ 過期的心跳不是目前狀態 ──────────────────────────────────────────

def test_stale_heartbeat_is_flagged_and_withholds_its_payload(tmp_path):
    """⭐⭐ 本檔最重要的一條。過期 → `status="stale"` ＋ 最後時刻 ＋ 年齡，
    而 `data is None`——過期心跳裡的值**結構上拿不到**。

    拿掉 `read_heartbeat` 的過期分支，本測試會紅在 `data is None` 那一行：一份
    40 分鐘前的「kill switch 未觸發」會被面板當成現況顯示，而那正是客戶的引擎
    已經熔斷、部位已被平掉的那一刻最不能發生的事（謊報健康比沒有面板更危險）。
    """
    p = heartbeat_path_for(tmp_path, ACCT)
    write_heartbeat(p, _payload(killswitch_tripped=False))

    hb = read_heartbeat(p, _NOW + HEARTBEAT_STALE_S + 1)

    assert hb.status == "stale"
    assert hb.fresh is False
    assert hb.data is None                    # ⭐ 過期的值一個都拿不到
    assert hb.age_s == HEARTBEAT_STALE_S + 1  # 但「多久沒心跳」要說得出來
    assert hb.at == _payload()["written_at"]  # 最後心跳時刻照實上呈


def test_heartbeat_is_fresh_right_up_to_the_threshold(tmp_path):
    """邊界：恰好等於門檻仍算新鮮（過期是**超過**，不是「到了」）。"""
    p = heartbeat_path_for(tmp_path, ACCT)
    write_heartbeat(p, _payload())
    assert read_heartbeat(p, _NOW + HEARTBEAT_STALE_S).status == "ok"
    assert read_heartbeat(p, _NOW + HEARTBEAT_STALE_S + 0.001).status == "stale"


def test_missing_heartbeat_is_missing_not_healthy(tmp_path):
    """⭐ 心跳檔不存在 → `missing` ＋ 全 null，**絕不是**一組看起來健康的預設值。"""
    hb = read_heartbeat(heartbeat_path_for(tmp_path, ACCT), _NOW)
    assert hb.status == "missing"
    assert hb.data is None and hb.age_s is None and hb.at is None


def test_corrupt_heartbeat_is_unreadable_not_missing(tmp_path):
    """`unreadable` 與 `missing` 分開：前者「有東西但看不懂」，後者「從沒寫過」。

    處置完全不同（查檔案 vs 查引擎有沒有跑），折疊成一個「未知」會把 admin 該做的
    第一步藏起來。
    """
    p = heartbeat_path_for(tmp_path, ACCT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert read_heartbeat(p, _NOW).status == "unreadable"


def test_heartbeat_without_timestamp_is_unreadable(tmp_path):
    """⭐ 算不出年齡的心跳 → `unreadable`，**不是**「當成剛寫的」。

    預設它新鮮正好是謊報健康的方向：一份沒有時間戳的檔案沒辦法證明自己是新的。
    """
    p = heartbeat_path_for(tmp_path, ACCT)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload()
    payload.pop("written_at_s")
    p.write_text(json.dumps(payload))
    assert read_heartbeat(p, _NOW).status == "unreadable"


def test_future_dated_heartbeat_is_unreadable(tmp_path):
    """未來時刻（時鐘跳動／手工放的檔）同樣無法證明自己反映現況 → unreadable。"""
    p = heartbeat_path_for(tmp_path, ACCT)
    write_heartbeat(p, _payload(now_s=_NOW + 10_000))
    hb = read_heartbeat(p, _NOW)
    assert hb.status == "unreadable" and hb.data is None


# ── ⭐ 密鑰材料：掃描在寫入邊界上 ────────────────────────────────────────

def _walk(obj, path=""):
    """遞迴列出 (鍵路徑, 值)，給下面的掃描測試用。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}{k}.")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}{i}.")
    else:
        yield path.rstrip("."), obj


def test_published_heartbeat_contains_no_secret_material(tmp_path):
    """⭐⭐ 掃描一份真的落地的心跳：沒有任何禁用鍵名、沒有任何長 hex 值。

    心跳落在一個權限較低的進程讀得到的目錄裡。簽章／nonce／私鑰材料一旦寫進去，
    就是把「客戶授權的證據」交給另一個信任域——而面板一個都不需要。
    """
    p = heartbeat_path_for(tmp_path, ACCT)
    write_heartbeat(p, _payload())
    raw = p.read_text()
    data = json.loads(raw)

    for key, value in _walk(data):
        low = key.lower()
        assert not any(part in low for part in FORBIDDEN_KEY_PARTS), \
            f"心跳含禁用欄位 {key}"
        if isinstance(value, str):
            # 位址（42 字元）合法；簽章（132）與私鑰（66）不得出現。
            assert not (value.startswith("0x") and len(value) > 42), \
                f"{key} 疑似含簽章或私鑰"
    for part in ("signature", "message", "nonce", "privateKey"):
        assert part not in raw


@pytest.mark.parametrize("bad", [
    {"signature": "0xdead"},
    {"capital": {"nonce": "n1"}},
    {"last_cycle": {"message": "客戶簽的原文"}},
    {"leader": {"agent_key": "x"}},
])
def test_write_refuses_payload_with_forbidden_keys(tmp_path, bad):
    """⭐ 禁用鍵名一律**拒寫**（檔案不產生），不是「寫了再說、事後 review」。"""
    p = heartbeat_path_for(tmp_path, ACCT)
    payload = _payload()
    payload.update(bad)
    with pytest.raises(HeartbeatRejected):
        write_heartbeat(p, payload)
    assert not p.exists()


def test_write_refuses_long_hex_hidden_in_an_innocent_field(tmp_path):
    """⭐⭐ 第二層：鍵名檢查擋不住「把簽章塞進一個叫 detail 的欄位」。

    這一層不靠命名——132 字元的 hex 出現在 payload 的任何位置都拒寫。
    """
    p = heartbeat_path_for(tmp_path, ACCT)
    payload = _payload(cycle_detail="0x" + "ab" * 65)
    with pytest.raises(HeartbeatRejected):
        write_heartbeat(p, payload)
    assert not p.exists()


def test_address_length_hex_is_allowed(tmp_path):
    """位址（42 字元）不得被誤傷——它是心跳的正常內容（目前跟的 leader）。"""
    p = heartbeat_path_for(tmp_path, ACCT)
    write_heartbeat(p, _payload(leader_address="0x" + "d4" * 20))
    assert read_heartbeat(p, _NOW).data["leader"]["address"] == "0x" + "d4" * 20


# ── ⭐ 寫入失敗不得中斷跟單 ──────────────────────────────────────────────

def test_publish_failure_never_raises(tmp_path):
    """⭐⭐ 心跳寫不出去 → 回 False ＋ log，**絕不 raise**。

    心跳是可觀測性，不是安全關鍵路徑。讓它 raise，「交換目錄權限給錯」會一路變成
    「引擎停機、客戶部位無人管理」——觀測層壞掉絕不能弄停被觀測的系統。
    這裡用「父路徑是一個檔案」製造真實的 OSError（mkdir 會失敗）。
    """
    blocker = tmp_path / "exchange"
    blocker.write_text("i am a file, not a directory")
    assert publish_heartbeat(heartbeat_path_for(blocker, ACCT), _payload()) is False


def test_publish_rejects_secrets_without_raising(tmp_path):
    """密鑰命中同樣不 raise（拒寫已生效），但回 False 讓呼叫端知道沒落地。"""
    payload = _payload()
    payload["signature"] = "0xdead"
    p = heartbeat_path_for(tmp_path, ACCT)
    assert publish_heartbeat(p, payload) is False
    assert not p.exists()


def test_publish_leaves_no_tmp_file_behind(tmp_path):
    """原子落檔：成功之後目錄裡只剩正式檔，沒有 .tmp 殘留。"""
    p = heartbeat_path_for(tmp_path, ACCT)
    assert publish_heartbeat(p, _payload()) is True
    assert [f.name for f in p.parent.iterdir()] == [f"{ACCT}.json"]


# ── ⭐ 兩個門檻不得漂移 ──────────────────────────────────────────────────

def test_heartbeat_stale_threshold_matches_engine_liveness_threshold():
    """⭐⭐ `HEARTBEAT_STALE_S` 必須等於 `ops.ENGINE_STALE_S`。

    兩者回答的是同一個問題（「引擎最近有沒有動」），基準也同源（都是「引擎每
    cycle 落一次的東西」的年齡）。各自漂移的話，面板上「引擎存活」與「心跳新鮮」
    會在同一個時刻給出相反的答案，而操作者無從判斷該信哪一個——一個會自相矛盾的
    面板等於沒有面板。要改就兩個一起改，本測試逼出這個決定。
    """
    from spark.publicapi.ops import ENGINE_STALE_S

    assert HEARTBEAT_STALE_S == ENGINE_STALE_S


def test_stale_threshold_is_many_cycles_not_one():
    """門檻必須遠大於 cycle 間隔：貼近 cycle 的門檻會被一次 GC 停頓打成紅燈，
    而會誤報的面板等於沒有面板（操作者會學會忽略它）。"""
    from spark.copytrade.config import CopySettings

    assert HEARTBEAT_STALE_S >= 5 * CopySettings.interval_s
