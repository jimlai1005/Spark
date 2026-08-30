"""src/spark/filet/aggregate.py
跨 follower 日報匯總。純函式為主，不觸網（collect_follower_summary 收 adapter 注入）。

⭐ 紅線 5（北極星不重複計）：`query_builder_accrued(builder)` 回的是 builder 位址的
**全域**累積量。M2 全部 follower 共用同一 builder，故北極星＝builder 層級查一次的
單日增量（builder_fee_delta），**絕不**跨 follower 加總 accrued_delta。
per-follower 的 FollowerSummary 只做 fills 衍生的活動歸屬（fills 數、taker_share），
完全不查 accrued——結構上不給「跨 follower 加總」留下輸入。
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from spark.filet.followers import FollowerRef


@dataclass(frozen=True)
class FollowerSummary:
    ref: FollowerRef
    fills: int                    # 該 follower 當日成交筆數（fills 衍生）
    taker_share: Decimal          # 該 follower 當日 taker 佔比
    notional: Decimal = Decimal("0")      # 當日路由名目（Σ sz×px）
    builder_fee: Decimal = Decimal("0")   # 當日歸屬我方的 builder fee（Σ fills 的 builder_fee）
                                          # ⚠️ 歸屬分析用，**非**北極星——北極星是 builder 層級查一次的
                                          # north_star_fee_delta，絕不由此加總推導（見 AggregateReport）
    error: str | None = None      # 查詢失敗時記錄，不中斷其他 follower
    # M3 round3 Task 2b：加法擴充，兩者皆有預設值，既有呼叫端（ops/revenue 等）
    # 不需要也不會被影響——沒有人讀這兩個欄位，行為與擴充前逐位元相同。
    total_fee: Decimal = Decimal("0")      # 當日總交易費（Σ fills 的 fee，含 rebate 為負；
                                            # 不是北極星，只用於「已實現淨 PnL」分母）
    realized_pnl: Decimal | None = None    # 當日 Σ closedPnl；**None** = 這批 fills 完全沒有
                                            # closedPnl 資料（型別/上游缺欄），不是「當日已實現 0」
                                            # ——沒資料就是沒資料，不可替換成 0（工程原則 1）。


def collect_follower_summary(ref: FollowerRef, adapter, start: datetime,
                              end: datetime, *, end_exclusive: bool = False
                              ) -> FollowerSummary:
    """對單一 follower 取 fills 算 summary（不查 accrued——避免重複計）。
    任何取數例外捕成 FollowerSummary(error=...)，不外拋（跨 follower 隔離：
    一個 follower 的 API 錯誤不得中止其他 follower 的匯總，工程原則 4）。
    taker_share = crossed 成交名目 / 總成交名目（總量 0 → 0），沿 report.py 語意。

    `end_exclusive`（M3 round3 Task 2b，**預設 False，行為與擴充前相同**）：
    `adapter.get_user_fills` 對應真實 `userFillsByTime`，`start`/`end` 兩端皆含
    ——同一筆恰好落在 `end` 整點（例如 UTC 午夜）的成交，會同時被相鄰兩個
    `[day, day+1)` 查詢窗各記一次（`/api/me/dashboard` 費用明細的逐日聚合舊病）。
    `end_exclusive=True` 時在抓到 fills 後本地過濾 `f.time < end`（同一份資料、
    同一組公式，不是另一個來源，只是把「兩端皆含」收斂成「半開區間」）——
    只有明確傳入的呼叫端會改變行為，`/api/ops/revenue`／`customer_pnl` 等既有
    呼叫端不傳這個參數，維持原本「兩端皆含」的既有行為不變。"""
    try:
        fills = adapter.get_user_fills(ref.user_address, start, end)
        if end_exclusive:
            fills = [f for f in fills if f.time < end]
        ntl = sum((f.sz * f.px for f in fills), Decimal("0"))
        taker_ntl = sum((f.sz * f.px for f in fills if f.crossed), Decimal("0"))
        share = (taker_ntl / ntl) if ntl > 0 else Decimal("0")
        fee_sum = sum((f.builder_fee for f in fills), Decimal("0"))
        # `getattr` 防禦：既有測試替身（例如 test_filet_aggregate.py 的 `_FakeFill`）
        # 只鴨模擬 sz/px/crossed/builder_fee，沒有 fee/closed_pnl——這兩個是 Task 2b
        # 加法擴充，缺席一律視為「沒有這筆資料」，不是本函式假造出 0（total_fee 在
        # `realized_pnl is None` 的路徑上本就不會被用到，見 `_pnl_share_pct`）。
        total_fee = sum((getattr(f, "fee", Decimal("0")) for f in fills), Decimal("0"))
        has_realized = any(getattr(f, "closed_pnl", None) is not None for f in fills)
        realized_pnl = (sum((getattr(f, "closed_pnl", None) or Decimal("0")
                             for f in fills), Decimal("0"))
                        if has_realized else None)
        return FollowerSummary(ref, len(fills), share, ntl, fee_sum, None,
                               total_fee=total_fee, realized_pnl=realized_pnl)
    except Exception as e:  # noqa: BLE001 — 跨 follower 隔離，錯誤入 summary 不外拋
        return FollowerSummary(ref, 0, Decimal("0"), Decimal("0"), Decimal("0"), error=str(e))


@dataclass(frozen=True)
class AggregateReport:
    day: date
    summaries: tuple[FollowerSummary, ...]
    north_star_fee_delta: Decimal  # builder 層級查一次的單日增量（絕不跨 follower 相加）
    # ⚠️ 與 summaries[].builder_fee 的區分：本欄是 builder 位址全域累積的差（**實收**，權威值）；
    # summaries[].builder_fee 是各 follower 成交歸屬的加總（**應收／歸屬**，分析與對帳用）。
    # 兩者的差額即收入對帳訊號，**不可互相取代**。
    follower_count: int
    ok_count: int


def builder_fee_delta(accrued_today: Decimal, accrued_prev: Decimal) -> Decimal:
    """北極星單日增量＝builder 位址全域累積的今昨差（查一次，不加總）。"""
    return accrued_today - accrued_prev


def aggregate(day: date, summaries: list[FollowerSummary], *,
              north_star_fee_delta: Decimal) -> AggregateReport:
    """組裝日報。north_star_fee_delta 是呼叫端已經查一次算好的 builder 層級增量，
    本函式不從 summaries 推導或加總它——結構上避免重複計的唯一入口就是這個關鍵字參數。"""
    ok = [s for s in summaries if s.error is None]
    return AggregateReport(day, tuple(summaries), north_star_fee_delta,
                           len(summaries), len(ok))


def render_aggregate(agg: AggregateReport) -> str:
    """渲染為 markdown 純文字。"""
    lines = [
        f"# Filet 跨 Follower 日報 {agg.day.isoformat()}",
        "",
        "## 北極星（builder 層級，查一次，不跨 follower 加總）",
        f"- 單日 builder fee 增量：{agg.north_star_fee_delta}",
        "",
        f"## Follower 明細（{agg.ok_count}/{agg.follower_count} 正常）",
    ]
    if not agg.summaries:
        lines.append("- （無 follower）")
    for s in agg.summaries:
        label = s.ref.label or s.ref.account_id
        if s.error is not None:
            lines.append(f"- {label}（{s.ref.network}）：查詢失敗 — {s.error}")
        else:
            lines.append(
                f"- {label}（{s.ref.network}）：fills={s.fills}，"
                f"taker_share={s.taker_share}")
    return "\n".join(lines)
