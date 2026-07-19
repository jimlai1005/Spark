"""tests/test_leader_change_apply.py
引擎側套用客戶簽章的換 leader（spark.filet.leader_change_apply）。

⭐ 這是全 repo 風險最高的一段之一：**它決定引擎拿客戶的錢跟誰交易**。本檔盯住的
三件事，每一件對應一個「錯了就是真錢」的失效：

(1) **簽章者必須等於 manifest 的 user_address**——不是記錄裡的任何欄位。破了這條，
    任何能寫記錄檔的人（被打穿的 API、內部誤操作）都能把客戶指向他挑的 leader。
(2) **冪等**：同一筆記錄只能兌現一次。破了這條，每個 cycle 重複套用一次，每次都是
    一輪真實的平舊開新（taker 成本），而且看起來一切正常。
(3) **撤銷優先**：安全動作（收尾）壓過便利動作（換 leader）。

用真密碼學（eth_account 本地運算，不觸網，沿 test_leader_change.py 慣例）；
純檔案操作，全離線。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.copytrade.notifier import RecordingNotifier
from spark.filet.leader_change import (build_leader_change_message,
                                       build_leader_change_record,
                                       write_leader_change)
from spark.filet.leader_change_apply import (DEFAULT_LEADER_CHANGES_PATH,
                                             AppliedChange, Ledger,
                                             LeaderChangeApplier,
                                             ledger_init_marker_path, load_ledger,
                                             resolve_changes_path, save_ledger)
from spark.filet.leader_resolve import (SOURCE_CUSTOMER_SIGNED, SOURCE_MANIFEST,
                                        LeaderResolution, LeaderResolutionError,
                                        LeaderRevokedError)

_OLD_LEADER = "0x" + "a1" * 20   # manifest 登錄的（base 解析結果）
_NEW_LEADER = "0x" + "d4" * 20   # 客戶簽章要換到的
_THIRD = "0x" + "c3" * 20
_NOW = 1_800_000_000.0

_BASE = LeaderResolution(_OLD_LEADER, SOURCE_MANIFEST)
_ALLOWLIST = [{"address": _OLD_LEADER, "name": "Alpha"},
              {"address": _NEW_LEADER, "name": "Delta"},
              {"address": _THIRD, "name": "Charlie"}]


def _at(offset_s: float = 0.0) -> str:
    """epoch 秒 → 記錄用的 ISO8601 UTC（時效比較的兩端同源，工程原則 1）。"""
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


class _Env:
    """一份完整的引擎現場：manifest ＋ 白名單 ＋ 記錄檔 ＋ 帳本 ＋ notifier。"""

    def __init__(self, tmp_path: Path, *, wallet, allowlist=None):
        self.tmp = tmp_path
        self.wallet = wallet
        self.account_id = _acct(wallet)
        self.manifest = tmp_path / "followers.json"
        self.manifest.write_text(json.dumps({"followers": [
            {"account_id": self.account_id, "user_address": wallet.address,
             "builder_address": "0x" + "22" * 20, "network": "mainnet",
             "label": "", "leader_address": _OLD_LEADER}]}))
        self.leaders = tmp_path / "leaders.json"
        self.set_allowlist(_ALLOWLIST if allowlist is None else allowlist)
        self.changes = tmp_path / "leader_changes.json"
        self.ledger = tmp_path / "state" / "leader_change_ledger.json"
        self.notifier = RecordingNotifier()

    def set_allowlist(self, entries):
        self.leaders.write_text(json.dumps({"leaders": entries}))

    def applier(self, *, notifier=None, now_s=_NOW, account_id=None):
        return LeaderChangeApplier(
            account_id=account_id or self.account_id,
            manifest_path=self.manifest, leaders_path=self.leaders,
            changes_path=self.changes, ledger_path=self.ledger,
            notifier=notifier or self.notifier, now_fn=lambda: now_s)

    def write_change(self, *, leader=_NEW_LEADER, nonce="n1", issued_at=None,
                     signer=None, account_id=None, tamper=None):
        """簽一筆合法記錄並落檔。`tamper` 用來在簽完之後改動欄位（重放攻擊）。"""
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        msg = build_leader_change_message(account_id=account_id,
                                          leader_address=leader, nonce=nonce,
                                          issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_leader_change_record(account_id=account_id,
                                         leader_address=leader, nonce=nonce,
                                         issued_at=issued_at, signature=sig,
                                         message=msg)
        if tamper:
            rec.update(tamper)
        write_leader_change(self.changes, rec)
        return rec

    def crits(self):
        return [r for r in self.notifier.records if r[0] == "critical"]


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path, wallet=Account.create())


# ── ⭐ 快樂路徑：套用 ＋ 告警 ＋ 落帳 ──────────────────────────────────

def test_applies_valid_signed_change(env):
    """⭐ 合法的客戶簽章記錄 → 套用，且 critical 含**舊 leader、新 leader、
    「客戶簽章授權」的來源標記**（換 leader 有實際 taker 成本，必須留痕；來源標記
    讓操作者一眼分辨「客戶自己要求的」與「有人改了 manifest」）。"""
    env.write_change()
    res = env.applier().effective(_BASE)

    assert res == LeaderResolution(_NEW_LEADER, SOURCE_CUSTOMER_SIGNED)
    crits = env.crits()
    assert len(crits) == 1
    text = crits[0][2]
    assert _OLD_LEADER in text and _NEW_LEADER in text
    assert "客戶簽章授權" in text


def test_applied_change_is_recorded_in_the_ledger(env):
    """套用後帳本要同時記下 (a) 已兌現的 nonce → leader，(b) 目前生效的 override。
    缺 (a) 會重複套用；缺 (b) 則重啟後靜默退回 manifest 的舊 leader
    ——那是一次**沒有客戶授權**的換手。"""
    env.write_change(nonce="n-abc")
    env.applier().effective(_BASE)

    ledger = load_ledger(env.ledger)
    assert ledger.redeemed == {"n-abc": _NEW_LEADER}
    assert ledger.applied is not None
    assert ledger.applied.nonce == "n-abc"
    assert ledger.applied.leader_address == _NEW_LEADER


def test_override_survives_process_restart(env):
    """⭐ 帳本裡的 override 在**沒有任何記錄檔**時仍然成立（模擬重啟後、記錄已被清）。
    否則每次重啟都會退回 manifest 的舊 leader，然後（若記錄還在）第一輪再換回來
    ——一次沒有意義的部位收斂，外加每次重啟一則假的「leader 已變更」告警。"""
    save_ledger(env.ledger, Ledger(
        redeemed={"n1": _NEW_LEADER},
        applied=AppliedChange("n1", _NEW_LEADER, _at(), _NOW)))
    res = env.applier().effective(_BASE)
    assert res == LeaderResolution(_NEW_LEADER, SOURCE_CUSTOMER_SIGNED)
    assert env.crits() == []       # 沿用現狀不是事件，不得告警


# ── ⭐⭐ 信任錨：簽章者必須是 manifest 的 user_address ──────────────────

def test_signer_other_than_manifest_user_address_is_rejected(env):
    """⭐⭐ 本檔最重要的一條。攻擊者**完整地**簽了一筆格式無懈可擊的記錄（正確的
    account_id、白名單內的 leader、新鮮的時間戳），只是那把私鑰不是客戶的。

    唯一擋得住他的是「recover 出的簽章者 == manifest 登錄的 user_address」。拿掉這
    一條，任何能寫記錄檔的人（被打穿的 API 進程、內部誤操作）就能把客戶的資金指向
    他挑的 leader，而白名單全程放行——因為白名單回答的是「這個 leader 可不可接受」，
    不是「這位客戶真的要求換到他嗎」。
    """
    env.write_change(signer=Account.create())      # 別人的私鑰
    res = env.applier().effective(_BASE)

    assert res == _BASE                            # 不套用，繼續跟舊 leader
    assert load_ledger(env.ledger).applied is None
    crits = env.crits()
    assert len(crits) == 1 and "驗簽失敗" in crits[0][2]


def test_verification_failure_alert_leaks_no_key_material(env):
    """⭐ 紅線 3：驗簽失敗的告警**不得包含簽章或任何密鑰材料**。

    做法是「只讓機器可讀的 reason 通過」，而不是「記得別把 signature 塞進去」——
    結構性成立，不靠記性（工程原則 5 的精神）。
    """
    rec = env.write_change(signer=Account.create())
    env.applier().effective(_BASE)
    text = env.crits()[0][2]
    assert rec["signature"] not in text
    assert rec["signature"][2:20] not in text     # 連片段都不得出現
    assert rec["message"] not in text
    assert "signer_mismatch" in text              # 但診斷資訊仍然足夠


def test_change_for_another_account_is_ignored(env):
    """記錄屬於別的 account_id → 不是我的意圖，靜靜忽略（不告警）。"""
    other = Account.create()
    env.write_change(account_id=_acct(other), signer=other)
    assert env.applier().effective(_BASE) == _BASE
    assert env.crits() == []


def test_manifest_without_my_account_refuses_to_apply(env):
    """⭐ manifest 查無此帳號 ⇒ **沒有可信的比對基準** → 一律不套用（fail-closed）。
    「先驗了再說」在這裡等於用記錄自己的內容驗它自己，那是假驗證。"""
    env.manifest.write_text(json.dumps({"followers": []}))
    env.write_change()
    assert env.applier().effective(_BASE) == _BASE
    assert load_ledger(env.ledger).applied is None


def test_expired_signature_is_rejected(env):
    """過期的意圖不得兌現（換手有實際成本，三小時前的意圖不代表現在的意圖）。"""
    env.write_change(issued_at=_at(-3600))
    assert env.applier().effective(_BASE) == _BASE
    assert len(env.crits()) == 1 and "驗簽失敗" in env.crits()[0][2]


# ── ⭐⭐ 冪等：同一筆記錄只能兌現一次 ─────────────────────────────────

def test_second_cycle_does_not_reapply(env):
    """⭐⭐ 記錄檔沒有人負責清，所以同一筆記錄會在**每一個 cycle** 被讀到。
    第二輪起必須：不重複套用、不再告警、帳本不變。

    破了這條的症狀特別惡劣：每輪一次真實的平舊開新（taker 成本 ＋ 滑價），而每一輪
    的告警內容都一模一樣、看起來像「換 leader 成功」——沒有任何跡象顯示出了問題。
    引擎讀不到 API 的 SQLite，所以這條**只能**靠引擎自己的已兌現帳本成立。
    """
    env.write_change(nonce="n1")
    first = env.applier().effective(_BASE)
    assert first == LeaderResolution(_NEW_LEADER, SOURCE_CUSTOMER_SIGNED)
    assert len(env.crits()) == 1
    before = env.ledger.read_text()

    for _ in range(3):                                   # 再跑三輪
        assert env.applier().effective(_BASE) == first   # 位址不變
    assert len(env.crits()) == 1                         # ⭐ 沒有新的告警
    assert env.ledger.read_text() == before              # ⭐ 帳本沒有再被寫


def test_restored_backup_replay_is_ignored_silently(env):
    """已兌現的 nonce 再次出現（例如有人把舊的記錄檔還原回去）→ 忽略且**不告警**：
    那是正常的檔案殘留，不是攻擊。這條路徑會在每個 cycle 走到，一旦會告警就是永遠
    在洗版，真正的 critical 反而被淹沒。"""
    env.write_change(nonce="n1")
    env.applier().effective(_BASE)
    env.notifier.records.clear()

    env.changes.unlink()
    env.write_change(nonce="n1")                # 同一顆 nonce、同一個 leader 被還原
    assert env.applier().effective(_BASE).address == _NEW_LEADER
    assert env.crits() == []                    # ⭐ 安靜


def test_same_nonce_with_a_different_leader_is_rejected_loudly(env):
    """⭐ 同一顆已兌現的 nonce 卻配上**另一個** leader → 竄改 → 拒絕 ＋ critical。

    簽章比對其實也會擋下它，但那會報成籠統的「驗簽失敗」，分不出「客戶簽壞了」
    與「有人在動這個檔」——後者需要有人去看那台機器。
    """
    env.write_change(nonce="n1", leader=_NEW_LEADER)
    env.applier().effective(_BASE)
    env.notifier.records.clear()

    env.changes.unlink()
    env.write_change(nonce="n1", leader=_THIRD)   # 同 nonce、換 leader（重新簽也一樣）
    res = env.applier().effective(_BASE)

    assert res.address == _NEW_LEADER             # 維持已套用的那個，不動
    crits = env.crits()
    assert len(crits) == 1 and "竄改" in crits[0][2]


# ── 白名單（引擎的述詞是 is_still_permitted）──────────────────────────

def test_new_leader_disabled_is_not_applied(env):
    """新 leader 已被安全撤銷（enabled=false）→ 不套用 ＋ critical，繼續跟舊 leader。
    簽章是有效的；擋下它的是白名單——能寫記錄的人與能寫白名單的人是兩個信任域。"""
    env.set_allowlist([{"address": _OLD_LEADER, "name": "Alpha"},
                       {"address": _NEW_LEADER, "name": "Delta", "enabled": False}])
    env.write_change()
    assert env.applier().effective(_BASE) == _BASE
    assert load_ledger(env.ledger).applied is None
    assert len(env.crits()) == 1 and "白名單" in env.crits()[0][2]


def test_new_leader_not_in_allowlist_is_not_applied(env):
    env.set_allowlist([{"address": _OLD_LEADER, "name": "Alpha"}])
    env.write_change()
    assert env.applier().effective(_BASE) == _BASE
    assert len(env.crits()) == 1


def test_routine_delisting_does_not_block_the_change(env):
    """⚠️ 引擎的述詞是 `is_still_permitted`（只看 enabled），**不是** `is_selectable`。

    accepting_new=false 是例行下架（名額滿／準備退場），對「已經在跟／剛被客戶選定
    的人」無影響。用錯述詞的實作會在這裡把一個合法變更擋掉——而客戶在 web 上明明
    選得到他（select 端點才是 is_selectable 的地方）。
    """
    env.set_allowlist([{"address": _OLD_LEADER, "name": "Alpha"},
                       {"address": _NEW_LEADER, "name": "Delta",
                        "accepting_new": False}])
    env.write_change()
    assert env.applier().effective(_BASE).address == _NEW_LEADER


def test_missing_allowlist_blocks_the_change_without_winding_down(env):
    """白名單檔不存在 → transient：不套用、不告警洗版、**絕不**升級成撤銷收尾
    （一次 IO 打嗝不該換來全體平倉，沿 leader_resolve 的既有分類）。"""
    env.leaders.unlink()
    env.write_change()
    assert env.applier().effective(_BASE) == _BASE
    assert env.crits() == []


# ── ⭐⭐ 撤銷優先（安全動作壓過便利動作）──────────────────────────────

def test_revocation_wins_over_a_valid_pending_change(env):
    """⭐⭐ 同一輪既有撤銷、又有一筆完全合法的客戶簽章變更 → **撤銷優先**。

    這個優先序是結構性的：接線是 `applier.effective(base_resolve())`，
    `base_resolve()` 在參數位置先被求值並 raise LeaderRevokedError，applier 一行
    都不會跑。錯誤代價不對稱——晚一輪換 leader 是客戶多等幾十秒；晚一輪收尾是
    繼續跟著一個**已知出事**的 leader 下單。
    """
    env.write_change()                       # 合法變更就緒

    def _base():
        raise LeaderRevokedError("manifest 的 leader 已被撤銷")

    applier = env.applier()
    with pytest.raises(LeaderRevokedError):
        applier.effective(_base())           # 與正式接線同一個求值順序

    assert load_ledger(env.ledger).applied is None   # ⭐ 變更完全沒有被套用
    assert env.crits() == []                          # 告警由收尾路徑負責，不在這裡


def test_applied_leader_later_revoked_winds_down_instead_of_reverting(env):
    """⭐ 已套用的（客戶授權的）leader 事後被撤銷 → raise LeaderRevokedError ⇒ 收尾。

    **刻意不是**「退回 manifest 的舊 leader」：客戶授權跟的人出事了，自作主張把他
    換到另一個，本身就是一次沒有授權的換手——正是本模組存在的理由所要防的事。
    """
    env.write_change()
    assert env.applier().effective(_BASE).address == _NEW_LEADER

    env.set_allowlist([{"address": _OLD_LEADER, "name": "Alpha"},
                       {"address": _NEW_LEADER, "name": "Delta", "enabled": False}])
    with pytest.raises(LeaderRevokedError):
        env.applier().effective(_BASE)


# ── 最常見的路徑：什麼都沒有 ──────────────────────────────────────────

def test_no_record_is_silent_and_changes_nothing(env):
    """⭐ 絕大多數 cycle 都走這條（沒有人在換 leader）：行為不變、**零告警**。
    這條路徑一旦會吵，告警就會失去意義。

    ⚠️ 2026-07-19 C2 修法後，這條路徑**會**在首次啟動落下一份空帳本（那份帳本正是
    「此後缺席＝遺失」的前提，見上方帳本遺失段）。原本的 `not ledger.exists()`
    斷言編碼的是舊契約，已換成「內容為空 ＋ 仍然零告警」——同等強度地釘住
    「這條路徑不改變任何行為、不吵」，但不再要求它把狀態目錄留成空白。
    """
    assert env.applier().effective(_BASE) == _BASE
    assert env.notifier.records == []
    assert load_ledger(env.ledger) == Ledger({}, None)   # 空帳本＝什麼都沒發生


def test_empty_changes_file_is_silent(env):
    env.changes.write_text(json.dumps({"changes": []}))
    assert env.applier().effective(_BASE) == _BASE
    assert env.notifier.records == []


# ── 壞檔：transient，沿用現狀、不崩潰 ─────────────────────────────────

@pytest.mark.parametrize("content", [
    "{not json",                    # 半個檔（寫到一半）
    "[1, 2, 3]",                    # 頂層不是物件
    '{"changes": "nope"}',          # changes 不是陣列
    '{"changes": [42]}',            # 條目不是物件
])
def test_malformed_change_file_keeps_the_status_quo(env, content):
    """記錄檔壞掉 → transient：log ＋ 沿用現狀，不崩潰、不告警洗版。"""
    env.changes.write_text(content)
    assert env.applier().effective(_BASE) == _BASE
    assert env.crits() == []


def test_malformed_change_file_keeps_a_previously_applied_override(env):
    """壞檔時「沿用現狀」指的是**已套用的 override**，不是 manifest 的舊 leader
    ——後者會是一次沒有客戶授權的換手。"""
    save_ledger(env.ledger, Ledger(
        redeemed={"n1": _NEW_LEADER},
        applied=AppliedChange("n1", _NEW_LEADER, _at(), _NOW)))
    env.changes.write_text("{not json")
    assert env.applier().effective(_BASE).address == _NEW_LEADER


def test_broken_ledger_is_transient_not_a_silent_revert(env):
    """⭐ 帳本壞掉 → raise LeaderResolutionError（transient），**不是**回退 base。

    帳本讀不到 ⇒ 既不知道哪些 nonce 兌現過，也不知道目前生效的 override 是誰。
    此時安靜地回 manifest 的 leader 會是一次無授權換手；宣告「本輪解析沒有結論」
    才對——LeaderWatch 會沿用上一個已驗證值並大聲告警。
    """
    env.ledger.parent.mkdir(parents=True, exist_ok=True)
    env.ledger.write_text("{not json")
    with pytest.raises(LeaderResolutionError) as e:
        env.applier().effective(_BASE)
    assert not isinstance(e.value, LeaderRevokedError)   # 不得誤升級成撤銷


# ── ⭐⭐ 帳本遺失（檔案不存在）≠ 首次啟動 ─────────────────────────────
# 2026-07-19 opus 對抗性審查 C2：`applied` 是客戶授權 override 唯一的持久化來源
# （manifest 不會被更新），所以帳本一被刪掉，引擎就會安靜地把客戶換回 manifest 的
# 舊 leader——一次沒有授權的換手，外加真實的平舊開新成本。審查者實跑重現：刪帳本 →
# 下一輪 source=manifest、alerts raised this cycle: 0。舊碼對「格式壞」fail-closed，
# 對「檔案不存在」沒有任何防線，而這一格正是既有測試沒覆蓋的。


def test_ledger_loss_after_an_applied_override_never_reverts_to_manifest(env):
    """⭐⭐ 本檔最重要的新增條目：**已套用 override 後帳本消失 → 不得退回 base**。

    這條盯的不是「讀檔失敗」而是「檔案不見了」——舊碼把它與首次啟動混為一談，
    於是 `applied is None` → 回 manifest 的舊 leader，全程零告警。
    """
    env.write_change()
    assert env.applier().effective(_BASE).address == _NEW_LEADER   # 先真的套用一次
    env.ledger.unlink()                                            # 帳本被刪掉
    env.notifier.records.clear()

    with pytest.raises(LeaderResolutionError) as e:
        env.applier().effective(_BASE)
    assert not isinstance(e.value, LeaderRevokedError)   # 不得誤升級成撤銷

    crits = env.crits()
    assert len(crits) == 1
    text = crits[0][2]
    # ⭐ 告警必須**指名事件**：操作者要能一眼分辨「一筆記錄被忽略」（無害）與
    # 「客戶授權的 override 遺失」（要立刻處理）。
    assert "遺失" in text and "拒絕退回" in text


def test_ledger_loss_alert_is_not_the_generic_verify_failed_reason(env):
    """遺失的 dedup_key 必須自成一類——複用 verify_failed 之類的既有 key 會讓這則
    告警被前一則同 key 的訊息去重掉，也會被操作者讀成完全不同的事件。"""
    env.write_change()
    env.applier().effective(_BASE)
    env.ledger.unlink()
    env.notifier.records.clear()

    with pytest.raises(LeaderResolutionError):
        env.applier().effective(_BASE)
    keys = [r[3] for r in env.crits()]
    assert keys == ["leader_change_ledger_lost"]


def test_genuine_first_start_is_silent_and_writes_an_initial_ledger(env):
    """真正的首次啟動（帳本與標記皆無）→ 正常運作、**不告警**，並落下初始帳本。

    初始帳本本身就是「此後缺席＝遺失」的前提：沒有這一步，上面那條偵測永遠不會觸發。
    """
    assert not env.ledger.exists()
    res = env.applier().effective(_BASE)

    assert res == _BASE                    # 沒有變更記錄 → 照常用 manifest 的 leader
    assert env.crits() == []               # 首次啟動不是事件
    assert load_ledger(env.ledger) == Ledger({}, None)
    assert ledger_init_marker_path(env.ledger).exists()


def test_second_start_after_initialisation_treats_absence_as_loss(env):
    """初始化過（標記在）之後，帳本缺席一律是遺失——即使當時還沒有任何 override。

    刻意不放寬成「只有 applied 不為 None 才算遺失」：判斷「有沒有 override」需要
    讀帳本，而帳本正是不見的那個東西（用遺失的證據去判斷遺失是不是要緊，是循環論證）。
    """
    env.applier().effective(_BASE)         # 首次啟動：落初始帳本＋標記
    env.ledger.unlink()
    env.notifier.records.clear()

    with pytest.raises(LeaderResolutionError):
        env.applier().effective(_BASE)


def test_override_survives_restart_when_the_ledger_is_still_there(env):
    """重啟後帳本仍在 → override 仍生效、不告警（新機制不得誤傷正常路徑）。"""
    env.write_change()
    assert env.applier().effective(_BASE).address == _NEW_LEADER
    env.notifier.records.clear()

    fresh = env.applier()                  # 模擬新進程：同樣的路徑、全新的物件
    assert fresh.effective(_BASE) == LeaderResolution(_NEW_LEADER, SOURCE_CUSTOMER_SIGNED)
    assert env.crits() == []


def test_initialisation_writes_the_ledger_before_the_marker(env, monkeypatch):
    """落檔順序：先帳本後標記。反過來的話，兩步之間斷電會留下「有標記無帳本」，
    下一輪被判成遺失 → 一則假告警（狼來了會讓真的那則沒人看）。"""
    import spark.filet.leader_change_apply as mod

    real_save = mod.save_ledger
    marker_existed_when_ledger_written: list[bool] = []

    def _spy(path, ledger):
        marker_existed_when_ledger_written.append(
            ledger_init_marker_path(path).exists())
        return real_save(path, ledger)

    monkeypatch.setattr(mod, "save_ledger", _spy)
    env.applier().effective(_BASE)

    assert marker_existed_when_ledger_written == [False]   # 寫帳本時標記尚不存在
    assert ledger_init_marker_path(env.ledger).exists()    # 但最終兩者都在


def test_initial_ledger_write_failure_does_not_block_following(env, monkeypatch):
    """初始帳本寫不進去 → 只 log、以空帳本繼續，**不**升級成解析失敗。

    首次啟動的定義就是「還沒有任何客戶授權的 override」，此刻沒有東西可以失去；
    把它升級成 raise 只會讓一個寫不進去的狀態目錄擋住跟單。真正該大聲的是稍後
    套用時的落帳失敗（見 test_ledger_write_failure_blocks_the_apply）。
    """
    import spark.filet.leader_change_apply as mod

    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "save_ledger", _boom)
    assert env.applier().effective(_BASE) == _BASE
    assert env.crits() == []
    assert not env.ledger.exists()


def test_ledger_write_failure_blocks_the_apply(env, monkeypatch):
    """⭐ 落帳失敗 ⇒ **不套用**。先落帳再套用是刻意的順序：套了卻沒記下 nonce，
    下一輪會再套用一次、再收斂一次——一個寫不進去的狀態目錄會變成無限次交易成本。"""
    import spark.filet.leader_change_apply as mod

    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "save_ledger", _boom)
    env.write_change()
    assert env.applier().effective(_BASE) == _BASE
    crits = env.crits()
    assert len(crits) == 1 and "落檔失敗" in crits[0][2]


# ── 告警失敗不得中斷跟單 ──────────────────────────────────────────────

class _ExplodingNotifier(RecordingNotifier):
    def critical(self, category, text, dedup_key=None):
        raise RuntimeError("telegram 掛了")


def test_notifier_failure_does_not_break_following(env):
    """⭐ 注入的 Notifier 拋例外 → effective() 不得 raise，套用照常完成。

    告警是觀測層；觀測層壞掉絕不能弄停被觀測的系統（「Telegram 掛了」演變成
    「引擎停機、部位無人看管」是最不該有的因果）。
    """
    env.write_change()
    res = env.applier(notifier=_ExplodingNotifier()).effective(_BASE)
    assert res.address == _NEW_LEADER
    assert load_ledger(env.ledger).applied is not None   # 落帳照做


def test_notifier_failure_on_rejection_path_is_contained(env):
    """拒絕路徑同理：告警炸掉不得讓 effective() raise。"""
    env.write_change(signer=Account.create())
    assert env.applier(notifier=_ExplodingNotifier()).effective(_BASE) == _BASE


# ── 帳本本身（原子落檔／格式）────────────────────────────────────────

def test_save_ledger_is_atomic_and_leaves_no_tmp(env):
    """原子換檔（tmp + os.replace）：落檔後不得留下任何 tmp 殘骸，且內容可讀回。"""
    save_ledger(env.ledger, Ledger({"n1": _NEW_LEADER},
                                   AppliedChange("n1", _NEW_LEADER, _at(), _NOW)))
    assert load_ledger(env.ledger).redeemed == {"n1": _NEW_LEADER}
    assert list(env.ledger.parent.glob("*.tmp")) == []


def test_save_ledger_never_leaves_a_torn_file(env):
    """覆寫既有帳本的過程中，正式檔案在任何時點都必須是完整可解析的
    （os.replace 的原子性；先寫 tmp 再換名，不是原地覆寫）。"""
    save_ledger(env.ledger, Ledger({"n1": _NEW_LEADER},
                                   AppliedChange("n1", _NEW_LEADER, _at(), _NOW)))
    for i in range(5):
        save_ledger(env.ledger, Ledger({f"n{i}": _THIRD},
                                       AppliedChange(f"n{i}", _THIRD, _at(), _NOW)))
        assert load_ledger(env.ledger).applied.leader_address == _THIRD


def test_missing_ledger_is_an_empty_ledger_not_an_error(env):
    """首次啟動沒有帳本是正常狀態，不是錯誤。"""
    assert load_ledger(env.ledger) == Ledger({}, None)


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    '{"redeemed": []}',
    '{"applied": {"nonce": "n1"}}',                       # 欄位不完整
    '{"applied": {"nonce": "n1", "leader_address": "0xnope", "issued_at": "x"}}',
])
def test_broken_ledger_fails_closed(env, content):
    """⭐ 壞帳本一律 raise，**絕不**當成空帳本：當空帳本會同時忘掉已兌現的 nonce
    （可重放）與生效中的 override（無授權換手），兩個災難一起發生。"""
    env.ledger.parent.mkdir(parents=True, exist_ok=True)
    env.ledger.write_text(content)
    with pytest.raises(ValueError):
        load_ledger(env.ledger)


# ── 路徑推導：寫端與讀端必須是同一份定義 ─────────────────────────────

def test_changes_path_follows_the_api_pending_env():
    """⭐ 引擎讀的檔＝API 寫的檔。兩邊各自一個 env 就有兩個能被半邊誤設的旋鈕，
    而設錯的症狀是靜默的（客戶按了換 leader 卻永遠不生效，兩邊 log 都正常）。"""
    from spark.publicapi.config import ApiConfig

    pending = "/var/lib/filet-api/pending.json"
    engine_side = resolve_changes_path({"FILET_PENDING_PATH": pending})
    api_side = ApiConfig(
        network="testnet", builder_address="0x" + "b1" * 20,
        siwe_domain="d", siwe_uri="u", db_path="db", keysvc_sock="s",
        pending_path=pending, admin_addresses=frozenset()).leader_changes_path
    assert engine_side == api_side == "/var/lib/filet-api/leader_changes.json"


def test_changes_path_default_is_absolute_and_beside_the_manifest():
    """未設 env → repo 根錨定的絕對路徑（不隨引擎的 CWD 漂移，沿
    DEFAULT_MANIFEST_PATH／DEFAULT_LEADERS_PATH 的既有理由）。"""
    p = Path(resolve_changes_path({}))
    assert p.is_absolute() and str(p) == DEFAULT_LEADER_CHANGES_PATH
    assert p.name == "leader_changes.json" and p.parent.name == "filet"
