"use client";
import type { DashboardEquity } from "@/lib/api";
import { fmtAmount, fmtRatioPct, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

/** NOTE 14：可用保證金低於此比例即出現黃色告警卡（設計稿錨例：0.64% → 觸發）。 */
export const LOW_MARGIN_THRESHOLD = 0.05;
/**
 * ⭐ M3 round3 Task 6（R2 P2「Dashboard 保證金」）：<2% 進一步升級為紅框＋紅字。
 * 門檻常數集中在這一處 export——Header.tsx 的保證金提示 pill 沿用
 * `LOW_MARGIN_THRESHOLD`（同一個 5% 判準，不另拼一份數字）。
 */
export const CRITICAL_MARGIN_THRESHOLD = 0.02;

function signedPct(v: string | null): string {
  if (v == null) return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}%`;
}

export function EquityCard({ equity }: { equity: DashboardEquity | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.equity;

  const accountValue = equity ? `$${fmtAmount(equity.account_value)}` : NO_VALUE;
  const ret = equity ? signedPct(equity.ret_30d_pct) : NO_VALUE;

  const av = equity ? Number(equity.account_value) : NaN;
  const used = equity ? Number(equity.margin_used) : NaN;
  const usedPct = Number.isFinite(av) && Number.isFinite(used) && av > 0
    ? Math.max(0, Math.min(1, used / av)) * 100
    : 0;

  const availablePctNum = equity?.available_pct != null ? Number(equity.available_pct) : null;
  const lowMargin = availablePctNum != null
    && Number.isFinite(availablePctNum) && availablePctNum < LOW_MARGIN_THRESHOLD;
  const criticalMargin = availablePctNum != null
    && Number.isFinite(availablePctNum) && availablePctNum < CRITICAL_MARGIN_THRESHOLD;
  // ⭐ R2 保證金分級：≥5% 無框；<5% 黃框；<2% 紅框（criticalMargin 蘊含 lowMargin，
  // 兩門檻同源同一個 available_pct，不另拼第二個判斷基準）。
  const marginLevel: "warning" | "critical" | null =
    criticalMargin ? "critical" : lowMargin ? "warning" : null;

  return (
    <div className="card dash-card dash-card-equity" data-margin={marginLevel ?? undefined}>
      <div className="dash-card-label">{c.label}</div>
      <div className="dash-equity-head">
        <span className="mono dash-equity-value">{accountValue}</span>
        <span
          className="mono dash-equity-ret"
          style={{ color: equity?.ret_30d_pct != null && Number(equity.ret_30d_pct) < 0 ? "var(--neg)" : "var(--pos)" }}
        >
          {ret}
          {c.retSuffix}
        </span>
      </div>
      <div className="dash-custody-note">{c.custodyNote}</div>
      <div className="dash-margin-block">
        <div>
          <div className="dash-margin-row">
            <span style={{ color: "var(--text-dim)" }}>{c.usedMargin}</span>
            <span className="mono">{equity ? `$${fmtAmount(equity.margin_used)}` : NO_VALUE}</span>
          </div>
          <div className="dash-margin-bar">
            <div className="dash-margin-bar-fill" style={{ width: `${usedPct}%` }} />
          </div>
        </div>
        <div className="dash-margin-row" style={{ marginBottom: 0 }}>
          <span style={{ color: "var(--text-dim)" }}>{c.availableMargin}</span>
          <span
            className="mono"
            style={{ color: criticalMargin ? "var(--neg)" : lowMargin ? "var(--warn)" : undefined }}
          >
            {equity ? `$${fmtAmount(equity.withdrawable)}` : NO_VALUE}{" "}
            <span style={{ color: "var(--text-dim)" }}>
              ({equity ? fmtRatioPct(equity.available_pct, 2) : NO_VALUE})
            </span>
          </span>
        </div>
        {lowMargin && (
          <div className="dash-low-margin-card" data-level={marginLevel ?? undefined}>
            {criticalMargin ? c.criticalMarginWarning : c.lowMarginWarning}
          </div>
        )}
      </div>
    </div>
  );
}
