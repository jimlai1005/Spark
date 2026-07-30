"""tests/test_risk_settings.py
客戶簽章的風控設定與解鎖記錄（spark.filet.risk_settings）——格式、驗證原語、域分隔。

⭐ 本檔盯住的是「能改這些門檻的人就能拿掉客戶帳戶的全部保護」這件事，以及
「一份解鎖授權絕不能是永久有效的」。

⭐⭐ 最重要的兩組：
1. **域分隔雙向拒絕**——風控設定的簽章不得被兌換成一次解除熔斷（反之亦然），
   而且與既有的資金設定／換 leader 記錄也互不相通。
2. **時效語意相反**——持續意圖（設定）引擎端放行時效，一次性動作（解鎖）強制時效。
   弄反的方向都是實害：前者讓客戶的設定無聲退回 env，後者讓熔斷保護永久失效。

用真密碼學（eth_account 本地運算，不觸網，沿 test_capital_settings.py 慣例）。
"""
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.filet.capital_settings import (build_capital_settings_message,
                                          build_capital_settings_record,
                                          verify_capital_settings)
from spark.filet.risk_prefs import RISK_PARAM_SPECS, default_prefs
from spark.filet.risk_settings import (ACTION_RISK_SETTINGS, ACTION_RISK_UNLOCK,
                                       RISK_SETTINGS_FIELDS,
                                       RISK_SETTINGS_MAX_AGE_S,
                                       RISK_UNLOCK_FIELDS, RiskSettingsError,
                                       build_risk_settings_message,
                                       build_risk_settings_record,
                                       build_risk_unlock_message,
                                       build_risk_unlock_record,
                                       load_risk_settings, load_risk_unlocks,
                                       risk_settings_path_for, risk_unlock_path_for,
                                       verify_risk_settings, verify_risk_unlock,
                                       write_risk_settings, write_risk_unlock)

_NOW = 1_800_000_000.0


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _always(_nonce: str) -> bool:
    """nonce 一律可用（把「一次性」隔離到它自己的測試裡）。"""
    return True


def _never(_nonce: str) -> bool:
    return False


def _sign(wallet, message: str) -> str:
    return wallet.sign_message(encode_defunct(text=message)).signature.hex()


def _prefs(**over) -> dict:
    return {**default_prefs(), "enabled": True, **over}


def _record(wallet, *, account_id=None, prefs=None, nonce="n1", issued_at=None,
            signer=None, tamper=None) -> dict:
    """簽一筆合法的風控設定記錄。`tamper` 在簽完之後改動欄位（重放／竄改）。"""
    account_id = account_id or _acct(wallet)
    issued_at = issued_at or _at()
    prefs = _prefs() if prefs is None else prefs
    msg = build_risk_settings_message(account_id=account_id, prefs=prefs,
                                      nonce=nonce, issued_at=issued_at)
    rec = build_risk_settings_record(account_id=account_id, prefs=prefs, nonce=nonce,
                                     issued_at=issued_at,
                                     signature=_sign(signer or wallet, msg),
                                     message=msg)
    if tamper:
        rec.update(tamper)
    return rec


def _unlock(wallet, *, account_id=None, nonce="u1", issued_at=None, signer=None,
            tamper=None) -> dict:
    account_id = account_id or _acct(wallet)
    issued_at = issued_at or _at()
    msg = build_risk_unlock_message(account_id=account_id, nonce=nonce,
                                    issued_at=issued_at)
    rec = build_risk_unlock_record(account_id=account_id, nonce=nonce,
                                   issued_at=issued_at,
                                   signature=_sign(signer or wallet, msg),
                                   message=msg)
    if tamper:
        rec.update(tamper)
    return rec


@pytest.fixture
def wallet():
    return Account.create()


# ── 快樂路徑 ──────────────────────────────────────────────────────────

def test_verifies_valid_settings_record(wallet):
    v = verify_risk_settings(_record(wallet), account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert v.account_id == _acct(wallet)
    assert v.user_address == wallet.address.lower()
    assert v.prefs["enabled"] is True
    assert v.prefs["max_drawdown_pct"] == "0.2"
    assert v.issued_at_s == pytest.approx(_NOW)


def test_verifies_valid_unlock_record(wallet):
    v = verify_risk_unlock(_unlock(wallet), account_id=_acct(wallet),
                           user_address=wallet.address, now_s=_NOW,
                           consume_nonce=_always)
    assert v.user_address == wallet.address.lower()
    assert v.nonce == "u1"


def test_record_field_sets_are_pinned_by_the_constants(wallet):
    """欄位集（含順序）＝ 常數。多一個少一個都要有人主動改。"""
    assert tuple(_record(wallet).keys()) == RISK_SETTINGS_FIELDS
    assert tuple(_unlock(wallet).keys()) == RISK_UNLOCK_FIELDS


def test_records_have_no_signer_field(wallet):
    """⭐ 記錄**沒有** signer 欄位：沒有這個欄位，就沒有人能誤用它當比對基準。"""
    assert "signer" not in _record(wallet) and "signer" not in _unlock(wallet)


def test_action_is_written_by_the_builder_not_the_caller(wallet):
    assert _record(wallet)["action"] == ACTION_RISK_SETTINGS
    assert _unlock(wallet)["action"] == ACTION_RISK_UNLOCK


# ── ⭐⭐ 待簽訊息：客戶簽的是完整內容 ──────────────────────────────────

def test_message_lists_every_single_risk_parameter(wallet):
    """⭐⭐ 每一個參數都必須逐行出現在客戶簽的原文裡（不是雜湊、不是部分欄位）。

    只簽雜湊的話，客戶在錢包裡看到的是一串他無從驗證的字元——而「我簽的是什麼」
    是這道防線唯一的價值來源。
    """
    prefs = _prefs()
    msg = build_risk_settings_message(account_id=_acct(wallet), prefs=prefs,
                                      nonce="n1", issued_at=_at())
    lines = msg.splitlines()
    assert "Risk Controls: enabled" in lines
    for spec in RISK_PARAM_SPECS:
        assert any(ln.startswith(f"{spec['name']}: ") for ln in lines), spec["name"]
    # 順序＝ RISK_PARAM_SPECS 的順序（單一定義點）。
    idx = [next(i for i, ln in enumerate(lines) if ln.startswith(f"{s['name']}: "))
           for s in RISK_PARAM_SPECS]
    assert idx == sorted(idx)


def test_message_reflects_the_actual_values_not_the_defaults(wallet):
    msg = build_risk_settings_message(
        account_id=_acct(wallet), prefs=_prefs(max_drawdown_pct="0.35",
                                               flatten_on_breach=False),
        nonce="n1", issued_at=_at())
    assert "max_drawdown_pct: 0.35" in msg
    assert "flatten_on_breach: false" in msg


def test_disabled_risk_controls_are_spelled_out_in_the_message(wallet):
    """關掉全部保護是最嚴重的一種調整——它必須是原文裡看得懂的一句話。"""
    msg = build_risk_settings_message(account_id=_acct(wallet),
                                      prefs=_prefs(enabled=False), nonce="n1",
                                      issued_at=_at())
    assert "Risk Controls: disabled" in msg


def test_unlock_message_spells_out_the_consequences(wallet):
    """解鎖訊息要用白話寫明後果：下一輪恢復交易、權益基準已在熔斷當下重置。"""
    msg = build_risk_unlock_message(account_id=_acct(wallet), nonce="u1",
                                    issued_at=_at())
    low = msg.lower()
    assert "resume" in low and "next cycle" in low
    assert "baseline" in low and "reset" in low


# ── ⭐⭐ 域分隔：四個模板兩兩不可碰撞 ──────────────────────────────────

def test_first_lines_are_distinct_domain_separators(wallet):
    """⭐⭐ 第一行是固定字面量且沒有任何輸入能到達它 ⇒ 兩個模板永遠不同字串。"""
    a = build_risk_settings_message(account_id=_acct(wallet), prefs=_prefs(),
                                    nonce="n1", issued_at=_at())
    b = build_risk_unlock_message(account_id=_acct(wallet), nonce="n1",
                                  issued_at=_at())
    c = build_capital_settings_message(account_id=_acct(wallet),
                                       allocated_capital="1000.00",
                                       capital_utilization="0.5000",
                                       nonce="n1", issued_at=_at())
    firsts = [m.splitlines()[0] for m in (a, b, c)]
    assert len(set(firsts)) == 3
    assert firsts[0] == "Filet: update copy-trading risk settings"
    assert firsts[1] == "Filet: resume copy-trading after a risk halt"


def test_settings_signature_cannot_be_redeemed_as_an_unlock(wallet):
    """⭐⭐ 一筆風控設定的授權絕不能被當成一次解除熔斷（攻擊者等客戶下次調門檻）。"""
    rec = _record(wallet)
    swapped = {**rec, "action": ACTION_RISK_UNLOCK}
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_unlock(swapped, account_id=_acct(wallet),
                           user_address=wallet.address, now_s=_NOW,
                           consume_nonce=_always)
    assert e.value.reason == "signer_mismatch"      # 訊息模板不同 → 還原出別人
    # action 欄位原封不動時，第 2 層（顯式動作檢查）先擋下並指名事件。
    with pytest.raises(RiskSettingsError) as e2:
        verify_risk_unlock(rec, account_id=_acct(wallet),
                           user_address=wallet.address, now_s=_NOW,
                           consume_nonce=_always)
    assert e2.value.reason == "action_mismatch"


def test_unlock_signature_cannot_be_redeemed_as_settings(wallet):
    """反方向同樣是結構性拒絕（只擋一邊的分隔符不是分隔符）。"""
    rec = _unlock(wallet)
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(rec, account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert e.value.reason == "action_mismatch"
    with pytest.raises(RiskSettingsError) as e2:
        verify_risk_settings({**rec, "action": ACTION_RISK_SETTINGS,
                              "prefs": default_prefs()},
                             account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert e2.value.reason == "signer_mismatch"


def test_capital_and_risk_records_do_not_cross_redeem(wallet):
    """既有的資金設定管線與新的風控管線互不相通（雙向）。"""
    account_id = _acct(wallet)
    cap_msg = build_capital_settings_message(
        account_id=account_id, allocated_capital="1000.00",
        capital_utilization="0.5000", nonce="n1", issued_at=_at())
    cap = build_capital_settings_record(
        account_id=account_id, allocated_capital="1000.00",
        capital_utilization="0.5000", nonce="n1", issued_at=_at(),
        signature=_sign(wallet, cap_msg), message=cap_msg)
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings({**cap, "prefs": default_prefs()},
                             account_id=account_id, user_address=wallet.address,
                             now_s=_NOW, consume_nonce=_always)
    assert e.value.reason == "action_mismatch"

    from spark.filet.capital_settings import CapitalSettingsError
    with pytest.raises(CapitalSettingsError) as e2:
        verify_capital_settings({**_record(wallet), "allocated_capital": "1000.00",
                                 "capital_utilization": "0.5000"},
                                account_id=account_id,
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always)
    assert e2.value.reason == "action_mismatch"


# ── 驗章：偽造與竄改 ──────────────────────────────────────────────────

def test_someone_elses_signature_is_rejected(wallet):
    other = Account.create()
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet, signer=other), account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert e.value.reason == "signer_mismatch"


def test_tampered_prefs_break_the_signature(wallet):
    """⭐ 竄改任何一個數值 → 重建的原文對不上 → signer_mismatch（不是靜默採用）。"""
    rec = _record(wallet)
    rec["prefs"] = {**rec["prefs"], "max_drawdown_pct": "0.5"}
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(rec, account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert e.value.reason == "signer_mismatch"


def test_flipping_enabled_breaks_the_signature(wallet):
    """總開關同樣受簽章保護——它是「有沒有保護」本身。"""
    rec = _record(wallet)
    rec["prefs"] = {**rec["prefs"], "enabled": False}
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(rec, account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert e.value.reason == "signer_mismatch"


def test_message_field_is_not_trusted(wallet):
    """`message` 只是稽核留存：改掉它不影響驗證（伺服器權威重建）。"""
    rec = _record(wallet, tamper={"message": "whatever the attacker likes"})
    assert verify_risk_settings(rec, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always).prefs["enabled"] is True


def test_account_mismatch_is_rejected(wallet):
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet), account_id="f" + "b2" * 20,
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always)
    assert e.value.reason == "account_mismatch"


def test_broken_signature_is_bad_signature(wallet):
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet, tamper={"signature": "0xdead"}),
                             account_id=_acct(wallet), user_address=wallet.address,
                             now_s=_NOW, consume_nonce=_always)
    assert e.value.reason == "bad_signature"


def test_malformed_nonce_is_rejected(wallet):
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet, tamper={"nonce": "bad nonce!"}),
                             account_id=_acct(wallet), user_address=wallet.address,
                             now_s=_NOW, consume_nonce=_always)
    assert e.value.reason == "malformed"


def test_out_of_range_values_are_rejected_not_clamped(wallet):
    """⭐ 超界一律拒絕（`malformed`），**絕不夾取**——夾取＝客戶簽 A、系統執行 B。"""
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet, tamper={
            "prefs": {**_prefs(), "max_drawdown_pct": "0.9"}}),
            account_id=_acct(wallet), user_address=wallet.address, now_s=_NOW,
            consume_nonce=_always)
    assert e.value.reason == "malformed"


def test_unknown_pref_field_is_rejected(wallet):
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet, tamper={
            "prefs": {**_prefs(), "surprise": "1"}}),
            account_id=_acct(wallet), user_address=wallet.address, now_s=_NOW,
            consume_nonce=_always)
    assert e.value.reason == "malformed"


def test_nonce_consumption_is_the_last_step(wallet):
    """副作用擺最後：格式錯／簽章錯時就燒掉 nonce ＝任何人送垃圾就能作廢客戶的授權。"""
    burned = []

    def _consume(n):
        burned.append(n)
        return True

    with pytest.raises(RiskSettingsError):
        verify_risk_settings(_record(wallet, signer=Account.create()),
                             account_id=_acct(wallet), user_address=wallet.address,
                             now_s=_NOW, consume_nonce=_consume)
    assert burned == []


def test_unusable_nonce_is_rejected(wallet):
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet), account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_never)
    assert e.value.reason == "nonce_unusable"


# ── ⭐⭐ 時效語意：持續意圖 vs 一次性動作 ──────────────────────────────

def test_settings_freshness_is_off_by_default(wallet):
    """⭐⭐ 引擎端（`max_age_s=None`）不檢查時效：風控設定是**持續意圖**。

    否則引擎停機超過 10 分鐘後，客戶的設定會因為過期而無聲地退回 env 值。
    """
    old = _record(wallet, issued_at=_at(-30 * 86400))
    assert verify_risk_settings(old, account_id=_acct(wallet),
                                user_address=wallet.address, now_s=_NOW,
                                consume_nonce=_always).prefs["enabled"] is True


def test_settings_freshness_can_be_enforced_by_the_api(wallet):
    """API 側可以（也應該）收緊：那裡驗的是「客戶剛剛按下的那一次」。"""
    old = _record(wallet, issued_at=_at(-RISK_SETTINGS_MAX_AGE_S - 60))
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(old, account_id=_acct(wallet),
                             user_address=wallet.address, now_s=_NOW,
                             consume_nonce=_always,
                             max_age_s=RISK_SETTINGS_MAX_AGE_S)
    assert e.value.reason == "expired"


def test_unlock_freshness_is_enforced_by_default(wallet):
    """⭐⭐ 解鎖是一次性動作：一份舊的解鎖記錄若還能生效，客戶等於簽一次就永久
    放棄熔斷保護（今天熔斷、舊記錄自動把它解開）。"""
    old = _unlock(wallet, issued_at=_at(-RISK_SETTINGS_MAX_AGE_S - 1))
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_unlock(old, account_id=_acct(wallet),
                           user_address=wallet.address, now_s=_NOW,
                           consume_nonce=_always)
    assert e.value.reason == "expired"


def test_unlock_from_the_future_is_rejected(wallet):
    future = _unlock(wallet, issued_at=_at(RISK_SETTINGS_MAX_AGE_S + 60))
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_unlock(future, account_id=_acct(wallet),
                           user_address=wallet.address, now_s=_NOW,
                           consume_nonce=_always)
    assert e.value.reason == "expired"


def test_naive_issued_at_is_rejected(wallet):
    """裸 naive 時間一律拒絕（沿 parse_issued_at：時區假設是看不見的假設）。"""
    with pytest.raises(RiskSettingsError) as e:
        verify_risk_settings(_record(wallet, tamper={"issued_at": "2026-07-30T00:00:00"}),
                             account_id=_acct(wallet), user_address=wallet.address,
                             now_s=_NOW, consume_nonce=_always)
    assert e.value.reason == "malformed"


# ── 落檔：兩個獨立的檔、同 account 覆蓋 ────────────────────────────────

def test_settings_and_unlock_live_in_separate_files(tmp_path):
    """⭐ 分開兩個檔：一個是持續意圖、一個是一次性動作，共命運的方向是 fail-open。"""
    assert risk_settings_path_for(tmp_path) != risk_unlock_path_for(tmp_path)
    assert risk_settings_path_for(tmp_path).endswith("risk_settings.json")
    assert risk_unlock_path_for(tmp_path).endswith("risk_unlock.json")


def test_write_overwrites_the_same_account(tmp_path, wallet):
    """同 account 覆蓋而非附加：檔案是「當前意圖」，不是流水帳。"""
    path = risk_settings_path_for(tmp_path)
    write_risk_settings(path, _record(wallet, nonce="n1"))
    write_risk_settings(path, _record(wallet, nonce="n2"))
    entries = load_risk_settings(path)
    assert [e["nonce"] for e in entries] == ["n2"]


def test_write_keeps_other_accounts(tmp_path, wallet):
    other = Account.create()
    path = risk_settings_path_for(tmp_path)
    write_risk_settings(path, _record(wallet))
    write_risk_settings(path, _record(other))
    assert {e["account_id"] for e in load_risk_settings(path)} == {
        _acct(wallet), _acct(other)}


def test_unlock_write_and_load_round_trip(tmp_path, wallet):
    path = risk_unlock_path_for(tmp_path)
    assert load_risk_unlocks(path) == []          # 不存在 → 空清單
    write_risk_unlock(path, _unlock(wallet, nonce="u1"))
    write_risk_unlock(path, _unlock(wallet, nonce="u2"))
    assert [e["nonce"] for e in load_risk_unlocks(path)] == ["u2"]


def test_written_files_are_readable_by_the_engine_user(tmp_path, wallet):
    """0644：讀端 filet-engine 是另一個 user（交換目錄無 setgid）。"""
    import os
    path = risk_settings_path_for(tmp_path)
    write_risk_settings(path, _record(wallet))
    assert os.stat(path).st_mode & 0o777 == 0o644
