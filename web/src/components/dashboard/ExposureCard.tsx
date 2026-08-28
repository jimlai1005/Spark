"use client";
import type { DashboardExposure } from "@/lib/api";
import { fmtAmount, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

function pctWidth(v: string | null): number {
  if (v == null) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0;
}

function biasLabel(
  longPct: string | null,
  shortPct: string | null,
  c: { biasLong: string; biasShort: string; biasNeutral: string },
): string {
  if (longPct == null || shortPct == null) return NO_VALUE;
  const l = Number(longPct);
  const s = Number(shortPct);
  if (!Number.isFinite(l) || !Number.isFinite(s)) return NO_VALUE;
  if (l > s) return c.biasLong;
  if (s > l) return c.biasShort;
  return c.biasNeutral;
}

export function ExposureCard({ exposure }: { exposure: DashboardExposure | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.exposure;

  return (
    <div className="card dash-card dash-card-exposure">
      <div className="dash-card-label">{c.label}</div>
      <div className="dash-exposure-head">
        <span className="mono dash-exposure-value">
          {exposure ? `$${fmtAmount(exposure.notional)}` : NO_VALUE}
        </span>
        <span style={{ color: "var(--text-dim)", fontSize: "var(--fs-small)" }}>
          {c.notionalSuffix}
          {exposure?.leverage != null ? ` · ${exposure.leverage}x` : ""}
        </span>
      </div>
      <div className="dash-exposure-bars">
        <div>
          <div className="dash-exposure-bar-row-head">
            <span style={{ color: "var(--text-dim)" }}>{c.long}</span>
            <span className="mono" style={{ color: "var(--pos)" }}>
              {exposure?.long_pct != null ? `${exposure.long_pct}%` : NO_VALUE}
            </span>
          </div>
          <div className="dash-exposure-bar">
            <div
              className="dash-exposure-bar-fill"
              style={{ width: `${pctWidth(exposure?.long_pct ?? null)}%`, background: "var(--pos)" }}
            />
          </div>
        </div>
        <div>
          <div className="dash-exposure-bar-row-head">
            <span style={{ color: "var(--text-dim)" }}>{c.short}</span>
            <span className="mono" style={{ color: "var(--text-dim)" }}>
              {exposure?.short_pct != null ? `${exposure.short_pct}%` : NO_VALUE}
            </span>
          </div>
          <div className="dash-exposure-bar">
            <div
              className="dash-exposure-bar-fill"
              style={{ width: `${pctWidth(exposure?.short_pct ?? null)}%`, background: "var(--neg)" }}
            />
          </div>
        </div>
      </div>
      <div className="dash-divider" />
      <div className="dash-kv-list">
        <div className="dash-kv-row">
          <span>{c.biasLabel}</span>
          <span>{biasLabel(exposure?.long_pct ?? null, exposure?.short_pct ?? null, c)}</span>
        </div>
        <div className="dash-kv-row">
          <span>{c.positionCount}</span>
          <span className="mono">{exposure?.position_count ?? NO_VALUE}</span>
        </div>
        <div className="dash-kv-row">
          <span>{c.maxPosition}</span>
          <span className="mono">
            {exposure?.max_position
              ? `${exposure.max_position.pct}% (${exposure.max_position.symbol})`
              : NO_VALUE}
          </span>
        </div>
      </div>
    </div>
  );
}
