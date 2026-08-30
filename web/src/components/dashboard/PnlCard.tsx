"use client";
import { useMemo } from "react";
import type { DashboardPnl } from "@/lib/api";
import { fmtAmount, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

const VIEW_W = 620;
const VIEW_H = 130;

function signedAmount(v: string | null): string {
  if (v == null) return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  const sign = n >= 0 ? "+" : "-";
  return `${sign}$${fmtAmount(String(Math.abs(n)))}`;
}

/** `series` → SVG polyline `points`（等距 x，y 依 min/max 正規化；全平時畫水平中線）。
 * 與 EquityCurve 的 `equity_index`（正規化淨值序列）不同資料形狀——這裡是
 * `[epoch_ms, 美元值]` 點列，故不重用該元件，改用同一套裁切/正規化邏輯自建。 */
function toPoints(values: number[]): string {
  if (values.length === 0) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const n = values.length;
  return values
    .map((v, i) => {
      const x = n === 1 ? 0 : (i / (n - 1)) * VIEW_W;
      const y = span === 0 ? VIEW_H / 2 : VIEW_H - ((v - min) / span) * VIEW_H;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

export function PnlCard({ pnl }: { pnl: DashboardPnl | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.pnl;

  const values = useMemo(() => {
    if (!pnl?.series) return [];
    return pnl.series.map(([, v]) => Number(v)).filter((v) => Number.isFinite(v));
  }, [pnl?.series]);
  const points = useMemo(() => toPoints(values), [values]);

  const net = pnl?.net ?? null;
  const sign = net == null ? undefined : Number(net) < 0 ? "neg" : "pos";

  return (
    <div className="card dash-card dash-card-pnl">
      <div className="dash-pnl-head">
        <div>
          <div className="dash-card-label">{c.label}</div>
          <div className="dash-pnl-value-row">
            <span className="mono dash-pnl-value" data-sign={sign}>{signedAmount(net)}</span>
            <span className="dash-pnl-sub mono">
              {c.realizedPrefix}{signedAmount(pnl?.realized ?? null)}
              {c.unrealizedPrefix}{signedAmount(pnl?.unrealized ?? null)}
            </span>
          </div>
        </div>
      </div>

      {values.length < 2 ? (
        <p className="hint">{c.chartEmpty}</p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          className="dash-pnl-svg"
          preserveAspectRatio="none"
          role="img"
          aria-label={c.label}
        >
          <polyline points={points} fill="none" stroke="var(--pos)" strokeWidth="2" />
        </svg>
      )}

      <div className="dash-pnl-metrics">
        <div>
          <div className="dash-pnl-metric-label">{c.winRate}</div>
          <div className="mono dash-pnl-metric-value">
            {pnl?.win_rate_pct != null ? `${pnl.win_rate_pct}%` : NO_VALUE}
          </div>
        </div>
        <div>
          <div className="dash-pnl-metric-label">{c.closedPositions}</div>
          <div className="mono dash-pnl-metric-value">{pnl?.closed_positions ?? NO_VALUE}</div>
        </div>
        <div>
          <div className="dash-pnl-metric-label">{c.maxDrawdown}</div>
          <div className="mono dash-pnl-metric-value" style={{ color: "var(--neg)" }}>
            {pnl?.max_drawdown_pct != null ? `${pnl.max_drawdown_pct}%` : NO_VALUE}
          </div>
        </div>
      </div>
    </div>
  );
}
