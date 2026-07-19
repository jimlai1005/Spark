"""TE + fee 日報計算：成交配對、滑價、taker 佔比、skipped 小額、accrued 增量。純函式，不觸網。"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from spark.exchange.base import UserFill

_MS = timedelta(milliseconds=1)


@dataclass(frozen=True)
class PairedFill:
    """一筆配對成交：leader fill 與我方 fill 的對應。"""
    leader: UserFill
    mine: UserFill
    delay_s: Decimal       # mine.time - leader.time 秒數（可為負）
    slippage_bp: Decimal   # 方向修正後的滑價；正 = 不利


def _median(values: list[Decimal]) -> Decimal | None:
    """中位數：空列表 → None；偶數個取兩中位平均（Decimal）。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _slippage_bp(leader: UserFill, mine: UserFill) -> Decimal:
    """方向修正滑價（bp）。買：(mine-leader)/leader×10000；賣：(leader-mine)/leader×10000。
    買貴 = 正、賣低 = 正；正 = 不利。"""
    if leader.side == "B":
        return (mine.px - leader.px) / leader.px * 10000
    return (leader.px - mine.px) / leader.px * 10000


def pair_fills(leader_fills: list[UserFill], my_fills: list[UserFill], *,
               window_s: int = 120) -> tuple[list[PairedFill], list[UserFill]]:
    """配對兩邊成交：同 coin 同方向(side)、|時間差| <= window_s 的最近鄰。

    貪婪依時間差排序配對：列出所有合格 (leader, mine) 候選，按 |Δt| 由小到大取用；
    每筆 leader fill 至多配一筆 my fill，my fill 不重複使用。

    delay 以整數毫秒計算後轉 Decimal 秒，不經 float 中間值。

    Returns:
        (paired, unpaired_mine)：配對結果與我方未被配對的 fills。
    """
    # 候選：(abs_delta_ms, leader_idx, my_idx, delta_ms)
    candidates: list[tuple[int, int, int, int]] = []
    for li, lf in enumerate(leader_fills):
        for mi, mf in enumerate(my_fills):
            if mf.coin != lf.coin or mf.side != lf.side:
                continue
            delta_ms = (mf.time - lf.time) // _MS   # timedelta // timedelta → 精確整數 ms
            if abs(delta_ms) <= window_s * 1000:
                candidates.append((abs(delta_ms), li, mi, delta_ms))

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))  # 時間差優先；平手依輸入順序，結果確定
    used_leader: set[int] = set()
    used_mine: set[int] = set()
    picked: list[tuple[int, int, int]] = []  # (leader_idx, my_idx, delta_ms)
    for _abs_ms, li, mi, delta_ms in candidates:
        if li in used_leader or mi in used_mine:
            continue
        used_leader.add(li)
        used_mine.add(mi)
        picked.append((li, mi, delta_ms))

    picked.sort()  # 輸出按 leader 原始順序
    paired = [
        PairedFill(
            leader=leader_fills[li],
            mine=my_fills[mi],
            delay_s=Decimal(delta_ms) / 1000,
            slippage_bp=_slippage_bp(leader_fills[li], my_fills[mi]),
        )
        for li, mi, delta_ms in picked
    ]
    unpaired_mine = [f for i, f in enumerate(my_fills) if i not in used_mine]
    return paired, unpaired_mine


@dataclass(frozen=True)
class TradeQuality:
    """成交品質的六個量，**與日報完全同源**（`DailyReport` 由本型別組出來）。

    ⭐ 抽出成獨立型別的理由（工程原則 1）：營運後台的跨 follower 品質面板要顯示
    的就是這幾個數，而它與日報並排被人閱讀、被人相減。兩邊各算一次「看起來一樣」
    的公式，遲早會有一邊改了另一邊沒改，而畫面上看不出兩個數字已經不同基準。
    唯一的防法是只有一份算式：日報與 /api/ops/trade-quality 都呼叫
    `compute_trade_quality`，不各自複製。

    `None` 一律代表「算不出來」（無配對／無 taker 成交），**不是 0**——
    0 是有意義且錯誤的訊息（「延遲為零」讀起來像完美，實際上是沒有樣本）。
    """
    pair_count: int
    median_delay_s: Decimal | None            # 無配對 → None
    taker_share: Decimal                      # crossed 成交量 / 我方總成交量；總量 0 → 0
    taker_slippage_bp_median: Decimal | None  # 只取配對中 mine.crossed=True 的滑價中位數
    skipped_small_notional: Decimal
    skipped_small_ratio: Decimal              # skipped / (我方總成交名目 + skipped)；分母 0 → 0


def compute_trade_quality(leader_fills: list[UserFill], my_fills: list[UserFill],
                          skipped: list[tuple[str, Decimal]]) -> TradeQuality:
    """成交品質六量的**唯一算式**（日報與營運後台共用，見 TradeQuality docstring）。

    Args:
        skipped: 被跳過的小額訂單 [(coin, notional), ...]（由呼叫端注入）。

    ⚠️ 呼叫端的同基準責任：`leader_fills`／`my_fills`／`skipped` 必須覆蓋**同一段
    期間**。本函式無從得知三者的時間窗，錯配的症狀是安靜的——例如拿日曆日的
    skipped 去除以一個非整日窗口的成交名目，得到的比例只是兩個不同期間的商。
    """
    paired, _ = pair_fills(leader_fills, my_fills)

    median_delay = _median([p.delay_s for p in paired])

    my_total = sum((f.sz * f.px for f in my_fills), Decimal("0"))
    if my_total > 0:
        taker_notional = sum((f.sz * f.px for f in my_fills if f.crossed), Decimal("0"))
        taker_share = taker_notional / my_total
    else:
        taker_share = Decimal("0")

    taker_slippage_median = _median([p.slippage_bp for p in paired if p.mine.crossed])

    skipped_notional = sum((n for _, n in skipped), Decimal("0"))
    denom = my_total + skipped_notional
    skipped_ratio = skipped_notional / denom if denom > 0 else Decimal("0")

    return TradeQuality(
        pair_count=len(paired),
        median_delay_s=median_delay,
        taker_share=taker_share,
        taker_slippage_bp_median=taker_slippage_median,
        skipped_small_notional=skipped_notional,
        skipped_small_ratio=skipped_ratio,
    )


@dataclass(frozen=True)
class DailyReport:
    """日報：TE（配對延遲/滑價）、taker 佔比、skipped 小額、accrued 增量、CSV 對帳三態。"""
    day: date
    pair_count: int
    median_delay_s: Decimal | None            # 無配對 → None
    taker_share: Decimal                      # crossed 成交量(sz×px) / 我方總成交量；總量 0 → 0
    taker_slippage_bp_median: Decimal | None  # 只取配對中 mine.crossed=True 的滑價中位數
    skipped_small_notional: Decimal
    skipped_small_ratio: Decimal              # skipped / (我方總成交名目 + skipped)；分母 0 → 0
    accrued_delta: Decimal                    # 今日 accrued - 昨日快照
    csv_matched: bool | None                  # fill_count > 0 → .matched；== 0 → None


def build_daily_report(day: date, leader_fills: list[UserFill], my_fills: list[UserFill],
                       skipped: list[tuple[str, Decimal]], accrued_today: Decimal,
                       accrued_prev: Decimal, csv_report) -> DailyReport:
    """構造日報。

    Args:
        skipped: 被跳過的小額訂單 [(coin, notional), ...]（由呼叫端注入，不依賴 orders 模組）。
        csv_report: ReconcileReport（duck-typed：需有 fill_count 與 matched）。

    成交品質六量一律取自 `compute_trade_quality`（**不在此重算**）：營運後台的
    跨 follower 面板顯示同一組數字，兩份算式會漂移而畫面上看不出來。
    """
    q = compute_trade_quality(leader_fills, my_fills, skipped)

    csv_matched = csv_report.matched if csv_report.fill_count > 0 else None

    return DailyReport(
        day=day,
        pair_count=q.pair_count,
        median_delay_s=q.median_delay_s,
        taker_share=q.taker_share,
        taker_slippage_bp_median=q.taker_slippage_bp_median,
        skipped_small_notional=q.skipped_small_notional,
        skipped_small_ratio=q.skipped_small_ratio,
        accrued_delta=accrued_today - accrued_prev,
        csv_matched=csv_matched,
    )


def render_report(r: DailyReport) -> str:
    """渲染為 markdown 純文字。"""
    delay_txt = f"{r.median_delay_s}" if r.median_delay_s is not None else "無配對"
    slip_txt = (f"{r.taker_slippage_bp_median}"
                if r.taker_slippage_bp_median is not None else "無")
    lines = [
        f"# Copytrade 日報 {r.day.isoformat()}",
        "",
        "## 成交配對（TE）",
        f"- 配對筆數：{r.pair_count}",
        f"- 中位延遲（秒）：{delay_txt}",
        f"- Taker 滑價中位（bp）：{slip_txt}",
        "",
        "## 成交結構",
        f"- Taker 佔比：{r.taker_share}",
        "",
        "## Skipped 小額",
        f"- 跳過名目：{r.skipped_small_notional}",
        f"- 跳過比例：{r.skipped_small_ratio}",
        "",
        "## Builder fee",
        f"- Accrued 增量：{r.accrued_delta}",
        "",
        "## CSV 對帳",
    ]
    if r.csv_matched is None:
        lines.append("- 狀態：CSV 無資料或無成交（含 403 被拒情形），不得視為相符")
    elif r.csv_matched:
        lines.append("- 狀態：相符")
    else:
        lines.append("- 狀態：不相符")
    return "\n".join(lines)
