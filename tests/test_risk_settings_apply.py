"""tests/test_risk_settings_apply.py
引擎側套用客戶簽章的風控設定與自助解除熔斷（spark.filet.risk_settings_apply）。

⭐ 這裡決定**引擎什麼時候會停下來保護客戶**。本檔盯住的五件事，每一件對應一個
「錯了就是真錢」的失效：

(1) **簽章者必須等於 manifest 的 user_address**——不是記錄裡的任何欄位。
    偽造／別人的簽章 → 不套用 ＋ critical ＋ 沿用現狀。
(2) **單調 issued_at 護欄**：舊記錄被放回去（回滾攻擊，用一份客戶真的簽過的舊授權
    換回較弱的保護）→ 拒絕 ＋ critical；相同 issued_at → 靜默冪等。
(3) **超界一律拒絕、絕不 clamp**——夾取＝客戶簽 A、引擎執行 B，而且沒有人會知道。
(4) **本管線失效絕不中斷跟單**：任何路徑都不 raise，安全方向是「沿用現狀」。
(5) **解鎖有四道閘**：驗章、時效、請求須晚於熔斷、觸發原因須可恢復
    （leader 撤銷是治理動作，不得由客戶自助解除）。

用真密碼學（eth_account 本地運算，不觸網）；純檔案操作，全離線。
"""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import ARM_FILE_RELPATH
from spark.copytrade.notifier import RecordingNotifier
from spark.filet.risk_prefs import default_prefs
from spark.filet.risk_settings import (RISK_SETTINGS_MAX_AGE_S,
                                       build_risk_settings_message,
                                       build_risk_settings_record,
                                       build_risk_unlock_message,
                                       build_risk_unlock_record,
                                       write_risk_settings, write_risk_unlock)
from spark.filet.risk_settings_apply import (APPLIED_STATE_RELPATH,
                                             RiskSettingsApplier,
                                             copy_settings_field,
                                             prefs_to_settings_overrides)
from spark.filet.risk_prefs import RISK_PARAM_SPECS

_NOW = 1_800_000_000.0
_LEADER = "0x" + "a1" * 20

# 基礎設定＝env 值（auto-activate watcher 在啟用當下依客戶偏好寫進 env 的那一份）。
_BASE = CopySettings(leader_address=_LEADER, risk_controls_enabled=True,
                     max_drawdown_pct=Decimal("0.20"),
                     max_total_drawdown_pct=Decimal("0.40"),
                     flatten_on_breach=True, size_tolerance=Decimal("0.08"),
                     risk_cooldown_hours=Decimal("12"))


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _prefs(**over) -> dict:
    return {**default_prefs(), "enabled": True, **over}


class _Env:
    """一份完整的引擎現場：manifest ＋ 兩個記錄檔 ＋ 狀態根 ＋ notifier。"""

    def __init__(self, tmp_path: Path, *, wallet):
        self.tmp = tmp_path
        self.wallet = wallet
        self.account_id = _acct(wallet)
        self.manifest = tmp_path / "followers.json"
        self.manifest.write_text(json.dumps({"followers": [
            {"account_id": self.account_id, "user_address": wallet.address,
             "builder_address": "0x" + "22" * 20, "network": "mainnet",
             "label": "", "leader_address": _LEADER}]}))
        self.settings_path = tmp_path / "risk_settings.json"
        self.unlock_path = tmp_path / "risk_unlock.json"
        self.state_root = tmp_path / "state"
        self.notifier = RecordingNotifier()

    def applier(self, *, notifier=None, now_s=_NOW, account_id=None):
        return RiskSettingsApplier(
            account_id=account_id or self.account_id,
            manifest_path=self.manifest, settings_path=self.settings_path,
            unlock_path=self.unlock_path, state_root=self.state_root,
            notifier=notifier or self.notifier, now_fn=lambda: now_s)

    def write_settings(self, *, prefs=None, nonce="n1", issued_at=None, signer=None,
                       account_id=None, tamper=None):
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        prefs = _prefs() if prefs is None else prefs
        msg = build_risk_settings_message(account_id=account_id, prefs=prefs,
                                          nonce=nonce, issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_risk_settings_record(account_id=account_id, prefs=prefs,
                                         nonce=nonce, issued_at=issued_at,
                                         signature=sig, message=msg)
        if tamper:
            rec.update(tamper)
        write_risk_settings(self.settings_path, rec)
        return rec

    def write_unlock(self, *, nonce="u1", issued_at=None, signer=None,
                     account_id=None, tamper=None):
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        msg = build_risk_unlock_message(account_id=account_id, nonce=nonce,
                                        issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_risk_unlock_record(account_id=account_id, nonce=nonce,
                                       issued_at=issued_at, signature=sig,
                                       message=msg)
        if tamper:
            rec.update(tamper)
        write_risk_unlock(self.unlock_path, rec)
        return rec

    def trip(self, *, reason="", tripped_at=None):
        """放一份 ARM 檔（kill switch 已觸發）。"""
        p = self.state_root / ARM_FILE_RELPATH
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tripped_at": tripped_at or _at(-3600),
                   "drawdown_pct": "0.25", "breached": True}
        if reason:
            payload["reason"] = reason
        p.write_text(json.dumps(payload))
        return p

    @property
    def state_file(self) -> Path:
        return self.state_root / APPLIED_STATE_RELPATH

    def crits(self):
        return [r for r in self.notifier.records if r[0] == "critical"]


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path, wallet=Account.create())


# ── 參數對映：spec ↔ CopySettings 欄位 ────────────────────────────────

def test_every_risk_param_maps_to_a_real_copy_settings_field():
    """⭐ 由 env 鍵推導欄位名（單一推導規則）。漂移 → 建構時就炸，不是靜默少套一項。"""
    for spec in RISK_PARAM_SPECS:
        assert copy_settings_field(spec)
    assert copy_settings_field(
        {"name": "cooldown_hours", "env": "COPY_RISK_COOLDOWN_HOURS"}
    ) == "risk_cooldown_hours"
    with pytest.raises(ValueError):
        copy_settings_field({"name": "x", "env": "COPY_NOT_A_FIELD"})


def test_overrides_cover_the_switch_and_every_parameter():
    ov = prefs_to_settings_overrides(_prefs())
    assert ov["risk_controls_enabled"] is True
    assert {copy_settings_field(s) for s in RISK_PARAM_SPECS} <= set(ov)


# ── ⭐ 快樂路徑 ───────────────────────────────────────────────────────

def test_applies_valid_signed_settings(env):
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.35",
                                    flatten_on_breach=False,
                                    cooldown_hours="0"))
    cs = env.applier().effective(_BASE)

    assert cs.risk_controls_enabled is True
    assert cs.max_drawdown_pct == Decimal("0.35")
    assert cs.flatten_on_breach is False
    assert cs.risk_cooldown_hours == Decimal("0")
    assert cs.leader_address == _LEADER          # 其餘欄位原樣


def test_disabling_risk_controls_is_applied(env):
    """客戶可以關掉全部風控——那是他的錢包、他的決定（產品裁決 2026-07-30）。"""
    env.write_settings(prefs=_prefs(enabled=False))
    assert env.applier().effective(_BASE).risk_controls_enabled is False


def test_applied_settings_are_persisted_in_the_engine_state_root(env):
    """⭐ 護欄狀態落在**引擎自己的狀態根**（filet-api 寫不到），不是交換目錄。"""
    env.write_settings(issued_at=_at(-10))
    env.applier().effective(_BASE)

    assert env.state_file.exists()
    assert env.state_root in env.state_file.parents
    saved = json.loads(env.state_file.read_text())
    assert saved["issued_at"] == _at(-10)
    assert saved["prefs"]["enabled"] is True


def test_apply_alerts_once_with_the_customer_signed_source(env):
    env.write_settings()
    env.applier().effective(_BASE)
    crits = env.crits()
    assert len(crits) == 1
    assert "客戶簽章" in crits[0][2]


def test_settings_survive_a_restart(env):
    """重啟後（新 applier 物件、記錄檔還在）仍然套用同一份設定，且不再重複告警。"""
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.30"))
    env.applier().effective(_BASE)
    n = env.notifier.records[:]

    cs = env.applier().effective(_BASE)
    assert cs.max_drawdown_pct == Decimal("0.30")
    assert env.notifier.records == n          # 第二輪完全安靜


# ── ⭐⭐ 冪等與回滾護欄 ───────────────────────────────────────────────

def test_same_issued_at_is_silently_idempotent(env):
    """同一筆記錄每輪都會被讀到（沒有人清它）→ 第二輪必須完全安靜。"""
    env.write_settings()
    applier = env.applier()
    applier.effective(_BASE)
    before = len(env.notifier.records)
    for _ in range(3):
        cs = applier.effective(_BASE)
    assert cs.max_drawdown_pct == Decimal("0.2")
    assert len(env.notifier.records) == before


def test_older_issued_at_is_rejected_as_a_rollback(env):
    """⭐⭐ 回滾攻擊：客戶真的簽過的舊記錄被放回去，想換回較弱的保護。

    簽章是真的、內容也是客戶的——只有時間順序擋得住它，所以必須拒絕 ＋ critical。
    """
    applier = env.applier()
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.10"), nonce="new",
                       issued_at=_at(0))
    applier.effective(_BASE)
    env.notifier.records.clear()

    # 攻擊者把三天前那份「風控全關」放回交換目錄。
    env.write_settings(prefs=_prefs(enabled=False), nonce="old",
                       issued_at=_at(-3 * 86400))
    cs = applier.effective(_BASE)

    assert cs.risk_controls_enabled is True                 # 沿用現狀
    assert cs.max_drawdown_pct == Decimal("0.10")
    crits = env.crits()
    assert len(crits) == 1 and "較舊" in crits[0][2]


def test_rollback_guard_survives_a_restart(env):
    """護欄是持久的：重啟後那份舊記錄仍然被擋（否則重啟一次就洗掉護欄）。"""
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.10"), issued_at=_at(0))
    env.applier().effective(_BASE)
    env.write_settings(prefs=_prefs(enabled=False), nonce="old",
                       issued_at=_at(-3 * 86400))
    env.notifier.records.clear()

    cs = env.applier().effective(_BASE)          # 全新的 applier ＝重啟
    assert cs.risk_controls_enabled is True
    assert cs.max_drawdown_pct == Decimal("0.10")
    assert len(env.crits()) == 1


def test_newer_issued_at_replaces_the_previous_intent(env):
    applier = env.applier()
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.10"), issued_at=_at(-100))
    applier.effective(_BASE)
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.30"), nonce="n2",
                       issued_at=_at(0))
    assert applier.effective(_BASE).max_drawdown_pct == Decimal("0.30")


# ── ⭐ 驗章失敗：不套用 ＋ critical ＋ 沿用現狀 ────────────────────────

def test_forged_signature_is_not_applied(env):
    env.write_settings(prefs=_prefs(enabled=False),
                       tamper={"signature": "0x" + "11" * 65})
    cs = env.applier().effective(_BASE)

    assert cs.risk_controls_enabled is True        # 沿用現狀（env 值仍在執法）
    crits = env.crits()
    assert len(crits) == 1 and "驗簽失敗" in crits[0][2]


def test_someone_elses_signature_is_not_applied(env):
    """⭐ 簽章者必須是 manifest 登錄的錢包主人——不是記錄裡的任何欄位。"""
    env.write_settings(prefs=_prefs(enabled=False), signer=Account.create())
    cs = env.applier().effective(_BASE)

    assert cs.risk_controls_enabled is True
    assert any("signer_mismatch" in c[2] for c in env.crits())


def test_verify_failure_alert_leaks_no_signature_material(env):
    """⚠️ 紅線：告警與 log 只帶 reason，不得帶簽章／訊息原文／記錄內容。"""
    rec = env.write_settings(signer=Account.create())
    env.applier().effective(_BASE)
    blob = "".join(r[2] for r in env.notifier.records)
    assert rec["signature"] not in blob
    assert rec["message"] not in blob


def test_tampered_prefs_are_not_applied(env):
    """竄改記錄裡的數值 → 重建原文對不上 → 不套用（不是靜默採用竄改後的值）。"""
    rec = env.write_settings(prefs=_prefs(max_drawdown_pct="0.10"))
    write_risk_settings(env.settings_path,
                        {**rec, "prefs": {**rec["prefs"],
                                          "max_drawdown_pct": "0.5"}})
    cs = env.applier().effective(_BASE)
    assert cs.max_drawdown_pct == Decimal("0.20")     # base，不是 0.5、也不是 0.10
    assert env.crits()


def test_out_of_range_value_is_rejected_and_never_clamped(env):
    """⭐ `max_drawdown_pct=0.9`（上限 0.50）→ 拒絕 ＋ critical，**絕不夾取到 0.50**。"""
    rec = env.write_settings()
    write_risk_settings(env.settings_path,
                        {**rec, "prefs": {**rec["prefs"],
                                          "max_drawdown_pct": "0.9"}})
    cs = env.applier().effective(_BASE)

    assert cs.max_drawdown_pct == Decimal("0.20")     # 沿用現狀
    assert cs.max_drawdown_pct != Decimal("0.50")     # 沒有被夾到邊界
    assert env.crits()


def test_record_for_another_account_is_ignored(env):
    other = Account.create()
    env.write_settings(prefs=_prefs(enabled=False), account_id=_acct(other),
                       signer=other)
    cs = env.applier().effective(_BASE)
    assert cs.risk_controls_enabled is True
    assert env.crits() == []           # 別人的記錄不是事件


# ── 讀取失敗：沿用現狀，只 log 不告警 ──────────────────────────────────

def test_missing_record_file_is_silent(env):
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert env.notifier.records == []


def test_unreadable_record_file_is_silent_and_keeps_status_quo(env):
    """讀取失敗是 transient；這條路徑一旦會告警就會每輪洗版，把真事件淹掉。"""
    env.settings_path.write_text("{ not json")
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert env.notifier.records == []


def test_missing_manifest_alerts_and_keeps_status_quo(env):
    """manifest 拿不到可信 user_address → 沿用現狀 ＋ critical（每次調整都不生效）。"""
    env.write_settings(prefs=_prefs(enabled=False))
    env.manifest.unlink()
    cs = env.applier().effective(_BASE)

    assert cs.risk_controls_enabled is True
    assert any("user_address" in c[2] for c in env.crits())


def test_account_absent_from_manifest_alerts(env):
    """記錄是我的、但 manifest 查無此帳號 ⇒ 沒有可信的比對基準 → 不套用 ＋ critical。"""
    unknown = "f" + "c3" * 20
    env.write_settings(account_id=unknown, prefs=_prefs(enabled=False))
    cs = env.applier(account_id=unknown).effective(_BASE)
    assert cs == _BASE
    assert any("user_address" in c[2] for c in env.crits())


# ── 狀態檔的失效路徑 ──────────────────────────────────────────────────

def test_state_write_failure_blocks_the_apply(env):
    """⭐ **先寫狀態檔再套用**：寫不進去就不套用（套了卻沒記下 issued_at ⇒
    下一輪重複套用，且回滾護欄對這段期間失效）。"""
    env.state_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(env.state_file.parent, 0o500)
    try:
        env.write_settings(prefs=_prefs(max_drawdown_pct="0.30"))
        cs = env.applier().effective(_BASE)
    finally:
        os.chmod(env.state_file.parent, 0o700)

    assert cs.max_drawdown_pct == Decimal("0.20")     # 未套用
    assert any("落檔失敗" in c[2] for c in env.crits())


def test_unreadable_state_file_keeps_status_quo_and_alerts(env):
    """狀態檔讀不到 ⇒ 判不出新舊 ⇒ 不套用任何新記錄 ＋ critical（護欄是回滾的唯一依據）。"""
    env.state_file.parent.mkdir(parents=True, exist_ok=True)
    env.state_file.write_text("{ corrupted")
    env.write_settings(prefs=_prefs(enabled=False))
    cs = env.applier().effective(_BASE)

    assert cs.risk_controls_enabled is True
    assert any("無法載入" in c[2] for c in env.crits())


def test_vanished_state_file_keeps_the_in_memory_settings(env):
    """狀態檔消失 ≠ 從未套用過：沿用記憶體中的現狀 ＋ critical，不打開回滾的門。"""
    applier = env.applier()
    env.write_settings(prefs=_prefs(max_drawdown_pct="0.30"))
    applier.effective(_BASE)
    env.state_file.unlink()
    env.notifier.records.clear()

    cs = applier.effective(_BASE)
    assert cs.max_drawdown_pct == Decimal("0.30")
    assert any("消失" in c[2] for c in env.crits())


def test_tampered_state_file_falls_back_to_env_without_clamping(env):
    """狀態檔被改成超界值 → 拒絕套用 ＋ critical，退回 env 的部署值（仍在執法）。"""
    env.write_settings()
    applier = env.applier()
    applier.effective(_BASE)
    saved = json.loads(env.state_file.read_text())
    saved["prefs"]["max_drawdown_pct"] = "0.95"
    env.state_file.write_text(json.dumps(saved))
    env.notifier.records.clear()

    cs = env.applier().effective(_BASE)
    assert cs.max_drawdown_pct == Decimal("0.20")
    assert applier is not None
    assert any("竄改" in c[2] for c in env.crits())


def test_last_applied_tracks_the_value_used_this_cycle(env):
    """心跳的來源標記必須與本輪真正用來判定風險的那組 settings 出自同一次求值。"""
    applier = env.applier()
    assert applier.effective(_BASE) == _BASE
    assert applier.last_applied is None            # env 預設

    env.write_settings(prefs=_prefs(max_drawdown_pct="0.30"))
    applier.effective(_BASE)
    assert applier.last_applied is not None
    assert applier.last_applied.prefs["max_drawdown_pct"] == "0.3"


def test_notifier_failure_never_breaks_the_cycle(env):
    """告警端掛掉不得中斷跟單——觀測層壞掉絕不能弄停被觀測的系統。"""
    class _Boom(RecordingNotifier):
        def critical(self, *a, **k):
            raise RuntimeError("telegram down")

    env.write_settings(prefs=_prefs(max_drawdown_pct="0.30"))
    cs = env.applier(notifier=_Boom()).effective(_BASE)
    assert cs.max_drawdown_pct == Decimal("0.30")


# ── ⭐⭐ 自助解除熔斷：四道閘 ─────────────────────────────────────────

def test_valid_unlock_removes_the_arm_file(env):
    """⭐ 正常路徑：驗章通過、時效內、晚於熔斷、原因可恢復 → 刪 ARM 檔 ＋ critical。"""
    arm = env.trip(tripped_at=_at(-3600))
    env.write_unlock(issued_at=_at(-10))

    assert env.applier().consume_unlock_request(env.state_root) is True
    assert not arm.exists()
    assert any("解除熔斷鎖定" in c[2] for c in env.crits())


def test_expired_unlock_keeps_the_lock(env):
    """⭐⭐ 一份舊的解鎖記錄若還能生效，客戶等於簽一次就永久放棄了熔斷保護。"""
    arm = env.trip(tripped_at=_at(-60))
    env.write_unlock(issued_at=_at(-RISK_SETTINGS_MAX_AGE_S - 60))

    assert env.applier().consume_unlock_request(env.state_root) is False
    assert arm.exists()
    assert env.crits() == []          # 過期是常態，不是事故（避免每輪洗版）


def test_unlock_signed_before_the_trip_keeps_the_lock(env):
    """⭐⭐ 熔斷**之前**簽好的解鎖請求不得解除這次熔斷（時效擋不住這一格：
    600 秒內先簽好、再讓熔斷觸發，在時效上完全合法）。"""
    arm = env.trip(tripped_at=_at(-10))
    env.write_unlock(issued_at=_at(-300))

    assert env.applier().consume_unlock_request(env.state_root) is False
    assert arm.exists()
    assert any("不晚於" in r[2] for r in env.notifier.records)


def test_leader_revoked_lock_cannot_be_self_unlocked(env):
    """⭐⭐ leader 被撤銷是**治理動作**，不是客戶可以自己作廢的風險事件。"""
    arm = env.trip(reason="leader_revoked", tripped_at=_at(-3600))
    env.write_unlock(issued_at=_at(-10))

    assert env.applier().consume_unlock_request(env.state_root) is False
    assert arm.exists()
    assert any("leader_revoked" in r[2] for r in env.notifier.records)


def test_forged_unlock_keeps_the_lock_and_alerts(env):
    arm = env.trip(tripped_at=_at(-3600))
    env.write_unlock(issued_at=_at(-10), signer=Account.create())

    assert env.applier().consume_unlock_request(env.state_root) is False
    assert arm.exists()
    assert any("解除熔斷請求驗簽失敗" in c[2] for c in env.crits())


def test_unlock_for_another_account_is_ignored(env):
    other = Account.create()
    arm = env.trip(tripped_at=_at(-3600))
    env.write_unlock(account_id=_acct(other), signer=other, issued_at=_at(-10))

    assert env.applier().consume_unlock_request(env.state_root) is False
    assert arm.exists()


def test_unlock_without_a_trip_is_a_no_op(env):
    """沒有鎖可解（最常見的正常狀態）→ 安靜回 False，不炸。"""
    env.write_unlock(issued_at=_at(-10))
    assert env.applier().consume_unlock_request(env.state_root) is False
    assert env.notifier.records == []


def test_unreadable_arm_payload_keeps_the_lock(env):
    """ARM payload 讀不到 ⇒ 證明不了這筆請求晚於熔斷 ⇒ 維持鎖定。"""
    arm = env.trip()
    arm.write_text("{ not json")
    env.write_unlock(issued_at=_at(-10))

    assert env.applier().consume_unlock_request(env.state_root) is False
    assert arm.exists()


def test_unlock_is_one_shot_because_the_arm_file_is_gone(env):
    """⭐ 一次性由 ARM 檔本身達成：解除之後同一筆記錄下一輪走不到刪檔那一步。"""
    env.trip(tripped_at=_at(-3600))
    env.write_unlock(issued_at=_at(-10))
    applier = env.applier()
    assert applier.consume_unlock_request(env.state_root) is True

    arm = env.trip(tripped_at=_at(-5))          # 又熔斷一次（更晚）
    assert applier.consume_unlock_request(env.state_root) is False
    assert arm.exists()


def test_unreadable_unlock_file_never_raises(env):
    env.trip()
    env.unlock_path.write_text("{{{")
    assert env.applier().consume_unlock_request(env.state_root) is False
