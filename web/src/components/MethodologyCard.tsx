/**
 * `MethodologyCard` — 方法論與樣本揭露段（M3 round3 Task 7 於 `strategies/[slug]`
 * 首建；M3 round4 Task R4-11 抽成共用元件，供 `/traders/[address]` 一併使用——
 * 兩頁的 `methodology`／`metrics` 形狀相同〔`PublicStrategyMethodology`／
 * `PublicStrategyMetrics`，見 `lib/publicApi.ts`〕，後端同一支 `build_methodology`
 * 供給，不重寫第二份句型組裝邏輯）。
 */
import type { COPY_ZH, DeepString } from "@/lib/copy";
import { fmtAmount } from "@/lib/format";
import type { PublicStrategyMethodology, PublicStrategyMetrics } from "@/lib/publicApi";

export type MethodologyCopy = DeepString<typeof COPY_ZH.strategyDetail.methodology>;

export function MethodologyCard({ methodology, metrics, copy }: {
  methodology: PublicStrategyMethodology;
  metrics: PublicStrategyMetrics;
  copy: MethodologyCopy;
}) {
  // ⭐ M3 round4 Task R4-2：查無鏈上真實入金（`initial_deposit_usd` 現在來自
  // `userNonFundingLedgerUpdates`，查無 deposit 紀錄 → null）時，改以起始權益
  // （`start_equity_usd`，同一份 accountValueHistory 首個非零快照）起算；兩者
  // 皆無才整句省略、改由 rangePrefix 開頭（2026-08-29 真資料驗證發現、裁決 5）。
  const depositNum = Number(methodology.initial_deposit_usd);
  const hasDeposit = methodology.initial_deposit_usd != null
    && Number.isFinite(depositNum) && depositNum > 0;
  const startEquityNum = Number(methodology.start_equity_usd);
  const hasStartEquity = !hasDeposit && methodology.start_equity_usd != null
    && Number.isFinite(startEquityNum);
  const hasRange = methodology.start_date != null && methodology.end_date != null
    && methodology.sample_count != null;
  const hasSharpe = !metrics.sharpe_insufficient && metrics.sharpe != null
    && !metrics.sharpe_se_insufficient && metrics.sharpe_se != null;
  const hasData = hasDeposit || hasStartEquity || hasRange || hasSharpe;

  return (
    <div className="inset methodology-card">
      <div className="methodology-heading">{copy.heading}</div>
      {hasData ? (
        <p className="methodology-body">
          {hasDeposit && (
            <>
              {copy.depositPrefix}
              {fmtAmount(methodology.initial_deposit_usd, 0)}
              {copy.depositSuffix}
            </>
          )}
          {hasStartEquity && (
            <>
              {copy.startEquityPrefix}
              {fmtAmount(methodology.start_equity_usd, 0)}
              {copy.startEquitySuffix}
            </>
          )}
          {hasRange && (
            <>
              {!hasDeposit && !hasStartEquity && copy.rangePrefix}
              {methodology.sample_count}
              {copy.daysSuffix}
              {methodology.start_date} → {methodology.end_date}
              {copy.rangeSuffix}
              {" "}
            </>
          )}
          {hasSharpe && (
            <>
              {copy.sharpePrefix}
              {metrics.sharpe}
              {copy.sharpeSeInfix}
              {metrics.sharpe_se}
              {copy.sharpeSeSuffix}
              {metrics.sample_count}
              {copy.sampleSuffix}
              {" "}
            </>
          )}
          {copy.conventionPrefix}
          {methodology.annualization_days}
          {copy.conventionMid}
          {methodology.risk_free_rate}
          {copy.conventionSuffix}
        </p>
      ) : (
        <p className="methodology-body">{copy.unavailable}</p>
      )}
    </div>
  );
}
