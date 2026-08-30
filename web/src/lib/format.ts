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

/**
 * bp（basis point）字串 → 顯示字串，2 位小數＋`bp` 後綴。
 * 後端可能給全精度字串（如 `36.25872425025574226880295914`）——不截斷會撐爆卡片。
 * null／非有限值 → NO_VALUE（不臆造）。
 */
export function fmtBp(v: string | null | undefined): string {
  if (v == null || v === "") return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  return `${n.toFixed(2)}bp`;
}

/** 比例（0.25）→ 百分比顯示（25.0%）。null／非數字 → NO_VALUE。 */
export function fmtRatioPct(v: string | null | undefined, dp = 1): string {
  if (v == null || v === "") return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  return `${(n * 100).toFixed(dp)}%`;
}

/**
 * epoch 秒 → `YYYY-MM-DD HH:mm UTC`（策略／交易員詳情頁「資料截至」列，
 * M3 round2 Task 6 從 `strategies/[slug]/page.tsx` 抽出，供 `/traders/[address]`
 * 共用同一份格式，避免兩處各自維護一份時間字串格式）。
 * `0`／`NaN` → NO_VALUE（沒有時間戳，不臆造）。
 */
export function fmtUpdatedAtUtc(epochSeconds: number): string {
  if (!epochSeconds) return NO_VALUE;
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/**
 * USD 金額 → 縮寫顯示（首頁證據列，設計稿 §03 錨例 $4.28M）。
 * ≥ 1,000,000 → `$X.XXM`；≥ 1,000 → `$X.XXK`；否則 `$X.XX`。
 * null／空／非數字 → NO_VALUE（不得寫死假數字，見不變量 0.3.6）。
 */
export function fmtUsdCompact(v: string | null | undefined): string {
  if (v == null || v === "") return NO_VALUE;
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(2)}K`;
  return `${sign}$${abs.toFixed(2)}`;
}
