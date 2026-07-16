"""sizing 純函式層：跟單比例（scale factor）、波動權重、抗單保護。

1:1 port 自 hl-copytrader（Decimal 化、網路取數改為呼叫端注入）：
  - `resolve_capital`/`compute_scale_factor` ← hl src/sync.py:21-51
  - `compute_volatility_stats`/`position_weight` ← hl src/weight.py:33-54,72-79
  - `anti_holding_flags`/`holding_stats_from_fills` ← hl src/protection.py:44-108

刻意未移植清單（hl 行號對應）：
  - weight.py:57-69 `get_vol_stats`（300s TTL 快取＋例外吞掉回 None）——快取與失敗
    處理屬呼叫端（adapter 層）職責，本檔只放純數學。
  - weight.py:82-98 `_daily_abs_pnl`（portfolio API 取數）——網路呼叫；由呼叫端經
    adapter 取得每日 |PnL| 數列後注入 `compute_volatility_stats`。
  - protection.py:31-41 `get_anti_holding_flags` 的快取層——同上，由呼叫端管理。
  - protection.py:79-82 userFillsByTime API 取數——fills 由呼叫端注入
    `holding_stats_from_fills`；LOOKBACK 時間窗由呼叫端以 `HOLDING_LOOKBACK_DAYS`
    在取數時套用。

結構偏差（相對 hl 語意的刻意調整，逐項說明）：
  - hl `compute_scale_factor` 在函式內呼叫 `get_position_weight()`（隱式讀模組常數
    與快取）；spark 由呼叫端先以 `position_weight()` 算好 `weight` 傳入——去掉隱式
    全域讀取，純函式可測。
  - hl `compute_volatility_stats` 回 dict（含 today/days 診斷欄位）；spark 回 frozen
    dataclass `VolStats`（僅 mu/sigma/z/weight——下游只消費 weight；today/days 可由
    呼叫端自持有的注入數列還原）。
  - hl `_compute_flags` 回 {coin: z}（僅含被旗標者）；spark `anti_holding_flags` 回
    {coin: bool}（輸入的每個 coin 都有值；不在輸入中的 coin 呼叫端以 False 對待）。
  - hl `_compute_flags` 對「帳戶全體完成交易」算一次 mu/sigma；spark 的基準池化在
    每個 `HoldingStats.completed_seconds`（呼叫端對所有 coin 傳同一份 tuple），
    數學結果相同。
  - float → Decimal（與 spark copytrade 全層一致）。

模組常數凍結自 hl config.py：M1 不開放 env 覆寫，值凍結自 hl（見下方常數區行號註解）。
"""
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, pstdev

from spark.copytrade.config import CopySettings
from spark.copytrade.instrument import _is_spot_coin

logger = logging.getLogger(__name__)

# ── 凍結自 hl config.py 的常數（M1 不開放 env 覆寫）──────────────────────────
VOL_LOOKBACK_DAYS = 14  # hl config.py:55 VOL_LOOKBACK_DAYS
VOL_Z_SLOPE = Decimal("0.2")  # hl config.py:56 VOL_Z_SLOPE
VOL_Z_MAX_REDUCTION = Decimal("0.7")  # hl config.py:57 VOL_Z_MAX_REDUCTION
HOLDING_PROTECTION_Z = Decimal("2.0")  # hl config.py:63 HOLDING_PROTECTION_Z
HOLDING_LOOKBACK_DAYS = 14  # hl config.py:66 HOLDING_LOOKBACK_DAYS（呼叫端取 fills 時間窗用）
HOLDING_MIN_TRADES = 10  # hl config.py:67 HOLDING_MIN_TRADES

_MIN_VOL_DAYS = 3  # hl weight.py:42 硬編（today + 至少 2 天基準才算得出標準差）
_EPS = Decimal("1e-9")  # hl protection.py:101,103 硬編的部位歸零判斷 epsilon


def resolve_capital(my_equity: Decimal, allocated: Decimal) -> Decimal:
    """跟單本金：allocated > 0 用固定值；<= 0 則自動用我方帳戶當前權益。

    port hl sync.py:21-26（hl 讀模組常數 ALLOCATED_CAPITAL；spark 由呼叫端傳入
    settings.allocated_capital）。負的 my_equity 夾為 0（照 hl 的 max(my_equity, 0.0)）。
    """
    return allocated if allocated > 0 else max(my_equity, Decimal("0"))


def compute_scale_factor(
    leader_equity: Decimal,
    my_equity: Decimal,
    *,
    target_notional: Decimal,
    settings: CopySettings,
    weight: Decimal,
) -> Decimal:
    """跟單比例 = (跟單本金 × 資金使用率 × 倉位權重) / 對方帳戶淨值。

    port hl sync.py:29-51。本金見 resolve_capital；分母用「帳戶淨值 equity（含未實現
    損益）」，使我方保證金使用率自動等於目標。settings.max_target_leverage > 0 時，
    若目標有效槓桿（target_notional / leader_equity）超過上限，等比例縮回——保護
    瀕臨清算時目標槓桿暴衝把我方拖下水。

    leader_equity <= 0 或本金 <= 0 → Decimal("0")（hl sync.py:40-41 的除零防禦，
    1:1 移植，非 spark 加固）。

    結構偏差：hl 在函式內呼叫 get_position_weight()（隱式全域＋快取）；spark 由
    呼叫端先以 position_weight() 算好 weight 傳入。
    """
    cap = resolve_capital(my_equity, settings.allocated_capital)
    if leader_equity <= 0 or cap <= 0:
        return Decimal("0")
    scale = (cap * settings.capital_utilization * weight) / leader_equity
    if settings.max_target_leverage > 0 and target_notional > 0:
        eff_lev = target_notional / leader_equity
        if eff_lev > settings.max_target_leverage:
            scale *= settings.max_target_leverage / eff_lev
            logger.warning(
                f"目標有效槓桿 {eff_lev:.1f}x 超過上限 {settings.max_target_leverage:.0f}x，"
                f"倉位縮至 {settings.max_target_leverage / eff_lev:.0%}"
            )
    return scale


@dataclass(frozen=True)
class VolStats:
    """波動統計結果。欄位語意照 hl weight.py:33-54 回傳 dict 的同名鍵。

    hl 版另帶 today/days 診斷欄位——spark 刻意不帶（下游只消費 weight；
    需要時可由呼叫端自持有的注入數列還原）。
    """

    mu: Decimal  # 基準期（最多前 VOL_LOOKBACK_DAYS 天）單日 |PnL| 平均
    sigma: Decimal  # 基準期單日 |PnL| 母體標準差（pstdev）
    z: Decimal  # 今日 |PnL| 相對基準的 Z-Score
    weight: Decimal  # 1 - clip(z × VOL_Z_SLOPE, 0, VOL_Z_MAX_REDUCTION)


def compute_volatility_stats(
    daily_abs_pnl: list[Decimal], equity: Decimal
) -> VolStats | None:
    """帳戶波動統計（偵測市場妖度，不看賺賠只看震盪）。

    port hl weight.py:33-54 純數學。hl 版自己打 portfolio API 取數
    （_daily_abs_pnl，weight.py:82-98）；spark 由呼叫端經 adapter 取得
    「每日 |PnL|」數列（時間升冪，最後一筆＝今日）後注入。

    基準最多取 today 前 VOL_LOOKBACK_DAYS 天，不足就用現有的（天數少時 Z 較
    不穩，但仍據實計算、不藏起來）。至少要有 today + 2 天基準（共 3 天）才算得
    出標準差，否則回 None。sigma=0 時 z=0、weight=1（hl weight.py:49-50）。

    equity 參數 M1 未使用（hl 版不以權益正規化 |PnL|；此參數為簽章預留，
    照 Task 規格保留）。
    """
    if len(daily_abs_pnl) < _MIN_VOL_DAYS:
        return None
    today = daily_abs_pnl[-1]
    # 最多前 VOL_LOOKBACK_DAYS 天，不足就用現有（hl weight.py:45）
    baseline = daily_abs_pnl[-(VOL_LOOKBACK_DAYS + 1):-1]
    mu = mean(baseline)
    sigma = pstdev(baseline)
    if sigma <= 0:
        return VolStats(mu=mu, sigma=Decimal("0"), z=Decimal("0"), weight=Decimal("1"))
    z = (today - mu) / sigma
    reduction = min(max(z * VOL_Z_SLOPE, Decimal("0")), VOL_Z_MAX_REDUCTION)
    return VolStats(mu=mu, sigma=sigma, z=z, weight=Decimal("1") - reduction)


def position_weight(settings: CopySettings, vol: VolStats | None) -> Decimal:
    """最終倉位權重（0~1）＝手動權重 × 目標的波動權重（若啟用）。

    port hl weight.py:72-79。hl 版在函式內呼叫 get_vol_stats(TARGET_TRADER)
    （網路＋快取）；spark 由呼叫端算好 VolStats 傳入（資料不足或取數失敗傳
    None → 不縮，安全預設，同 hl）。手動權重與最終結果都 clip 到 [0, 1]。
    """
    w = min(Decimal("1"), max(Decimal("0"), settings.position_weight))
    if settings.volatility_weight_enabled and vol is not None:
        w *= vol.weight
    return min(Decimal("1"), max(Decimal("0"), w))


@dataclass(frozen=True)
class HoldingStats:
    """單一 coin 的持倉統計（由呼叫端從 fills 算出後注入 anti_holding_flags）。

    completed_seconds 是「帳戶全體」已完成交易的持倉秒數基準（全幣種池化，
    對照 hl protection.py:46 的 completed；呼叫端對所有 coin 傳同一份 tuple，
    可由 holding_stats_from_fills 產出：holding_seconds = now - open_since[coin]）。
    """

    holding_seconds: Decimal  # 該 coin 目前未平倉部位已持有秒數
    completed_seconds: tuple[Decimal, ...]  # 基準：已完成交易的持倉秒數（池化）


def anti_holding_flags(
    settings: CopySettings, holding_stats_by_coin: Mapping[str, HoldingStats]
) -> dict[str, bool]:
    """抗單偵測：對「目前持倉時間」算 Z-Score，超過門檻的 coin 標 True（拒絕補倉）。

    port hl protection.py:44-71 純數學。settings.holding_protection_enabled=False
    （預設）→ 全 False 短路，完全不做計算（同 hl 開關語意）。啟用時：
      - 完成交易數 < HOLDING_MIN_TRADES 或 sigma <= 0 → 不啟動（False，hl:47-53）。
      - z = (holding - mu) / sigma > HOLDING_PROTECTION_Z（嚴格大於，hl:62）→ True。

    回傳對輸入的每個 coin 都有值；不在輸入中的 coin（呼叫端無其開倉時間資訊）
    視同 hl 的 continue——呼叫端以 flags.get(coin, False) 消費。
    """
    flags = dict.fromkeys(holding_stats_by_coin, False)
    if not settings.holding_protection_enabled:
        return flags
    for coin, st in holding_stats_by_coin.items():
        completed = st.completed_seconds
        if len(completed) < HOLDING_MIN_TRADES:
            continue
        mu = mean(completed)
        sigma = pstdev(completed)
        if sigma <= 0:
            continue
        z = (st.holding_seconds - mu) / sigma
        if z > HOLDING_PROTECTION_Z:
            flags[coin] = True
            logger.warning(
                f"抗單偵測：{coin} 已持倉 {st.holding_seconds / 3600:.1f}h "
                f"(平均 {mu / 3600:.1f}h, Z={z:.1f}) → 拒絕補倉"
            )
    return flags


def holding_stats_from_fills(
    fills: Iterable[Mapping],
) -> tuple[list[Decimal], dict[str, Decimal]]:
    """從成交紀錄重建每筆交易持倉時間。

    port hl protection.py:74-108 純數學（hl 版自己打 userFillsByTime API；spark 由
    呼叫端注入 fills——時間窗過濾由呼叫端在取數時以 HOLDING_LOOKBACK_DAYS 套用）。

    用 startPosition + 帶號 sz 追蹤部位：0→非0 為開倉、非0→0 為平倉；現貨標的跳過。
    回傳 (completed_seconds, open_since)：completed_seconds 為各筆已完成交易的持倉
    秒數；open_since 為 {coin: 開倉 unix 秒}（目前仍持倉者）。
    """
    by_coin: defaultdict[str, list[Mapping]] = defaultdict(list)
    for f in fills:
        coin = str(f.get("coin", ""))
        if not coin or _is_spot_coin(coin):
            continue
        by_coin[coin].append(f)

    completed: list[Decimal] = []
    open_since: dict[str, Decimal] = {}
    for coin, fs in by_coin.items():
        fs.sort(key=lambda x: x.get("time", 0))
        cur_open: Decimal | None = None
        for f in fs:
            start = Decimal(str(f.get("startPosition") or 0))
            sz = Decimal(str(f.get("sz") or 0))
            delta = sz if f.get("side") == "B" else -sz
            after = start + delta
            t = Decimal(str(f.get("time", 0))) / 1000
            if abs(start) < _EPS and abs(after) > _EPS:
                cur_open = t  # 全新開倉
            elif abs(after) < _EPS and cur_open is not None:
                completed.append(t - cur_open)  # 平回 0 → 一筆完成
                cur_open = None
        if cur_open is not None:
            open_since[coin] = cur_open  # 目前仍持倉
    return completed, open_since
