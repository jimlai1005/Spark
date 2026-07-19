/** 顯示層工具。位址縮寫照 v1 原型格式：0x5579…B5d（前 6 字元 + 省略號 + 後 3）。 */
export function shortAddr(addr: string): string {
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-3)}`;
}

/** 無值佔位符。⭐ 缺值一律顯示這個而非 0——0 是「有查到且為零」，兩者在對帳上意義相反。 */
export const NO_VALUE = "—";

/**
 * 金額字串（後端 Decimal 序列化結果）→ 顯示字串。
 * null／空／非數字 → NO_VALUE（不當 0）。小額（|v| < 1）保留 4 位小數，
 * 因為 builder fee 常落在小數點後三、四位，2 位會把它顯示成 0.00。
 * ⚠️ 只用於顯示：所有比較與門檻判定都在後端以 Decimal 完成，前端不做金額算術。
 */
export function fmtAmount(v: string | null | undefined, dp?: number): string {
  if (v == null || v === "") return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  // 對帳欄位須指定 dp（例如 4）：把應收／實收各自四捨五入到 2 位，會讓小於 0.01
  // 的真實差額在畫面上憑空消失，正是這頁要抓的訊號。
  const places = dp ?? (Math.abs(n) < 1 && n !== 0 ? 4 : 2);
  return n.toLocaleString("en-US", { minimumFractionDigits: places, maximumFractionDigits: places });
}

/** 比例（0.25）→ 百分比顯示（25.0%）。null／非數字 → NO_VALUE。 */
export function fmtRatioPct(v: string | null | undefined, dp = 1): string {
  if (v == null || v === "") return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  return `${(n * 100).toFixed(dp)}%`;
}
