"""tests/test_capital_settings_apply.py
引擎側套用客戶簽章的資金設定（spark.filet.capital_settings_apply）。

⭐ 這裡決定**引擎拿客戶的錢押多大**。本檔盯住的四件事，每一件對應一個
「錯了就是真錢」的失效：

(1) **簽章者必須等於 manifest 的 user_address**——不是記錄裡的任何欄位。
(2) **引擎端邊界再驗**——這兩個值直接乘進部位大小，一個壞值就是一次超額曝險。
    引擎不因為「API 驗過」而省略（API 是可能被打穿、可能被繞過的另一個進程）。
(3) **冪等**：同一筆記錄只兌現一次。
(4) **帳本遺失 → 不退回 env 預設值**：退回是一次沒有客戶授權的曝險變更。

用真密碼學（eth_account 本地運算，不觸網）；純檔案操作，全離線。
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.copytrade.config import CopySettings
from spark.copytrade.notifier import RecordingNotifier
from spark.filet.capital_settings import (build_capital_settings_message,
                                          build_capital_settings_record,
                                          canonical_capital_values,
                                          write_capital_settings)
from spark.filet.capital_settings_apply import (AppliedCapital, CapitalLedger,
                                                CapitalSettingsApplier,
                                                CapitalSettingsUnavailable,
                                                ledger_init_marker_path,
                                                load_ledger, save_ledger)

_NOW = 1_800_000_000.0
_LEADER = "0x" + "a1" * 20

# 基礎設定＝env 預設（客戶「還沒授權過任何調整」時該用的值）。
_BASE = CopySettings(leader_address=_LEADER, allocated_capital=Decimal("0"),
                     capital_utilization=Decimal("1.0"))


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


class _Env:
    """一份完整的引擎現場：manifest ＋ 記錄檔 ＋ 帳本 ＋ notifier。"""

    def __init__(self, tmp_path: Path, *, wallet):
        self.tmp = tmp_path
        self.wallet = wallet
        self.account_id = _acct(wallet)
        self.manifest = tmp_path / "followers.json"
        self.manifest.write_text(json.dumps({"followers": [
            {"account_id": self.account_id, "user_address": wallet.address,
             "builder_address": "0x" + "22" * 20, "network": "mainnet",
             "label": "", "leader_address": _LEADER}]}))
        self.settings = tmp_path / "capital_settings.json"
        self.ledger = tmp_path / "state" / "capital_settings_ledger.json"
        self.notifier = RecordingNotifier()

    def applier(self, *, notifier=None, now_s=_NOW, account_id=None):
        return CapitalSettingsApplier(
            account_id=account_id or self.account_id,
            manifest_path=self.manifest, settings_path=self.settings,
            ledger_path=self.ledger, notifier=notifier or self.notifier,
            now_fn=lambda: now_s)

    def write_settings(self, *, alloc="5000", util="0.4", nonce="n1",
                       issued_at=None, signer=None, account_id=None, tamper=None):
        """簽一筆合法記錄並落檔。`tamper` 在簽完之後改動欄位（重放／竄改）。"""
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        _, alloc_s, _, util_s = canonical_capital_values(alloc, util)
        msg = build_capital_settings_message(
            account_id=account_id, allocated_capital=alloc_s,
            capital_utilization=util_s, nonce=nonce, issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_capital_settings_record(
            account_id=account_id, allocated_capital=alloc_s,
            capital_utilization=util_s, nonce=nonce, issued_at=issued_at,
            signature=sig, message=msg)
        if tamper:
            rec.update(tamper)
        write_capital_settings(self.settings, rec)
        return rec

    def crits(self):
        return [r for r in self.notifier.records if r[0] == "critical"]


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path, wallet=Account.create())


# ── ⭐ 快樂路徑：套用 ＋ 告警 ＋ 落帳 ──────────────────────────────────

def test_applies_valid_signed_settings(env):
    """⭐ 合法的客戶簽章記錄 → 套用到 CopySettings。"""
    env.write_settings(alloc="5000", util="0.4")
    cs = env.applier().effective(_BASE)

    assert cs.allocated_capital == Decimal("5000.00")
    assert cs.capital_utilization == Decimal("0.4000")
    assert cs.leader_address == _LEADER          # 其餘欄位原樣


def test_applied_settings_are_recorded_in_the_ledger(env):
    """套用後帳本要同時記下 (a) 已兌現的 nonce → 指紋，(b) 目前生效的 override。
    缺 (a) 會重複套用；缺 (b) 則重啟後靜默退回 env 預設——那是一次**沒有客戶
    授權**的曝險變更。"""
    env.write_settings(alloc="5000", util="0.4", nonce="n-abc")
    env.applier().effective(_BASE)

    ledger = load_ledger(env.ledger)
    assert ledger.redeemed == {"n-abc": "5000.00|0.4000"}
    assert ledger.applied is not None
    assert ledger.applied.nonce == "n-abc"
    assert ledger.applied.as_decimals() == (Decimal("5000.00"), Decimal("0.4000"))


def test_apply_alerts_with_old_new_and_source(env):
    """critical 必須含舊值、新值與**「客戶簽章授權」的來源標記**——曝險變更要留痕，
    來源標記讓操作者一眼分辨「客戶自己要求的」與「有人改了設定檔」。"""
    env.write_settings(alloc="5000", util="0.4")
    env.applier().effective(_BASE)
    crits = env.crits()
    assert len(crits) == 1
    text = crits[0][2]
    assert "5000.00" in text and "0.4000" in text
    assert "客戶簽章授權" in text


def test_apply_alert_states_no_forced_rebalance(env):
    """⭐ 「下一 cycle 生效、不做即時強制再平衡」必須寫在告警裡：操作者看到部位
    沒有立刻變化時，這行字決定他是「正常」還是「開工單查為什麼沒生效」。"""
    env.write_settings()
    env.applier().effective(_BASE)
    text = env.crits()[0][2]
    assert "下一個 cycle" in text
    assert "不做即時強制再平衡" in text


def test_override_survives_process_restart(env):
    """⭐ 帳本裡的 override 在**沒有任何記錄檔**時仍然成立（模擬重啟後、記錄已清）。
    否則每次重啟都會退回 env 預設的曝險。"""
    save_ledger(env.ledger, CapitalLedger(
        redeemed={"n1": "5000.00|0.4000"},
        applied=AppliedCapital("n1", "5000.00", "0.4000", _at(), _NOW)))
    cs = env.applier().effective(_BASE)
    assert cs.allocated_capital == Decimal("5000.00")
    assert cs.capital_utilization == Decimal("0.4000")
    assert env.crits() == []                    # 沿用現狀不告警


def test_no_record_is_completely_silent(env):
    """最常見的路徑（絕大多數 cycle 沒有待套用的記錄）：原樣回傳 base、零告警。"""
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert env.crits() == []


# ── ⭐⭐ 引擎端邊界再驗（本 commit 的核心） ───────────────────────────

@pytest.mark.parametrize("alloc,util", [
    ("0", "0.5"), ("-100", "0.5"), ("5000", "0"), ("5000", "-0.2"),
    ("5000", "1.5"), ("5000", "10"),
])
def test_engine_rejects_out_of_range_even_with_valid_signature(env, alloc, util):
    """⭐⭐ 簽章完全合法、數值超界 → **拒絕套用 ＋ critical**，沿用現狀。

    這是本模組存在的核心理由：API 也擋這些值，但 API 是一個可能被打穿、可能有
    bug、可能被繞過（直接寫交換目錄）的進程。這兩個值直接乘進部位大小，
    `capital_utilization = 10` 就是十倍曝險，而下單那一刻看起來完全正常。
    """
    env.write_settings(alloc=alloc, util=util)
    cs = env.applier().effective(_BASE)

    assert cs == _BASE                          # 完全沒套用
    crits = env.crits()
    assert len(crits) == 1
    assert "超出允許範圍" in crits[0][2]
    assert load_ledger(env.ledger).applied is None   # 也沒落帳


def test_engine_never_clamps_out_of_range_values(env):
    """⭐⭐ 超界一律拒絕，**絕不夾取**。夾取讓流程順利跑完，代價是引擎執行了一個
    客戶沒有簽署的數字——而且沒有人會知道。"""
    # base 用一個**與上界不同**的值：預設的 1.0 在數值上等於「夾到上界」的結果，
    # 用它當基準就分不出「沿用現狀」與「被夾成 1」——兩者都會讓斷言通過。
    base = CopySettings(leader_address=_LEADER, allocated_capital=Decimal("100"),
                        capital_utilization=Decimal("0.3"))
    env.write_settings(alloc="5000", util="10")
    cs = env.applier().effective(base)
    assert cs.capital_utilization == Decimal("0.3")     # 沿用現狀
    assert cs.capital_utilization != Decimal("1")       # 沒有被夾到上界
    assert cs.allocated_capital == Decimal("100")       # 也沒有部分套用


def test_out_of_range_does_not_burn_the_nonce(env):
    """超界拒絕之後，客戶用同一顆 nonce 重簽一組合法值仍然可以套用——超界是
    政策判斷，不該連帶作廢那張授權。"""
    env.write_settings(alloc="5000", util="10")
    env.applier().effective(_BASE)
    env.write_settings(alloc="5000", util="0.4", nonce="n1")
    cs = env.applier().effective(_BASE)
    assert cs.capital_utilization == Decimal("0.4000")


def test_engine_revalidates_bounds_from_the_ledger_each_cycle(env):
    """⭐⭐ 帳本裡的值**每輪都要重驗**：帳本也可能被竄改（狀態目錄權限出問題、
    備份還原了一份壞檔），而它每一輪都會被乘進部位大小。
    「寫進帳本時驗過了」不保證「現在讀出來的還是那個值」。

    超界的帳本 → 拒絕套用**且拒絕退回 base**（兩者都是無授權的曝險變更）→
    呼叫端本輪零交易動作。
    """
    save_ledger(env.ledger, CapitalLedger(
        redeemed={"n1": "5000.00|9.0000"},
        applied=AppliedCapital("n1", "5000.00", "9.0000", _at(), _NOW)))
    with pytest.raises(CapitalSettingsUnavailable):
        env.applier().effective(_BASE)
    assert any("帳本疑遭竄改" in c[2] for c in env.crits())


# ── ⭐ 簽章：信任來源與竄改 ───────────────────────────────────────────

def test_rejects_wrong_signer(env):
    """⭐ 簽章者不是 manifest 登錄的 user_address → 拒絕 ＋ critical，不套用。"""
    env.write_settings(signer=Account.create())
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert any("驗簽失敗" in c[2] for c in env.crits())


def test_alert_carries_only_the_reason_code_not_the_record(env):
    """⚠️ 紅線 3：告警只帶機器可讀的 reason，**不帶簽章、不帶記錄內容**。"""
    rec = env.write_settings(signer=Account.create())
    env.applier().effective(_BASE)
    text = env.crits()[0][2]
    assert rec["signature"] not in text
    assert rec["message"] not in text
    assert "reason=" in text


def test_tampered_amount_is_rejected(env):
    """簽完之後把使用比例改大 → 重建訊息不同 → 簽章者對不上 → 拒絕。
    這是本模組要防的核心攻擊。"""
    env.write_settings(alloc="5000", util="0.2",
                       tamper={"capital_utilization": "1.0000"})
    cs = env.applier().effective(_BASE)
    assert cs == _BASE


def test_manifest_is_the_only_trusted_signer_source(env):
    """manifest 查無此帳號 → 沒有可信基準 → 不套用（fail-closed，不告警洗版）。"""
    env.write_settings()
    other = "f" + "9" * 40
    cs = env.applier(account_id=other).effective(_BASE)
    assert cs == _BASE


def test_missing_manifest_does_not_apply(env):
    env.write_settings()
    env.manifest.unlink()
    assert env.applier().effective(_BASE) == _BASE


def test_expired_signature_is_rejected(env):
    env.write_settings(issued_at=_at(-3600))
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert any("驗簽失敗" in c[2] for c in env.crits())


# ── ⭐ 冪等 ───────────────────────────────────────────────────────────

def test_second_cycle_does_not_reapply(env):
    """⭐ 同一筆記錄第二輪不得重複套用（否則每輪一則 critical，把真事件淹掉）。"""
    env.write_settings(alloc="5000", util="0.4")
    first = env.applier().effective(_BASE)
    second = env.applier().effective(_BASE)

    assert first == second                       # 值一樣
    assert len(env.crits()) == 1                 # 只告警一次


def test_repeated_cycles_are_stable(env):
    """連跑五輪：值穩定、告警只有一則、帳本不變。"""
    env.write_settings(alloc="5000", util="0.4")
    applier = env.applier()
    results = [applier.effective(_BASE) for _ in range(5)]
    assert len({(r.allocated_capital, r.capital_utilization) for r in results}) == 1
    assert len(env.crits()) == 1


def test_same_nonce_with_different_values_is_flagged_as_tampering(env):
    """同一顆 nonce 配上**不同數值**再次出現 ⇒ 竄改（不是正常的檔案殘留）→
    拒絕 ＋ 指名事件的告警。籠統的「驗簽失敗」會被讀成客戶簽壞了。"""
    env.write_settings(alloc="5000", util="0.4", nonce="n1")
    env.applier().effective(_BASE)
    env.write_settings(alloc="5000", util="0.9", nonce="n1")   # 同 nonce、新數值
    cs = env.applier().effective(_BASE)

    assert cs.capital_utilization == Decimal("0.4000")         # 沿用已套用的
    assert any("疑遭竄改" in c[2] for c in env.crits())


def test_new_nonce_applies_a_new_value(env):
    """客戶再調一次（新 nonce）→ 正常套用新值。"""
    env.write_settings(alloc="5000", util="0.4", nonce="n1")
    env.applier().effective(_BASE)
    env.write_settings(alloc="8000", util="0.9", nonce="n2")
    cs = env.applier().effective(_BASE)
    assert cs.allocated_capital == Decimal("8000.00")
    assert cs.capital_utilization == Decimal("0.9000")


# ── ⭐⭐ 帳本遺失：fail-closed，不退回預設 ────────────────────────────

def test_lost_ledger_refuses_to_fall_back_to_defaults(env):
    """⭐⭐ 帳本曾經存在、現在不見了 → raise ＋ critical，**絕不退回 env 預設**。

    退回預設是一次沒有客戶授權的曝險變更，而且全程安靜——這正是整個模組要防的事。
    """
    save_ledger(env.ledger, CapitalLedger(
        redeemed={"n1": "5000.00|0.4000"},
        applied=AppliedCapital("n1", "5000.00", "0.4000", _at(), _NOW)))
    ledger_init_marker_path(env.ledger).write_text("{}")
    env.ledger.unlink()                          # 帳本不見了，標記還在

    with pytest.raises(CapitalSettingsUnavailable):
        env.applier().effective(_BASE)
    crits = env.crits()
    assert any("帳本遺失" in c[2] for c in crits)
    assert any("拒絕退回環境預設值" in c[2] for c in crits)


def test_first_start_is_not_treated_as_loss(env):
    """真正的首次啟動（帳本無、標記也無）→ 安靜建立初始帳本並繼續。
    此刻沒有 override 可以失去，把它升級成失敗只會擋住跟單。"""
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert env.crits() == []
    assert env.ledger.exists()
    assert ledger_init_marker_path(env.ledger).exists()


def test_corrupt_ledger_fails_closed(env):
    """格式壞掉一律 raise，**絕不當成空帳本**：當成空帳本會同時忘記已兌現的 nonce
    （可重放）與 applied（悄悄退回預設曝險）。"""
    env.ledger.parent.mkdir(parents=True, exist_ok=True)
    env.ledger.write_text("{ not json")
    with pytest.raises(CapitalSettingsUnavailable):
        env.applier().effective(_BASE)
    assert any("無法載入" in c[2] for c in env.crits())


def test_ledger_with_unparsable_amounts_fails_closed(env):
    """帳本裡的數值解不出 Decimal → fail-closed，而不是把壞值一路帶進 sizing。"""
    env.ledger.parent.mkdir(parents=True, exist_ok=True)
    env.ledger.write_text(json.dumps({"redeemed": {}, "applied": {
        "nonce": "n1", "allocated_capital": "abc", "capital_utilization": "0.4",
        "issued_at": _at(), "applied_at": _NOW}}))
    with pytest.raises(ValueError):
        load_ledger(env.ledger)


def test_ledger_write_failure_does_not_apply(env, monkeypatch):
    """⭐ 先落帳再套用：落帳失敗 ⇒ 不套用（否則下一輪會重複套用、重複告警）。"""
    env.write_settings()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("spark.filet.capital_settings_apply.save_ledger", boom)
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert any("落檔失敗" in c[2] for c in env.crits())


def test_unreadable_records_file_is_transient_and_silent(env):
    """記錄檔壞掉是 transient：只 log 不告警（絕大多數 cycle 沒有記錄，
    這條路徑一旦告警就會每輪洗版，真正的 critical 反而被淹沒）。"""
    env.settings.write_text("{ not json")
    cs = env.applier().effective(_BASE)
    assert cs == _BASE
    assert env.crits() == []


def test_alert_failure_does_not_break_the_cycle(env):
    """觀測層壞掉絕不能弄停被觀測的系統：notifier 拋例外時仍要正常回傳設定。"""
    class _Boom:
        def critical(self, *_a, **_k):
            raise RuntimeError("telegram down")

        def info(self, *_a, **_k):
            raise RuntimeError("telegram down")

    env.write_settings(alloc="5000", util="0.4")
    cs = env.applier(notifier=_Boom()).effective(_BASE)
    assert cs.capital_utilization == Decimal("0.4000")


# ── 與換 leader 同輪 ──────────────────────────────────────────────────

def test_capital_and_leader_change_apply_in_the_same_cycle(env, tmp_path):
    """⭐ 同一輪內換 leader 與資金設定**都能套用**，互不干擾。

    兩者改的是 `CopySettings` 的不同欄位（leader_address vs 資金兩欄），且用
    **各自的**記錄檔與**各自的**帳本，所以沒有先後相依。run_copytrade 的接線是
    「先解析 leader（LeaderWatch）→ 再套資金設定」——這個順序不是正確性需求，
    而是為了讓「本輪最終使用的 settings」只有一個產生點。
    """
    from spark.filet.leader_change import (build_leader_change_message,
                                           build_leader_change_record,
                                           write_leader_change)
    from spark.filet.leader_change_apply import LeaderChangeApplier
    from spark.filet.leader_resolve import (SOURCE_CUSTOMER_SIGNED, SOURCE_MANIFEST,
                                            LeaderResolution)

    new_leader = "0x" + "d4" * 20
    leaders = tmp_path / "leaders.json"
    leaders.write_text(json.dumps({"leaders": [{"address": _LEADER, "name": "A"},
                                               {"address": new_leader, "name": "D"}]}))
    # 換 leader 記錄（自己的檔）
    issued_at = _at()
    lmsg = build_leader_change_message(account_id=env.account_id,
                                       leader_address=new_leader,
                                       nonce="L1", issued_at=issued_at)
    lrec = build_leader_change_record(
        account_id=env.account_id, leader_address=new_leader, nonce="L1",
        issued_at=issued_at,
        signature=env.wallet.sign_message(encode_defunct(text=lmsg)).signature.hex(),
        message=lmsg)
    changes = tmp_path / "leader_changes.json"
    write_leader_change(changes, lrec)
    # 資金設定記錄（另一個檔）
    env.write_settings(alloc="5000", util="0.4", nonce="C1")

    leader_applier = LeaderChangeApplier(
        account_id=env.account_id, manifest_path=env.manifest,
        leaders_path=leaders, changes_path=changes,
        ledger_path=tmp_path / "state" / "leader_change_ledger.json",
        notifier=env.notifier, now_fn=lambda: _NOW)

    res = leader_applier.effective(LeaderResolution(_LEADER, SOURCE_MANIFEST))
    cs = env.applier().effective(
        CopySettings(leader_address=res.address,
                     allocated_capital=_BASE.allocated_capital,
                     capital_utilization=_BASE.capital_utilization))

    assert res == LeaderResolution(new_leader, SOURCE_CUSTOMER_SIGNED)
    assert cs.leader_address == new_leader                  # 換 leader 生效
    assert cs.capital_utilization == Decimal("0.4000")      # 資金設定也生效


def test_ledgers_are_separate_files(env, tmp_path):
    """兩份帳本分開落檔：資金設定的寫入問題不得讓換 leader 的帳本也讀不出來。"""
    from spark.filet.capital_settings_apply import LEDGER_RELPATH as CAP
    from spark.filet.leader_change_apply import LEDGER_RELPATH as LC
    assert CAP != LC
