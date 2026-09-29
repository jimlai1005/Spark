import { NO_VALUE } from "@/lib/format";

/**
 * 2026-09-30（plan `leader-truth-and-liq-risk` 第二輪審查後修正 Task 11，S1）：
 * 「距強平」欄的顯示文字＋顏色 class 單一定義點——原本 `explore/page.tsx` 與
 * `traders/[address]/page.tsx` 各自拷貝一份同形邏輯，日後只改一邊會讓同一
 * leader 兩頁顏色不一致；收斂到這裡，兩頁改 import。
 *
 * 顏色分級（既有裁決）：`< LIQ_DANGER_PCT` danger、`< LIQ_WARN_PCT` warn，
 * 其餘（含門檻本身）不變色；已越線（負值）顯示為 `0.0%` 並套 danger
 * （不 clamp 原始數值，只 clamp 顯示文字）。門檻常數只在此檔定義。
 *
 * 文案中性（既有裁決二）：不放嚇人紅框／急迫文案，只用數字顏色。
 */
export const LIQ_DANGER_PCT = 15;
export const LIQ_WARN_PCT = 30;

export function liqDistanceDisplay(pct: number | null): { text: string; className: string } {
  if (pct == null) return { text: NO_VALUE, className: "" };
  if (pct < 0) return { text: "0.0%", className: "risk-num--danger" };
  if (pct < LIQ_DANGER_PCT) return { text: `${pct.toFixed(1)}%`, className: "risk-num--danger" };
  if (pct < LIQ_WARN_PCT) return { text: `${pct.toFixed(1)}%`, className: "risk-num--warn" };
  return { text: `${pct.toFixed(1)}%`, className: "" };
}
