"""CopySettings 配置解析測試。"""
from decimal import Decimal

import pytest

from spark.copytrade.config import CopySettings


class TestCopySettingsDefaults:
    """測試預設值與硬編值。"""

    def test_from_env_all_defaults(self):
        """空 env 下應能建構全預設實例。"""
        settings = CopySettings.from_env({})

        # 硬編值驗證
        assert settings.leader_address == "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
        assert settings.live_trading is False
        assert settings.interval_s == 60
        assert settings.modify_policy == "modify-first"
        assert settings.flatten_on_breach is True
        assert settings.allocated_capital == Decimal("0")

        # 照抄 hl 的值驗證
        assert settings.capital_utilization == Decimal("1.0")
        assert settings.position_weight == Decimal("1.0")
        assert settings.max_target_leverage == Decimal("0")
        assert settings.min_order_notional == Decimal("10")
        assert settings.size_tolerance == Decimal("0.02")
        assert settings.max_drawdown_pct == Decimal("0.20")
        assert settings.settle_seconds == 2
        assert settings.modify_fail_ttl_s == 120
        assert settings.volatility_weight_enabled is True
        assert settings.holding_protection_enabled is False

        # 函式層預設
        assert settings.px_rel_tol == Decimal("1e-4")
        assert settings.slippage == Decimal("0.05")

    def test_env_override_bool(self, monkeypatch):
        """bool 型別環境變數覆蓋。"""
        monkeypatch.setenv("COPY_LIVE_TRADING", "true")
        settings = CopySettings.from_env({})
        assert settings.live_trading is True

    def test_env_override_decimal(self, monkeypatch):
        """Decimal 型別環境變數覆蓋。"""
        monkeypatch.setenv("COPY_MAX_DRAWDOWN_PCT", "0.25")
        settings = CopySettings.from_env({})
        assert settings.max_drawdown_pct == Decimal("0.25")

    def test_env_override_int(self, monkeypatch):
        """int 型別環境變數覆蓋。"""
        monkeypatch.setenv("COPY_INTERVAL_S", "30")
        settings = CopySettings.from_env({})
        assert settings.interval_s == 30

    def test_env_override_str(self, monkeypatch):
        """str 型別環境變數覆蓋。"""
        monkeypatch.setenv("COPY_LEADER_ADDRESS", "0xabcdef1234567890")
        settings = CopySettings.from_env({})
        assert settings.leader_address == "0xabcdef1234567890"

    def test_env_override_modify_policy(self, monkeypatch):
        """modify_policy 環境變數覆蓋。"""
        monkeypatch.setenv("COPY_MODIFY_POLICY", "cancel-place")
        settings = CopySettings.from_env({})
        assert settings.modify_policy == "cancel-place"

    def test_env_dict_override_os_environ(self, monkeypatch):
        """傳入 env dict 應覆蓋 os.environ。"""
        monkeypatch.setenv("COPY_INTERVAL_S", "60")
        settings = CopySettings.from_env({"COPY_INTERVAL_S": "120"})
        assert settings.interval_s == 120

    def test_env_from_none_uses_os_environ(self, monkeypatch):
        """from_env(None) 應使用 os.environ。"""
        monkeypatch.setenv("COPY_INTERVAL_S", "45")
        settings = CopySettings.from_env(None)
        assert settings.interval_s == 45


class TestInlineCommentParsing:
    """測試行內註解容錯（移植 hl test_config_inline_comment.py）。"""

    def test_env_bool_strips_inline_comment(self):
        """bool 值應去掉行內註解。"""
        settings = CopySettings.from_env({"COPY_LIVE_TRADING": "true  # 說明"})
        assert settings.live_trading is True

        settings = CopySettings.from_env({"COPY_LIVE_TRADING": "false # comment"})
        assert settings.live_trading is False

    def test_env_decimal_strips_inline_comment(self):
        """Decimal 值應去掉行內註解。"""
        settings = CopySettings.from_env({
            "COPY_MAX_DRAWDOWN_PCT": "0.15  # 回撤上限"
        })
        assert settings.max_drawdown_pct == Decimal("0.15")

    def test_env_int_strips_inline_comment(self):
        """int 值應去掉行內註解。"""
        settings = CopySettings.from_env({"COPY_INTERVAL_S": "90  # 秒"})
        assert settings.interval_s == 90

    def test_env_str_strips_inline_comment(self):
        """str 值應去掉行內註解。"""
        settings = CopySettings.from_env({
            "COPY_MODIFY_POLICY": "modify-first  # 優先修改"
        })
        assert settings.modify_policy == "modify-first"


class TestValidation:
    """測試 __post_init__ 驗證。"""

    def test_interval_s_must_be_positive(self):
        """interval_s <= 0 應拋 ValueError。"""
        with pytest.raises(ValueError, match="interval_s"):
            CopySettings.from_env({"COPY_INTERVAL_S": "0"})

        with pytest.raises(ValueError, match="interval_s"):
            CopySettings.from_env({"COPY_INTERVAL_S": "-1"})

    def test_max_drawdown_pct_must_be_in_range(self):
        """max_drawdown_pct 應在 (0, 1)。"""
        with pytest.raises(ValueError, match="max_drawdown_pct"):
            CopySettings.from_env({"COPY_MAX_DRAWDOWN_PCT": "0"})

        with pytest.raises(ValueError, match="max_drawdown_pct"):
            CopySettings.from_env({"COPY_MAX_DRAWDOWN_PCT": "1.0"})

        with pytest.raises(ValueError, match="max_drawdown_pct"):
            CopySettings.from_env({"COPY_MAX_DRAWDOWN_PCT": "1.5"})

        # 合法
        settings = CopySettings.from_env({"COPY_MAX_DRAWDOWN_PCT": "0.5"})
        assert settings.max_drawdown_pct == Decimal("0.5")

    def test_modify_policy_must_be_valid(self):
        """modify_policy 應在 {"modify-first", "cancel-place"}。"""
        with pytest.raises(ValueError, match="modify_policy"):
            CopySettings.from_env({"COPY_MODIFY_POLICY": "yolo"})

        # 合法
        settings = CopySettings.from_env({"COPY_MODIFY_POLICY": "cancel-place"})
        assert settings.modify_policy == "cancel-place"

    def test_capital_utilization_must_be_in_range(self):
        """capital_utilization 應在 (0, 1]。"""
        with pytest.raises(ValueError, match="capital_utilization"):
            CopySettings.from_env({"COPY_CAPITAL_UTILIZATION": "0"})

        with pytest.raises(ValueError, match="capital_utilization"):
            CopySettings.from_env({"COPY_CAPITAL_UTILIZATION": "1.5"})

        # 合法
        settings = CopySettings.from_env({"COPY_CAPITAL_UTILIZATION": "1.0"})
        assert settings.capital_utilization == Decimal("1.0")

    def test_min_order_notional_must_be_non_negative(self):
        """min_order_notional 應 >= 0。"""
        with pytest.raises(ValueError, match="min_order_notional"):
            CopySettings.from_env({"COPY_MIN_ORDER_NOTIONAL": "-1"})

        # 合法
        settings = CopySettings.from_env({"COPY_MIN_ORDER_NOTIONAL": "0"})
        assert settings.min_order_notional == Decimal("0")
