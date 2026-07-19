"""tests/test_leader_change.py — 客戶簽章的換 leader 記錄（驗簽原語＋落檔格式）。

用真密碼學（eth_account 本地運算，不觸網，沿 test_publicapi_siwe.py 慣例）。

盯住的核心只有一句：**驗證不得信任記錄裡的任何身分宣稱**。所以「簽章者不是該帳號
的持有人」與兩種重放（換 leader／挪帳號）是本檔的主測試，其餘（時效、nonce、壞格式）
是圍繞它的邊界。
"""
import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.filet.leader_change import (LEADER_CHANGE_MAX_AGE_S, LeaderChangeError,
                                       build_leader_change_message,
                                       build_leader_change_record,
                                       load_leader_changes, verify_leader_change,
                                       write_leader_change)

_LEADER = "0x" + "a1" * 20
_OTHER_LEADER = "0x" + "d4" * 20
_NOW = 1_800_000_000.0
_ISSUED_AT = "2027-01-15T08:00:00+00:00"   # 對應 _NOW 的同一時刻（見 _at()）


def _at(offset_s: float = 0.0) -> str:
    """把 epoch 秒轉成記錄用的 ISO8601 UTC 字串——時效測試的兩端同源（原則 1）。"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct_id(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _sign(wallet, text: str) -> str:
    return wallet.sign_message(encode_defunct(text=text)).signature.hex()


def _record(wallet, *, account_id=None, leader=_LEADER, nonce="n0",
            issued_at=None, signer_wallet=None):
    """一筆**由 wallet 本人正確簽署**的記錄。signer_wallet 可覆寫成別人（攻擊情境）。"""
    account_id = account_id or _acct_id(wallet)
    issued_at = issued_at or _at()
    msg = build_leader_change_message(account_id=account_id, leader_address=leader,
                                      nonce=nonce, issued_at=issued_at)
    return build_leader_change_record(
        account_id=account_id, leader_address=leader, nonce=nonce,
        issued_at=issued_at, signature=_sign(signer_wallet or wallet, msg),
        message=msg)


def _consume_ok(_nonce: str) -> bool:
    return True


class _NonceLedger:
    """一次性 nonce 的最小替身（真 API 走 ApiStore.consume_nonce 的原子 UPDATE）。"""

    def __init__(self, *issued):
        self.available = set(issued)
        self.consumed: list[str] = []

    def __call__(self, nonce: str) -> bool:
        if nonce not in self.available:
            return False
        self.available.discard(nonce)
        self.consumed.append(nonce)
        return True


def _verify(record, wallet, *, account_id=None, now_s=_NOW, consume=_consume_ok):
    return verify_leader_change(
        record, account_id=account_id or _acct_id(wallet),
        user_address=wallet.address, now_s=now_s, consume_nonce=consume)


# ---------- 正例 ----------

def test_correctly_signed_record_verifies():
    w = Account.create()
    out = _verify(_record(w), w)
    assert out.account_id == _acct_id(w)
    assert out.leader_address == _LEADER          # 正規化小寫
    assert out.user_address == w.address.lower()  # 同基準（原則 1）
    assert out.nonce == "n0"


def test_leader_address_is_normalised_lowercase():
    """記錄用大寫位址簽名也要驗得過，且結果一律小寫——比較基準只有一個。"""
    w = Account.create()
    upper = "0x" + "A1" * 20
    out = _verify(_record(w, leader=upper), w)
    assert out.leader_address == upper.lower()


# ---------- ⭐ 核心：簽章者必須是該帳號的持有人 ----------

def test_signature_from_wrong_signer_is_rejected():
    """⭐ 本檔的核心。攻擊者自簽一筆**格式完美**的記錄——每個欄位都合法、訊息與
    簽章完全自洽——差別只在簽的人不是該帳號的持有人。

    這正是「直接驗 signature 有沒有簽 message」會放行、而只有「recover 出的簽章者
    == manifest/session 的 user_address」擋得下來的情境。
    """
    victim, attacker = Account.create(), Account.create()
    rec = _record(victim, signer_wallet=attacker)
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, victim)
    assert e.value.reason == "signer_mismatch"


# ---------- ⭐ 重放：兩種都由同一條簽章者比對擋下 ----------

def test_swapping_leader_but_reusing_signature_is_rejected():
    """⭐ 重放一：客戶簽了「換到 A」，攻擊者把記錄的 leader 改成 B、沿用舊簽章。
    重建訊息時綁的是 B → recover 出的不是客戶 → 拒絕。"""
    w = Account.create()
    rec = _record(w, leader=_LEADER)
    rec["leader_address"] = _OTHER_LEADER      # 竄改意圖，簽章原封不動
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, w)
    assert e.value.reason == "signer_mismatch"


def test_replaying_record_into_another_account_is_rejected():
    """⭐ 重放二：把客戶 X 的合法記錄挪到客戶 Y 的槽位。

    攻擊者當然會**一併把 record["account_id"] 改成 Y**（那個欄位就在他手上），
    所以「記錄 account_id 與待驗帳號一致」這道檢查對他毫無阻力——真正擋下來的是
    「以 Y 的身分重建訊息 → recover 出 X → 不等於 Y」。這條測試因此刻意讓
    account_mismatch 那道檢查通過，把壓力全部壓在簽章者比對上。
    """
    x, y = Account.create(), Account.create()
    rec = _record(x)                       # X 本人正確簽署
    rec["account_id"] = _acct_id(y)        # 挪到 Y 的槽位（連宣稱一起改）
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, y)                    # 以 Y 的可信身分驗
    assert e.value.reason == "signer_mismatch"


def test_record_account_id_must_match_the_account_being_verified():
    """記錄自稱的 account_id 與待驗帳號不符 → 早退（誤放槽位的清楚訊息）。
    這是便利性檢查，不是防線——防線見上一條。"""
    x, y = Account.create(), Account.create()
    with pytest.raises(LeaderChangeError) as e:
        _verify(_record(x), y)
    assert e.value.reason == "account_mismatch"


def test_message_field_is_audit_only_and_never_trusted():
    """⭐ 記錄自帶的 message 欄位對驗證**毫無影響**：把它換成攻擊者想要的文字，
    合法記錄照樣通過（驗證重建自己的版本）；反之偽造的 message＋自洽簽章也過不了
    （由上面的 signer_mismatch 測試覆蓋）。"""
    w = Account.create()
    rec = _record(w)
    rec["message"] = "Filet: change copy-trading leader\n\nAccount: whatever"
    assert _verify(rec, w).leader_address == _LEADER


def test_message_binds_all_four_fields():
    """訊息版型必須綁 account_id/leader/nonce/issued_at——任一不同即不同訊息。"""
    base = dict(account_id="fabc", leader_address=_LEADER, nonce="n0",
                issued_at=_ISSUED_AT)
    msg = build_leader_change_message(**base)
    for key, other in [("account_id", "fdef"), ("leader_address", _OTHER_LEADER),
                       ("nonce", "n1"), ("issued_at", "2027-01-15T09:00:00+00:00")]:
        assert build_leader_change_message(**{**base, key: other}) != msg
    # 客戶在錢包裡就該看到後果，不是簽一串看不懂的東西
    assert "closed" in msg and "cost" in msg


def test_message_is_not_confusable_with_siwe():
    """跨協定重放：一次 SIWE 登入簽名不得被當成一次換 leader 授權（版型域分隔）。"""
    from spark.publicapi.siwe import build_siwe_message
    siwe = build_siwe_message(domain="filet.example", uri="https://filet.example",
                              address="0x" + "a1" * 20, chain_id=42161,
                              nonce="n0", issued_at=_ISSUED_AT)
    msg = build_leader_change_message(account_id="fabc", leader_address=_LEADER,
                                      nonce="n0", issued_at=_ISSUED_AT)
    assert msg.splitlines()[0] != siwe.splitlines()[0]
    assert "wants you to sign in" not in msg


# ---------- 時效 ----------

def test_expired_issued_at_is_rejected():
    w = Account.create()
    rec = _record(w, issued_at=_at(-LEADER_CHANGE_MAX_AGE_S - 1))
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, w)
    assert e.value.reason == "expired"


def test_issued_at_just_inside_window_is_accepted():
    """邊界：剛好在窗內要過（否則「過期」的判準會靜靜地比宣稱的更嚴）。"""
    w = Account.create()
    rec = _record(w, issued_at=_at(-LEADER_CHANGE_MAX_AGE_S + 1))
    assert _verify(rec, w).nonce == "n0"


def test_far_future_issued_at_is_rejected():
    """未來時間戳＝竄改或時鐘漂移，兩者都不放行。"""
    w = Account.create()
    rec = _record(w, issued_at=_at(LEADER_CHANGE_MAX_AGE_S + 60))
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, w)
    assert e.value.reason == "expired"


def test_naive_issued_at_is_rejected():
    """無時區的時間戳不得被默認成 UTC——那個假設看不見，且會直接歪掉時效檢查。"""
    w = Account.create()
    rec = _record(w, issued_at="2027-01-15T08:00:00")
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, w)
    assert e.value.reason == "malformed"


# ---------- nonce 一次性 ----------

def test_nonce_reuse_is_rejected():
    """同一份簽章只能兌現一次：第一次通過，第二次 nonce 已被消耗 → 拒絕。"""
    w = Account.create()
    ledger = _NonceLedger("n0")
    rec = _record(w)
    assert _verify(rec, w, consume=ledger).nonce == "n0"
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, w, consume=ledger)
    assert e.value.reason == "nonce_unusable"
    assert ledger.consumed == ["n0"]


def test_unknown_nonce_is_rejected():
    w = Account.create()
    with pytest.raises(LeaderChangeError) as e:
        _verify(_record(w, nonce="never-issued"), w, consume=_NonceLedger("n0"))
    assert e.value.reason == "nonce_unusable"


def test_nonce_not_consumed_when_verification_fails():
    """⭐ nonce 是一次性資源：格式錯／簽章錯就把它燒掉，等於任何人送一筆垃圾記錄
    就能作廢客戶手上的合法授權（自我 DoS）。副作用必須擺在所有檢查之後。"""
    victim, attacker = Account.create(), Account.create()
    ledger = _NonceLedger("n0")
    with pytest.raises(LeaderChangeError):
        _verify(_record(victim, signer_wallet=attacker), victim, consume=ledger)
    assert ledger.consumed == [] and "n0" in ledger.available


# ---------- 壞格式：一律拒絕且不拋未處理例外 ----------

@pytest.mark.parametrize("mutate", [
    {"signature": "0xdeadbeef"},              # 長度不對
    {"signature": "not-hex-at-all"},          # 根本不是 hex
    {"signature": ""},                        # 空字串
    {"leader_address": "0xnothex"},           # 位址壞掉
    {"leader_address": "zzz"},
    {"nonce": "bad nonce\nLeader: 0x00"},     # 換行注入企圖
    {"issued_at": "not-a-date"},
])
def test_malformed_records_are_rejected_cleanly(mutate):
    """壞格式一律轉成 LeaderChangeError（semantic），**不得**逸出 eth_account 的
    原始例外——呼叫端只需處理一種失敗型別才可能處理得完整。"""
    w = Account.create()
    rec = _record(w)
    rec.update(mutate)
    with pytest.raises(LeaderChangeError):
        _verify(rec, w)


@pytest.mark.parametrize("key", ["account_id", "leader_address", "nonce",
                                 "issued_at", "signature"])
def test_missing_required_field_is_rejected(key):
    w = Account.create()
    rec = _record(w)
    del rec[key]
    with pytest.raises(LeaderChangeError):
        _verify(rec, w)


def test_bad_signature_reason_is_distinguishable():
    """壞簽名（驗不出來）與簽錯人（驗出別人）是兩種失敗，reason 必須分得開。"""
    w = Account.create()
    rec = _record(w)
    rec["signature"] = "0x" + "00" * 65
    with pytest.raises(LeaderChangeError) as e:
        _verify(rec, w)
    assert e.value.reason == "bad_signature"


# ---------- 落檔 ----------

def test_write_and_load_roundtrip(tmp_path):
    w = Account.create()
    p = tmp_path / "leader_changes.json"
    rec = _record(w)
    write_leader_change(p, rec)
    loaded = load_leader_changes(p)
    assert loaded == [rec]
    assert _verify(loaded[0], w).leader_address == _LEADER   # 落檔後仍驗得過


def test_load_missing_file_returns_empty(tmp_path):
    assert load_leader_changes(tmp_path / "nope.json") == []


def test_second_change_replaces_the_first_for_same_account(tmp_path):
    """同帳號覆蓋而非附加：套用端不該有機會挑到一筆舊意圖。"""
    w = Account.create()
    p = tmp_path / "leader_changes.json"
    write_leader_change(p, _record(w, nonce="n0", leader=_LEADER))
    write_leader_change(p, _record(w, nonce="n1", leader=_OTHER_LEADER))
    entries = load_leader_changes(p)
    assert len(entries) == 1
    assert entries[0]["leader_address"] == _OTHER_LEADER
    assert entries[0]["nonce"] == "n1"


def test_different_accounts_coexist(tmp_path):
    a, b = Account.create(), Account.create()
    p = tmp_path / "leader_changes.json"
    write_leader_change(p, _record(a))
    write_leader_change(p, _record(b))
    assert {e["account_id"] for e in load_leader_changes(p)} == {_acct_id(a),
                                                                _acct_id(b)}


def test_record_carries_no_signer_field(tmp_path):
    """⭐ 格式**刻意沒有** signer 欄位：沒有這個欄位，就沒有人能誤把它當比對基準
    （用記錄自帶的 signer 比對＝攻擊者自己說自己是誰）。"""
    w = Account.create()
    rec = _record(w)
    assert set(rec) == {"account_id", "leader_address", "nonce", "issued_at",
                        "signature", "message"}
    p = tmp_path / "leader_changes.json"
    write_leader_change(p, rec)
    assert "signer" not in json.loads(p.read_text())["changes"][0]


def test_write_is_atomic_no_partial_file(tmp_path):
    """原子換檔：落地的檔永遠是完整合法 JSON（沿 pending.py 的 _atomic_write）。"""
    w = Account.create()
    p = tmp_path / "sub" / "leader_changes.json"
    write_leader_change(p, _record(w))
    assert json.loads(p.read_text())["changes"]
    assert not list(p.parent.glob("*.tmp"))


# ---------- 依賴方向（結構性斷言） ----------

def test_verification_primitive_does_not_import_publicapi():
    """⭐ 驗簽原語必須放在引擎與 API **都** import 得到、且不成環的位置。

    依賴方向是單向的 publicapi → filet（followers.py 檔頭已把它寫成慣例）。原語留在
    publicapi 的話，引擎要二次驗章就得 filet → publicapi，正好把方向掉頭成環。
    這條斷言擋的是「有人為了方便在 filet 側 import 一下 publicapi」。
    """
    import ast
    from pathlib import Path

    import spark.filet.leader_change as mod
    import spark.filet.signing as sig
    for m in (mod, sig):
        tree = ast.parse(Path(m.__file__).read_text())
        imported = {n.module or "" for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                     for a in n.names}
        # 掃 AST 而非文字：檔頭正是在**論述**這個方向，文字掃描會被自己的說明打到
        assert not any(x.startswith("spark.publicapi") for x in imported), imported


def test_siwe_and_leader_change_share_one_recover_implementation():
    """⭐ 兩邊驗的必須是同一份程式碼（工程原則 1：同源）——不是兩份長得很像的實作。"""
    from spark.filet.signing import recover_personal_sign_address
    from spark.publicapi import siwe
    assert siwe.recover_personal_sign_address is recover_personal_sign_address
