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
from decimal import Decimal
from pathlib import Path

from spark.exchange.base import EquityView

SAMPLES_RELPATH = Path("var/copytrade/equity_samples.json")
WINDOW_S = 7 * 24 * 3600  # 7 天，對齊 hl 的 week-window 語意

logger = logging.getLogger(__name__)


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


def reset_samples(root: Path) -> None:
    """清空樣本。kill switch 觸發時呼叫——防止人工 re-arm 後被舊 peak 立刻再熔斷。

    **絕不拋例外**：本函式在 `killswitch.trip()` 中位於「ARM 已落地」與「總結 critical
    告警」之間，若在此拋錯會吃掉那則告警（操作者收不到平倉成敗清單），違反工程原則 3。
    清理失敗只是「下次 re-arm 可能被舊 peak 再熔斷」，遠輕於告警遺失。
    """
    try:
        (root / SAMPLES_RELPATH).unlink(missing_ok=True)
    except OSError as e:  # noqa: BLE001 — 清理失敗絕不能擋告警，見 docstring
        logger.warning("清空 perp 權益樣本失敗（不影響 ARM 鎖定）: %r", e)


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
    path = root / SAMPLES_RELPATH
    # `0 <= age` 不可省：時鐘前跳／NTP 校正會寫下未來戳，`now - ts` 為負而恆滿足
    # 上界，該樣本將永不出窗（污染期＝跳動幅度＋window）。未來戳一律丟棄。
    samples = [(ts, v) for ts, v in _load(path) if 0 <= now - ts <= window_s]
    samples.append((now, str(current)))
    if persist:
        _save(path, samples)
    peak = max([Decimal(v) for _, v in samples] + [current])
    return EquityView(current=current, recent_peak=peak)
