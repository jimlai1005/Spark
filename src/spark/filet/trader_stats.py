"""src/spark/filet/trader_stats.py
探索清單（/api/public/explore）與交易員詳情（/api/public/traders/{address}）**共用**
的指標純函式。零網路、零檔案 IO。兩個端點的每一個數字都只能從這裡出來——
這是 2026-09-04 事故（兩頁各自一套公式、各自錯法不同）的結構性修法：
一個函式、一組錨例測試，兩頁自然一致。

單窗指標（`window_stats`）：
- `pnl_usd`      ＝ 該窗 `pnlHistory` 末值 − 首值。HL 官方定義 pnlHistory 已扣除出入金
                   （P(t) = AV(t) − F(t)），含未實現損益、funding、手續費——與 Hyperbot
                   圖表「Total PnL」逐位一致（實證 0x6648 perpWeek 764.19）。
- `max_dd_pct`   ＝ `leader_perf.compute_window_performance` 的權益指數 MDD ×100，取負值
                   （≤ 0；沿探索頁既有慣例）。perf 非 ok → `None`，`max_dd_reason` 帶
                   leader_perf 的 reason（`flow_dominated_interval`／`too_many_skipped_intervals`
                   ／`need_at_least_two_samples`…），前端顯示「—」並可 tooltip 原因。
                   永不算在 accountValue 上（leader_perf 檔頭閘門 2）。
- `spark`        ＝ 同一 `pnlHistory` 等距降採樣 ≤ 30 點（不補點）。
三者出自同一次 `portfolio()` 回應的同一個窗（工程原則 1）。

成交統計（`fills_stats`，Hyperbot 已驗證定義，見 memory hyperbot-metrics-reference）：
- `order_count`      ＝ distinct `oid`（不是 fills 數；HL 一張單可拆多筆 fill）。
- `closed_positions` ＝ 部位歸零的生命週期數：`dir` 以 "Close" 開頭且 |startPosition| == sz，
                       或 `dir` 含 ">"（翻倉）。
- `wins`             ＝ 生命週期累積 closedPnl > 0 的次數；`win_rate_pct` = wins/closed×100。
- `realized_pnl_usd` ＝ Σ closedPnl（未扣手續費／funding，與 Hyperbot totalPnl 同定義）。
- 只算 perp fills（D4）：spot 成交（Buy/Sell/Spot Dust Conversion）跟單複製不到，排除。
錨例：0x6648…b1f3 2026-09-04 30 天 → 221／27／15／40225.79，與 Hyperbot period=30 逐位一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from spark.filet.leader_perf import STATUS_OK, compute_window_performance, extract_window

SPARK_POINTS = 30
PERP_DIRS = frozenset({"Open Long", "Open Short", "Close Long", "Close Short",
                       "Long > Short", "Short > Long"})
_CENTS = Decimal("0.01")


def _q2(v: Decimal) -> float:
    return float(v.quantize(_CENTS, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class WindowStats:
    pnl_usd: float
    max_dd_pct: float | None          # ≤ 0；None = 該窗算不出（見 max_dd_reason）
    max_dd_reason: str | None         # None 當且僅當 max_dd_pct 非 None
    spark: tuple[float, ...]          # pnlHistory 降採樣

    def to_dict(self) -> dict[str, Any]:
        return {"pnl_usd": self.pnl_usd, "max_dd_pct": self.max_dd_pct,
                "max_dd_reason": self.max_dd_reason, "spark": list(self.spark)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WindowStats":
        return cls(pnl_usd=float(d["pnl_usd"]),
                   max_dd_pct=None if d.get("max_dd_pct") is None else float(d["max_dd_pct"]),
                   max_dd_reason=d.get("max_dd_reason"),
                   spark=tuple(float(x) for x in d.get("spark", [])))


def downsample(values: list[float], n: int = SPARK_POINTS) -> list[float]:
    """等距抽樣至最多 n 點；點數 <= n 全部回傳，不補點（補點＝編造沒發生過的損益）。"""
    if not values:
        return []
    if len(values) <= n:
        return [float(v) for v in values]
    # n-1 分母：讓 i=n-1 精確落在最後一個索引，末值永遠被保留（不是「大約在尾端」）。
    step = (len(values) - 1) / (n - 1)
    idxs = [min(len(values) - 1, round(i * step)) for i in range(n)]
    return [float(values[i]) for i in idxs]


def window_stats(portfolio_raw: Any, period: str) -> WindowStats | None:
    """`portfolio()` 原始回應 + 期別 → `WindowStats`；窗缺席／形狀不符／不足兩點 → None。"""
    extracted = extract_window(portfolio_raw, period)
    if extracted is None:
        return None
    _av, pnl = extracted
    if len(pnl) < 2:
        return None
    pnl_usd = _q2(pnl[-1][1] - pnl[0][1])
    spark = tuple(downsample([float(v) for _, v in pnl]))
    perf = compute_window_performance(portfolio_raw, period)
    if perf.get("status") == STATUS_OK and "max_drawdown" in perf:
        mdd = -(Decimal(perf["max_drawdown"]) * Decimal("100"))
        return WindowStats(pnl_usd=pnl_usd, max_dd_pct=_q2(mdd), max_dd_reason=None, spark=spark)
    return WindowStats(pnl_usd=pnl_usd, max_dd_pct=None,
                       max_dd_reason=str(perf.get("reason") or "insufficient"), spark=spark)


def live_days_from_av(av_points: list[tuple[int, Any]]) -> int:
    """實盤天數＝allTime accountValueHistory 首末點日曆跨距（沿探索頁 W1 定義）。"""
    if not av_points:
        return 0
    first = datetime.fromtimestamp(av_points[0][0] / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(av_points[-1][0] / 1000, tz=timezone.utc).date()
    return (last - first).days


@dataclass(frozen=True)
class FillsStats:
    order_count: int
    closed_positions: int
    wins: int
    win_rate_pct: float | None        # closed_positions == 0 → None
    realized_pnl_usd: float
    concentration_pct: float | None   # 最大單幣名目佔比；無 perp 成交 → None
    coins: tuple[str, ...]            # 名目降冪前 3
    truncated: bool                   # fills 分頁到上限仍滿頁 → 以上皆為下限值

    def to_dict(self) -> dict[str, Any]:
        return {"order_count": self.order_count, "closed_positions": self.closed_positions,
                "wins": self.wins, "win_rate_pct": self.win_rate_pct,
                "realized_pnl_usd": self.realized_pnl_usd,
                "concentration_pct": self.concentration_pct, "coins": list(self.coins),
                "truncated": self.truncated}


def _is_flat_close(fill: dict) -> bool:
    d = str(fill.get("dir", ""))
    if ">" in d:
        return True
    if not d.startswith("Close"):
        return False
    try:
        return abs(abs(Decimal(str(fill["startPosition"]))) - Decimal(str(fill["sz"]))) < Decimal("1e-9")
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return False


def fills_stats(fills: list[dict], *, truncated: bool) -> FillsStats:
    perp = sorted((f for f in fills if str(f.get("dir", "")) in PERP_DIRS),
                  key=lambda f: int(f.get("time", 0)))
    oids: set[Any] = set()
    closed = wins = 0
    realized = Decimal("0")
    acc: dict[str, Decimal] = {}
    notional: dict[str, Decimal] = {}
    for f in perp:
        oids.add(f.get("oid"))
        coin = str(f.get("coin", ""))
        try:
            pnl = Decimal(str(f.get("closedPnl", "0") or "0"))
            n = abs(Decimal(str(f.get("px", "0"))) * Decimal(str(f.get("sz", "0"))))
        except (ArithmeticError, TypeError, ValueError):
            continue
        realized += pnl
        acc[coin] = acc.get(coin, Decimal("0")) + pnl
        notional[coin] = notional.get(coin, Decimal("0")) + n
        if _is_flat_close(f):
            closed += 1
            if acc[coin] > 0:
                wins += 1
            acc[coin] = Decimal("0")
    total_n = sum(notional.values(), Decimal("0"))
    ranked = sorted(notional.items(), key=lambda kv: kv[1], reverse=True)
    concentration = _q2(ranked[0][1] / total_n * 100) if ranked and total_n > 0 else None
    win_rate = _q2(Decimal(wins) / Decimal(closed) * 100) if closed > 0 else None
    return FillsStats(order_count=len(oids), closed_positions=closed, wins=wins,
                      win_rate_pct=win_rate, realized_pnl_usd=_q2(realized),
                      concentration_pct=concentration,
                      coins=tuple(c for c, _ in ranked[:3]), truncated=truncated)
