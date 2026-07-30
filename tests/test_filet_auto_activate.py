"""tests/test_filet_auto_activate.py
auto-activate watcher：pending ∩ 已簽章選 leader → 自動啟用（2026-07-30 紅線 5 例外路徑）。
全離線：真密碼學（eth_account 本地運算，沿 test_leader_change_apply 慣例）、
systemctl 以注入的 recorder 取代、chown 因非 root 自動跳過、notifier 用 Recording。
覆蓋 2026-07-30 opus 審查的失敗路徑：F1（env 完整性）、F2（start 失敗重試）、
F3（人工停機不復活）、F5（壞條目不連坐）、F6（偽 user_address 攻擊）、F10（空佇列
也驗範本）、逐條目隔離、freshness 放行的行為釘住。
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from scripts.filet_auto_activate import GENERATED_KEYS, run_once
from spark.copytrade.notifier import RecordingNotifier
from spark.filet.leader_change import (build_leader_change_message,
                                       build_leader_change_record,
                                       leader_changes_path_for,
                                       write_leader_change)
from spark.filet.user_leaders import record_user_leader, user_leaders_path_for
from spark.publicapi.config import derive_account_id
from spark.filet.risk_prefs import canonical_prefs
from spark.filet.risk_settings import (build_risk_settings_message,
                                       build_risk_settings_record,
                                       risk_settings_path_for, write_risk_settings)
from spark.publicapi.pending import load_pending, write_pending_entry

_BUILDER = "0x" + "22" * 20
_LEADER = "0x" + "a1" * 20      # 白名單內
_CUSTOM = "0x" + "d4" * 20      # 只在 user registry
_OUTSIDE = "0x" + "e5" * 20     # 兩邊都不在
_NOW = 1_800_000_000.0

# ⚠️ 風控四鍵自 2026-07-30 起由 watcher 代入，範本不得包含（見 GENERATED_KEYS）。
_TEMPLATE_OK = (
    "COPY_LIVE_TRADING=true\n"
    "COPY_TG_BOT_TOKEN=tok\n"
    "COPY_TG_CHAT_ID=123\n")


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


class _Site:
    """一個完整的 watcher 現場：pending＋manifest＋白名單＋交換目錄＋env/state 基座。"""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.wallet = Account.create()
        self.user_address = self.wallet.address
        self.account_id = derive_account_id(self.user_address)
        self.pending = tmp_path / "pending.json"
        self.manifest = tmp_path / "followers.json"
        self.leaders = tmp_path / "leaders.json"
        self.leaders.write_text(json.dumps(
            {"leaders": [{"address": _LEADER, "name": "Alpha"}]}))
        self.exchange = tmp_path / "exchange"
        self.exchange.mkdir()
        self.env_dir = tmp_path / "followers-env"
        self.state_base = tmp_path / "state"
        self.state_file = tmp_path / "watcher-state.json"
        self.template = tmp_path / "follower.env.template"
        self.template.write_text(_TEMPLATE_OK)
        self.notifier = RecordingNotifier()
        self.calls: list[list[str]] = []
        self.fail_start = False   # True → 注入的 systemctl 拋 CalledProcessError
        self.write_entry()

    def write_entry(self, *, account_id=None, user_address=None,
                    builder=_BUILDER, network="testnet"):
        write_pending_entry(self.pending,
                            account_id=account_id or self.account_id,
                            user_address=user_address or self.user_address,
                            builder_address=builder, network=network,
                            agent_address="0x" + "cd" * 20, label="internal")

    def sign_change(self, *, leader=_LEADER, nonce="n1", issued_at=None,
                    signer=None, account_id=None, tamper=None):
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        msg = build_leader_change_message(
            account_id=account_id, leader_address=leader,
            nonce=nonce, issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_leader_change_record(
            account_id=account_id, leader_address=leader, nonce=nonce,
            issued_at=issued_at, signature=sig, message=msg)
        if tamper:
            rec.update(tamper)
        write_leader_change(leader_changes_path_for(self.exchange), rec)
        return rec

    def sign_risk(self, prefs=None, *, nonce="r1", issued_at=None, signer=None,
                  account_id=None, tamper=None):
        """寫一筆**客戶簽章**的風控設定記錄（真密碼學，沿 sign_change 的形狀）。"""
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        prefs = canonical_prefs({"enabled": True} if prefs is None else prefs)
        msg = build_risk_settings_message(account_id=account_id, prefs=prefs,
                                          nonce=nonce, issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_risk_settings_record(
            account_id=account_id, prefs=prefs, nonce=nonce, issued_at=issued_at,
            signature=sig, message=msg)
        if tamper:
            rec.update(tamper)
        write_risk_settings(risk_settings_path_for(self.exchange), rec)
        return rec

    def run(self) -> int:
        def _recorder(cmd, check):
            assert check is True
            self.calls.append(list(cmd))
            if self.fail_start:
                raise subprocess.CalledProcessError(1, cmd)
        return run_once(
            pending_path=str(self.pending), manifest_path=str(self.manifest),
            builder=_BUILDER, leaders_path=str(self.leaders),
            exchange_dir=str(self.exchange), env_template=self.template,
            env_dir=self.env_dir, state_base=self.state_base,
            owner="nobody", group="nobody", state_file=self.state_file,
            notifier=self.notifier, run_cmd=_recorder)

    def manifest_leaders(self) -> dict[str, str | None]:
        if not self.manifest.exists():
            return {}
        data = json.loads(self.manifest.read_text())
        return {f["account_id"]: f["leader_address"] for f in data["followers"]}

    def crits(self):
        return [r for r in self.notifier.records if r[0] == "critical"]

    def warns(self):
        return [r for r in self.notifier.records if r[0] == "warn"]


@pytest.fixture
def site(tmp_path):
    return _Site(tmp_path)


def test_activates_whitelisted_selection(site):
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert site.manifest_leaders() == {site.account_id: _LEADER}
    assert load_pending(site.pending) == []
    assert site.calls == [["systemctl", "start",
                           f"filet-follower@{site.account_id}"]]
    env = site.env_dir / f"{site.account_id}.env"
    assert (os.stat(env).st_mode & 0o777) == 0o640
    state = site.state_base / site.account_id
    assert (os.stat(state).st_mode & 0o777) == 0o700


def test_generated_env_has_every_engine_required_var(site):
    """⭐ 審查 F1：產出的 env 必須足以讓引擎啟動——範本原文之外，watcher 必須
    代入引擎必填的四個 SPARK_*（缺 USER_ADDR/BUILDER_ADDR 引擎 exit 2；
    缺 NETWORK 靜默預設 testnet＝LIVE 配 testnet 的壞局）。"""
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    text = (site.env_dir / f"{site.account_id}.env").read_text()
    assert text.startswith(_TEMPLATE_OK)
    expected = {
        "SPARK_NETWORK": "testnet",          # 取自 pending 條目，非寫死
        "SPARK_ACCOUNT_ID": site.account_id,
        "SPARK_USER_ADDR": site.user_address,
        "SPARK_BUILDER_ADDR": _BUILDER,
        # 風控四鍵一律明確寫出（未表達＝產品預設不啟用），理由見 risk_prefs.py：
        # 一顆引擎的風控姿態要能從它自己的 env 讀出來，不必回推當時的預設值。
        "COPY_RISK_CONTROLS_ENABLED": "false",
        # 值經 risk_prefs 正規化（對齊 0.1% 刻度、定點格式）：規格的 "0.20"
        # 與 env 的 "0.2" 是同一個值，三處字串一致由 default_prefs 保證。
        "COPY_MAX_DRAWDOWN_PCT": "0.2",
        "COPY_MAX_TOTAL_DRAWDOWN_PCT": "0.4",
        "COPY_FLATTEN_ON_BREACH": "true",
        "COPY_SIZE_TOLERANCE": "0.08",      # tracking 組：不受風控開關影響
        "COPY_RISK_COOLDOWN_HOURS": "12",   # 熔斷後自動恢復的冷靜期
    }
    lines = dict(ln.split("=", 1) for ln in text.splitlines()
                 if "=" in ln and not ln.startswith("#"))
    for key in GENERATED_KEYS:
        assert lines[key] == expected[key], key


def _env_kv(site) -> dict[str, str]:
    text = (site.env_dir / f"{site.account_id}.env").read_text()
    return dict(ln.split("=", 1) for ln in text.splitlines()
                if "=" in ln and not ln.lstrip().startswith("#"))


def test_owner_signed_risk_settings_land_in_the_env(site):
    """⭐ 錢包主人**簽章**選的風控參數，必須原樣進到他自己的 env。

    來源刻意是簽章記錄而不是 pending 條目：能寫 pending 的人（filet-api）不該能
    替客戶關掉風控。"""
    site.sign_risk({"enabled": True, "max_drawdown_pct": "0.3",
                    "max_total_drawdown_pct": "0", "flatten_on_breach": False})
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    kv = _env_kv(site)
    assert kv["COPY_RISK_CONTROLS_ENABLED"] == "true"
    assert kv["COPY_MAX_DRAWDOWN_PCT"] == "0.3"
    assert kv["COPY_MAX_TOTAL_DRAWDOWN_PCT"] == "0"
    assert kv["COPY_FLATTEN_ON_BREACH"] == "false"
    assert not any(r[0] == "critical" for r in site.notifier.records)


def test_tampered_risk_record_fails_closed_to_protection_on(site):
    """⭐⭐ 驗章不過的風控記錄（簽完之後被改值）**不得**沿用「風控關閉」的產品
    預設——那會讓一次竄改靜默變成一顆沒有任何保護的實盤引擎。方向是往保護靠，
    並且要大聲（工程原則 3）。

    觸發情境：能寫交換目錄的人把客戶簽過的 `max_drawdown_pct` 從 0.3 改成 0.5
    （或直接把 enabled 改成 false）——重建訊息後 recover 出的位址對不上。
    """
    site.sign_risk({"enabled": True, "max_drawdown_pct": "0.3"},
                   tamper={"prefs": canonical_prefs(
                       {"enabled": False, "max_drawdown_pct": "0.5"})})
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert _env_kv(site)["COPY_RISK_CONTROLS_ENABLED"] == "true"
    crits = [r for r in site.notifier.records if r[0] == "critical"]
    assert any("風控設定記錄驗章失敗" in r[2] for r in crits)
    # 簽章材料不得進告警文字（紅線 2）
    assert not any("0x" in r[2] and len(r[2]) > 300 for r in crits)


def test_someone_elses_signature_cannot_set_my_risk(site):
    """⭐ 別人的私鑰簽出的風控記錄 → 驗章失敗 → 安全側 ＋ critical，
    絕不是「照著它寫」。這是本管線改讀簽章記錄的全部理由。"""
    site.sign_risk({"enabled": False}, signer=Account.create())
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert _env_kv(site)["COPY_RISK_CONTROLS_ENABLED"] == "true"
    assert any("驗章失敗" in r[2] for r in site.notifier.records if r[0] == "critical")


def test_unparseable_risk_prefs_fail_closed_to_protection_on(site):
    """簽章對得上但值超界（規格收窄後的舊記錄）→ 同樣走安全側 ＋ critical。"""
    rec = site.sign_risk({"enabled": True, "max_drawdown_pct": "0.3"})
    bad = dict(rec, prefs={**rec["prefs"], "max_drawdown_pct": "999"})
    write_risk_settings(risk_settings_path_for(site.exchange), bad)
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert _env_kv(site)["COPY_RISK_CONTROLS_ENABLED"] == "true"
    assert any(r[0] == "critical" for r in site.notifier.records)


def test_another_accounts_signed_record_is_not_used(site):
    """記錄檔是多帳號共用的：只有**這個 account_id** 的那一筆算數。
    別人的有效記錄不得漏進我的 env，也不該讓我被誤判成「驗章失敗」。"""
    other = Account.create()
    site.sign_risk({"enabled": True}, signer=other,
                   account_id=derive_account_id(other.address))
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert _env_kv(site)["COPY_RISK_CONTROLS_ENABLED"] == "false"
    assert not any(r[0] == "critical" for r in site.notifier.records)


def test_stale_signed_risk_settings_are_still_honoured(site):
    """⚠️ freshness 刻意放行：風控設定是**持續意圖**。一份三天前簽的「回撤上限 30%」
    今天仍然是客戶的意圖——因過期而退回產品預設（不啟用）是無聲的降級。"""
    site.sign_risk({"enabled": True, "max_drawdown_pct": "0.3"},
                   issued_at=_at(-3 * 86400))
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    kv = _env_kv(site)
    assert kv["COPY_RISK_CONTROLS_ENABLED"] == "true"
    assert kv["COPY_MAX_DRAWDOWN_PCT"] == "0.3"


def test_no_risk_choice_means_product_default_off_without_alarm(site):
    """未表達＝新錢包的產品預設（不啟用），這是合法路徑，不該發 critical。"""
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert _env_kv(site)["COPY_RISK_CONTROLS_ENABLED"] == "false"
    assert not any(r[0] == "critical" for r in site.notifier.records)


def test_template_containing_spark_keys_fails_closed(site):
    """範本自帶 SPARK_*＝與代入區塊重複定義的歧義 → 整輪拒跑。"""
    site.template.write_text(_TEMPLATE_OK + "SPARK_NETWORK=mainnet\n")
    site.sign_change(leader=_LEADER)
    with pytest.raises(SystemExit):
        site.run()
    assert site.manifest_leaders() == {}


def test_waits_until_leader_selected(site):
    """沒有任何簽章選擇 → 不啟用、不算失敗；條目留給下一輪（產品語意）。"""
    assert site.run() == 0
    assert site.manifest_leaders() == {}
    assert len(load_pending(site.pending)) == 1
    assert site.calls == []


def test_custom_leader_admitted_via_registry(site):
    """自訂 leader：選擇時 API 已寫入 user registry → 准入集合＝白名單 ∪ registry。"""
    record_user_leader(user_leaders_path_for(site.exchange),
                       address=_CUSTOM, added_by=site.account_id)
    site.sign_change(leader=_CUSTOM)
    assert site.run() == 0
    assert site.manifest_leaders() == {site.account_id: _CUSTOM}


def test_rejects_leader_outside_admissible_set(site):
    """白名單與 registry 都沒有 → 拒絕啟用（CRIT＋TG），條目保留供調查。"""
    site.sign_change(leader=_OUTSIDE)
    assert site.run() == 1
    assert site.manifest_leaders() == {}
    assert len(load_pending(site.pending)) == 1
    assert site.calls == []
    assert len(site.crits()) == 1


def test_forged_signature_is_not_a_selection(site):
    """⭐ 拆掉人工審核後的那道防線：別人的私鑰簽的「選擇」不算數。"""
    site.sign_change(leader=_LEADER, signer=Account.create())
    assert site.run() == 0                      # 視同「尚未選 leader」
    assert site.manifest_leaders() == {}
    assert len(load_pending(site.pending)) == 1


def test_tampered_record_treated_as_no_selection(site):
    """簽章後被改欄位（例如 API 被打穿改 leader_address）→ 驗章必敗 → 視同未選。"""
    site.sign_change(leader=_CUSTOM, nonce="n2",
                     tamper={"leader_address": _OUTSIDE})
    assert site.run() == 0
    assert site.manifest_leaders() == {}
    assert len(load_pending(site.pending)) == 1


def test_stale_signature_still_activates(site):
    """⭐ 釘住刻意的政策（檔頭「freshness 放行」）：用戶選完 leader 隔很久才入金，
    舊簽章仍是有效的啟用依據；引擎側對同 leader 記錄「忽略不告警」。"""
    site.sign_change(leader=_LEADER, issued_at=_at(-100_000))
    assert site.run() == 0
    assert site.manifest_leaders() == {site.account_id: _LEADER}


def test_spoofed_user_address_rejected_before_any_root_action(site):
    """⭐ 審查 F6：被打穿的 API 寫 {account_id: 受害者, user_address: 攻擊者}，
    攻擊者用自己私鑰簽受害者的 account_id——derive 綁定必須在驗章與任何
    root 建檔**之前**擋下：不建 env、不建 state、不碰 systemctl。"""
    attacker = Account.create()
    victim_acct = derive_account_id("0x" + "99" * 20)
    site.pending.unlink()
    write_pending_entry(site.pending, account_id=victim_acct,
                        user_address=attacker.address,
                        builder_address=_BUILDER, network="testnet",
                        agent_address="0x" + "cd" * 20, label="x")
    site.sign_change(leader=_LEADER, signer=attacker, account_id=victim_acct)
    assert site.run() == 1
    assert site.manifest_leaders() == {}
    assert not site.env_dir.exists()
    assert not (site.state_base / victim_acct).exists()
    assert site.calls == []
    assert len(load_pending(site.pending)) == 1


def test_start_failure_keeps_pending_and_retries_next_round(site):
    """⭐ 審查 F2：systemctl start 失敗 → pending 必須還在（重試載體）；
    下一輪走 healed_started 補救：重試 start、成功後才清佇列。"""
    site.sign_change(leader=_LEADER)
    site.fail_start = True
    assert site.run() == 1
    assert site.manifest_leaders() == {site.account_id: _LEADER}
    assert len(load_pending(site.pending)) == 1     # 沒被清掉
    assert len(site.crits()) == 1
    site.fail_start = False
    assert site.run() == 0                          # 第二輪補救成功
    assert load_pending(site.pending) == []
    assert len(site.calls) == 2                     # 兩輪各一次 start


def test_operator_stopped_follower_is_not_resurrected(site):
    """⭐ 審查 F3：曾由 watcher 啟動（phase=started）的帳號，之後人工 stop、
    用戶重按「完成綁定」產生新 pending → 只清佇列＋warn，**絕不** start。"""
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    start_count = len(site.calls)
    site.write_entry()                              # 重按「完成綁定」
    assert site.run() == 0
    assert len(site.calls) == start_count           # 沒有新的 systemctl
    assert load_pending(site.pending) == []
    assert len(site.warns()) == 1


def test_manually_activated_account_not_touched(site):
    """manifest 有、watcher state 無（人工 CLI 啟用的帳號）→ 同樣不碰引擎，
    清佇列＋warn 留痕。"""
    site.manifest.write_text(json.dumps({"followers": [
        {"account_id": site.account_id, "user_address": site.user_address,
         "builder_address": _BUILDER, "network": "testnet",
         "label": "", "leader_address": _LEADER}]}))
    assert site.run() == 0
    assert site.calls == []
    assert load_pending(site.pending) == []
    assert len(site.warns()) == 1


def test_bad_entry_does_not_block_others(site):
    """⭐ 逐條目隔離＋F5 前置驗證：一筆 network 壞掉的條目 CRIT 保留，
    其他條目照常啟用；manifest 不被壞值污染。"""
    other = Account.create()
    other_acct = derive_account_id(other.address)
    # 壞條目：write_pending_entry 會驗 network，繞過它直接手寫檔案（模擬 API 被打穿）。
    entries = json.loads(site.pending.read_text())
    entries["pending"].append({
        "account_id": other_acct, "user_address": other.address,
        "builder_address": _BUILDER, "network": "mainnett",
        "agent_address": "0x" + "cd" * 20, "label": ""})
    site.pending.write_text(json.dumps(entries))
    site.sign_change(leader=_LEADER)                # 好條目（site.wallet）的選擇
    assert site.run() == 1
    assert site.manifest_leaders() == {site.account_id: _LEADER}   # 好條目成功
    remaining = load_pending(site.pending)
    assert [e["account_id"] for e in remaining] == [other_acct]    # 壞條目保留
    assert len(site.crits()) == 1


def test_template_placeholder_fails_closed_even_with_empty_queue(site):
    """⭐ 審查 F10：範本沒填好，**空佇列也要炸**——部署驗收當下就發現，
    而不是第一個用戶出現那天。"""
    site.template.write_text("COPY_TG_BOT_TOKEN=REPLACE_WITH_TELEGRAM_BOT_TOKEN\n")
    site.pending.write_text(json.dumps({"pending": []}))
    with pytest.raises(SystemExit):
        site.run()


def test_activation_recycles_consumed_record(site):
    """⭐ 審查 F7A：啟用成功後，已消化的變更記錄必須從記錄檔回收——否則引擎
    每 cycle 對過期簽章發假 critical、每日對帳永久列 not_redeemed。"""
    site.sign_change(leader=_LEADER, issued_at=_at(-100_000))
    assert site.run() == 0
    from spark.filet.leader_change import load_leader_changes
    remaining = load_leader_changes(leader_changes_path_for(site.exchange))
    assert [r for r in remaining if r["account_id"] == site.account_id] == []


def test_unsatisfied_selection_is_not_recycled(site):
    """回收判定是「目標 == manifest 現值」：用戶啟用後（人工 CLI 情境）簽了一筆
    **不同** leader 的新選擇——那是真的待套用意圖，heal 路徑不得誤刪。"""
    site.manifest.write_text(json.dumps({"followers": [
        {"account_id": site.account_id, "user_address": site.user_address,
         "builder_address": _BUILDER, "network": "testnet",
         "label": "", "leader_address": _LEADER}]}))
    record_user_leader(user_leaders_path_for(site.exchange),
                       address=_CUSTOM, added_by=site.account_id)
    site.sign_change(leader=_CUSTOM)            # 目標 ≠ manifest 現值
    assert site.run() == 0                      # healed_no_start（人工啟用、無標記）
    from spark.filet.leader_change import load_leader_changes
    remaining = load_leader_changes(leader_changes_path_for(site.exchange))
    assert [r["leader_address"] for r in remaining
            if r["account_id"] == site.account_id] == [_CUSTOM]


def test_shipped_example_template_works_after_filling(site):
    """⭐ 用**實際出貨的** example 檔走完整流程（2026-07-30 實機部署踩到：合成範本
    測不到「範本自己的註解含 REPLACE_WITH／SPARK_* 字樣」把檢查誤觸發的情境）。
    模擬部署步驟：sed 填兩個 TG 佔位符 → 應可正常啟用。"""
    example = Path(__file__).parent.parent / "deploy" / "follower.env.autoactivate.example"
    filled = (example.read_text()
              .replace("REPLACE_WITH_TELEGRAM_BOT_TOKEN", "tok")
              .replace("REPLACE_WITH_TELEGRAM_CHAT_ID", "123"))
    site.template.write_text(filled)
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert site.manifest_leaders() == {site.account_id: _LEADER}


def test_unfilled_value_line_still_fails_closed(site):
    """對照組：**值行**的佔位符殘留仍然整輪拒跑（註解行的字樣則不觸發）。"""
    example = Path(__file__).parent.parent / "deploy" / "follower.env.autoactivate.example"
    site.template.write_text(example.read_text())   # 兩個 TG 佔位符未填
    site.sign_change(leader=_LEADER)
    with pytest.raises(SystemExit):
        site.run()


def test_existing_env_file_not_overwritten(site):
    """已存在的 per-follower env（人工調過的風險參數）不得被範本蓋掉。"""
    site.env_dir.mkdir(parents=True)
    custom = "COPY_LIVE_TRADING=false\nCOPY_MAX_DRAWDOWN_PCT=0.05\n"
    (site.env_dir / f"{site.account_id}.env").write_text(custom)
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert (site.env_dir / f"{site.account_id}.env").read_text() == custom
