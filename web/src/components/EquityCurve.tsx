"use client";
import { useMemo, useState } from "react";
import { fmtAmount } from "@/lib/format";
import { useCopy } from "@/lib/lang";

type Period = "all" | "30d" | "7d";

const VIEW_W = 900;
const VIEW_H = 230;

/** overlay 對照資產（NOTE 09）的固定顯示順序＋顏色——**不含文案**：標籤來自
 * `COPY.strategyDetail.equity.overlays`（陣列，zh/en 對稱），這裡只給「第 i 個
 * overlay 用什麼顏色」這種與語言無關的結構資料。v1 無現成資料源，checkbox
 * 一律 disabled——保留 UI 骨架讓後續接上資料源時只需拿掉 `disabled`（plan §0.2）。 */
const OVERLAY_COLORS = ["#e9853f", "#6b8afd", "#4da3ff", "#e9b872"] as const;

const Y_TICK_COUNT = 6;
const X_TICK_TARGET = 7;

/** min/max 依 `Y_TICK_COUNT` 均分出刻度值，由大到小排列（頂到底），
 * 供左側 y 軸使用。min===max 時退化為單一值重複。 */
function yTicks(values: number[], count: number): number[] {
  if (values.length === 0) return [];
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) return Array.from({ length: count }, () => min);
  const step = (max - min) / (count - 1);
  return Array.from({ length: count }, (_, i) => max - i * step);
}

/** 挑出最多 `target` 個等距索引（含頭尾）供 x 軸標籤使用；`n <= target` 時全取。 */
function pickTickIndices(n: number, target: number): number[] {
  if (n <= target) return Array.from({ length: n }, (_, i) => i);
  const step = (n - 1) / (target - 1);
  const out = new Set<number>();
  for (let i = 0; i < target; i++) out.add(Math.round(i * step));
  return Array.from(out).sort((a, b) => a - b);
}

/** `YYYY-MM-DD` → `MM-DD`（軸標窄，只需月日）。格式不符則原樣返回。 */
function shortDate(iso: string): string {
  const m = /^\d{4}-(\d{2}-\d{2})/.exec(iso);
  return m ? m[1] : iso;
}

/**
 * x 軸日期標籤：有 `startDate`/`endDate` 時，依「一點 ≈ 一天」假設從 `endDate`
 * 往回推（`sliced` 是尾端裁切後的視窗，最後一點即 `endDate`）；沒有日期資訊時
 * 退化為相對天數 `D{n}`（n 為該點在**完整序列**中的第幾天，1-based）。
 */
function xLabels(
  sliced: number[], totalLen: number, startDate: string | null | undefined, endDate: string | null | undefined,
): { idx: number; label: string }[] {
  const indices = pickTickIndices(sliced.length, X_TICK_TARGET);
  const offset = totalLen - sliced.length;
  if (endDate) {
    const end = new Date(`${endDate}T00:00:00Z`);
    if (!Number.isNaN(end.getTime())) {
      return indices.map((idx) => {
        const daysFromEnd = sliced.length - 1 - idx;
        const d = new Date(end.getTime() - daysFromEnd * 86400000);
        return { idx, label: shortDate(d.toISOString().slice(0, 10)) };
      });
    }
  }
  void startDate;
  return indices.map((idx) => ({ idx, label: `D${offset + idx + 1}` }));
}

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
export function EquityCurve({ equityIndex, initialDepositUsd, startDate, endDate }: {
  equityIndex: string[];
  /** 有值時 y 軸刻度換算成美元金額（= 本值 × 首點比值 × 入金額，同
   * `computeStartEndEquity` 的換算式）；缺席則顯示原始 index 值。 */
  initialDepositUsd?: string | null;
  /** x 軸日期標籤來源（`methodology.start_date`/`end_date`）；缺席則退化為
   * 相對天數 `D1…Dn`。 */
  startDate?: string | null;
  endDate?: string | null;
}) {
  const COPY = useCopy();
  const c = COPY.strategyDetail.equity;
  const [period, setPeriod] = useState<Period>("all");

  const values = useMemo(
    () => equityIndex.map((v) => Number(v)).filter((v) => Number.isFinite(v)),
    [equityIndex],
  );
  const sliced = useMemo(() => sliceByPeriod(values, period), [values, period]);
  const points = useMemo(() => toPoints(sliced), [sliced]);

  const depositNum = initialDepositUsd == null ? null : Number(initialDepositUsd);
  // depositNum > 0：首快照為 0 的帳戶換算出全 $0 的 y 軸（2026-08-29 真資料驗證），
  // 此時退回指數刻度而非假裝有美元金額。
  const hasDeposit = depositNum != null && Number.isFinite(depositNum) && depositNum > 0
    && values.length > 0 && values[0] !== 0;
  const dollarSliced = useMemo(() => {
    if (!hasDeposit) return sliced;
    return sliced.map((v) => (depositNum as number) * (v / values[0]));
  }, [sliced, hasDeposit, depositNum, values]);
  const yTickValues = useMemo(() => yTicks(dollarSliced, Y_TICK_COUNT), [dollarSliced]);
  const xTickLabels = useMemo(
    () => xLabels(sliced, values.length, startDate, endDate),
    [sliced, values.length, startDate, endDate],
  );

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
        <div className="equity-curve-body">
          <div className="equity-curve-yaxis mono">
            {yTickValues.map((v, i) => (
              <span key={i}>{hasDeposit ? `$${fmtAmount(String(v), 0)}` : v.toFixed(3)}</span>
            ))}
          </div>
          <div className="equity-curve-chart">
            <svg
              viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
              className="equity-curve-svg"
              preserveAspectRatio="none"
              role="img"
              aria-label={c.heading}
            >
              {Array.from({ length: Y_TICK_COUNT }, (_, i) => {
                const y = (i / (Y_TICK_COUNT - 1)) * VIEW_H;
                return (
                  <line
                    key={i}
                    x1="0" y1={y} x2={VIEW_W} y2={y}
                    stroke="#1a1e23" strokeWidth="1"
                  />
                );
              })}
              <polyline points={points} fill="none" stroke="var(--pos)" strokeWidth="2" />
            </svg>
            <div className="equity-curve-xaxis mono">
              {xTickLabels.map(({ idx, label }) => (
                <span key={idx}>{label}</span>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
