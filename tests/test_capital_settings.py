"""tests/test_capital_settings.py
客戶簽章的資金設定記錄（spark.filet.capital_settings）——格式、驗證原語、域分隔。

⭐ 本檔盯住的是「能改這兩個值的人就能改客戶的曝險倍數」這件事。
`allocated_capital` 與 `capital_utilization` 直接乘進部位大小
（sizing.compute_scale_factor），把使用比例從 0.2 拉到 1.0，客戶的部位變成五倍、
清算距離縮到五分之一——而每一筆交易看起來都完全正常。

⭐⭐ 最重要的一組：**域分隔雙向拒絕**。一筆換 leader 的簽章絕不能被當成資金設定
授權，反之亦然。這是先前 chain_id=0 那一課的同一題：域分隔要**兩個方向**都是
結構性的，只擋一邊的分隔符不是分隔符。

用真密碼學（eth_account 本地運算，不觸網，沿 test_leader_change.py 慣例）。
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.filet.capital_settings import (ACTION_CAPITAL_SETTINGS,
                                          CAPITAL_SETTINGS_FIELDS,
                                          FULL_EQUITY_MESSAGE_VALUE,
                                          CapitalSettingsError,
                                          build_capital_settings_message,
                                          build_capital_settings_record,
                                          canonical_capital_values,
                                          capital_settings_path_for,
                                          load_capital_settings,
                                          validate_capital_bounds,
                                          verify_capital_settings,
                                          write_capital_settings)
from spark.filet.leader_change import (LeaderChangeError,
                                       build_leader_change_message,
                                       build_leader_change_record,
                                       verify_leader_change)

_NOW = 1_800_000_000.0
_LEADER = "0x" + "d4" * 20


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _always(_nonce: str) -> bool:
    """nonce 一律可用（把「一次性」這件事隔離到它自己的測試裡）。"""
    return True


def _never(_nonce: str) -> bool:
    return False


def _sign(wallet, message: str) -> str:
    return wallet.sign_message(encode_defunct(text=message)).signature.hex()


def _record(wallet, *, account_id=None, alloc="1000", util="0.5", nonce="n1",
            issued_at=None, signer=None, tamper=None,
            use_full_equity=False) -> dict:
    """簽一筆合法的資金設定記錄。`tamper` 在簽完之後改動欄位（重放／竄改）。"""
    account_id = account_id or _acct(wallet)
    issued_at = issued_at or _at()
    _, alloc_s, _, util_s = canonical_capital_values(alloc, util)
    msg = build_capital_settings_message(
        account_id=account_id, allocated_capital=alloc_s,
        capital_utilization=util_s, nonce=nonce, issued_at=issued_at,
        use_full_equity=use_full_equity)
    rec = build_capital_settings_record(
        account_id=account_id, allocated_capital=alloc_s,
        capital_utilization=util_s, nonce=nonce, issued_at=issued_at,
        signature=_sign(signer or wallet, msg), message=msg,
        use_full_equity=use_full_equity)
    if tamper:
        rec.update(tamper)
    return rec


@pytest.fixture
def wallet():
    return Account.create()


# ── 快樂路徑 ──────────────────────────────────────────────────────────

def test_verifies_valid_record(wallet):
    v = verify_capital_settings(_record(wallet), account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert v.account_id == _acct(wallet)
    assert v.user_address == wallet.address.lower()
    assert v.allocated_capital == Decimal("1000")
    assert v.capital_utilization == Decimal("0.5")
    assert v.allocated_capital_str == "1000.00"
    assert v.capital_utilization_str == "0.5000"


def test_record_field_set_is_pinned_by_the_constant(wallet):
    """欄位集（含順序）＝ CAPITAL_SETTINGS_FIELDS。多一個少一個都要有人主動改。"""
    assert tuple(_record(wallet).keys()) == CAPITAL_SETTINGS_FIELDS


def test_record_has_no_signer_field(wallet):
    """⭐ 記錄**沒有** signer 欄位：沒有這個欄位，就沒有人能誤用它當比對基準。"""
    assert "signer" not in _record(wallet)


def test_record_action_is_written_by_the_builder_not_the_caller(wallet):
    """action 由 builder 寫死——呼叫端能指定的話，一個手滑就能造出跨域記錄。"""
    assert _record(wallet)["action"] == ACTION_CAPITAL_SETTINGS


# ── ⭐⭐ 域分隔：雙向拒絕 ──────────────────────────────────────────────

def test_leader_change_signature_is_rejected_by_capital_verifier(wallet):
    """⭐⭐ 方向一：拿一筆**合法的換 leader 簽章記錄**去餵資金設定的驗證 → 拒絕。

    這一筆記錄本身完全合法（客戶本人簽的、沒過期、nonce 可用），擋下它的**只有**
    動作類型檢查。若這條路徑放行，一次換 leader 的授權就能被兌換成一次資金設定
    變更（例如把使用比例拉滿）。
    """
    account_id = _acct(wallet)
    issued_at = _at()
    msg = build_leader_change_message(account_id=account_id, leader_address=_LEADER,
                                      nonce="n1", issued_at=issued_at)
    leader_rec = build_leader_change_record(
        account_id=account_id, leader_address=_LEADER, nonce="n1",
        issued_at=issued_at, signature=_sign(wallet, msg), message=msg)

    # 先確認這筆記錄在**它自己的**域裡確實是合法的（否則本測試只是在驗一筆壞資料）
    assert verify_leader_change(leader_rec, account_id=account_id,
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always).leader_address == _LEADER

    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(leader_rec, account_id=account_id,
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "action_mismatch"


def test_capital_signature_is_rejected_by_leader_change_verifier(wallet):
    """⭐⭐ 方向二：拿一筆**合法的資金設定簽章記錄**去餵換 leader 的驗證 → 拒絕。

    同上：這筆記錄在它自己的域裡合法，擋下它的是動作類型檢查。
    """
    account_id = _acct(wallet)
    cap_rec = _record(wallet)

    # 先確認它在自己的域裡合法
    assert verify_capital_settings(cap_rec, account_id=account_id,
                                   user_address=wallet.address, now_s=_NOW,
                                   consume_nonce=_always).nonce == "n1"

    with pytest.raises(LeaderChangeError) as ei:
        verify_leader_change(cap_rec, account_id=account_id,
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert ei.value.reason == "action_mismatch"


def test_cross_domain_replay_fails_even_without_the_action_field(wallet):
    """⭐⭐ 最終防線（**不依賴 action 欄位**）：把 action 拿掉、把缺的欄位補上，
    跨域重放仍然失敗——因為兩種待簽訊息**結構上不可能是同一個字串**。

    這正是「域分隔必須是結構性的」的意思：欄位檢查是給人看的（指名事件的拒絕
    理由），真正擋住攻擊的是訊息模板。若這個測試轉紅，代表域分隔退化成只剩一層
    可被繞過的欄位檢查。
    """
    account_id = _acct(wallet)
    cap_rec = _record(wallet)
    forged = {k: v for k, v in cap_rec.items() if k != "action"}
    forged["leader_address"] = _LEADER          # 補上換 leader 需要的欄位

    with pytest.raises(LeaderChangeError) as ei:
        verify_leader_change(forged, account_id=account_id,
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    # 落回簽章比對：重建出來的是換 leader 的原文，recover 出的不是這位客戶。
    assert ei.value.reason == "signer_mismatch"


def test_the_two_message_templates_cannot_collide(wallet):
    """⭐⭐ 域分隔的論證本體，寫成可執行的斷言：兩個模板的**第一行**是不同的固定
    字面量，而所有可變輸入都出現在第一行之後 ⇒ 不存在一組輸入能產生同一字串。

    這裡直接餵一組「盡可能想撞在一起」的輸入（含換行、含對方的標籤字串），
    確認第一行仍然分開、整體仍然不相等。
    """
    hostile = "x\nLeader: " + _LEADER + "\nNonce: n1"
    leader_msg = build_leader_change_message(
        account_id=hostile, leader_address=_LEADER, nonce="n", issued_at="i")
    cap_msg = build_capital_settings_message(
        account_id=hostile, allocated_capital="1", capital_utilization="1",
        nonce="n", issued_at="i")

    assert leader_msg.splitlines()[0] == "Filet: change copy-trading leader"
    assert cap_msg.splitlines()[0] == "Filet: update copy-trading capital allocation"
    assert leader_msg.splitlines()[0] != cap_msg.splitlines()[0]
    assert leader_msg != cap_msg
    # 欄位標籤集也不重疊（第二重不可碰撞性）
    assert "Allocated Capital:" not in leader_msg
    assert "Leader:" not in cap_msg.replace(hostile, "")


# ── 簽章者、nonce、時效 ───────────────────────────────────────────────

def test_rejects_wrong_signer(wallet):
    """⭐ 全模組最關鍵的一條：簽章者必須等於可信來源給的 user_address。"""
    attacker = Account.create()
    rec = _record(wallet, signer=attacker)
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "signer_mismatch"


def test_expected_signer_comes_from_caller_not_record(wallet):
    """記錄裡的任何身分宣稱都不是基準：把 account_id 改成別人的 → 拒絕。"""
    other = Account.create()
    rec = _record(wallet, account_id=_acct(other))
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "account_mismatch"


def test_tampered_amount_is_rejected(wallet):
    """⭐ 簽完之後把金額改大 → 重建出的訊息不同 → 簽章者對不上 → 拒絕。
    這是本模組要防的核心攻擊：把使用比例悄悄拉滿。"""
    rec = _record(wallet, util="0.2", tamper={"capital_utilization": "1.0000"})
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "signer_mismatch"


def test_message_field_is_audit_only(wallet):
    """`message` 純為稽核留存：把它換成任意內容不影響驗證結果（驗證端重建自己的
    版本）。直接驗「這個簽章有簽這個 message」是假驗證。"""
    rec = _record(wallet, tamper={"message": "完全不相干的內容"})
    assert verify_capital_settings(rec, account_id=_acct(wallet),
                                   user_address=wallet.address, now_s=_NOW,
                                   consume_nonce=_always).nonce == "n1"


def test_nonce_reuse_is_rejected(wallet):
    """一次性：consume_nonce 回 False（不存在／已用過／已過期）→ 拒絕。"""
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(_record(wallet), account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_never)
    assert ei.value.reason == "nonce_unusable"


def test_nonce_is_consumed_last(wallet):
    """⭐ 唯一的副作用擺在最後：格式錯／簽章錯時**不得**燒掉 nonce，否則任何人送
    一筆垃圾記錄就能作廢客戶手上那張合法授權（自我 DoS）。"""
    consumed = []

    def _spy(nonce):
        consumed.append(nonce)
        return True

    bad = _record(wallet, signer=Account.create())
    with pytest.raises(CapitalSettingsError):
        verify_capital_settings(bad, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_spy)
    assert consumed == []


def test_expired_signature_is_rejected(wallet):
    rec = _record(wallet, issued_at=_at(-3600))
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "expired"


def test_future_issued_at_is_rejected(wallet):
    rec = _record(wallet, issued_at=_at(3600))
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "expired"


def test_naive_issued_at_is_rejected(wallet):
    """裸 naive 時間拒絕（沿用 leader_change 的同一個解析器，不另寫一份）。"""
    rec = _record(wallet, tamper={"issued_at": "2026-07-19T00:00:00"})
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "malformed"


def test_bad_nonce_charset_is_rejected(wallet):
    """nonce 會被拼進待簽訊息的一行；允許換行等於允許攻擊者自己捏造額外欄位。"""
    rec = _record(wallet, tamper={"nonce": "n1\nCapital Utilization: 1"})
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "malformed"


# ── canonical 化與邊界 ────────────────────────────────────────────────

def test_canonical_form_is_stable_across_equivalent_spellings():
    """⭐ `1000`、`1000.0`、`1000.00` 是同一個數，必須組出**同一個字串**——否則
    客戶端與伺服器會得到兩份不同的原文，症狀是「本人簽的卻一直被拒」。"""
    forms = ["1000", "1000.0", "1000.00", " 1000 "]
    strs = {canonical_capital_values(f, "0.5")[1] for f in forms}
    assert strs == {"1000.00"}
    msgs = {build_capital_settings_message(account_id="f" + "1" * 40,
                                           allocated_capital=f,
                                           capital_utilization="0.50",
                                           nonce="n", issued_at="i")
            for f in forms}
    assert len(msgs) == 1


def test_excess_precision_is_rejected_not_truncated():
    """⭐ 小數位超過上限 → **拒絕**，不四捨五入：截斷會在客戶不知情的情況下改掉
    他簽署的數字，而這個數字直接乘進部位大小。"""
    with pytest.raises(CapitalSettingsError) as ei:
        canonical_capital_values("1000.005", "0.5")
    assert ei.value.reason == "malformed"
    with pytest.raises(CapitalSettingsError):
        canonical_capital_values("1000", "0.123456")


def test_non_numeric_and_non_finite_are_rejected():
    for alloc, util in [("abc", "0.5"), ("NaN", "0.5"), ("Infinity", "0.5"),
                        ("1000", "abc"), (None, "0.5"), (True, "0.5")]:
        with pytest.raises(CapitalSettingsError):
            canonical_capital_values(alloc, util)


def test_absurd_magnitude_is_rejected():
    """`1e100000` 印進待簽訊息會產生十萬位數的字串——格式層就該擋。"""
    with pytest.raises(CapitalSettingsError):
        canonical_capital_values("1e100000", "0.5")


@pytest.mark.parametrize("alloc,util", [
    ("0", "0.5"),        # 本金必須 > 0
    ("-100", "0.5"),
    ("1000", "0"),       # 使用比例下界開區間
    ("1000", "-0.5"),
    ("1000", "1.5"),     # 上界
])
def test_out_of_range_values_are_rejected(alloc, util):
    """⭐ 超界一律 raise，**不夾取**：夾取會讓流程順利跑完，代價是客戶簽了 A、
    系統執行了 B，而且沒有人會知道。"""
    a, _, u, _ = canonical_capital_values(alloc, util)
    with pytest.raises(CapitalSettingsError) as ei:
        validate_capital_bounds(a, u)
    assert ei.value.reason == "out_of_range"


@pytest.mark.parametrize("alloc,util", [
    ("0.01", "0.0001"), ("1000", "1"), ("1000", "1.0000"), ("999999", "0.3333"),
])
def test_in_range_values_are_accepted(alloc, util):
    a, _, u, _ = canonical_capital_values(alloc, util)
    validate_capital_bounds(a, u)          # 不 raise 即通過


def test_bounds_are_not_checked_by_the_verifier(wallet):
    """⭐⭐ 刻意的職責切割：驗簽只回答「這是不是客戶本人的意圖」（真實性），
    邊界由**每個執行點各自**呼叫 validate_capital_bounds（政策）。

    這個切割正是引擎能夠、也必須自己再驗一次邊界的原因——若邊界綁進驗簽，
    引擎端的再驗就會變成一句「反正 verify 驗過了」而被省略。
    """
    # 一筆**簽章完全合法、數值卻超界**的記錄（使用比例 2.0 ＝ 兩倍曝險）。
    rec = _record(wallet, alloc="1000", util="2")
    v = verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert v.capital_utilization == Decimal("2")      # 真實性：通過

    with pytest.raises(CapitalSettingsError) as ei:   # 政策：擋下
        validate_capital_bounds(v.allocated_capital, v.capital_utilization)
    assert ei.value.reason == "out_of_range"


# ── 落檔 ──────────────────────────────────────────────────────────────

def test_write_overwrites_same_account(tmp_path, wallet):
    """同 account 覆蓋而非附加：檔案是「當前意圖」，不是流水帳。"""
    p = tmp_path / "capital_settings.json"
    write_capital_settings(p, _record(wallet, util="0.2"))
    write_capital_settings(p, _record(wallet, util="0.8", nonce="n2"))
    entries = load_capital_settings(p)
    assert len(entries) == 1 and entries[0]["capital_utilization"] == "0.8000"


def test_write_keeps_other_accounts(tmp_path, wallet):
    other = Account.create()
    p = tmp_path / "capital_settings.json"
    write_capital_settings(p, _record(wallet))
    write_capital_settings(p, _record(other))
    assert {e["account_id"] for e in load_capital_settings(p)} == {
        _acct(wallet), _acct(other)}


def test_load_missing_file_is_empty(tmp_path):
    assert load_capital_settings(tmp_path / "nope.json") == []


def test_path_is_anchored_on_the_exchange_dir(tmp_path):
    """路徑錨在交換目錄（API 寫、引擎讀），且與換 leader 記錄**分開一個檔**。"""
    from spark.filet.leader_change import leader_changes_path_for
    p = capital_settings_path_for(tmp_path)
    assert p == str(tmp_path / "capital_settings.json")
    assert p != leader_changes_path_for(tmp_path)


def test_record_is_json_serialisable(tmp_path, wallet):
    """落檔內容必須是純 JSON（Decimal 不得漏進來）。"""
    json.dumps(_record(wallet))


# ── ⭐ use_full_equity：顯式旗標（解除 `0` 的語意重載） ────────────────

def test_full_equity_record_verifies(wallet):
    """⭐ 「用全部權益」是一筆合法的客戶簽章意圖（本金 0 ＋ 旗標 True）。
    改動前這組值無法通過邊界檢查，客戶因此無法表達這個意圖。"""
    rec = _record(wallet, alloc="0", util="0.5", use_full_equity=True)
    v = verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert v.use_full_equity is True
    assert v.allocated_capital == Decimal("0")


def test_absent_flag_reads_as_fixed_capital(wallet):
    """⭐ 向後相容：改動前寫下的記錄沒有這個欄位 → False（固定本金）。
    那是它當時的正確語意；讀成 True 會把一筆固定本金授權放大成整個帳戶。"""
    rec = _record(wallet)
    del rec["use_full_equity"]
    v = verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert v.use_full_equity is False
    assert v.allocated_capital == Decimal("1000")


@pytest.mark.parametrize("bad", ["true", "True", 1, 0, "yes", []])
def test_non_boolean_flag_is_malformed(wallet, bad):
    """⭐ 嚴格型別：只收真正的 JSON bool。寬鬆解析等於替這個旗標多開幾種等價
    寫法，而每一種都是「驗證端與套用端解讀不一致」的機會。"""
    rec = _record(wallet, tamper={"use_full_equity": bad})
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "malformed"


def test_flipping_the_flag_after_signing_is_rejected(wallet):
    """⭐⭐ 旗標**受簽章保護**：簽完之後翻轉它 → 重建出的原文不同 → signer_mismatch。

    若這條轉綠（放行），任何能寫記錄檔的人都能把客戶的本金基準從一個固定數字
    換成整個帳戶——白名單全程放行，事後看紀錄一切合規。
    """
    rec = _record(wallet, alloc="1000", tamper={"use_full_equity": True})
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "signer_mismatch"


def test_flipping_the_flag_off_after_signing_is_rejected(wallet):
    """反方向（全權益 → 固定本金）同樣被擋：兩個方向都必須是簽章保護的。"""
    rec = _record(wallet, alloc="0", use_full_equity=True,
                  tamper={"use_full_equity": False})
    with pytest.raises(CapitalSettingsError) as ei:
        verify_capital_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert ei.value.reason == "signer_mismatch"


def test_the_two_capital_modes_produce_different_messages(wallet):
    """⭐ 兩種模式的待簽原文必定不同，且**只差在 `Allocated Capital:` 那一行的值**。
    金額經 canonical 化恆為數字，永遠寫不出 `full account equity` 這個字面量
    ⇒ 兩種模式結構上不可能碰撞（不依賴任何欄位檢查）。"""
    common = dict(account_id="f" + "1" * 40, capital_utilization="0.5000",
                  nonce="n", issued_at="2026-07-19T00:00:00Z")
    fixed = build_capital_settings_message(allocated_capital="0.00",
                                           use_full_equity=False, **common)
    full = build_capital_settings_message(allocated_capital="0.00",
                                          use_full_equity=True, **common)
    assert fixed != full
    assert "Allocated Capital: 0.00 USDC" in fixed
    assert f"Allocated Capital: {FULL_EQUITY_MESSAGE_VALUE}" in full
    diff = [(a, b) for a, b in zip(fixed.splitlines(), full.splitlines()) if a != b]
    assert len(diff) == 1 and diff[0][0].startswith("Allocated Capital:")


def test_fixed_mode_message_is_byte_identical_to_the_legacy_template(wallet):
    """⭐ 向後相容的關鍵斷言：`use_full_equity=False`（預設）的原文與改動前
    **逐位元組相同**——旗標編碼進既有那一行的值，沒有新增任何一行。
    這是「舊客戶端不送旗標也照常運作」的結構性依據。"""
    msg = build_capital_settings_message(
        account_id="f" + "1" * 40, allocated_capital="1000.00",
        capital_utilization="0.5000", nonce="n", issued_at="2026-07-19T00:00:00Z")
    assert msg == (
        "Filet: update copy-trading capital allocation\n"
        "\n"
        "Signing this authorises Filet to change how much capital mirrors your leader.\n"
        "These values scale your position sizes directly: a higher utilisation means\n"
        "larger positions and a closer liquidation distance.\n"
        "It takes effect on the engine's next cycle. No immediate forced rebalance is\n"
        "performed; positions converge naturally as the leader trades.\n"
        "\n"
        f"Account: {'f' + '1' * 40}\n"
        "Allocated Capital: 1000.00 USDC\n"
        "Capital Utilization: 0.5000\n"
        "Nonce: n\n"
        "Issued At: 2026-07-19T00:00:00Z")


@pytest.mark.parametrize("alloc", ["1000", "0.01", "-1"])
def test_full_equity_mode_requires_zero_capital(alloc):
    """⭐⭐ 「兩個都有值」被**拒絕**，不是「其中一個蓋過另一個」。

    客戶簽的是人看得懂的文字；「本金 1000 USDC」與「用全部權益」同時出現在
    錢包裡，他無從知道系統會執行哪一句。忽略其中一句＝系統替他選，而選錯
    不會有任何人發現。
    """
    a, _, u, _ = canonical_capital_values(alloc, "0.5")
    with pytest.raises(CapitalSettingsError) as ei:
        validate_capital_bounds(a, u, use_full_equity=True)
    assert ei.value.reason == "out_of_range"


def test_fixed_mode_still_requires_positive_capital():
    """固定本金模式的既有邊界不變（本金 0 仍然是非法值）——旗標是新增的表達
    途徑，不是把舊邊界放寬。"""
    a, _, u, _ = canonical_capital_values("0", "0.5")
    with pytest.raises(CapitalSettingsError) as ei:
        validate_capital_bounds(a, u, use_full_equity=False)
    assert ei.value.reason == "out_of_range"


def test_bounds_default_is_the_restrictive_mode():
    """⭐ 漏傳 use_full_equity 時落到**限制較嚴**的那一邊（固定本金）：
    預設值只可能誤擋，不可能誤放一筆「用全部權益」的授權。"""
    a, _, u, _ = canonical_capital_values("0", "0.5")
    with pytest.raises(CapitalSettingsError):
        validate_capital_bounds(a, u)        # 不傳旗標 → 走 > 0 的嚴格分支


def test_full_equity_with_zero_capital_is_accepted():
    a, _, u, _ = canonical_capital_values("0", "0.5")
    validate_capital_bounds(a, u, use_full_equity=True)   # 不 raise 即通過
