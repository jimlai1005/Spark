"""perp-basis 權益取樣（回撤熔斷專用）。

背景（2026-07-19 testnet 實測，findings F1）：`HyperliquidAdapter.get_equity_view()`
的資料源 HL `portfolio()` 回的是 **spot + perp 總值**，但跟單策略只能動 perp——
以總值當回撤分母會稀釋保護（客戶 100 perp + 10000 spot ⇒ 保護實質失效）。
本模組改以 `get_account_value()`（perp accountValue，**與 sizing 用的是同一個數字**）
為基準，peak 由本地滾動樣本維護（預設 7 天窗），語意對齊 hl 原設計的 week-window max
——防「近期急跌」而非「終身高水位」（後者會讓慢跌後貼著門檻反覆熔斷）。

樣本檔：`<state_root>/var/copytrade/equity_samples.json`（原子寫 tmp+replace）。
kill switch 觸發時由 `killswitch.trip()` 呼叫 `reset_samples()` 清空——否則人工
re-arm 後崩跌前的舊 peak 仍在窗內，會立刻再次熔斷。

已知極限（誠實標註）：客戶自行把 perp 資金轉出會被視為回撤（方向 fail-safe，
且「持倉中抽走保證金」本就該被風控視為危險）；引擎停機期間的樣本缺口會使 peak 低估。
ledger-aware 的出入金校正延到 public beta。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from spark.exchange.base import EquityView

SAMPLES_RELPATH = Path("var/copytrade/equity_samples.json")
WINDOW_S = 7 * 24 * 3600  # 7 天，對齊 hl 的 week-window 語意
MIN_COVERAGE_S = 3600  # 樣本覆蓋不足 1 小時＝回撤保護尚未生效（見 SampleCoverage）
_WICK_GUARD_MIN_SAMPLES = 3  # 少於此筆數無從判斷離群值，peak 退回最高值（見 perp_equity_view）

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SampleCoverage:
    """樣本覆蓋度——回撤保護是否真的生效的可觀測指標。

    `peak = max(樣本 ∪ current)` 恆 >= current，故 peak 永遠不會 <= 0（除非帳戶被清算），
    既有的 degenerate 告警在此路徑上結構性失效。空樣本會讓 drawdown 恆為 0——
    「無資料」偽裝成「無回撤」。本型別讓呼叫端能分辨兩者並大聲告警（findings F1/C1）。
    """
    count: int
    oldest_age_s: float  # 無樣本時為 0.0
    read_error: bool     # True＝檔案存在但讀不出來（與「首次啟動無檔」不同，更嚴重）
    # ⭐ 最新樣本的年齡（秒）；**無樣本時為 None，不是 0.0**。
    # 引擎每個 cycle 寫一筆樣本，所以這個值是「引擎上次動的時間」最好的可觀測代理
    # （見 /api/ops/health 的 liveness_basis）。None 與 0.0 在這裡差很多：0.0 是
    # 「剛剛才寫過」＝最健康的值，而無樣本其實是「完全不知道引擎在不在」。
    newest_age_s: float | None = None

    @property
    def sufficient(self) -> bool:
        return (not self.read_error) and self.count >= 2 and self.oldest_age_s >= MIN_COVERAGE_S


def sample_coverage(root: Path, *, now_fn=time.time, window_s: int = WINDOW_S) -> SampleCoverage:
    """回報樣本覆蓋度。read_error 與「檔案不存在」分開——讀不到 ≠ 沒有歷史。"""
    path = root / SAMPLES_RELPATH
    read_error = False
    if path.exists():
        try:
            path.read_text()
        except OSError:
            read_error = True
    now = float(now_fn())
    samples = [(ts, v) for ts, v in _load(path) if 0 <= now - ts <= window_s]
    oldest = min((ts for ts, _ in samples), default=None)
    newest = max((ts for ts, _ in samples), default=None)
    return SampleCoverage(
        count=len(samples),
        oldest_age_s=(now - oldest) if oldest is not None else 0.0,
        read_error=read_error,
        newest_age_s=(now - newest) if newest is not None else None,
    )


def _load(path: Path) -> list[tuple[float, str]]:
    """讀樣本。壞檔／不存在一律回空清單（不阻斷交易；peak 會退回 current）。"""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    out: list[tuple[float, str]] = []
    if not isinstance(raw, list):
        return []
    for item in raw:
        if isinstance(item, list) and len(item) == 2:
            try:
                out.append((float(item[0]), str(item[1])))
            except (TypeError, ValueError):
                continue
    return out


def _save(path: Path, samples: list[tuple[float, str]]) -> None:
    """原子寫（tmp+os.replace，同目錄）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 檔名帶 pid：固定 tmp 名在多行程（多 follower 誤共用 state root）下會互相覆寫，
    # 產生內容交錯的檔案後才被原子 rename 成正式檔——撕裂檔會讓 _load 解析失敗回空，
    # 進而使 peak 退化成 current（靜默失去保護）。
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps([[ts, v] for ts, v in samples], ensure_ascii=False))
    os.replace(tmp, path)


LIFETIME_PEAK_RELPATH = Path("var/copytrade/equity_lifetime_peak.json")


def update_lifetime_peak(root: Path, current: Decimal, *, persist: bool = True) -> Decimal:
    """維護「自開始跟單以來」的高水位（慢速絕對底線用，findings F1/C2）。

    與 7 天滾動 peak 不同：本值只升不降，由 `killswitch.trip()` 與人工 re-arm 流程重置
    （`reset_samples` 一併清除）——語意是「這一段跟單期間的最高點」。
    persist=False：唯讀（--status／panic 用），不落檔。
    """
    path = root / LIFETIME_PEAK_RELPATH
    prev = Decimal("0")
    if path.exists():
        try:
            prev = Decimal(str(json.loads(path.read_text())))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError, ArithmeticError):
            prev = Decimal("0")
    peak = max(prev, current)
    if persist and peak != prev:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(str(peak)))
        os.replace(tmp, path)
    return peak


def _unlink_quietly(root: Path, rel: Path) -> None:
    """刪一個狀態檔；**絕不拋例外**。

    這些清除動作在 `killswitch.trip()` 中位於「ARM 已落地」與「總結 critical 告警」
    之間，若在此拋錯會吃掉那則告警（操作者收不到平倉成敗清單），違反工程原則 3。
    """
    try:
        (root / rel).unlink(missing_ok=True)
    except OSError as e:  # noqa: BLE001 — 清理失敗絕不能擋告警，見 docstring
        logger.warning("清空 %s 失敗（不影響 ARM 鎖定）: %r", rel, e)


def reset_samples(root: Path) -> None:
    """只清 **7 天滾動樣本**。kill switch 觸發時呼叫——防止恢復後被崩跌前的舊 peak
    立刻再熔斷（滾動窗量的是「跌得多快」，重新起算才是它的正確語意）。

    ⚠️⚠️ **2026-07-30 起不再連帶清除全期高水位**（獨立審查 F2）。原本兩者一起清，
    在「人工 re-arm」的世界裡尚可接受；加入冷靜期自動恢復之後，它變成一個棘輪：
    每次熔斷都把絕對底線的基準重設成崩跌後的權益，12 小時後自動恢復，下一段跌幅
    又從更低的基底重新起算——實測連續四輪累虧 61%，而「慢速絕對底線」一次都沒有觸發。
    絕對底線的意義正是**不隨時間重設**；要重設它只能是客戶親自簽章接受新基準
    （見 `killswitch.manual_rearm` 對 `total_drawdown` 的處理）。
    """
    _unlink_quietly(root, SAMPLES_RELPATH)


def reset_lifetime_peak(root: Path) -> None:
    """清除全期高水位＝**接受一個新的絕對底線基準**。

    唯一的合法呼叫點是「客戶親自簽章解除了一次 `total_drawdown` 熔斷」：他看懂了
    自己已經虧掉多少，並決定以現在的權益為新的起點繼續。系統自己（冷靜期、
    營運端、任何自動路徑）**不得**呼叫它——那等於替客戶抹掉他的虧損記錄。
    """
    _unlink_quietly(root, LIFETIME_PEAK_RELPATH)


def perp_equity_view(adapter, address: str, root: Path, *,
                     now_fn=time.time, window_s: int = WINDOW_S,
                     persist: bool = True) -> EquityView:
    """以 perp accountValue 為基準的 EquityView（current 與 peak 同源同單位）。

    current = `adapter.get_account_value(address)`（與 sizing 同一數字，工程原則 1）。
    peak = 滾動窗內樣本與 current 的最大值。

    persist=False：只讀不寫（供 `--status` 顯示與 panic 記錄用——兩者皆有零寫入／
    不改變狀態的契約，但顯示的基準必須與引擎判定一致，否則操作者的心智模型會脫節）。
    """
    current = adapter.get_account_value(address)
    now = float(now_fn())
    samples = _windowed_samples(root, now, window_s)
    samples.append((now, str(current)))
    if persist:
        _save(root / SAMPLES_RELPATH, samples)
    return EquityView(current=current, recent_peak=_peak_from(samples, current))


def _windowed_samples(root: Path, now: float, window_s: int) -> list[tuple[float, str]]:
    """讀樣本檔並套出窗過濾（perp_equity_view / recompute_view 共用同一段）。

    `0 <= age` 不可省：時鐘前跳／NTP 校正會寫下未來戳，`now - ts` 為負而恆滿足
    上界，該樣本將永不出窗（污染期＝跳動幅度＋window）。未來戳一律丟棄。
    """
    path = root / SAMPLES_RELPATH
    return [(ts, v) for ts, v in _load(path) if 0 <= now - ts <= window_s]


def _peak_from(samples: list[tuple[float, str]], current: Decimal) -> Decimal:
    """窗內樣本 ∪ current → peak（wick-guard；兩個視圖函式共用的唯一實作）。

    I2 插針防護：樣本充足時取**次高值**——單一樣本的價格插針（unrealizedPnl 受 mark
    price 影響，冷門幣插針即可造成）不會成為 peak 並污染整個窗；真實的持續高點必然
    有多筆樣本，次高值≈最高值。
    **門檻 3 筆**：少於 3 筆時無從判斷「哪一筆是離群值」（2 筆取次高＝取最低，會直接
    摧毀真實 peak 使回撤歸零），故退回最高值。此區間覆蓋度告警本來就在提醒
    「回撤保護尚未生效」，不會讓操作者誤以為有保護。
    最後與 current 取 max：維持「peak 為高水位」慣例；current 若本身是插針，當輪
    dd≈0 無害，下一輪該樣本會被次高值邏輯排除。
    """
    _vals = sorted((Decimal(v) for _, v in samples), reverse=True)
    if len(_vals) >= _WICK_GUARD_MIN_SAMPLES:
        _base = _vals[1]
    elif _vals:
        _base = _vals[0]
    else:
        _base = current
    return max(_base, current)


def recompute_view(root: Path, current: Decimal, *,
                   now_fn=time.time, window_s: int = WINDOW_S) -> EquityView:
    """免取樣的重算視圖——**breach 二次確認的重判專用**（2026-08-01 審查 F1）。

    與 perp_equity_view 的差異只有兩點：current 由呼叫端傳入（不重讀 AV，
    維持與一次判定同一快照——工程原則 1 同源），且**不 append 樣本**。
    樣本數不得因重判 +1：樣本檔只有 1 筆歷史時（trip 後 reset 重建的頭幾輪
    ——正是二次回撤最可能的時候），重判 append 的那筆 current 會讓樣本數跨過
    _WICK_GUARD_MIN_SAMPLES，peak 從最高值降級成次高值，**真**破線被洗掉
    （fail-open）。窗過濾與 wick-guard 與 perp_equity_view 共用同一實作
    （_windowed_samples／_peak_from），不存在第二份邏輯。
    """
    now = float(now_fn())
    samples = _windowed_samples(root, now, window_s)
    return EquityView(current=current, recent_peak=_peak_from(samples, current))
