"""tests/test_vault_policy.py
vault leader 保護設定（spark.copytrade.vault_policy）。

⭐ 這是「引擎每輪自衛」那一層的單一常數來源：watcher 注入 env 是顯式紀錄，
但運行中的 follower 可經簽章換 leader 而 watcher 不重寫 env——引擎層按本輪
解析出的 kind 套保護，才是不可繞過的執行點。兩層共用同一顆常數
（VAULT_MAX_TARGET_LEVERAGE），不並存兩個 20（工程原則 1）。
"""
from dataclasses import fields
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.vault_policy import (KIND_STANDARD, KIND_VAULT,
                                          VAULT_MAX_TARGET_LEVERAGE,
                                          apply_vault_policy)


def _diff_fields(a: CopySettings, b: CopySettings) -> set[str]:
    return {f.name for f in fields(CopySettings)
            if getattr(a, f.name) != getattr(b, f.name)}


def test_standard_kind_returns_the_very_same_object():
    """standard leader → 原物件原樣回傳（is 同一，不是等值複本）。"""
    s = CopySettings()
    assert apply_vault_policy(s, KIND_STANDARD) is s


def test_vault_enables_cap_when_env_disables_it():
    """env 沒設槓桿上限（0＝停用）→ vault 保護補上 20。"""
    s = CopySettings(max_target_leverage=Decimal("0"))
    out = apply_vault_policy(s, KIND_VAULT)
    assert out.max_target_leverage == VAULT_MAX_TARGET_LEVERAGE == Decimal("20")


def test_vault_clamps_a_looser_env_cap():
    """env 設了比 20 鬆的上限（25）→ 收緊到 20（min 語意，不是覆蓋）。"""
    s = CopySettings(max_target_leverage=Decimal("25"))
    assert apply_vault_policy(s, KIND_VAULT).max_target_leverage == Decimal("20")


def test_vault_keeps_a_tighter_env_cap():
    """env 設了比 20 緊的上限（10）→ 尊重客戶更保守的選擇，不放寬。"""
    s = CopySettings(max_target_leverage=Decimal("10"))
    assert apply_vault_policy(s, KIND_VAULT).max_target_leverage == Decimal("10")


def test_vault_forces_flow_neutralization_on():
    """vault → 申贖流量中性化強制開啟（防贖回潮分母瞬縮的被動放大）。"""
    s = CopySettings(leader_flow_neutralization_enabled=False)
    assert apply_vault_policy(s, KIND_VAULT).leader_flow_neutralization_enabled is True


def test_vault_apply_changes_exactly_two_fields_and_not_in_place():
    """只動兩個欄位，且原物件不被就地修改（frozen dataclass ＋ replace）。"""
    s = CopySettings(max_target_leverage=Decimal("0"),
                     leader_flow_neutralization_enabled=False)
    out = apply_vault_policy(s, KIND_VAULT)
    assert _diff_fields(s, out) == {"max_target_leverage",
                                    "leader_flow_neutralization_enabled"}
    # 原物件維持原值（frozen 本身保證；此處釘住行為）。
    assert s.max_target_leverage == Decimal("0")
    assert s.leader_flow_neutralization_enabled is False


def test_vault_apply_is_idempotent_and_does_not_rebuild():
    """已套用過的 settings 再套一次 → 同一物件。引擎每輪都套（每輪自衛），
    idempotent 才不會讓 vault follower 每輪重建一份 settings。"""
    s = apply_vault_policy(CopySettings(), KIND_VAULT)
    assert apply_vault_policy(s, KIND_VAULT) is s
