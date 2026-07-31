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
       flatten_on_breach, allocated_capital, use_full_equity, size_tolerance
    2. 照抄 hl 預設值：capital_utilization, position_weight, max_target_leverage,
       min_order_notional, max_drawdown_pct, settle_seconds,
       modify_fail_ttl_s, max_consecutive_errors, volatility_weight_enabled,
       holding_protection_enabled
    3. 函式層預設（硬編）：px_rel_tol, slippage
    """
    # 刻意覆蓋
    leader_address: str = "0xfb9c52f56f03d786ad5d435aa70fe45d80569760"
    live_trading: bool = False  # 紅線 5：刻意覆蓋 hl，預設關
    interval_s: int = 60  # 每分鐘（刻意覆蓋 hl 的 hourly CHECK_MINUTE=55）
    modify_policy: str = "modify-first"  # 或 "cancel-place"；預設不得改（等 T1.3）
    # 拍板 #2：回撤自動全平預設開。設 False 時為**軟暫停**：breach 的輪次發 critical
    # ＋不做任何交易動作，equity 回升到未 breach 即自動恢復跟單——不寫 ARM 檔、
    # 不需人工 re-arm（與 True 的 kill switch 硬鎖死是兩種語意）。
    flatten_on_breach: bool = True
    # ⭐⭐ 風控總開關（2026-07-30：錢包主人自選）。False ⇒ 回撤 kill switch 與成本
    # 熔斷器**都不執法**（判定與平倉都不做）；True ⇒ 行為與本旗標存在之前完全相同。
    #
    # 為什麼是顯式布林而不是把門檻設成 0／0.99：`max_drawdown_pct` 的驗證是
    # `0 < x < 1`（0 不合法），而 0.99 是「幾乎不可能觸發」不是「關閉」——兩者在
    # 日誌與告警裡長得一樣，但一個是客戶的決定、另一個是設定錯誤。本檔對
    # `allocated_capital` 的那段檢討（見下方）就是同一個教訓：值不該兼任開關。
    #
    # ⭐ 預設 True（安全側）刻意與「新錢包預設關」不一致：新錢包的關閉來自 env 範本
    # （deploy/follower.env.autoactivate.example 的 COPY_RISK_CONTROLS_ENABLED=false），
    # 而**既有部署的 env 沒有這一行**。若把 dataclass 預設改成 False，那些既有 env
    # 會在下一次重啟時靜默失去風控——安全保護不該因為「沒設定」而消失（fail-open）。
    #
    # ⚠️ 本旗標**關不掉**兩件事，因為它們不是客戶的自我保護：
    #   1. `killswitch.is_tripped` 的鎖檔短路——已鎖死的交易只能人工 re-arm；
    #   2. leader 被白名單撤銷時的強制平倉（run_copytrade 層，屬合約與權限）。
    risk_controls_enabled: bool = True
    # 熔斷後的冷靜期（小時）。屆滿即自動恢復跟單（`killswitch.auto_rearm_if_cooled_down`）。
    # `0` ＝不自動恢復（只有人工刪 ARM 檔才恢復），那是客戶明確選擇「鎖死等我處理」。
    # ⭐ 預設 12 而不是 0（2026-07-30 使用者裁決）：把「什麼時候恢復」的決定權交回
    # 客戶手上。願意付錢限制自己的人不多，我們能做的是提供保護，不是替他上鎖。
    # ⚠️ 本值不受 `risk_controls_enabled` 影響：它管的是**恢復**，不是執法。客戶關掉
    # 風控之後，先前留下的鎖檔仍應照冷靜期自動解除，否則他等於被一個已停用的機制鎖住。
    risk_cooldown_hours: Decimal = Decimal("12")
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
    # 2026-07-28：預設偏離 hl 的 SIZE_TOLERANCE=0.02，改為 0.08（刻意覆蓋）。
    # 原因：2% 會讓小錢包的殘量落在「容忍度外、又低於 $10 最小名目」的區間，
    # 形成每輪重試的 min-notional 死循環；正式機以 COPY_SIZE_TOLERANCE=0.08
    # 實跑驗證收斂。詳見
    # docs/superpowers/research/2026-07-28-min-notional-dust-and-scale-drift.md
    # ⚠️ 本值同時是 positions.py 部位校正的死區（size_diff_pct > tolerance 才校正，
    # positions.py:273-274）——0.08 等於放寬校正死區到 8%；調整時兩處一起考慮。
    size_tolerance: Decimal = Decimal("0.08")
    max_drawdown_pct: Decimal = Decimal("0.20")  # hl MAX_DRAWDOWN_PCT
    # ⚠️ **與成本熔斷器耦合（閘門排序的副產物，不是設計意圖）**：`loop.run_cycle` 把
    # 回撤判定排在成本判定**之前**（D8），所以本欄位實際上決定了「成本閘的分母最低
    # 會被允許掉到多少」——權益跌到 `(1 - max_drawdown_pct) × 高水位` 才會被前一道閘
    # 攔下，而換手率 = 成交名目 ÷ 權益。同一份成交名目，分母越低換手率越高：
    #   放大倍率 = 1 / (1 - max_drawdown_pct)
    #   0.20（預設）⇒ 1.25×｜0.30 ⇒ 1.43×｜0.40 ⇒ 1.67×｜0.50 ⇒ 2.00×
    # 即：**調高 max_drawdown_pct 會等比放大成本閘的誤觸面**，而誤觸的下游是累犯升級
    # → kill switch → 強制平掉客戶部位。調高本欄位時請一併檢視
    # `cost_max_turnover_24h`（它的 20 是在 max_drawdown_pct=0.20 的前提下推導的：
    # 「20×／日 ≈ 每日磨 1.3% ⇒ 約 15 天磨到 0.20」——改了 0.20，那句推導也跟著變）。
    # 慢速絕對底線（findings F1/C2）：7 天滾動窗只量「虧損速度」，慢跌（每窗跌幅低於
    # max_drawdown_pct）可累積成巨額虧損而從不觸發。本門檻以「自開始跟單以來的高水位」
    # 為基準，兩道閘任一觸發即熔斷。0 = 停用。
    max_total_drawdown_pct: Decimal = Decimal("0.40")
    # ── 成本熔斷器（2026-07-19，plans/2026-07-19-cost-circuit-breaker.md）─────
    # 對「客戶資金被交易磨損的速度」設上限，**與 leader 是誰無關**。度量是換手率
    # （滾動 24h 成交名目 ÷ perp 權益）與成交筆數，不是手續費——手續費資料不完整
    # （UserFill 只有 builder_fee，HL 自身費率與滑價不在內），拿不完整的分子算
    # 「成本佔比」會系統性低估（D1）。
    #
    # ⭐ 這道閘門為何必須預設開啟：我方從客戶的每一筆成交賺 builder fee，
    # **從磨損中獲利的一方正是被指定守住磨損的一方**。靠營運端自律不成立，
    # 只能靠結構。0 = 停用該項（兩項皆 0 ⇒ 完全停用，行為與加入本功能前一致）。
    #
    # ⚠️ **此預設未經真實資料校準，上線前需以實際跟單資料調整**（D7 誠實標註）。
    # 目前唯一的真實觀測：2026-07-18 testnet 日報 accrued 增量 0.279122 USDC ÷
    # f=20（0.02%）⇒ builder 歸屬名目約 1395.6 USDC；該測試中 builder == leader
    # 錢包，故此數含兩邊，follower 側約 700 ÷ perp 權益 500 ⇒ 換手率約 1.4×／日，
    # 且那是刻意密集的全生命週期演練日（開/加/減/反手/全平＋modify 探針），
    # 不是常態跟單。樣本 = 1 個 testnet 日，不足以訂出有信心的值。
    #
    # 20 的取值理由（保守＝不易誤觸，D7 指定方向）：單位名目摩擦約 0.065%
    # （HL taker 0.045% + builder 0.02%），20×／日 ≈ 每日磨掉權益 1.3%，
    # 約 15 天磨到 max_drawdown_pct(0.20)——即純磨損攻擊下成本閘**早於**回撤閘
    # 觸發（它該是較輕、較早的一道），同時仍高於唯一真實觀測值約 14 倍。
    # 收緊是安全方向；放寬前請先有真實跟單資料。
    cost_max_turnover_24h: Decimal = Decimal("20")
    # 次要度量：滾動 24h 成交筆數。抓「換手率看不到的高頻小額對敲」——大帳戶用
    # min_order_notional(10) 的碎單洗量，名目佔權益比例很小但筆筆都付 taker 成本。
    # 400 筆 ≈ 觀測值(10 筆/日)的 40 倍；引擎 interval_s=60 ⇒ 每日至多 1440 輪。
    cost_max_fills_24h: int = 400
    # ⭐ 累犯升級（D6）：純自動恢復會讓濫用**穩定在門檻上**持續進行——每次觸發後
    # 稍微收手、掉回門檻下自動恢復、再衝上去。滾動 24h 內觸發達此次數 → trip kill
    # switch（需人工 re-arm），讓持續性問題必須有人看一眼。0/1 皆視為「一次即升級」。
    cost_breach_escalate_count: int = 3

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

    # ── leader 申贖流量中性化（vault leader，2026-07-31 Wave 4）─────────────
    # vault 的 accountValue（scale 分母）會被 depositor 申贖被動改變，產生跟單
    # churn。開啟後：流量發生當下全額從分母扣除、之後 flow_decay_hours 線性衰減
    # 歸零（leader 事後會照新 TVL 調倉，分母須收斂回真實值）。預設關——一般錢包
    # leader 沒有第三方申贖，不需要也不該多打一個 ledger API。
    leader_flow_neutralization_enabled: bool = False
    flow_decay_hours: Decimal = Decimal("36")

    # ── follower 出入金校正（2026-07-31 Wave 5）────────────────────────────
    # follower（我方錢包）的入出金會等額平移回撤基準（滾動樣本＋lifetime peak），
    # 不再被熔斷器當成虧損／獲利。這是**正確性修復，預設開**；本鍵是逃生閥——
    # 校正邏輯若出問題可暫時關閉，回到「出金被視為虧損」的 fail-safe 舊行為。
    # 語意與 leader 側 36h 衰減刻意不同：follower 回撤量的是交易損益，
    # 流量**永久**排除、不衰減（見 follower_flow.py 模組 docstring）。
    follower_flow_correction_enabled: bool = True

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
            risk_controls_enabled=_env_bool(
                "COPY_RISK_CONTROLS_ENABLED", str(cls.risk_controls_enabled).lower(), env
            ),
            risk_cooldown_hours=_env_decimal(
                "COPY_RISK_COOLDOWN_HOURS", str(cls.risk_cooldown_hours), env
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
            cost_max_turnover_24h=_env_decimal(
                "COPY_COST_MAX_TURNOVER_24H", str(cls.cost_max_turnover_24h), env),
            cost_max_fills_24h=_env_int(
                "COPY_COST_MAX_FILLS_24H", str(cls.cost_max_fills_24h), env),
            cost_breach_escalate_count=_env_int(
                "COPY_COST_BREACH_ESCALATE_COUNT", str(cls.cost_breach_escalate_count), env),
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
            leader_flow_neutralization_enabled=_env_bool(
                "COPY_LEADER_FLOW_NEUTRALIZATION",
                str(cls.leader_flow_neutralization_enabled).lower(), env),
            flow_decay_hours=_env_decimal(
                "COPY_FLOW_DECAY_HOURS", str(cls.flow_decay_hours), env),
            follower_flow_correction_enabled=_env_bool(
                "COPY_FOLLOWER_FLOW_CORRECTION",
                str(cls.follower_flow_correction_enabled).lower(), env),
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

        if self.risk_cooldown_hours < 0:
            raise ValueError(
                f"risk_cooldown_hours must be >= 0 (0=不自動恢復), "
                f"got {self.risk_cooldown_hours}")

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

        # 成本熔斷器：負值一律拒絕啟動。負門檻會讓「任何成交都超標」永久擋住開新倉，
        # 那不是保護而是靜默停止跟單——與 allocated_capital<=0 同一類錯誤，拒絕而非
        # 靜默挑一邊。0 是合法的（＝停用該項）。
        if self.cost_max_turnover_24h < 0:
            raise ValueError(
                f"cost_max_turnover_24h must be >= 0 (0=停用), got {self.cost_max_turnover_24h}")
        if self.cost_max_fills_24h < 0:
            raise ValueError(
                f"cost_max_fills_24h must be >= 0 (0=停用), got {self.cost_max_fills_24h}")
        if self.cost_breach_escalate_count < 0:
            raise ValueError(
                "cost_breach_escalate_count must be >= 0, got "
                f"{self.cost_breach_escalate_count}")

        # 中性化衰減窗：0 或負值會讓 weight 除式退化（除零／每筆流量權重永遠 0 或
        # 爆表），那不是「關閉」——關閉走 leader_flow_neutralization_enabled=False。
        if self.flow_decay_hours <= 0:
            raise ValueError(
                f"flow_decay_hours must be > 0, got {self.flow_decay_hours}")

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
