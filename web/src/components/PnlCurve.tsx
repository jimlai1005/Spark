"use client";
import { useMemo } from "react";

/**
 * `PnlCurve` — 損益曲線（2026-09-05 explore/trader 指標統一 plan Task 6 新增）。
 *
 * 輸入後端 `windows[w].spark`（`pnlHistory` 降採樣，USD，≤ 30 點，見
 * `spark.filet.trader_stats.window_stats`）——取代舊版 `EquityCurve`（`equity_index`
 * 已移除，見 `lib/publicApi.ts` `PublicTraderDetail` 檔頭）。畫零線；正段綠負段紅
 * 只靠終值決定線色（與探索表格 sparkline 同規則），不做分段著色。
 */
export function PnlCurve({ values, ariaLabel }: { values: number[]; ariaLabel: string }) {
  const W = 640, H = 200, PAD = 8;
  const { points, zeroY, last } = useMemo(() => {
    const vs = values.filter((v) => Number.isFinite(v));
    if (vs.length < 2) return { points: "", zeroY: null as number | null, last: 0 };
    const min = Math.min(0, ...vs), max = Math.max(0, ...vs);
    const span = max - min || 1;
    const x = (i: number) => PAD + (i / (vs.length - 1)) * (W - 2 * PAD);
    const y = (v: number) => PAD + (1 - (v - min) / span) * (H - 2 * PAD);
    return {
      points: vs.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "),
      zeroY: y(0),
      last: vs[vs.length - 1],
    };
  }, [values]);
  if (!points) return <div className="pnl-curve pnl-curve-empty">—</div>;
  return (
    <svg className="pnl-curve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel}>
      {zeroY != null && <line x1={PAD} x2={W - PAD} y1={zeroY} y2={zeroY} className="pnl-curve-zero" />}
      <polyline points={points} fill="none" strokeWidth={2}
        stroke={last >= 0 ? "var(--pos)" : "var(--neg)"} />
    </svg>
  );
}
