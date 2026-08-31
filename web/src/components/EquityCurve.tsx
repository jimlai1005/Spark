"use client";
import { useMemo, useState } from "react";
import { fmtAmount } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import { getPublicBenchmarks, type PublicBenchmarksResp } from "@/lib/publicApi";

type Period = "all" | "30d" | "7d";

const VIEW_W = 900;
const VIEW_H = 230;

/** overlay 對照資產（issue log I-19）的固定顯示順序＋顏色＋後端回應鍵——
 * **不含文案**：標籤來自 `COPY.strategyDetail.equity.overlays`（陣列，zh/en
 * 對稱），這裡只給「第 i 個 overlay 用什麼顏色／對應 `/api/public/benchmarks`
 * 回應的哪個鍵」這種與語言無關的結構資料，順序必須與 `overlays` 陣列一致。 */
// 順序＝COPY.strategyDetail.equity.overlays：BTC 橘、ETH 藍、S&P 500 綠
// （2026-08-31 使用者指定；取比主曲線 #3ecf8e 深一階的綠避免混淆）、黃金 金。
const OVERLAY_COLORS = ["#e9853f", "#6b8afd", "#1f8a4c", "#e9b872"] as const;
const OVERLAY_KEYS = ["btc", "eth", "sp500", "gold"] as const;

/** `getPublicBenchmarks` 的抓取窗天數——固定用後端夾取上限（見
 * `publicapi/benchmarks.py` 的 `MAX_DAYS`，鏡射常數，兩處必須同值：抓太短會讓
 * 「全部」期間的疊加線在較舊的日期對不到收盤價）。一次抓齊，不隨 period 切換
 * 重抓（見下方 `ensureOverlayData`）。 */
const OVERLAY_FETCH_DAYS = 400;

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

/** values → SVG polyline 的 `points` 字串（等距 x，y 依 `range`——缺省時退回
 * 依自身 min/max 正規化，沿舊行為）。全平（min===max）時畫一條水平中線，
 * 避免除以零。`range` 讓主線與 overlay 疊加線共用同一個 y 軸座標系
 * （見 `EquityCurve` 內 `yRange` 的計算，兩者才畫得出可比較的相對位置）。 */
function toPoints(values: number[], range?: { min: number; max: number }): string {
  if (values.length === 0) return "";
  const min = range ? range.min : Math.min(...values);
  const max = range ? range.max : Math.max(...values);
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

/** overlay（可能含 `null` 缺格）→ SVG polyline 的 `points` 字串。`n`＝主曲線
 * 目前裁切後的點數（不是 overlay 自己非 null 值的個數）——x 位置依「這一天在
 * 主曲線裁切窗內的第幾個」計算，才能與主線對齊；`null` 缺格直接跳過、不插值
 * 補一個數字（issue log I-19 明訂：日期缺格跳過該點，不編數字），polyline
 * 會因此在缺格處以直線連接前後兩個有值的點。 */
function toOverlayPoints(values: (number | null)[], n: number, range: { min: number; max: number }): string {
  const span = range.max - range.min;
  const pts: string[] = [];
  values.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) return;
    const x = n === 1 ? 0 : (i / (n - 1)) * VIEW_W;
    const y = span === 0 ? VIEW_H / 2 : VIEW_H - ((v - range.min) / span) * VIEW_H;
    pts.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  });
  return pts.join(" ");
}

/** `sliced`（裁切後主曲線）每個位置對應的 `YYYY-MM-DD` 日期——與 `xLabels`
 * 同一套「從 `endDate` 往回推、一點 ≈ 一天」假設（見該函式），供 overlay 對齊
 * 收盤價用。`endDate` 缺席或格式不符 → 全部回 `null`（無法對齊，overlay 該日
 * 期沒有東西可畫）。 */
function datesForSlice(sliced: number[], endDate: string | null | undefined): (string | null)[] {
  if (!endDate) return sliced.map(() => null);
  const end = new Date(`${endDate}T00:00:00Z`);
  if (Number.isNaN(end.getTime())) return sliced.map(() => null);
  return sliced.map((_, idx) => {
    const daysFromEnd = sliced.length - 1 - idx;
    const d = new Date(end.getTime() - daysFromEnd * 86400000);
    return d.toISOString().slice(0, 10);
  });
}

/** `/api/public/benchmarks` 的單一標的原始序列 `[[epoch_ms, "close"], ...]` →
 * `YYYY-MM-DD`（UTC）→ 收盤價（number）的查表。`null`（該標的上游查詢失敗）
 * 原樣傳遞——呼叫端據此判斷 checkbox 是否該顯示「資料暫不可用」。 */
function closeByDate(series: [number, string][] | null): Record<string, number> | null {
  if (series == null) return null;
  const map: Record<string, number> = {};
  for (const [ms, close] of series) {
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) continue;
    const n = Number(close);
    if (!Number.isFinite(n)) continue;
    map[d.toISOString().slice(0, 10)] = n;
  }
  return map;
}

/**
 * rebase：`overlay[i] = mainFirst × (closes[i] / closes[0])`——`closes[0]`
 * 是裁切窗內首日的收盤價（錨點），與主曲線同基準才能直接比形狀（issue log
 * I-19）。錨點缺席／非有限數／為零 → 整條 overlay 回全 `null`（沒有錨點就無法
 * rebase 任何一天，不是「這一天沒資料」那種可以單點跳過的情形）。其餘位置
 * 缺席／非有限數 → 該點 `null`（跳過，不插值）。
 *
 * 匯出供單元測試直接驗證公式本身（不必經過完整元件與日期對齊管線）。
 */
export function rebaseCloses(mainFirst: number, closes: (number | null)[]): (number | null)[] {
  const anchor = closes.length > 0 ? closes[0] : null;
  if (anchor == null || !Number.isFinite(anchor) || anchor === 0) {
    return closes.map(() => null);
  }
  return closes.map((c) => (c == null || !Number.isFinite(c) ? null : mainFirst * (c / anchor)));
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
  /** 有值時 y 軸刻度換算成美元金額（= 本值 × 首點比值 × 入金額）；缺席則顯示
   * 原始 index 值。M3 round4 Task R4-2 起，這個值來自鏈上真實入金
   * （`userNonFundingLedgerUpdates`），不再是 `accountValueHistory` 首點。 */
  initialDepositUsd?: string | null;
  /** x 軸日期標籤來源（`methodology.start_date`/`end_date`）；缺席則退化為
   * 相對天數 `D1…Dn`。 */
  startDate?: string | null;
  endDate?: string | null;
}) {
  const COPY = useCopy();
  const c = COPY.strategyDetail.equity;
  const [period, setPeriod] = useState<Period>("all");

  // ⭐ issue log I-19：overlay 對照資料。`overlayData === null` 代表「還沒抓過」
  // （首次勾任一項 checkbox 才 lazy fetch，見 `ensureOverlayData`）；抓過一次後
  // 就地記在 state 裡不再重抓（同一元件實例內，切換 period／勾選其他 overlay
  // 都不會觸發第二次請求）。`getPublicBenchmarks` 本身不拋（fail-safe 全降級為
  // null，見 `lib/publicApi.ts`），這裡不需要 `.catch`。
  const [overlayData, setOverlayData] = useState<PublicBenchmarksResp | null>(null);
  const [overlayLoading, setOverlayLoading] = useState(false);
  const [overlayChecked, setOverlayChecked] = useState<boolean[]>(() => OVERLAY_KEYS.map(() => false));

  function ensureOverlayData() {
    if (overlayData != null || overlayLoading) return;
    setOverlayLoading(true);
    void getPublicBenchmarks(OVERLAY_FETCH_DAYS).then((resp) => {
      setOverlayData(resp);
      setOverlayLoading(false);
    });
  }

  function toggleOverlay(i: number) {
    ensureOverlayData();
    setOverlayChecked((prev) => prev.map((v, idx) => (idx === i ? !v : v)));
  }

  const values = useMemo(
    () => equityIndex.map((v) => Number(v)).filter((v) => Number.isFinite(v)),
    [equityIndex],
  );
  const sliced = useMemo(() => sliceByPeriod(values, period), [values, period]);

  const depositNum = initialDepositUsd == null ? null : Number(initialDepositUsd);
  // depositNum > 0：首快照為 0 的帳戶換算出全 $0 的 y 軸（2026-08-29 真資料驗證），
  // 此時退回指數刻度而非假裝有美元金額。
  const hasDeposit = depositNum != null && Number.isFinite(depositNum) && depositNum > 0
    && values.length > 0 && values[0] !== 0;
  const dollarSliced = useMemo(() => {
    if (!hasDeposit) return sliced;
    return sliced.map((v) => (depositNum as number) * (v / values[0]));
  }, [sliced, hasDeposit, depositNum, values]);

  // overlay 對齊窗（issue log I-19）：`sliced` 每個位置對應的日期，供從
  // `/api/public/benchmarks` 查表取收盤價；rebase 錨點＝裁切窗內首日。
  const slicedDates = useMemo(() => datesForSlice(sliced, endDate), [sliced, endDate]);
  const overlayValuesByKey = useMemo(() => {
    const out: Record<(typeof OVERLAY_KEYS)[number], (number | null)[] | null> = {
      btc: null, eth: null, sp500: null, gold: null,
    };
    if (overlayData == null || dollarSliced.length === 0) return out;
    for (const key of OVERLAY_KEYS) {
      const table = closeByDate(overlayData.series[key]);
      if (table == null) continue;   // 該標的上游查詢失敗（null）→ 維持 null
      const closes = slicedDates.map((d) => (d != null && table[d] != null ? table[d] : null));
      out[key] = rebaseCloses(dollarSliced[0], closes);
    }
    return out;
  }, [overlayData, slicedDates, dollarSliced]);

  const yInputValues = useMemo(() => {
    const vals = [...dollarSliced];
    OVERLAY_KEYS.forEach((key, i) => {
      if (!overlayChecked[i]) return;
      const ov = overlayValuesByKey[key];
      if (ov == null) return;
      for (const v of ov) if (v != null) vals.push(v);
    });
    return vals;
  }, [dollarSliced, overlayChecked, overlayValuesByKey]);

  const yRange = useMemo(() => {
    if (yInputValues.length === 0) return { min: 0, max: 0 };
    return { min: Math.min(...yInputValues), max: Math.max(...yInputValues) };
  }, [yInputValues]);

  const yTickValues = useMemo(() => yTicks(yInputValues, Y_TICK_COUNT), [yInputValues]);
  const points = useMemo(() => toPoints(dollarSliced, yRange), [dollarSliced, yRange]);
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

      <div className="equity-curve-overlay-row" aria-busy={overlayLoading}>
        <span className="equity-overlay-label">{c.overlayLabel}</span>
        {c.overlays.map((label, i) => {
          const key = OVERLAY_KEYS[i];
          // 未抓過資料前（`overlayData === null`）不代表「不可用」——只有抓過
          // 之後該標的仍是 `null` 才是真的不可用（見 `lib/publicApi.ts` 的
          // null/[] 語意區分）。
          const unavailable = overlayData != null && overlayData.series[key] == null;
          const disabled = overlayLoading || unavailable;
          return (
            <label
              key={label}
              className="equity-overlay-item"
              title={unavailable ? c.overlayUnavailable : overlayLoading ? c.overlayLoading : undefined}
            >
              <input
                type="checkbox"
                aria-label={label}
                disabled={disabled}
                checked={overlayChecked[i] && !unavailable}
                onChange={() => toggleOverlay(i)}
              />
              <span
                className="equity-overlay-swatch"
                style={{ background: OVERLAY_COLORS[i % OVERLAY_COLORS.length] }}
                aria-hidden="true"
              />
              {label}
            </label>
          );
        })}
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
              {OVERLAY_KEYS.map((key, i) => {
                if (!overlayChecked[i]) return null;
                const ov = overlayValuesByKey[key];
                if (ov == null) return null;
                // 細線、低飽和，不搶主線（issue log I-19 沿設計稿 NOTE 09 精神）。
                return (
                  <polyline
                    key={key}
                    points={toOverlayPoints(ov, dollarSliced.length, yRange)}
                    fill="none"
                    stroke={OVERLAY_COLORS[i % OVERLAY_COLORS.length]}
                    strokeWidth="1"
                    opacity="0.7"
                  />
                );
              })}
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
