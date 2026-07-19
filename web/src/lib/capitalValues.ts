/**
 * lib/capitalValues.ts — 資金設定兩個數值的 **canonical 形式**（前端這一側的唯一定義）。
 *
 * ⭐ 為什麼前端也需要一份 canonical 化：送出前要比對「伺服器回傳的待簽設定值」與
 * 「使用者實際調的值」，而 `1000`、`1000.0`、`1000.00` 是同一個數卻是三個不同字串。
 * 兩側各自決定怎麼印，比對就會在**正常情況下**誤報不符，把一道安全檢查變成一個
 * 每次都擋人的 bug（工程原則 1：被比較的兩個值必須同源、同基準、同處計算）。
 * 規則寫成一份、兩側都走它，才談得上比對。
 *
 * 規則鏡射自 src/spark/filet/capital_settings.py：
 *   - `CAPITAL_DECIMALS = 2`、`UTILIZATION_DECIMALS = 4`（固定位數，不足補零）。
 *   - 小數位超過上限一律**拒絕**，不 quantize——靜默截斷等於在使用者不知情的情況下
 *     改掉他要簽署的數字，而這個數字直接乘進部位大小（sizing.compute_scale_factor）。
 *   - 政策邊界 `allocated_capital > 0`、`capital_utilization ∈ (0, 1]`
 *     （validate_capital_bounds），前端**阻擋**而不是夾取後照送。
 *
 * 後端改了那幾個常數而這裡沒跟著改 → 比對必定失敗 → 送不出去。方向是安全的那一邊
 * （fail closed），但它會壞在使用者面前，所以兩邊要一起改。
 *
 * ⚠️ 全程字串／整數運算，**不經過 Number**：0.1 在 float 裡不是 0.1，而這兩個值要拿去
 * 和伺服器的字串逐字比對，也直接乘進部位大小。
 */

/** 鏡射後端 CAPITAL_DECIMALS。 */
export const CAPITAL_DECIMALS = 2;
/** 鏡射後端 UTILIZATION_DECIMALS。 */
export const UTILIZATION_DECIMALS = 4;

/**
 * 本金整數部位數上限。鏡射後端的 `_MAX_ABS = 1e15`（格式健全性檢查，不是政策邊界）：
 * 15 位以內的整數必定 < 1e15，一定過得了後端那一關。這裡只是把「多打了幾位數」
 * 提早變成看得懂的訊息，最終權威仍是後端（超了它會回 400）。
 */
const MAX_INT_DIGITS = 15;

/** 使用比例滑桿的兩端（整數百分比）。0% 不在後端允許的區間內，所以最小值是 1%。 */
export const MIN_UTILIZATION_PCT = 1;
export const MAX_UTILIZATION_PCT = 100;

export type CapitalParseFailure =
  | "empty"
  | "format"
  /** 小數位超過上限——**拒絕而非四捨五入**（見檔頭）。 */
  | "decimals"
  | "not_positive"
  | "too_large";

export type CapitalParseResult =
  | { ok: true; value: string }
  | { ok: false; reason: CapitalParseFailure };

/**
 * 使用者輸入的投入本金 → canonical 字串（固定 2 位小數）。
 *
 * 刻意**不接受**千分位逗號、單位、正負號與科學記號：`Decimal("1e100000")` 是合法輸入，
 * 印進待簽訊息會變成一個十萬位數的字串。這是格式層該擋的東西（同後端 `_canonical_decimal`
 * 的理由），不該讓它一路走到簽章運算。
 */
export function canonicalAllocatedCapital(raw: string): CapitalParseResult {
  const s = raw.trim();
  if (s === "") return { ok: false, reason: "empty" };

  const m = /^(\d+)(?:\.(\d*))?$/.exec(s);
  if (!m) return { ok: false, reason: "format" };

  const frac = m[2] ?? "";
  // ⭐ 超過上限就拒絕，不進位也不截斷：「送 1000.005 的人得到 1000.01」是一次
  // 使用者不知情的曝險變更，而且方向還不確定。
  if (frac.length > CAPITAL_DECIMALS) return { ok: false, reason: "decimals" };

  const int = m[1].replace(/^0+(?=\d)/, "");
  if (int.length > MAX_INT_DIGITS) return { ok: false, reason: "too_large" };

  const value = `${int}.${frac.padEnd(CAPITAL_DECIMALS, "0")}`;
  // 政策邊界（鏡射 validate_capital_bounds 的 `allocated_capital > 0`）：
  // 一個非零數字都沒有就是 0。字串判定，不繞 Number。
  if (!/[1-9]/.test(value)) return { ok: false, reason: "not_positive" };
  return { ok: true, value };
}

/**
 * 使用比例：以**整數百分比**（1..100）為輸入 → canonical 字串（固定 4 位小數）。
 *
 * ⭐ 刻意不收 number 型的比例值（0.07）：float 的 0.07 不是 0.07，格式化到第 4 位時的
 * 進位方向不可預期，而這個字串要拿去和伺服器的字串**逐字**比對。從整數百分比用整數
 * 運算組字串，就沒有浮點介入的餘地。
 *
 * 超出 [1, 100] 或非整數 → `null`（呼叫端據此阻擋送出，不夾取）。滑桿本身的
 * min/max 已經擋住一輪，這裡是第二道：值域不是靠控制項的屬性成立的。
 */
export function canonicalUtilizationPct(pct: number): string | null {
  if (!Number.isInteger(pct)) return null;
  if (pct < MIN_UTILIZATION_PCT || pct > MAX_UTILIZATION_PCT) return null;
  const bp = pct * 100; // 萬分之一為單位的整數（100..10000）
  const int = Math.floor(bp / 10000);
  const frac = String(bp % 10000).padStart(UTILIZATION_DECIMALS, "0");
  return `${int}.${frac}`;
}

/** 一組已 canonical 化、可以拿去比對的設定值。 */
export interface CapitalValues {
  allocated_capital: string;
  capital_utilization: string;
}

/**
 * 伺服器待簽原文中，載有這兩個設定值的那兩行（版型鏡射
 * `filet/capital_settings.py::build_capital_settings_message`）。
 *
 * ⭐ 為什麼是**整行相等**而不是 `includes`：數值的子字串比對會被包含關係騙過去——
 * `"11000.00".includes("1000.00")` 為真，也就是說攻擊者把本金放大十倍，子字串比對
 * 會照樣放行。位址（leaderSelectFlow）沒有這個問題是因為位址等長，數值有。
 *
 * 代價明講：後端改版型而這裡沒跟著改 → 每一次送出都會被自己的預驗擋下。這是
 * fail closed 的方向（不會放行錯的值），但它會壞在使用者面前，所以是同改的兩處。
 */
export function capitalMessageLines(v: CapitalValues): string[] {
  return [
    `Allocated Capital: ${v.allocated_capital} USDC`,
    `Capital Utilization: ${v.capital_utilization}`,
  ];
}
