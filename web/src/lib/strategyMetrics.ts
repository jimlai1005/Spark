/**
 * lib/strategyMetrics.ts — 策略／交易員詳情頁共用的展示層 helper（M3 round2
 * Task 6 從 `strategies/[slug]/page.tsx` 抽出；M3 round3 Task 7 移除 CAGR 自算；
 * M3 round4 Task R4-2 移除 `computeStartEndEquity` client 端推算）。
 *
 * ⭐ M3 round3 Task 7（D5 數字一致性）：`computeCagrPct` 已刪除——CAGR 現在
 * 一律由後端 `PublicStrategyDetail.cagr_pct` 直接供給（`sample_days<30` 時
 * 該鍵整個不存在，結構性防呆），前端不再用 `total_return_pct`／`live_days`
 * 外推年化，避免與後端 `annualized_return` 各自維護一份「365 日慣例」公式、
 * 遲早漂移（工程原則 1：同一個值只能有一個計算來源）。
 *
 * ⭐ M3 round4 Task R4-2：`computeStartEndEquity`（`initial_deposit_usd` ×
 * `equity_index` 首尾比值換算）已移除——真實帳戶的 `accountValueHistory` 首點
 * 常態性是 0（錢包晚於序列起點入金），用它當比值分母會整卡判定「樣本不足」，
 * 即使鏈上明明查得到起訖淨值（2026-08-30 使用者裁決）。後端改直接供給
 * `methodology.start_equity_usd`／`end_equity_usd`（同一份 `accountValueHistory`
 * 的首個非零值與末值，見 `filet.strategies.build_equity_range`），前端只需要
 * 格式化，不再自己算比值——`formatStartEndEquity` 是純格式化，不是統計外推。
 */
import { NO_VALUE } from "@/lib/format";
import type { PublicStrategyMethodology } from "@/lib/publicApi";

/** insufficient → 佔位符；否則附尾綴（例如 %）。與 StrategyCard 的 metricText 同形狀。 */
export function metricText(value: string | null, insufficient: boolean, suffix = ""): string {
  if (insufficient || value == null) return NO_VALUE;
  return `${value}${suffix}`;
}

/**
 * 起訖淨值（USD）：直接格式化後端供給的 `methodology.start_equity_usd`／
 * `end_equity_usd`（同一份鏈上 `accountValueHistory` 的首個非零值與末值）。
 * 任一欄位缺席（後端也查不到非零快照）→ `null`。
 */
export function formatStartEndEquity(
  methodology: PublicStrategyMethodology,
  fmtAmount: (v: string, decimals?: number) => string,
): { start: string; end: string } | null {
  if (methodology.start_equity_usd == null || methodology.end_equity_usd == null) return null;
  return {
    start: fmtAmount(methodology.start_equity_usd, 0),
    end: fmtAmount(methodology.end_equity_usd, 0),
  };
}
