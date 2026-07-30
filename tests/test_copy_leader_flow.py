"""leader 申贖流量中性化：adjusted_leader_equity 純函式錨例＋config 欄位。

錨例為預註冊數字（spec §3.3 表格），**不得改動**：decay=36h=129,600,000ms、
raw=Decimal("902154.21")。weight = max(0, 1 − age/decay)，age<0（時鐘漂移）視為 0。
"""
from decimal import Decimal

import pytest

from spark.copytrade.config import CopySettings
from spark.copytrade.leader_flow import adjusted_leader_equity
from spark.exchange.base import LedgerFlow

DECAY_MS = 129_600_000  # 36h
RAW = Decimal("902154.21")
NOW_MS = 1_753_920_000_000  # 固定 now（任意錨點；只有差值 age 有意義）
_H = 3_600_000  # 1 小時（ms）


def _flow(usdc: str, age_ms: int) -> LedgerFlow:
    return LedgerFlow(time_ms=NOW_MS - age_ms, usdc=Decimal(usdc))


def _adj(flows: list[LedgerFlow]) -> Decimal:
    return adjusted_leader_equity(RAW, flows, NOW_MS, DECAY_MS)


# ── 預註冊錨例（缺一不可）──────────────────────────────────────────────
def test_anchor_1_deposit_age_0h_fully_neutralized():
    assert _adj([_flow("1000", 0)]) == Decimal("901154.21")


def test_anchor_2_deposit_age_18h_half_neutralized():
    assert _adj([_flow("1000", 18 * _H)]) == Decimal("901654.21")


def test_anchor_3_deposit_age_36h_and_older_fully_decayed():
    assert _adj([_flow("1000", 36 * _H)]) == RAW
    assert _adj([_flow("1000", 72 * _H)]) == RAW  # 更老：weight 夾在 0，不得變負


def test_anchor_4_withdrawal_age_0h_adds_back_to_denominator():
    assert _adj([_flow("-10630.91", 0)]) == Decimal("912785.12")


def test_anchor_5_mixed_deposit_0h_withdrawal_18h():
    flows = [_flow("1000", 0), _flow("-10630.91", 18 * _H)]
    assert _adj(flows) == Decimal("906469.665")


def test_anchor_6_negative_age_clock_drift_treated_as_age_0():
    # 交易所時間戳在本機時鐘之後（漂移）：視同剛發生 → 全額中性化
    assert _adj([_flow("1000", -5 * 60 * 1000)]) == Decimal("901154.21")


def test_anchor_7_empty_flows_returns_raw():
    assert _adj([]) == RAW


def test_returns_decimal_type():
    assert isinstance(_adj([_flow("1000", 0)]), Decimal)


# ── config 欄位（Wave 4 規格 A）────────────────────────────────────────
def test_settings_defaults_disabled_and_36h():
    s = CopySettings()
    assert s.leader_flow_neutralization_enabled is False
    assert s.flow_decay_hours == Decimal("36")


def test_settings_from_env_parses_both_fields():
    s = CopySettings.from_env({
        "COPY_LEADER_FLOW_NEUTRALIZATION": "true  # 行內註解",
        "COPY_FLOW_DECAY_HOURS": "12",
    })
    assert s.leader_flow_neutralization_enabled is True
    assert s.flow_decay_hours == Decimal("12")


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_settings_rejects_non_positive_decay(bad):
    with pytest.raises(ValueError, match="flow_decay_hours"):
        CopySettings(flow_decay_hours=Decimal(bad))
