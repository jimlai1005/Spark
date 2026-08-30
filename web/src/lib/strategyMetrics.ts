/**
 * lib/strategyMetrics.ts — 策略／交易員詳情頁共用的展示層 helper（M3 round2
 * Task 6 從 `strategies/[slug]/page.tsx` 抽出；M3 round3 Task 7 移除 CAGR 自算）。
 *
 * ⭐ M3 round3 Task 7（D5 數字一致性）：`computeCagrPct` 已刪除——CAGR 現在
 * 一律由後端 `PublicStrategyDetail.cagr_pct` 直接供給（`sample_days<60` 時
 * 該鍵整個不存在，結構性防呆），前端不再用 `total_return_pct`／`live_days`
 * 外推年化，避免與後端 `annualized_return` 各自維護一份「365 日慣例」公式、
 * 遲早漂移（工程原則 1：同一個值只能有一個計算來源）。
 *
 * `computeStartEndEquity` 保留：它不是「統計外推」（不像 CAGR 把短樣本硬套
 * 年化週期），而是把後端已供給的兩個真實原始值——`methodology.
 * initial_deposit_usd`（鏈上真實入金）與 `equity_index`（後端算好的淨值比值
 * 序列）——換算成美金顯示，數學上等價於 `total_return_pct` 的另一種呈現方式，
 * 不產生後端沒有的新資訊。`/traders/[address]`（Task 6）與 `/strategies/[slug]`
 * 共用同一份，避免兩處各自維護。
 */
import { NO_VALUE } from "@/lib/format";
import type { PublicStrategyMethodology } from "@/lib/publicApi";

/** insufficient → 佔位符；否則附尾綴（例如 %）。與 StrategyCard 的 metricText 同形狀。 */
export function metricText(value: string | null, insufficient: boolean, suffix = ""): string {
  if (insufficient || value == null) return NO_VALUE;
  return `${value}${suffix}`;
}

/**
 * 起訖淨值（USD）：`methodology.initial_deposit_usd`（真實入金起點）×
 * `equity_index` 首尾比值 → 起點／終點淨值。任一輸入缺席或首點為 0（無法取
 * 比值）→ `null`（樣本不足）。
 */
export function computeStartEndEquity(
  methodology: PublicStrategyMethodology,
  equityIndex: string[],
  fmtAmount: (v: string, decimals?: number) => string,
): { start: string; end: string } | null {
  const depositNum = methodology.initial_deposit_usd == null
    ? null : Number(methodology.initial_deposit_usd);
  if (depositNum == null || !Number.isFinite(depositNum) || depositNum <= 0
    || equityIndex.length === 0) return null;
  const first = Number(equityIndex[0]);
  const last = Number(equityIndex[equityIndex.length - 1]);
  if (!Number.isFinite(first) || !Number.isFinite(last) || first === 0) return null;
  return {
    start: fmtAmount(String(depositNum), 0),
    end: fmtAmount(String(depositNum * (last / first)), 0),
  };
}
