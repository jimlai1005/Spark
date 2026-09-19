"""tests/test_referral_apply.py
引擎側套用客戶簽章的推薦碼 opt-in（spark.filet.referral_apply）。

⭐ 這裡決定引擎什麼時候會對主網送出不可撤銷的 `setReferrer`。本檔盯住的事：
(1) 沒有記錄／記錄壞掉／推薦碼不符 → 不送、不重複告警。
(2) 送之前**先查**鏈上狀態，已有推薦人（不論是不是 Filet 的碼）→ 不送。
(3) 送出的每一種結果（成功、`Referrer already set`、其他 err、例外）分屬不同分類
    （工程原則 2）：transient 例外重試，`already set` 視同已設，其他 err 永久拒絕。
(4) 全程**絕不 raise**、絕不中斷跟單。

用真密碼學（eth_account 本地運算，不觸網）；純檔案操作＋FakeAdapter，全離線。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.copytrade.notifier import RecordingNotifier
from spark.exchange.fakes import FakeAdapter
from spark.filet.referral_apply import ReferralOptinApplier
from spark.filet.referral_optin import (build_referral_optin_message,
                                        build_referral_optin_record,
                                        write_referral_optin)

_NOW = 1_800_000_000.0
_LEADER = "0x" + "a1" * 20
_CODE = "FILET123"


def _at(offset_s: float = 0.0) -> str:
    return datetime.fromtimestamp(_NOW + offset_s, timezone.utc).isoformat()


def _acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


class _SeqAdapter(FakeAdapter):
    """`query_referred_by` 依呼叫序回傳不同值（測「送出前後鏈上狀態不同」）。

    真實 HL 在 `setReferrer` 送達後鏈上狀態才會變化；`FakeAdapter` 本身刻意維持
    單一靜態槽（Task 2 範圍），這裡疊一層供本檔測試「送出前 None、送出後有值」
    的情境，不改動 `fakes.py`。"""

    def __init__(self, *, referred_by_sequence, **kw):
        super().__init__(**kw)
        self._referred_by_sequence = list(referred_by_sequence)

    def query_referred_by(self, user: str) -> str | None:
        self.calls["query_referred_by"].append({"user": user})
        return self._referred_by_sequence.pop(0)


class _Env:
    """一份完整的引擎現場：manifest ＋ opt-in 記錄檔 ＋ notifier。"""

    def __init__(self, tmp_path: Path, *, wallet):
        self.tmp = tmp_path
        self.wallet = wallet
        self.account_id = _acct(wallet)
        self.manifest = tmp_path / "followers.json"
        self.manifest.write_text(json.dumps({"followers": [
            {"account_id": self.account_id, "user_address": wallet.address,
             "builder_address": "0x" + "22" * 20, "network": "mainnet",
             "label": "", "leader_address": _LEADER}]}))
        self.optin_path = tmp_path / "referral_optin.json"
        self.notifier = RecordingNotifier()

    def applier(self, *, adapter, expected_code=_CODE, notifier=None,
               account_id=None) -> ReferralOptinApplier:
        return ReferralOptinApplier(
            account_id=account_id or self.account_id,
            manifest_path=self.manifest, optin_path=self.optin_path,
            expected_code=expected_code, adapter=adapter,
            notifier=notifier or self.notifier)

    def write_optin(self, *, code=_CODE, nonce="n1", issued_at=None, signer=None,
                    account_id=None, tamper=None) -> dict:
        account_id = account_id or self.account_id
        issued_at = issued_at or _at()
        msg = build_referral_optin_message(account_id=account_id, code=code,
                                           nonce=nonce, issued_at=issued_at)
        sig = (signer or self.wallet).sign_message(
            encode_defunct(text=msg)).signature.hex()
        rec = build_referral_optin_record(account_id=account_id, code=code,
                                          nonce=nonce, issued_at=issued_at,
                                          signature=sig, message=msg)
        if tamper:
            rec.update(tamper)
        write_referral_optin(self.optin_path, rec)
        return rec

    def crits(self):
        return [r for r in self.notifier.records if r[0] == "critical"]

    def warns(self):
        return [r for r in self.notifier.records if r[0] == "warn"]

    def infos(self):
        return [r for r in self.notifier.records if r[0] == "info"]


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path, wallet=Account.create())


# ── 沒有記錄／記錄壞掉 ─────────────────────────────────────────────────

def test_no_record_does_nothing_and_does_not_alert(env):
    adapter = FakeAdapter()
    applier = env.applier(adapter=adapter)
    applier.apply_once()
    assert dict(adapter.calls) == {}
    assert env.notifier.records == []
    assert applier.status == "pending"


def test_verify_failure_is_critical_once_and_not_repeated_for_same_record(env):
    env.write_optin(tamper={"signature": "0xbad"})
    adapter = FakeAdapter()
    applier = env.applier(adapter=adapter)
    applier.apply_once()
    applier.apply_once()
    assert len(env.crits()) == 1
    assert env.crits()[0][3].startswith("referral_verify_failed:")
    # 驗章失敗發生在鏈上查詢之前，不該打任何 adapter 呼叫。
    assert dict(adapter.calls) == {}
    assert applier.status == "pending"


def test_code_mismatch_is_critical(env):
    env.write_optin(code="OTHERCODE")
    adapter = FakeAdapter()
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert len(env.crits()) == 1
    assert env.crits()[0][3] == "referral_code_mismatch"
    assert dict(adapter.calls) == {}
    assert applier.status == "pending"


# ── 鏈上查詢 ───────────────────────────────────────────────────────────

def test_query_exception_warns_and_retries_next_round(env):
    env.write_optin()
    adapter = FakeAdapter(referred_by_raises=RuntimeError("boom"))
    applier = env.applier(adapter=adapter)
    applier.apply_once()
    assert len(env.warns()) == 1
    assert env.warns()[0][3] == "referral_query_failed"
    assert applier.status == "pending"
    applier.apply_once()
    assert len(env.warns()) == 2
    assert len(adapter.calls["query_referred_by"]) == 2
    assert adapter.calls.get("set_referrer", []) == []


def test_onchain_already_has_our_code_is_done_and_not_sent(env):
    env.write_optin(code=_CODE)
    adapter = FakeAdapter(referred_by=_CODE)
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert applier.status == "already_referred"
    assert len(env.infos()) == 1
    assert adapter.calls.get("set_referrer", []) == []


def test_onchain_already_has_other_referrer_is_done_and_not_sent(env):
    env.write_optin(code=_CODE)
    adapter = FakeAdapter(referred_by="SOMEONE_ELSE")
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert applier.status == "already_referred"
    assert "其他推薦人" in env.infos()[0][2]
    assert adapter.calls.get("set_referrer", []) == []


# ── setReferrer 送出 ──────────────────────────────────────────────────

def test_success_path_sets_once_and_confirms(env):
    env.write_optin(code=_CODE)
    adapter = _SeqAdapter(referred_by_sequence=[None, _CODE])
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert applier.status == "applied"
    assert len(adapter.calls["set_referrer"]) == 1
    assert len(env.infos()) == 1
    # 已完成：再呼叫一次不該再打 adapter。
    applier.apply_once()
    assert len(adapter.calls["set_referrer"]) == 1
    assert len(adapter.calls["query_referred_by"]) == 2


def test_referrer_already_set_error_is_treated_as_already_referred(env):
    env.write_optin(code=_CODE)
    adapter = _SeqAdapter(referred_by_sequence=[None, _CODE],
                          set_referrer_result={"status": "err",
                                               "response": "Referrer already set"})
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert applier.status == "already_referred"
    assert len(adapter.calls["set_referrer"]) == 1
    assert len(adapter.calls["query_referred_by"]) == 2


def test_other_err_is_critical_and_rejected_and_second_call_skips_adapter(env):
    env.write_optin(code=_CODE)
    adapter = FakeAdapter(referred_by=None,
                          set_referrer_result={"status": "err",
                                               "response": "Referral code not registered"})
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert applier.status == "rejected"
    assert len(env.crits()) == 1
    assert env.crits()[0][3] == "referral_set_rejected"
    assert len(adapter.calls["query_referred_by"]) == 1
    assert len(adapter.calls["set_referrer"]) == 1
    applier.apply_once()
    assert len(adapter.calls["query_referred_by"]) == 1
    assert len(adapter.calls["set_referrer"]) == 1


def test_confirm_mismatch_after_set_warns_and_retries_next_round(env):
    env.write_optin(code=_CODE)
    adapter = _SeqAdapter(referred_by_sequence=[None, "SOMETHING_ELSE"])
    applier = env.applier(adapter=adapter, expected_code=_CODE)
    applier.apply_once()
    assert applier.status == "pending"
    assert len(env.warns()) == 1
    assert env.warns()[0][3] == "referral_verify_mismatch"
    assert len(adapter.calls["set_referrer"]) == 1
