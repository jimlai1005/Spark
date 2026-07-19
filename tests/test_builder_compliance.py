"""tests/test_builder_compliance.py — builder 地址的 builder-fee 資格監控。

盯住的失效只有一種，但它是營收級的：**builder fee 無聲停止累積**。
HL 官方條件的主詞是 builder（我方），兩個條件任一不成立，成交照常、log 正常、
客戶無感，只有收入曲線變平。所以這裡的每一條測試都在問同一件事——
「這個狀況會不會被靜默放行」。

離線：只用純 stub adapter，不觸網（沿 tests/conftest.py 的 socket-ban）。
"""
from decimal import Decimal

import pytest

from spark.filet.builder_compliance import (BUILDER_MIN_PERP_EQUITY,
                                            COMPLIANT_ABSTRACTION_MODES,
                                            check_builder_compliance,
                                            check_builders, compliance_alerts)

_B = "0x" + "b" * 40
_B2 = "0x" + "c" * 40


class _StubAdapter:
    """唯讀 stub：淨值與模式可注入，也可注入「查詢爆炸」。"""

    def __init__(self, equity=Decimal("500"), mode="disabled",
                 equity_exc=None, mode_exc=None):
        self._equity = equity
        self._mode = mode
        self._equity_exc = equity_exc
        self._mode_exc = mode_exc
        self.calls = []

    def get_account_value(self, address):
        self.calls.append(("get_account_value", address))
        if self._equity_exc:
            raise self._equity_exc
        return self._equity

    def query_user_abstraction(self, user):
        self.calls.append(("query_user_abstraction", user))
        if self._mode_exc:
            raise self._mode_exc
        return self._mode


# --- 條件 1：perp 淨值門檻（邊界） ------------------------------------------

def test_equity_below_threshold_alerts():
    r = check_builder_compliance(_StubAdapter(equity=Decimal("99.99")), _B)
    assert r.equity_ok is False
    assert r.is_compliant is False
    alerts = compliance_alerts(r)
    assert len(alerts) == 1
    assert "99.99" in alerts[0] and "100" in alerts[0]


def test_equity_exactly_at_threshold_is_compliant():
    """⭐ 邊界：官方措辭是 "at least 100"，所以 == 100 必須放行。

    寫成 `> BUILDER_MIN_PERP_EQUITY` 會讓一個剛好合規的帳戶天天收到假告警——
    狼來了會訓練操作者忽略這個告警，而它正是唯一會示警真正斷線的地方。
    """
    assert BUILDER_MIN_PERP_EQUITY == Decimal("100")
    r = check_builder_compliance(_StubAdapter(equity=Decimal("100")), _B)
    assert r.equity_ok is True
    assert r.is_compliant is True
    assert compliance_alerts(r) == []


# --- 條件 2：account abstraction mode --------------------------------------

def test_disabled_is_the_only_compliant_mode():
    assert COMPLIANT_ABSTRACTION_MODES == frozenset({"disabled"})
    r = check_builder_compliance(_StubAdapter(mode="disabled"), _B)
    assert r.mode_ok is True
    assert compliance_alerts(r) == []


@pytest.mark.parametrize("mode", ["unifiedAccount", "default", "somethingNew", ""])
def test_non_disabled_modes_alert_including_default(mode):
    """⭐⭐ `"default"` 也必須告警——它的語意（是否等同 standard）**未經官方確認**，
    而猜錯的兩個方向代價不對稱：猜合規而錯 → builder fee 靜默歸零且監控說一切正常；
    猜不合規而錯 → 一則需人工確認一次的告警。不賭未經驗證的語意。

    ⭐ 變異測試點：把 "default" 加進 COMPLIANT_ABSTRACTION_MODES → 本測試轉紅。

    `"somethingNew"` 一併釘住白名單語意：沒見過的新模式一律不放行（黑名單會讓 HL
    明天新增一種模式時靜默放行，那正是本監控存在的理由）。
    """
    r = check_builder_compliance(_StubAdapter(mode=mode), _B)
    assert r.mode_ok is False
    assert r.is_compliant is False
    alerts = compliance_alerts(r)
    assert len(alerts) == 1
    assert repr(mode) in alerts[0]


# --- 兩者皆不符：兩則告警都要出現 ------------------------------------------

def test_both_conditions_failing_produce_two_separate_alerts():
    """⭐ 不得只報一個：兩者的處置動作完全不同（入金 vs 改帳戶設定），
    一則含糊的告警會讓操作者只修掉他先看到的那個，以為修完了。"""
    r = check_builder_compliance(
        _StubAdapter(equity=Decimal("10"), mode="unifiedAccount"), _B)
    assert r.equity_ok is False and r.mode_ok is False
    alerts = compliance_alerts(r)
    assert len(alerts) == 2
    assert any("淨值" in a for a in alerts)
    assert any("abstraction mode" in a for a in alerts)


# --- 查詢失敗：未知 ＋ 告警，絕不報合規（fail-closed） ----------------------

def test_equity_query_failure_is_unknown_not_compliant():
    r = check_builder_compliance(
        _StubAdapter(equity_exc=ConnectionError("boom")), _B)
    assert r.equity_ok is None          # 三態：不知道 ≠ False ≠ True
    assert r.has_unknown is True
    assert r.is_compliant is False      # ⭐ 查不到絕不放行
    alerts = compliance_alerts(r)
    assert any("未知" in a for a in alerts)
    assert any("不得視為合規" in a for a in alerts)
    assert any("perp 淨值查詢失敗" in a for a in alerts)


def test_mode_query_failure_is_unknown_not_compliant():
    r = check_builder_compliance(_StubAdapter(mode_exc=TimeoutError("slow")), _B)
    assert r.mode_ok is None
    assert r.is_compliant is False
    assert any("不得視為合規" in a for a in compliance_alerts(r))


def test_one_query_failing_does_not_mask_the_other_verdict():
    """⭐ 錯誤隔離：模式查詢掛掉時，淨值那一半仍要給出明確判定。
    一個不相干的 API 故障不該把「真的跌破門檻」蓋成「未知」。"""
    r = check_builder_compliance(
        _StubAdapter(equity=Decimal("5"), mode_exc=ConnectionError("boom")), _B)
    assert r.equity_ok is False         # 明確判定，沒被拖成 None
    assert r.mode_ok is None
    alerts = compliance_alerts(r)
    assert any("低於 HL 官方門檻" in a for a in alerts)
    assert any("未知" in a for a in alerts)


def test_check_builder_compliance_never_raises():
    """本函式的呼叫端是日報：任何一個 builder 的查詢炸掉都不該中止整份報表。"""
    r = check_builder_compliance(
        _StubAdapter(equity_exc=ValueError("schema drift"),
                     mode_exc=ValueError("schema drift")), _B)
    assert r.is_compliant is False
    assert len(r.errors) == 2


# --- 多 builder 進入點 ------------------------------------------------------

def test_check_builders_aggregates_all_alerts():
    adapters = {_B: _StubAdapter(equity=Decimal("1")),
                _B2: _StubAdapter(mode="unifiedAccount")}
    seq = [adapters[_B], adapters[_B2]]
    results, alerts = check_builders(lambda: seq.pop(0), [_B, _B2])
    assert [r.builder for r in results] == [_B, _B2]
    assert len(alerts) == 2
    assert any(_B in a for a in alerts) and any(_B2 in a for a in alerts)


def test_check_builders_all_compliant_is_silent():
    """完全合規 → 空清單。無話可說時就不要說話，否則告警會變成背景噪音。"""
    _results, alerts = check_builders(lambda: _StubAdapter(), [_B, _B2])
    assert alerts == []
