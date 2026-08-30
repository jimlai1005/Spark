"use client";
import type { DashboardFeesMonth } from "@/lib/api";
import { fmtAmount, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

/** bps → 百分比字串（4 位小數，對齊設計稿「= 簽署上限」列的呈現精度）。 */
function bpsToPct(bps: string | null): string {
  if (bps == null) return NO_VALUE;
  const n = Number(bps);
  if (!Number.isFinite(n)) return NO_VALUE;
  return `${(n / 100).toFixed(4)}%`;
}

export function FeesCard({ feesMonth }: { feesMonth: DashboardFeesMonth | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.fees;

  // ⭐ daily_bars 自 M3 round3 起是物件列（{date, builder_fee, …}），不是
  // [date, fee] tuple——2026-08-30 曾因本元件滯留 tuple 解構而 runtime 崩潰。
  const bars = feesMonth?.daily_bars ?? [];
  const values = bars
    .map((bar) => Number(bar.builder_fee))
    .filter((v) => Number.isFinite(v));
  const maxVal = values.length > 0 ? Math.max(...values, 0) : 0;

  return (
    <div className="card dash-card dash-card-fees">
      <div className="dash-card-label">{c.label}</div>
      <div className="dash-fee-metrics">
        <div>
          <div className="dash-fee-metric-label">{c.routedVolume}</div>
          <div className="mono dash-fee-metric-value">
            {feesMonth ? `$${fmtAmount(feesMonth.routed_volume, 0)}` : NO_VALUE}
          </div>
        </div>
      </div>

      {bars.length > 0 && (
        <div className="dash-fee-bars" aria-hidden="true">
          {bars.map((bar, i) => {
            const n = Number(bar.builder_fee);
            const h = maxVal > 0 && Number.isFinite(n) ? Math.max(2, (n / maxVal) * 100) : 2;
            return <div key={bar.date || i} className="dash-fee-bar" style={{ height: `${h}%` }} />;
          })}
        </div>
      )}

      <div className="dash-divider" />
      <div className="dash-kv-list">
        <div className="dash-kv-row">
          <span>{c.fillCount}</span>
          <span className="mono">{feesMonth?.fill_count ?? NO_VALUE}</span>
        </div>
        <div className="dash-kv-row">
          <span>{c.avgFee}</span>
          <span className="mono">
            {feesMonth?.avg_fee != null ? `$${fmtAmount(feesMonth.avg_fee)}` : NO_VALUE}
          </span>
        </div>
        <div className="dash-kv-row">
          <span>{c.effectiveRate}</span>
          <span className="mono">{feesMonth ? bpsToPct(feesMonth.effective_rate_bps) : NO_VALUE}</span>
        </div>
      </div>
    </div>
  );
}
