"use client";
import type { DashboardSync } from "@/lib/api";
import { NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

function formatTs(epochSeconds: number | null): string {
  if (epochSeconds == null) return NO_VALUE;
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  return `${d.toISOString().slice(11, 16)} UTC`;
}

/** NOTE 15：延遲、價差、遺漏訊號都要誠實顯示——不足的量一律「—」，不臆造。 */
export function SyncCard({ sync }: { sync: DashboardSync | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.sync;

  return (
    <div className="card dash-card">
      <div className="dash-sync-head">
        <div className="dash-card-label" style={{ marginBottom: 0 }}>{c.label}</div>
      </div>
      <div className="dash-sync-metrics">
        <div className="dash-sync-metric">
          <div className="dash-sync-metric-label">{c.latencyMedian}</div>
          <div className="mono dash-sync-metric-value">
            {sync?.latency_median_ms != null ? `${sync.latency_median_ms}ms` : NO_VALUE}
          </div>
          <div className="dash-sync-metric-note">
            {c.latencyP95Prefix}{sync?.latency_p95_ms != null ? `${sync.latency_p95_ms}ms` : NO_VALUE}
          </div>
        </div>
        <div className="dash-sync-metric">
          <div className="dash-sync-metric-label">{c.priceDiff}</div>
          <div className="mono dash-sync-metric-value">
            {sync?.price_diff_bp != null ? `${sync.price_diff_bp}bp` : NO_VALUE}
          </div>
          <div className="dash-sync-metric-note">{c.priceDiffNote}</div>
        </div>
        <div className="dash-sync-metric">
          <div className="dash-sync-metric-label">{c.unsyncedPositions}</div>
          <div className="mono dash-sync-metric-value">
            {sync?.unsynced_positions ?? NO_VALUE}
          </div>
        </div>
      </div>
      <div className="dash-kv-list">
        <div className="dash-kv-row">
          <span>{c.scaleDeviation}</span>
          <span className="mono">
            {sync?.scale_deviation_pct != null ? `±${sync.scale_deviation_pct}%` : NO_VALUE}
          </span>
        </div>
        <div className="dash-kv-row">
          <span>{c.missedSignals}</span>
          <span className="mono">
            {sync?.missed_signals_24h ?? NO_VALUE}
            {sync?.missed_signals_24h != null && sync?.missed_reason
              ? ` (${sync.missed_reason})`
              : ""}
          </span>
        </div>
        <div className="dash-kv-row">
          <span>{c.lastRecon}</span>
          <span className="mono">{formatTs(sync?.last_recon_ts ?? null)}</span>
        </div>
      </div>
    </div>
  );
}
