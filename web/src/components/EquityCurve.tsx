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
 * x 軸日期標籤（**舊行為**，只在無可用 `timestamps` 時使用；有時間戳走 `xLabelsFromTs`）：
 * 有 `startDate`/`endDate` 時，依「一點 ≈ 一天」假設從 `endDate`
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

/** 依 period 從序列尾端裁切——**舊行為**（無時間戳時沿用）：假設「一點 ≈ 一天」，
 * 「30D/7D」直接取最後 N 點。⚠️ HL `allTime` 序列實為約每 1–2 週一點，此假設不成立；
 * 2026-11-17（plan perf-followups E1）起後端供給 `equity_index_ts`，有時間戳時改走
 * `sliceStartByTime`，本函式只服務舊回應相容。 */
function sliceByPeriod(values: number[], period: Period): number[] {
  if (period === "all") return values;
  const n = period === "30d" ? 30 : 7;
  return values.slice(Math.max(0, values.length - n));
}

const PERIOD_DAYS: Record<Exclude<Period, "all">, number> = { "30d": 30, "7d": 7 };

/** 有時間戳時的期間切片起點（含）：`ts >= lastTs − N×86400000` 的第一個索引；
 * `all` ＝ 0。`ts` 為空 → 0。 */
function sliceStartByTime(ts: number[], period: Period): number {
  if (period === "all" || ts.length === 0) return 0;
  const cutoff = ts[ts.length - 1] - PERIOD_DAYS[period] * 86400000;
  const i = ts.findIndex((t) => t >= cutoff);
  return i < 0 ? 0 : i;
}

/** 毫秒時間戳 → `YYYY-MM-DD`（UTC）。無法轉換 → `null`。 */
function msToIsoDate(ms: number): string | null {
  const d = new Date(ms);
  return Number.isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10);
}

/** 切片首末點相距超過此天數 → x 軸標籤改 `YYYY-MM`（跨年時 `MM-DD` 會看起來重複）。 */
const LONG_SPAN_DAYS = 300;

/** 有時間戳時的 x 軸標籤（plan perf-followups Task F2）：點依時間比例排列，刻度也**按時間等距**——
 * 刻度數 `k` 沿用 `pickTickIndices` 算出的數量，第 `j` 個刻度的時間＝
 * `ts_first + j/(k−1) × (ts_last − ts_first)`，標籤顯示**該時間**的 UTC 日期（不是最近資料點的
 * 日期）；`k == 1` 或 `ts_last == ts_first` 只顯示首點日期，不除以零。切片首末相距 > 300 天用
 * `YYYY-MM`，否則 `MM-DD`（B5）。`idx` 為刻度序號（React key 用）。匯出供單元測試。 */
export function xLabelsFromTs(slicedTs: number[]): { idx: number; label: string }[] {
  const n = slicedTs.length;
  if (n === 0) return [];
  const first = slicedTs[0];
  const span = slicedTs[n - 1] - first;
  const long = span > LONG_SPAN_DAYS * 86400000;
  const fmt = (ms: number, j: number) => {
    const iso = msToIsoDate(ms);
    if (!iso) return { idx: j, label: `#${j + 1}` };
    return { idx: j, label: long ? iso.slice(0, 7) : shortDate(iso) };
  };
  const k = pickTickIndices(n, X_TICK_TARGET).length;
  if (k <= 1 || !(span > 0)) return [fmt(first, 0)];
  return Array.from({ length: k }, (_, j) => fmt(Math.round(first + (j / (k - 1)) * span), j));
}

/**
 * 有時間戳時各點的 x 位置（0..1）：`(ts_i − ts_first) / (ts_last − ts_first)`——依時間比例
 * 排點（HL 全期序列早期稀疏、近期密集，等距會扭曲時間軸）。主曲線與對照線共用同一組。
 * `ts_last == ts_first`（含單點）不得除以零：退化為等距（單點為 0）。匯出供單元測試。
 */
export function xFractionsFromTs(ts: number[]): number[] {
  const n = ts.length;
  if (n === 0) return [];
  const span = ts[n - 1] - ts[0];
  if (!(span > 0)) return ts.map((_, i) => (n === 1 ? 0 : i / (n - 1)));
  return ts.map((t) => (t - ts[0]) / span);
}

/** values → SVG polyline 的 `points` 字串（等距 x，y 依 `range`——缺省時退回
 * 依自身 min/max 正規化，沿舊行為）。全平（min===max）時畫一條水平中線，
 * 避免除以零。`range` 讓主線與 overlay 疊加線共用同一個 y 軸座標系
 * （見 `EquityCurve` 內 `yRange` 的計算，兩者才畫得出可比較的相對位置）。 */
function toPoints(
  values: number[], range?: { min: number; max: number }, xs?: number[] | null,
): string {
  if (values.length === 0) return "";
  const min = range ? range.min : Math.min(...values);
  const max = range ? range.max : Math.max(...values);
  const span = max - min;
  const n = values.length;
  return values
    .map((v, i) => {
      const x = xs ? xs[i] * VIEW_W : n === 1 ? 0 : (i / (n - 1)) * VIEW_W;
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
function toOverlayPoints(
  values: (number | null)[], n: number, range: { min: number; max: number }, xs?: number[] | null,
): string {
  const span = range.max - range.min;
  const pts: string[] = [];
  values.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) return;
    const x = xs ? xs[i] * VIEW_W : n === 1 ? 0 : (i / (n - 1)) * VIEW_W;
    const y = span === 0 ? VIEW_H / 2 : VIEW_H - ((v - range.min) / span) * VIEW_H;
    pts.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  });
  return pts.join(" ");
}

/** `sliced`（裁切後主曲線）每個位置對應的 `YYYY-MM-DD` 日期（**舊行為**，無可用
 * `timestamps` 時使用；有時間戳直接用各點真實日期）——與 `xLabels`
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

/** 收盤價回溯上限（日曆日）：取樣點當日沒有收盤價（週末、假日）時，最多往前找幾天。 */
export const CLOSE_LOOKBACK_DAYS = 4;

/**
 * 有時間戳路徑的收盤價對齊（plan perf-followups §6）：對每個 `YYYY-MM-DD`，取
 * 「當日或之前最近一筆」收盤價，回溯上限 `CLOSE_LOOKBACK_DAYS` 個日曆日，超過 → `null`。
 * 序列為 `null`（上游查詢失敗）→ 回 `null`。匯出供單元測試。
 */
export function alignClosesOnOrBefore(
  series: [number, string][] | null, dates: (string | null)[],
): (number | null)[] | null {
  const table = closeByDate(series);
  if (table == null) return null;
  return dates.map((d) => {
    if (d == null) return null;
    const base = Date.parse(`${d}T00:00:00Z`);
    if (Number.isNaN(base)) return null;
    for (let k = 0; k <= CLOSE_LOOKBACK_DAYS; k++) {
      const v = table[new Date(base - k * 86400000).toISOString().slice(0, 10)];
      if (v != null) return v;
    }
    return null;
  });
}

/**
 * 有時間戳路徑的 rebase：錨點＝切片內**第一個**有效收盤價（有限且非零）的點 `k`，
 * `overlay[i] = main[k] × (closes[i] / closes[k])`——乘數是**錨點當天的主曲線值**（不是
 * 切片首點的值），使對照線在錨點那天與主曲線重合（plan perf-followups §7 W1，錨例 B6）。
 * 錨點之前的點為 `null`（對照線從有資料處開始畫）；全部都沒有有效收盤價 → 全 `null`。
 * 其餘缺格／非有限數 → `null`，不插值。錨點就是切片首點時等同舊式 `main[0] × c/anchor`。
 * `main` 為主曲線切片值（與 `closes` 逐點對應）。（舊路徑仍用 `rebaseCloses`。）
 */
export function rebaseFromFirstAvailable(
  main: number[], closes: (number | null)[],
): (number | null)[] {
  const anchorIdx = closes.findIndex((c) => c != null && Number.isFinite(c) && c !== 0);
  if (anchorIdx < 0) return closes.map(() => null);
  const anchor = closes[anchorIdx] as number;
  const mult = main[anchorIdx];
  if (mult == null || !Number.isFinite(mult)) return closes.map(() => null);
  return closes.map((c, i) => (
    i < anchorIdx || c == null || !Number.isFinite(c) ? null : mult * (c / anchor)
  ));
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
 * EquityCurve — 策略詳情頁的「投入本金加累計損益」曲線（Task 9，設計稿 §04；
 * 2026-11-16 plan strategy-return-hl-roi D4 起改 ROI 基準，不再是帳戶淨值曲線）。
 *
 * `equityIndex` 是後端 `build_equity_index` 給的 ROI 指數（`1 + 累計損益 ÷ D`，
 * 起點 = "1"），不是美元金額；有 `initialDepositUsd`（＝ROI 分母 D）時 y 軸刻度
 * 換算為 D × index ＝ 投入本金加累計損益。
 */
export function EquityCurve({ equityIndex, timestamps, initialDepositUsd, startDate, endDate }: {
  equityIndex: string[];
  /** 每個取樣點的毫秒時間戳（後端 `equity_index_ts`，與 `equityIndex` 等長）。
   * 存在且長度相符 → 期間切片、x 軸日期、對照線對齊全部改用真實時間；缺席或長度
   * 不符 → 維持舊行為（「一點 ≈ 一天」，相容舊回應），不拋例外。 */
  timestamps?: number[] | null;
  /** 有值時 y 軸刻度換算成美元金額（= 本值 × 首點比值 × 投入本金）；缺席則顯示
   * 原始 index 值。2026-11-16 起這個值是後端 `methodology.initial_deposit_usd`
   * ＝ROI 分母（起始淨值＋期間最大累計淨入金），不再是鏈上 ledger 入金加總。 */
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

  // values 與 ts 同步過濾（非有限值連同它的時間戳一起丟），保持逐點對應。
  const tsUsable = Array.isArray(timestamps) && timestamps.length === equityIndex.length
    && timestamps.every((t) => typeof t === "number" && Number.isFinite(t));
  const { values, ts } = useMemo(() => {
    const vals: number[] = [];
    const stamps: number[] = [];
    equityIndex.forEach((raw, i) => {
      const n = Number(raw);
      if (!Number.isFinite(n)) return;
      vals.push(n);
      if (tsUsable) stamps.push((timestamps as number[])[i]);
    });
    return { values: vals, ts: tsUsable ? stamps : null };
  }, [equityIndex, timestamps, tsUsable]);

  // E6：有時間戳時，30D／7D 切片內不足 2 點 → 該期間不可用（按鈕 disabled）；
  // 目前選中的期間若變成不可用，退回「全部」（衍生值，不另開 effect）。
  const unavailable = useMemo(() => {
    const out: Record<Period, boolean> = { all: false, "30d": false, "7d": false };
    if (ts == null) return out;
    for (const p of ["30d", "7d"] as const) {
      out[p] = ts.length - sliceStartByTime(ts, p) < 2;
    }
    return out;
  }, [ts]);
  const activePeriod: Period = unavailable[period] ? "all" : period;

  const sliceStart = useMemo(
    () => (ts == null ? 0 : sliceStartByTime(ts, activePeriod)),
    [ts, activePeriod],
  );
  const sliced = useMemo(
    () => (ts == null ? sliceByPeriod(values, activePeriod) : values.slice(sliceStart)),
    [ts, values, activePeriod, sliceStart],
  );
  const slicedTs = useMemo(() => (ts == null ? null : ts.slice(sliceStart)), [ts, sliceStart]);

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
  const slicedDates = useMemo(
    () => (slicedTs == null
      ? datesForSlice(sliced, endDate)
      : slicedTs.map((t) => msToIsoDate(t))),
    [slicedTs, sliced, endDate],
  );
  const overlayValuesByKey = useMemo(() => {
    const out: Record<(typeof OVERLAY_KEYS)[number], (number | null)[] | null> = {
      btc: null, eth: null, sp500: null, gold: null,
    };
    if (overlayData == null || dollarSliced.length === 0) return out;
    for (const key of OVERLAY_KEYS) {
      if (slicedTs != null) {
        // 有時間戳：當日或之前最近一筆（回溯 4 日）＋錨點取切片內第一個有收盤價的點。
        const closes = alignClosesOnOrBefore(overlayData.series[key], slicedDates);
        if (closes == null) continue;   // 該標的上游查詢失敗（null）→ 維持 null
        out[key] = rebaseFromFirstAvailable(dollarSliced, closes);
        continue;
      }
      const table = closeByDate(overlayData.series[key]);
      if (table == null) continue;   // 該標的上游查詢失敗（null）→ 維持 null
      const closes = slicedDates.map((d) => (d != null && table[d] != null ? table[d] : null));
      out[key] = rebaseCloses(dollarSliced[0], closes);
    }
    return out;
  }, [overlayData, slicedDates, slicedTs, dollarSliced]);

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
  // x 位置：有時間戳依時間比例、無則等距（舊路徑）；主曲線與對照線共用這一組。
  const xFractions = useMemo(
    () => (slicedTs == null ? null : xFractionsFromTs(slicedTs)),
    [slicedTs],
  );
  const points = useMemo(
    () => toPoints(dollarSliced, yRange, xFractions),
    [dollarSliced, yRange, xFractions],
  );
  const xTickLabels = useMemo(
    () => (slicedTs == null
      ? xLabels(sliced, values.length, startDate, endDate)
      : xLabelsFromTs(slicedTs)),
    [slicedTs, sliced, values.length, startDate, endDate],
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
          {periods.map((p) => {
            const off = unavailable[p.key];
            return (
              <button
                key={p.key}
                type="button"
                className="pill equity-period-btn"
                data-active={activePeriod === p.key}
                aria-pressed={activePeriod === p.key}
                disabled={off}
                title={off ? c.periodUnavailable : undefined}
                aria-label={off ? `${p.label} · ${c.periodUnavailable}` : undefined}
                onClick={() => setPeriod(p.key)}
              >
                {p.label}
              </button>
            );
          })}
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
                    points={toOverlayPoints(ov, dollarSliced.length, yRange, xFractions)}
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
