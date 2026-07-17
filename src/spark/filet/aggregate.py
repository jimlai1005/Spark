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
    error: str | None = None      # 查詢失敗時記錄，不中斷其他 follower


def collect_follower_summary(ref: FollowerRef, adapter, start: datetime,
                              end: datetime) -> FollowerSummary:
    """對單一 follower 取 fills 算 summary（不查 accrued——避免重複計）。
    任何取數例外捕成 FollowerSummary(error=...)，不外拋（跨 follower 隔離：
    一個 follower 的 API 錯誤不得中止其他 follower 的匯總，工程原則 4）。
    taker_share = crossed 成交名目 / 總成交名目（總量 0 → 0），沿 report.py 語意。"""
    try:
        fills = adapter.get_user_fills(ref.user_address, start, end)
        ntl = sum((f.sz * f.px for f in fills), Decimal("0"))
        taker_ntl = sum((f.sz * f.px for f in fills if f.crossed), Decimal("0"))
        share = (taker_ntl / ntl) if ntl > 0 else Decimal("0")
        return FollowerSummary(ref, len(fills), share, None)
    except Exception as e:  # noqa: BLE001 — 跨 follower 隔離，錯誤入 summary 不外拋
        return FollowerSummary(ref, 0, Decimal("0"), error=str(e))


@dataclass(frozen=True)
class AggregateReport:
    day: date
    summaries: tuple[FollowerSummary, ...]
    north_star_fee_delta: Decimal  # builder 層級查一次的單日增量（絕不跨 follower 相加）
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
