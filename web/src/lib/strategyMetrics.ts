/**
 * lib/strategyMetrics.ts — 策略／交易員詳情頁共用的**純算術**推導（M3 round2
 * Task 6 從 `strategies/[slug]/page.tsx` 抽出）。
 *
 * 抽出理由：`/traders/[address]`（Task 6）需要與 `/strategies/[slug]`（Task 9）
 * 完全相同的 CAGR 外推與起訖淨值換算——兩邊都是「後端 `PublicStrategyMetrics`／
 * `PublicStrategyMethodology` 形狀 ＋ 客戶端純算術」，複製這幾個函式會讓兩份
 * 「365 日年化慣例」的公式各自维护、遲早漂移。單一來源見下方各函式 docstring
 * （與原本 `strategies/[slug]/page.tsx` 檔頭的說明一致）。
 */
import { NO_VALUE } from "@/lib/format";
import type { PublicStrategyMethodology } from "@/lib/publicApi";

/** insufficient → 佔位符；否則附尾綴（例如 %）。與 StrategyCard 的 metricText 同形狀。 */
export function metricText(value: string | null, insufficient: boolean, suffix = ""): string {
  if (insufficient || value == null) return NO_VALUE;
  return `${value}${suffix}`;
}

/**
 * CAGR（年化外推）：由 `total_return_pct`＋`live_days`，用 365 日/年慣例
 * （與 methodology.annualization_days 對齊）算 `(1+r)^(365/live_days) - 1`。
 * 回傳 `null`＝樣本不足或數學上無定義（帳戶歸零，`1+r<=0`），呼叫端一律顯示
 * 「樣本不足」，不強行印出一個沒有意義的數字。
 */
export function computeCagrPct(
  totalReturnPct: string | null,
  insufficient: boolean,
  liveDays: number,
): string | null {
  if (insufficient || totalReturnPct == null || liveDays <= 0) return null;
  const r = Number(totalReturnPct) / 100;
  if (!Number.isFinite(r)) return null;
  const base = 1 + r;
  if (base <= 0) return null;
  const cagr = base ** (365 / liveDays) - 1;
  if (!Number.isFinite(cagr)) return null;
  return (cagr * 100).toFixed(2);
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
