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
       flatten_on_breach, allocated_capital, use_full_equity
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
    # 拍板 #2：回撤自動全平預設開。設 False 時為**軟暫停**：breach 的輪次發 critical
    # ＋不做任何交易動作，equity 回升到未 breach 即自動恢復跟單——不寫 ARM 檔、
    # 不需人工 re-arm（與 True 的 kill switch 硬鎖死是兩種語意）。
    flatten_on_breach: bool = True
    # ⭐⭐ 跟單本金的**兩種模式，由顯式旗標選擇**（2026-07-19：解除 `0` 的語意重載）。
    # 舊版讓 `allocated_capital == 0` 兼任「用全部權益」的開關，於是同一個值同時是
    # 「邊界檢查的下界」與「語意開關」——兩者撞在一起的直接後果是：資金設定 API
    # 要求 `allocated_capital > 0`（那是對的，客戶不該簽一筆「本金 0」的授權），
    # 客戶因此**無法透過 API 表達「用全部權益」**。加旗標而不是放寬邊界，是因為
    # 放寬邊界會讓「客戶手滑送 0」與「客戶要用全部權益」變成同一筆記錄。
    #
    # 兩個欄位的合法組合由 `__post_init__` 收斂成**恰好兩種**（見該處）：
    #   use_full_equity=True  ⇒ allocated_capital 必須為 0（不是「被忽略」，是不准有值）
    #   use_full_equity=False ⇒ allocated_capital 必須 > 0
    # 「兩個都有值」在型別層就不存在，所以不需要規定誰蓋過誰。
    #
    # 預設 True 的理由（向後相容）：本欄位的預設值必須與 `allocated_capital` 的預設
    # 值（0）配對成合法組合，而 0 在舊語意下就是「用全部權益」——`CopySettings()`
    # 因此與改動前**行為完全相同**。既有部署走的是 `from_env`，那裡另有一層遷移
    # 推導（見該處），設了 COPY_ALLOCATED_CAPITAL 的部署不會被這個預設值改掉。
    allocated_capital: Decimal = Decimal("0")  # use_full_equity=False 時才有意義
    use_full_equity: bool = True

    # 照抄 hl 預設值（來自 hl-copytrader config.py:33-115）
    capital_utilization: Decimal = Decimal("1.0")  # hl CAPITAL_UTILIZATION
    position_weight: Decimal = Decimal("1.0")  # hl POSITION_WEIGHT
    max_target_leverage: Decimal = Decimal("0")  # hl MAX_TARGET_LEVERAGE
    min_order_notional: Decimal = Decimal("10")  # hl MIN_ORDER_NOTIONAL
    size_tolerance: Decimal = Decimal("0.02")  # hl SIZE_TOLERANCE
    max_drawdown_pct: Decimal = Decimal("0.20")  # hl MAX_DRAWDOWN_PCT
    # 慢速絕對底線（findings F1/C2）：7 天滾動窗只量「虧損速度」，慢跌（每窗跌幅低於
    # max_drawdown_pct）可累積成巨額虧損而從不觸發。本門檻以「自開始跟單以來的高水位」
    # 為基準，兩道閘任一觸發即熔斷。0 = 停用。
    max_total_drawdown_pct: Decimal = Decimal("0.40")
    settle_seconds: int = 2  # hl orders.py SETTLE_SECONDS
    modify_fail_ttl_s: int = 120  # hl orders.py _MODIFY_SKIP_TTL
    max_consecutive_errors: int = 5  # hl main.py:292 MAX_CONSECUTIVE_ERRORS
    volatility_weight_enabled: bool = True  # hl VOLATILITY_WEIGHT_ENABLED
    holding_protection_enabled: bool = False  # hl HOLDING_PROTECTION_ENABLED

    # 函式層預設（硬編，移植自 hl）
    px_rel_tol: Decimal = Decimal("1e-4")  # hl orders.py:40 _prices_equal rel
    slippage: Decimal = Decimal("0.05")  # hl trader.py:312 硬編
    # 緊急平倉（kill switch / panic）專用滑價容忍。一般跟單平倉沒成交無所謂（下一輪再試），
    # 緊急平倉沒成交＝保護整個失效、部位繼續曝險——故用遠寬的頻寬。
    # 注意 IOC 限價語意：這是「可接受的最差價」，不是「一定成交在這個價」——
    # 實際仍從最佳價往下吃，寬頻寬只是確保跳空時仍能出場。
    flatten_slippage: Decimal = Decimal("0.30")

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "CopySettings":
        """從環境變數建構配置。

        env=None 時用 os.environ；否則用傳入的 dict（可覆蓋 os.environ）。
        變數名前綴 COPY_（如 COPY_LEADER_ADDRESS、COPY_LIVE_TRADING）。

        ⭐ `COPY_USE_FULL_EQUITY` 未設時的預設值由 `COPY_ALLOCATED_CAPITAL` **推導**
        （`alloc <= 0` ⇒ True）。這是一次性的**遷移相容層**，不是把 0 重新請回來當
        語意開關：欄位本身仍然是顯式的，`__post_init__` 仍然拒絕任何矛盾組合，只是
        「完全沒聽過這個旗標的既有 EnvironmentFile」會被解讀成它在舊語意下的意思。
        既有部署因此零改動：設了 COPY_ALLOCATED_CAPITAL=5000 的機器推導出 False
        （固定本金，行為不變），沒設的機器推導出 True（全權益，行為不變）。
        顯式設了 COPY_USE_FULL_EQUITY 卻與金額矛盾的組合，在 `__post_init__` 直接
        拒絕啟動——那是設定錯誤，靜默挑一邊執行等於替客戶決定曝險。
        """
        alloc = _env_decimal("COPY_ALLOCATED_CAPITAL", str(cls.allocated_capital), env)
        return cls(
            leader_address=_env_str("COPY_LEADER_ADDRESS", cls.leader_address, env),
            live_trading=_env_bool("COPY_LIVE_TRADING", str(cls.live_trading).lower(), env),
            interval_s=_env_int("COPY_INTERVAL_S", str(cls.interval_s), env),
            modify_policy=_env_str("COPY_MODIFY_POLICY", cls.modify_policy, env),
            flatten_on_breach=_env_bool(
                "COPY_FLATTEN_ON_BREACH", str(cls.flatten_on_breach).lower(), env
            ),
            allocated_capital=alloc,
            use_full_equity=_env_bool("COPY_USE_FULL_EQUITY",
                                      str(alloc <= 0).lower(), env),
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
            max_total_drawdown_pct=_env_decimal(
                "COPY_MAX_TOTAL_DRAWDOWN_PCT", str(cls.max_total_drawdown_pct), env),
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
            flatten_slippage=_env_decimal(
                "COPY_FLATTEN_SLIPPAGE", str(cls.flatten_slippage), env),
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

        if not (0 <= self.slippage < 1):
            raise ValueError(
                f"slippage must be in [0, 1), got {self.slippage}")

        if not (0 <= self.flatten_slippage < 1):
            raise ValueError(
                f"flatten_slippage must be in [0, 1), got {self.flatten_slippage}")

        if not (0 <= self.max_total_drawdown_pct < 1):
            raise ValueError(
                f"max_total_drawdown_pct must be in [0, 1), got {self.max_total_drawdown_pct}")

        # ⭐⭐ 本金模式的不變量：兩個欄位只有**兩種**合法組合（見欄位宣告處）。
        # 兩個方向都檢查是刻意的——只檢查一邊會留下一個「有值卻不生效」的狀態，
        # 而那正是本次改動要根除的東西（設定檔寫著 5000、引擎實際用全部權益，
        # 兩邊的 log 都正常）。矛盾一律**拒絕啟動**，不靜默挑一邊：挑哪一邊都是
        # 在替客戶決定曝險倍數，而錯的那一邊沒有任何人會發現。
        if self.use_full_equity:
            if self.allocated_capital != 0:
                raise ValueError(
                    "use_full_equity=True 時 allocated_capital 必須為 0（收到 "
                    f"{self.allocated_capital}）——兩個都給值的話，「本金」到底是哪一個"
                    "沒有答案。要用固定本金請設 use_full_equity=False")
        elif self.allocated_capital <= 0:
            raise ValueError(
                "use_full_equity=False 時 allocated_capital 必須 > 0（收到 "
                f"{self.allocated_capital}）——本金 0 會讓所有目標部位歸零，"
                "那是靜默停止跟單。要用全部權益請設 use_full_equity=True")
