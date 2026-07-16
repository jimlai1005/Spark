"""CopySettings——環境驅動的配置，live_trading 預設關。"""
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


def _clean(val: str | None) -> str | None:
    """去掉行內註解與前後空白。
    為何需要：systemd 的 EnvironmentFile 不會去掉行內註解（python-dotenv 會），
    所以 `KEY=true   # 說明` 經 systemd 會變成 'true   # 說明'，直接 .lower()=='true' 會失敗。
    這裡統一處理，讓兩種載入方式行為一致。對「沒有行內註解」的值無任何影響。"""
    if val is None:
        return val
    return val.split("#", 1)[0].strip()


def _env_str(key: str, default: str, env: Mapping[str, str] | None = None) -> str:
    """解析字串型環境變數（去掉行內註解）。
    優先序：env dict（如有）> os.environ > default。"""
    # 先檢查傳入的 env dict
    if env is not None and key in env:
        val = env[key]
    else:
        # 沒有就試 os.environ
        val = os.getenv(key)
    return _clean(val if val is not None else default)


def _env_bool(key: str, default: str, env: Mapping[str, str] | None = None) -> bool:
    """解析布林型環境變數（去掉行內註解）。
    優先序：env dict（如有）> os.environ > default。"""
    if env is not None and key in env:
        val = env[key]
    else:
        val = os.getenv(key)
    return _clean(val if val is not None else default).lower() == "true"


def _env_int(key: str, default: str, env: Mapping[str, str] | None = None) -> int:
    """解析整數型環境變數（去掉行內註解）。
    優先序：env dict（如有）> os.environ > default。
    解析失敗時 ValueError 帶 env key 名，方便定位是哪個變數壞了。"""
    if env is not None and key in env:
        val = env[key]
    else:
        val = os.getenv(key)
    cleaned = _clean(val if val is not None else default)
    try:
        return int(cleaned)
    except (ValueError, TypeError) as e:
        raise ValueError(f"{key} 解析失敗: {cleaned!r}") from e


def _env_decimal(key: str, default: str, env: Mapping[str, str] | None = None) -> Decimal:
    """解析 Decimal 型環境變數（去掉行內註解）。
    優先序：env dict（如有）> os.environ > default。
    解析失敗時 ValueError 帶 env key 名，方便定位是哪個變數壞了。"""
    if env is not None and key in env:
        val = env[key]
    else:
        val = os.getenv(key)
    cleaned = _clean(val if val is not None else default)
    try:
        return Decimal(cleaned)
    except (ArithmeticError, ValueError, TypeError) as e:
        raise ValueError(f"{key} 解析失敗: {cleaned!r}") from e


@dataclass(frozen=True)
class CopySettings:
    """跟單引擎配置。

    分三類欄位：
    1. 刻意覆蓋（不讀 hl）：leader_address, live_trading, interval_s, modify_policy,
       flatten_on_breach, allocated_capital
    2. 照抄 hl 預設值：capital_utilization, position_weight, max_target_leverage,
       min_order_notional, size_tolerance, max_drawdown_pct, settle_seconds,
       modify_fail_ttl_s, max_consecutive_errors, volatility_weight_enabled,
       holding_protection_enabled
    3. 函式層預設（硬編）：px_rel_tol, slippage
    """
    # 刻意覆蓋
    leader_address: str = "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
    live_trading: bool = False  # 紅線 5：刻意覆蓋 hl，預設關
    interval_s: int = 60  # 每分鐘（刻意覆蓋 hl 的 hourly CHECK_MINUTE=55）
    modify_policy: str = "modify-first"  # 或 "cancel-place"；預設不得改（等 T1.3）
    flatten_on_breach: bool = True  # 拍板 #2：回撤自動全平預設開
    allocated_capital: Decimal = Decimal("0")  # 0=用全權益，刻意覆蓋 hl 的 5000

    # 照抄 hl 預設值（來自 hl-copytrader config.py:33-115）
    capital_utilization: Decimal = Decimal("1.0")  # hl CAPITAL_UTILIZATION
    position_weight: Decimal = Decimal("1.0")  # hl POSITION_WEIGHT
    max_target_leverage: Decimal = Decimal("0")  # hl MAX_TARGET_LEVERAGE
    min_order_notional: Decimal = Decimal("10")  # hl MIN_ORDER_NOTIONAL
    size_tolerance: Decimal = Decimal("0.02")  # hl SIZE_TOLERANCE
    max_drawdown_pct: Decimal = Decimal("0.20")  # hl MAX_DRAWDOWN_PCT
    settle_seconds: int = 2  # hl orders.py SETTLE_SECONDS
    modify_fail_ttl_s: int = 120  # hl orders.py _MODIFY_SKIP_TTL
    max_consecutive_errors: int = 5  # hl main.py:292 MAX_CONSECUTIVE_ERRORS
    volatility_weight_enabled: bool = True  # hl VOLATILITY_WEIGHT_ENABLED
    holding_protection_enabled: bool = False  # hl HOLDING_PROTECTION_ENABLED

    # 函式層預設（硬編，移植自 hl）
    px_rel_tol: Decimal = Decimal("1e-4")  # hl orders.py:40 _prices_equal rel
    slippage: Decimal = Decimal("0.05")  # hl trader.py:312 硬編

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "CopySettings":
        """從環境變數建構配置。

        env=None 時用 os.environ；否則用傳入的 dict（可覆蓋 os.environ）。
        變數名前綴 COPY_（如 COPY_LEADER_ADDRESS、COPY_LIVE_TRADING）。
        """
        return cls(
            leader_address=_env_str("COPY_LEADER_ADDRESS", cls.leader_address, env),
            live_trading=_env_bool("COPY_LIVE_TRADING", str(cls.live_trading).lower(), env),
            interval_s=_env_int("COPY_INTERVAL_S", str(cls.interval_s), env),
            modify_policy=_env_str("COPY_MODIFY_POLICY", cls.modify_policy, env),
            flatten_on_breach=_env_bool(
                "COPY_FLATTEN_ON_BREACH", str(cls.flatten_on_breach).lower(), env
            ),
            allocated_capital=_env_decimal("COPY_ALLOCATED_CAPITAL", str(cls.allocated_capital), env),
            capital_utilization=_env_decimal(
                "COPY_CAPITAL_UTILIZATION", str(cls.capital_utilization), env
            ),
            position_weight=_env_decimal("COPY_POSITION_WEIGHT", str(cls.position_weight), env),
            max_target_leverage=_env_decimal(
                "COPY_MAX_TARGET_LEVERAGE", str(cls.max_target_leverage), env
            ),
            min_order_notional=_env_decimal(
                "COPY_MIN_ORDER_NOTIONAL", str(cls.min_order_notional), env
            ),
            size_tolerance=_env_decimal("COPY_SIZE_TOLERANCE", str(cls.size_tolerance), env),
            max_drawdown_pct=_env_decimal("COPY_MAX_DRAWDOWN_PCT", str(cls.max_drawdown_pct), env),
            settle_seconds=_env_int("COPY_SETTLE_SECONDS", str(cls.settle_seconds), env),
            modify_fail_ttl_s=_env_int("COPY_MODIFY_FAIL_TTL_S", str(cls.modify_fail_ttl_s), env),
            max_consecutive_errors=_env_int(
                "COPY_MAX_CONSECUTIVE_ERRORS", str(cls.max_consecutive_errors), env
            ),
            volatility_weight_enabled=_env_bool(
                "COPY_VOLATILITY_WEIGHT_ENABLED", str(cls.volatility_weight_enabled).lower(), env
            ),
            holding_protection_enabled=_env_bool(
                "COPY_HOLDING_PROTECTION_ENABLED", str(cls.holding_protection_enabled).lower(), env
            ),
            px_rel_tol=_env_decimal("COPY_PX_REL_TOL", str(cls.px_rel_tol), env),
            slippage=_env_decimal("COPY_SLIPPAGE", str(cls.slippage), env),
        )

    def __post_init__(self) -> None:
        """驗證配置的不變量。"""
        if not self.leader_address or not self.leader_address.startswith("0x") \
                or len(self.leader_address) != 42:
            raise ValueError(
                f"leader_address must be a 0x-prefixed 42-char address, got {self.leader_address!r}"
            )

        if self.interval_s <= 0:
            raise ValueError(f"interval_s must be > 0, got {self.interval_s}")

        if self.max_consecutive_errors <= 0:
            raise ValueError(
                f"max_consecutive_errors must be > 0, got {self.max_consecutive_errors}"
            )

        if not (0 < self.max_drawdown_pct < 1):
            raise ValueError(
                f"max_drawdown_pct must be in (0, 1), got {self.max_drawdown_pct}"
            )

        if self.modify_policy not in ("modify-first", "cancel-place"):
            raise ValueError(
                f"modify_policy must be 'modify-first' or 'cancel-place', got {self.modify_policy}"
            )

        if not (0 < self.capital_utilization <= 1):
            raise ValueError(
                f"capital_utilization must be in (0, 1], got {self.capital_utilization}"
            )

        if self.min_order_notional < 0:
            raise ValueError(f"min_order_notional must be >= 0, got {self.min_order_notional}")
