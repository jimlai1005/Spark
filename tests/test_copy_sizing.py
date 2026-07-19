"""copytrade/sizing.py 純函式層測試。數字全部手算可驗（各案例附計算式）。

hl-copytrader 對應（唯讀移植來源）：
  - scale factor：hl src/sync.py:21-51
  - 波動權重：hl src/weight.py:33-54,72-79（案例形態 port 自 hl tests/test_vol_partial.py）
  - 抗單保護：hl src/protection.py:44-108
"""
from decimal import Decimal

import pytest

from spark.copytrade.config import CopySettings
from spark.copytrade.sizing import (
    HoldingStats,
    VolStats,
    anti_holding_flags,
    compute_scale_factor,
    compute_volatility_stats,
    holding_stats_from_fills,
    position_weight,
    resolve_capital,
)

EQ = Decimal("10000")  # compute_volatility_stats 的 equity 參數（M1 未使用，僅佔位）


class TestResolveCapital:
    """port hl sync.py:21-26。模式由**顯式旗標**選擇（不再由 allocated 的值兼任）。"""

    def test_full_equity_uses_equity(self):
        assert resolve_capital(Decimal("2500"), Decimal("0"),
                               use_full_equity=True) == Decimal("2500")

    def test_allocated_positive_uses_fixed(self):
        assert resolve_capital(Decimal("2500"), Decimal("5000"),
                               use_full_equity=False) == Decimal("5000")

    def test_negative_equity_clamped_to_zero(self):
        # hl sync.py:26 的 max(my_equity, 0.0)
        assert resolve_capital(Decimal("-100"), Decimal("0"),
                               use_full_equity=True) == Decimal("0")

    def test_use_full_equity_is_required_keyword(self):
        """⭐ 模式必須在**呼叫點**宣告：沒有預設值可以讓呼叫端不小心選到一邊。
        兩個模式差的是整個曝險基準（固定本金 vs 整個帳戶）。"""
        with pytest.raises(TypeError):
            resolve_capital(Decimal("2500"), Decimal("0"))

    def test_fixed_mode_ignores_equity_entirely(self):
        """固定本金模式下 my_equity 完全不參與——包含權益比本金大的情形。
        （舊實作在此路徑上同解；本測試釘住的是「模式由旗標決定」而非由大小關係決定。）"""
        assert resolve_capital(Decimal("999999"), Decimal("5000"),
                               use_full_equity=False) == Decimal("5000")


class TestComputeScaleFactor:
    """port hl sync.py:29-51。"""

    def test_basic_hand_computed(self):
        # allocated=0 → resolve=my_equity=1000；scale = 1000×1×1/100000 = 0.01
        scale = compute_scale_factor(
            Decimal("100000"), Decimal("1000"),
            target_notional=Decimal("0"), settings=CopySettings(), weight=Decimal("1"),
        )
        assert scale == Decimal("0.01")

    def test_leverage_cap_halves_scale(self):
        # eff_lev = 800000/100000 = 8 > 上限 4 → scale ×= 4/8 → 0.01×0.5 = 0.005
        s = CopySettings(max_target_leverage=Decimal("4"))
        scale = compute_scale_factor(
            Decimal("100000"), Decimal("1000"),
            target_notional=Decimal("800000"), settings=s, weight=Decimal("1"),
        )
        assert scale == Decimal("0.005")

    def test_util_and_weight_compound(self):
        # 1000×0.5×0.8/100000 = 0.004
        s = CopySettings(capital_utilization=Decimal("0.5"))
        scale = compute_scale_factor(
            Decimal("100000"), Decimal("1000"),
            target_notional=Decimal("0"), settings=s, weight=Decimal("0.8"),
        )
        assert scale == Decimal("0.004")

    def test_zero_leader_equity_returns_zero(self):
        # hl sync.py:40-41 的除零防禦（1:1 移植，非 spark 加固）
        scale = compute_scale_factor(
            Decimal("0"), Decimal("1000"),
            target_notional=Decimal("0"), settings=CopySettings(), weight=Decimal("1"),
        )
        assert scale == Decimal("0")

    def test_cap_disabled_when_max_leverage_zero(self):
        # max_target_leverage=0（預設）→ 即使目標槓桿極高也不縮
        scale = compute_scale_factor(
            Decimal("100000"), Decimal("1000"),
            target_notional=Decimal("800000"), settings=CopySettings(), weight=Decimal("1"),
        )
        assert scale == Decimal("0.01")

    def test_allocated_capital_overrides_equity(self):
        # allocated=5000 → 5000×1×1/100000 = 0.05（不看 my_equity=1000）
        s = CopySettings(allocated_capital=Decimal("5000"), use_full_equity=False)
        scale = compute_scale_factor(
            Decimal("100000"), Decimal("1000"),
            target_notional=Decimal("0"), settings=s, weight=Decimal("1"),
        )
        assert scale == Decimal("0.05")


class TestComputeVolatilityStats:
    """port hl weight.py:33-54（案例形態對照 hl tests/test_vol_partial.py）。"""

    def test_too_few_days_is_none(self):
        # 只有 2 天（< 3）→ None（hl weight.py:42）
        assert compute_volatility_stats([Decimal("10"), Decimal("12")], EQ) is None

    def test_hand_computed(self):
        # baseline [8,8,12,12]：mu=10、sigma=pstdev=2；today=14 → z=(14-10)/2=2
        # reduction = min(max(2×0.2, 0), 0.7) = 0.4 → weight = 0.6
        series = [Decimal(x) for x in ("8", "8", "12", "12", "14")]
        vol = compute_volatility_stats(series, EQ)
        assert vol == VolStats(
            mu=Decimal("10"), sigma=Decimal("2"), z=Decimal("2"), weight=Decimal("0.6")
        )

    def test_sigma_zero_boundary(self):
        # 基準全同值 → sigma=0 → z=0、weight=1（hl weight.py:49-50）
        vol = compute_volatility_stats([Decimal("10")] * 4, EQ)
        assert vol == VolStats(
            mu=Decimal("10"), sigma=Decimal("0"), z=Decimal("0"), weight=Decimal("1")
        )

    def test_baseline_capped_at_lookback(self):
        # 20 天（值 1..20）→ 基準只取 today 前 14 天（值 6..19）→ mu=(6+19)/2=12.5
        # 若誤用全部 19 天，mu=10——此斷言鎖住 VOL_LOOKBACK_DAYS 上限（hl weight.py:45）
        series = [Decimal(i) for i in range(1, 21)]
        vol = compute_volatility_stats(series, EQ)
        assert vol.mu == Decimal("12.5")
        assert vol.weight < Decimal("1")

    def test_abnormal_today_lowers_weight(self):
        # port hl tests/test_vol_partial.py::test_abnormal_today_lowers_weight
        series = [Decimal(x) for x in ("8", "10", "12", "9", "100")]
        vol = compute_volatility_stats(series, EQ)
        assert vol.z > 0
        assert vol.weight < Decimal("1")

    def test_reduction_capped_at_max(self):
        # today=1000：z=(1000-10)/2=495 → reduction 封頂 0.7 → weight=0.3
        series = [Decimal(x) for x in ("8", "8", "12", "12", "1000")]
        vol = compute_volatility_stats(series, EQ)
        assert vol.weight == Decimal("0.3")


class TestPositionWeight:
    """port hl weight.py:72-79。"""

    VOL = VolStats(mu=Decimal("10"), sigma=Decimal("2"), z=Decimal("2"), weight=Decimal("0.6"))

    def test_vol_none_returns_manual_weight(self):
        s = CopySettings(position_weight=Decimal("0.8"))
        assert position_weight(s, None) == Decimal("0.8")

    def test_disabled_ignores_vol(self):
        s = CopySettings(position_weight=Decimal("0.8"), volatility_weight_enabled=False)
        assert position_weight(s, self.VOL) == Decimal("0.8")

    def test_enabled_multiplies(self):
        # 0.8 × 0.6 = 0.48
        s = CopySettings(position_weight=Decimal("0.8"), volatility_weight_enabled=True)
        assert position_weight(s, self.VOL) == Decimal("0.48")

    def test_manual_weight_clamped_to_one(self):
        # hl weight.py:74 的 clip 到 [0,1]
        s = CopySettings(position_weight=Decimal("1.5"))
        assert position_weight(s, None) == Decimal("1")


# 基準：5 筆 1h + 5 筆 2h → mu=5400s、sigma=1800s、n=10（= HOLDING_MIN_TRADES）
BASELINE = tuple([Decimal("3600")] * 5 + [Decimal("7200")] * 5)


class TestAntiHoldingFlags:
    """port hl protection.py:44-71。"""

    def test_disabled_all_false_regardless_of_input(self):
        # 預設 holding_protection_enabled=False → 全 False 短路（即使 z=3 極端輸入）
        stats = {
            "BTC": HoldingStats(Decimal("10800"), BASELINE),  # z=3
            "ETH": HoldingStats(Decimal("5400"), BASELINE),
        }
        assert anti_holding_flags(CopySettings(), stats) == {"BTC": False, "ETH": False}

    def test_enabled_flags_abnormal_holding(self):
        # BTC 持倉 10800s：z=(10800-5400)/1800=3 > 2 → True；ETH z=0 → False
        s = CopySettings(holding_protection_enabled=True)
        stats = {
            "BTC": HoldingStats(Decimal("10800"), BASELINE),
            "ETH": HoldingStats(Decimal("5400"), BASELINE),
        }
        assert anti_holding_flags(s, stats) == {"BTC": True, "ETH": False}

    def test_z_at_threshold_not_flagged(self):
        # z=(9000-5400)/1800=2.0 不超過門檻（hl protection.py:62 為嚴格 >）→ False
        s = CopySettings(holding_protection_enabled=True)
        stats = {"BTC": HoldingStats(Decimal("9000"), BASELINE)}
        assert anti_holding_flags(s, stats) == {"BTC": False}

    def test_insufficient_trades_not_flagged(self):
        # 完成交易數 9 < HOLDING_MIN_TRADES=10 → 不啟動（hl protection.py:47-49）
        s = CopySettings(holding_protection_enabled=True)
        stats = {"BTC": HoldingStats(Decimal("999999"), BASELINE[:9])}
        assert anti_holding_flags(s, stats) == {"BTC": False}

    def test_sigma_zero_not_flagged(self):
        # 基準全同值 → sigma=0 → 不啟動（hl protection.py:52-53）
        s = CopySettings(holding_protection_enabled=True)
        stats = {"BTC": HoldingStats(Decimal("999999"), tuple([Decimal("3600")] * 10))}
        assert anti_holding_flags(s, stats) == {"BTC": False}


class TestHoldingStatsFromFills:
    """port hl protection.py:74-108（fills 由呼叫端注入，不取網）。"""

    def test_reconstructs_completed_and_open(self):
        # 刻意亂序：BTC 平倉 fill 排最前，驗證函式內部依 time 排序（hl protection.py:93）
        fills = [
            # BTC：t=1000s 開倉（0→1），t=5000s 平倉（1→0）→ 完成一筆 4000s
            {"coin": "BTC", "side": "A", "sz": "1", "startPosition": "1", "time": 5_000_000},
            {"coin": "BTC", "side": "B", "sz": "1", "startPosition": "0", "time": 1_000_000},
            # ETH：t=2000s 開空（0→-2）後未平 → open_since
            {"coin": "ETH", "side": "A", "sz": "2", "startPosition": "0", "time": 2_000_000},
            # 現貨跳過（hl protection.py:86）
            {"coin": "PURR/USDC", "side": "B", "sz": "1", "startPosition": "0", "time": 3_000_000},
        ]
        completed, open_since = holding_stats_from_fills(fills)
        assert completed == [Decimal("4000")]
        assert open_since == {"ETH": Decimal("2000")}

    def test_reopen_after_close_tracks_new_open(self):
        fills = [
            {"coin": "BTC", "side": "B", "sz": "1", "startPosition": "0", "time": 1_000_000},
            {"coin": "BTC", "side": "A", "sz": "1", "startPosition": "1", "time": 5_000_000},
            {"coin": "BTC", "side": "B", "sz": "3", "startPosition": "0", "time": 8_000_000},
        ]
        completed, open_since = holding_stats_from_fills(fills)
        assert completed == [Decimal("4000")]
        assert open_since == {"BTC": Decimal("8000")}
