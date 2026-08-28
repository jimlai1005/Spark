"use client";
import { useMemo, useState } from "react";
import { useCopy } from "@/lib/lang";

type Period = "all" | "30d" | "7d";

const VIEW_W = 900;
const VIEW_H = 230;

/** overlay 對照資產（NOTE 09）的固定顯示順序＋顏色——**不含文案**：標籤來自
 * `COPY.strategyDetail.equity.overlays`（陣列，zh/en 對稱），這裡只給「第 i 個
 * overlay 用什麼顏色」這種與語言無關的結構資料。v1 無現成資料源，checkbox
 * 一律 disabled——保留 UI 骨架讓後續接上資料源時只需拿掉 `disabled`（plan §0.2）。 */
const OVERLAY_COLORS = ["#e9853f", "#6b8afd", "#4da3ff", "#e9b872"] as const;

/** 依 period 從序列尾端裁切——`equity_index` 是每日對齊的序列（一點 ≈ 一天，
 * 見 leader_perf 檔頭），因此「30D/7D」直接取最後 N 點即可，不需要另外的日期陣列。 */
function sliceByPeriod(values: number[], period: Period): number[] {
  if (period === "all") return values;
  const n = period === "30d" ? 30 : 7;
  return values.slice(Math.max(0, values.length - n));
}

/** values → SVG polyline 的 `points` 字串（等距 x，y 依 min/max 正規化）。
 * 全平（min===max）時畫一條水平中線，避免除以零。 */
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

/**
 * EquityCurve — 策略詳情頁的帳戶淨值曲線（Task 9，設計稿 §04）。
 *
 * `equityIndex` 是後端 `build_equity_index` 給的正規化序列（起點 = "1"），不是
 * 美元金額——這裡只負責畫形狀（漲跌走勢），不做金額換算（金額換算需要
 * `methodology.initial_deposit_usd`，由呼叫端在「起訖淨值」指標卡另外處理）。
 */
export function EquityCurve({ equityIndex }: { equityIndex: string[] }) {
  const COPY = useCopy();
  const c = COPY.strategyDetail.equity;
  const [period, setPeriod] = useState<Period>("all");

  const values = useMemo(
    () => equityIndex.map((v) => Number(v)).filter((v) => Number.isFinite(v)),
    [equityIndex],
  );
  const sliced = useMemo(() => sliceByPeriod(values, period), [values, period]);
  const points = useMemo(() => toPoints(sliced), [sliced]);

  const periods: { key: Period; label: string }[] = [
    { key: "all", label: c.periodAll },
    { key: "30d", label: c.period30d },
    { key: "7d", label: c.period7d },
  ];

  return (
    <div className="card equity-curve">
      <div className="equity-curve-head">
        <div className="equity-curve-title">{c.heading}</div>
        <div className="equity-curve-periods">
          {periods.map((p) => (
            <button
              key={p.key}
              type="button"
              className="pill equity-period-btn"
              data-active={period === p.key}
              aria-pressed={period === p.key}
              onClick={() => setPeriod(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="equity-curve-overlay-row">
        <span className="equity-overlay-label">{c.overlayLabel}</span>
        {c.overlays.map((label, i) => (
          <label key={label} className="equity-overlay-item" title={c.overlayNote}>
            <input type="checkbox" disabled aria-label={label} />
            <span
              className="equity-overlay-swatch"
              style={{ background: OVERLAY_COLORS[i % OVERLAY_COLORS.length] }}
              aria-hidden="true"
            />
            {label}
          </label>
        ))}
      </div>

      {sliced.length < 2 ? (
        <p className="hint">{c.empty}</p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          className="equity-curve-svg"
          preserveAspectRatio="none"
          role="img"
          aria-label={c.heading}
        >
          <polyline points={points} fill="none" stroke="var(--pos)" strokeWidth="2" />
        </svg>
      )}
    </div>
  );
}
