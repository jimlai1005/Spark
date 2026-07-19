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
        assert settings.max_consecutive_errors == 5
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
        """str 型別環境變數覆蓋（合法 42 字元地址）。"""
        addr = "0xabcdef1234567890abcdef1234567890abcdef12"
        monkeypatch.setenv("COPY_LEADER_ADDRESS", addr)
        settings = CopySettings.from_env({})
        assert settings.leader_address == addr

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

    def test_max_consecutive_errors_must_be_positive(self):
        """max_consecutive_errors <= 0 應拋 ValueError。"""
        with pytest.raises(ValueError, match="max_consecutive_errors"):
            CopySettings.from_env({"COPY_MAX_CONSECUTIVE_ERRORS": "0"})

        with pytest.raises(ValueError, match="max_consecutive_errors"):
            CopySettings.from_env({"COPY_MAX_CONSECUTIVE_ERRORS": "-3"})

        # env 覆蓋合法值
        settings = CopySettings.from_env({"COPY_MAX_CONSECUTIVE_ERRORS": "10"})
        assert settings.max_consecutive_errors == 10

    def test_leader_address_must_be_valid(self):
        """leader_address 需非空、0x 前綴、長度 42。"""
        with pytest.raises(ValueError, match="leader_address"):
            CopySettings.from_env({"COPY_LEADER_ADDRESS": ""})

        with pytest.raises(ValueError, match="leader_address"):
            CopySettings.from_env(
                {"COPY_LEADER_ADDRESS": "f97ad6704baec104d00b88e0c157e2b7b3a1ddd1ab"}
            )  # 42 字元但無 0x 前綴

        with pytest.raises(ValueError, match="leader_address"):
            CopySettings.from_env({"COPY_LEADER_ADDRESS": "0xabc"})  # 太短


class TestEnvParseErrors:
    """測試 env 解析失敗時的錯誤訊息含欄位名。"""

    def test_int_parse_error_names_key(self):
        """int 解析空字串應拋含 env key 名的 ValueError。"""
        with pytest.raises(ValueError, match="COPY_INTERVAL_S"):
            CopySettings.from_env({"COPY_INTERVAL_S": ""})

    def test_int_parse_error_garbage_names_key(self):
        """int 解析非數字應拋含 env key 名的 ValueError。"""
        with pytest.raises(ValueError, match="COPY_SETTLE_SECONDS"):
            CopySettings.from_env({"COPY_SETTLE_SECONDS": "abc"})

    def test_decimal_parse_error_names_key(self):
        """Decimal 解析空字串應拋含 env key 名的 ValueError。"""
        with pytest.raises(ValueError, match="COPY_MAX_DRAWDOWN_PCT"):
            CopySettings.from_env({"COPY_MAX_DRAWDOWN_PCT": ""})


class TestSlippageValidation:
    """測試 slippage 範圍守衛。"""

    def test_slippage_must_be_in_range(self):
        """slippage 應在 [0, 1)。"""
        # 負值拒絕
        with pytest.raises(ValueError, match="slippage"):
            CopySettings.from_env({"COPY_SLIPPAGE": "-0.1"})

        # >= 1 拒絕
        with pytest.raises(ValueError, match="slippage"):
            CopySettings.from_env({"COPY_SLIPPAGE": "1.0"})

        with pytest.raises(ValueError, match="slippage"):
            CopySettings.from_env({"COPY_SLIPPAGE": "1.5"})

        # 合法值通過
        settings = CopySettings.from_env({"COPY_SLIPPAGE": "0.01"})
        assert settings.slippage == Decimal("0.01")

        settings = CopySettings.from_env({"COPY_SLIPPAGE": "0.0"})
        assert settings.slippage == Decimal("0.0")

    def test_flatten_slippage_default_and_env_override(self):
        """緊急滑價：預設值與 env 覆寫。"""
        assert CopySettings().flatten_slippage == Decimal("0.30")
        s = CopySettings.from_env({
            "COPY_FLATTEN_SLIPPAGE": "0.5",
        })
        assert s.flatten_slippage == Decimal("0.5")

    def test_flatten_slippage_out_of_range_rejected(self):
        """越界守衛：[0,1) 之外必須拒絕。"""
        with pytest.raises(ValueError, match="flatten_slippage"):
            CopySettings(flatten_slippage=Decimal("1.0"))
        with pytest.raises(ValueError, match="flatten_slippage"):
            CopySettings(flatten_slippage=Decimal("-0.1"))


class TestUseFullEquityFlag:
    """⭐ 本金模式的顯式旗標（解除 `allocated_capital == 0` 的語意重載）。

    改動前 `0` 同時是「邊界檢查的下界」與「用全部權益」的開關，兩者相撞讓客戶
    無法透過資金設定 API 表達「用全部權益」。本組測試釘住新的不變量與**遷移
    相容性**——既有部署的 EnvironmentFile 不含 COPY_USE_FULL_EQUITY。
    """

    def test_default_is_full_equity_matching_the_legacy_zero(self):
        """預設＝全權益，與改動前 `allocated_capital=0` 的語意相同（零行為變化）。"""
        s = CopySettings()
        assert s.use_full_equity is True
        assert s.allocated_capital == Decimal("0")

    def test_legacy_env_with_allocated_capital_infers_fixed_mode(self):
        """⭐ 遷移相容：設了 COPY_ALLOCATED_CAPITAL、沒設旗標的既有部署 → 固定本金。
        推導成全權益會讓那台機器的本金基準從 5000 變成整個帳戶。"""
        s = CopySettings.from_env({"COPY_ALLOCATED_CAPITAL": "5000"})
        assert s.use_full_equity is False
        assert s.allocated_capital == Decimal("5000")

    def test_legacy_env_without_allocated_capital_infers_full_equity(self):
        """沒設金額、也沒設旗標的既有部署 → 全權益（＝改動前 0 的語意）。"""
        assert CopySettings.from_env({}).use_full_equity is True

    def test_explicit_flag_overrides_the_inference(self):
        s = CopySettings.from_env({"COPY_ALLOCATED_CAPITAL": "5000",
                                   "COPY_USE_FULL_EQUITY": "false"})
        assert s.use_full_equity is False and s.allocated_capital == Decimal("5000")

    def test_contradictory_env_refuses_to_start(self):
        """⭐⭐ 顯式旗標與金額矛盾 → **拒絕啟動**，不靜默挑一邊。
        挑哪一邊都是替客戶決定曝險倍數，而錯的那一邊沒有人會發現。"""
        with pytest.raises(ValueError, match="必須為 0"):
            CopySettings.from_env({"COPY_ALLOCATED_CAPITAL": "5000",
                                   "COPY_USE_FULL_EQUITY": "true"})

    def test_fixed_mode_with_zero_capital_refuses_to_start(self):
        """另一半的矛盾：固定本金模式配 0 元。本金 0 會讓所有目標部位歸零——
        那是**靜默**停止跟單，比拒絕啟動危險得多。"""
        with pytest.raises(ValueError, match="必須 > 0"):
            CopySettings.from_env({"COPY_ALLOCATED_CAPITAL": "0",
                                   "COPY_USE_FULL_EQUITY": "false"})

    def test_direct_construction_enforces_the_same_invariant(self):
        """不變量在 __post_init__，所以 from_env 之外的建構路徑（引擎套用客戶
        設定時用的 dataclasses.replace）同樣受保護。"""
        with pytest.raises(ValueError):
            CopySettings(allocated_capital=Decimal("100"), use_full_equity=True)
        with pytest.raises(ValueError):
            CopySettings(allocated_capital=Decimal("0"), use_full_equity=False)

    def test_env_flag_accepts_inline_comment(self):
        """systemd EnvironmentFile 的行內註解（沿 _clean 的既有慣例）。"""
        s = CopySettings.from_env({"COPY_ALLOCATED_CAPITAL": "5000",
                                   "COPY_USE_FULL_EQUITY": "false   # 固定本金"})
        assert s.use_full_equity is False
