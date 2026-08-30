"use client";
import type { DashboardSync } from "@/lib/api";
import { fmtBp, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

function formatTs(epochSeconds: number | null): string {
  if (epochSeconds == null) return NO_VALUE;
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  return `${d.toISOString().slice(11, 16)} UTC`;
}

/**
 * NOTE 15：延遲、價差、遺漏訊號都要誠實顯示——不足的量一律「—」，不臆造。
 *
 * ⭐ M3 round3 Task 6（R2·C 空值三態）：`sync.data_state` 三態決定整卡呈現——
 * `"warming"`／`"error"` 摺為一行，不留一整塊「—」空白卡片；`sync` 整塊為 `null`
 * （`_safe_block` 吞掉未預期例外）視同 `"error"`——結構上就是讀不到，與內部
 * `data_state="error"` 同一種處境，不該顯示成「尚無資料」讓人以為自己帳戶沒事。
 * `"ok"` 時維持既有逐欄渲染，個別欄位仍可能 `null` →「—」，但絕不顯示 `0ms`
 * （後端 Task 3 已改為無樣本送 `null` 不送 0；此處只是不阻擋真實的 0 值）。
 */
export function SyncCard({
  sync, updatedAt, onRetry,
}: {
  sync: DashboardSync | null;
  /** dashboard 回應的 `updated_at`（epoch 秒）——`data_state="error"` 時作為
   * 「讀取失敗發生於」的最佳已知時間戳（R2·C 態三要求附時間戳）。 */
  updatedAt: number | null;
  /** 態三「重試」鍵觸發——呼叫端重新整理整份 dashboard（沿用既有 refetch）。 */
  onRetry: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.dashboard.sync;
  const dataState = sync?.data_state ?? "error";

  if (dataState === "warming") {
    return (
      <div className="card dash-card dash-card-sync dash-card-collapsed" data-state="warming">
        <div className="dash-card-label" style={{ marginBottom: 0 }}>{c.label}</div>
        <p className="hint dash-collapsed-line">{c.warmingLine}</p>
      </div>
    );
  }

  if (dataState === "error") {
    return (
      <div className="card dash-card dash-card-sync dash-card-collapsed" data-state="error">
        <div className="dash-card-label" style={{ marginBottom: 0 }}>{c.label}</div>
        <p className="dash-collapsed-line dash-collapsed-error">
          {c.errorLine} · {formatTs(updatedAt)} ·{" "}
          <button type="button" className="dash-retry-btn" onClick={onRetry}>
            {COPY.common.retry}
          </button>
        </p>
      </div>
    );
  }

  return (
    <div className="card dash-card dash-card-sync" data-state="ok">
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
          <div className="mono dash-sync-metric-value">{fmtBp(sync?.price_diff_bp)}</div>
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
