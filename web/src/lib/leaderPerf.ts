/**
 * leader 績效的「這個窗到底能顯示什麼」判定——顯示層的**單一判定點**。
 *
 * ⭐⭐ 本模組存在的唯一理由：後端的分級揭露用**鍵存不存在**承載
 * （filet/leader_perf.py；publicapi/app.py 的 `_leader_perf_public` 用 `if k in row`
 * 投影而不是 `.get()`，就是為了不把缺席的鍵補成 null）。缺鍵的意思是
 * **「不該顯示」，不是「顯示為空」**。把這個判定散在 JSX 各處，等於要求每個
 * 寫畫面的人都記得問一次「這個鍵在不在」——而那正是後端用結構換掉的東西。
 * 所以：JSX 只讀 `view.shown` 上**存在的**鍵，判定全部收斂在這裡。
 *
 * 判定規則是**兩個條件的交集**，兩個方向都要擋：
 *   1. `disclosure_tier` 授權到哪一級（後端說「這段資料誠實可顯示到什麼程度」）。
 *   2. 鍵是否真的存在且是合法數字字串。
 * 只滿足其一都不顯示——
 *   - 鍵在但 tier 不許（例如 tier=pnl_only 卻夾帶 twr）→ 不顯示：tier 才是揭露決策。
 *   - tier 許可但鍵不在（schema 漂移／舊快照）→ 不顯示：不能無中生有。
 * 第二種情況會把 `degraded` 設為 true：靜默降級會讓後端的資料缺漏在畫面上
 * 完全看不出來（工程原則 3：失敗要出聲，不要 log 完就吞掉）。
 *
 * ⚠️ 型別註記（實測後端，非推測）：Decimal 欄位序列化後是 **string**
 * （`jsonable_performance`：`str(v) if isinstance(v, Decimal)`），
 * 所以 twr／max_drawdown／annualized_return／cum_pnl／covered_days 都是字串。
 */
import type { LeaderPerfNotes, LeaderPerfWindow } from "./api";

/** 後端 `leader_perf` 的四個揭露層級（TIER_* 常數，改一邊要改兩邊）。 */
export const TIERS = ["insufficient", "pnl_only", "window_return", "annualizable"] as const;
export type PerfTier = (typeof TIERS)[number];

/**
 * 天數門檻。⭐ 與 filet/leader_perf.py 的 `MIN_DAYS_FOR_RETURN`／
 * `MIN_DAYS_FOR_ANNUALIZATION` 同源（改一邊要改兩邊）。前端只拿它算「還差幾天」
 * 這種**說明文字**，不用它決定顯示什麼——顯示與否一律由後端的 tier 決定，
 * 否則兩邊門檻一旦漂移，畫面就會顯示後端不打算給的東西。
 */
export const MIN_DAYS_FOR_RETURN = 30;
export const MIN_DAYS_FOR_ANNUALIZATION = 90;

/** tier → 授權等級。未知 tier → 0（fail closed：不認得就什麼都不給）。 */
const TIER_RANK: Record<string, number> = {
  insufficient: 0, pnl_only: 1, window_return: 2, annualizable: 3,
};

/** 實際可顯示到的層級（＝後端授權 ∩ 資料具備）。 */
export type PerfLevel = "none" | "pnl" | "window" | "annualized";

/**
 * 可顯示的數值。⭐ 欄位名刻意與後端原名一字不差（`cum_pnl` 而非 `cumPnl`）：
 * lib/redline.test.ts 擋的是這四個**識別字**後面接 `??`／`||`，沿用原名等於讓
 * 那道結構性防線一路涵蓋到顯示層。⭐ 不可顯示的欄位是**鍵不存在**，不是 null——
 * 與後端同一種語意，也讓 `"twr" in shown` 這種二元判斷在前端一樣成立。
 */
export interface PerfShown {
  cum_pnl?: string;
  twr?: string;
  max_drawdown?: string;
  annualized_return?: string;
}

export interface LeaderPerfView {
  period: string;
  /** 後端宣稱的層級；不在已知四級內 → `"unknown"`（當作 insufficient 處理）。 */
  tier: PerfTier | "unknown";
  status: string;
  /** 資料不足的機器可讀原因碼（`ok` 時為 null）。 */
  reason: string | null;
  level: PerfLevel;
  /** 後端宣稱的層級 > 資料實際具備 → true。顯示層必須說出來，不得靜默降級。 */
  degraded: boolean;
  sampleCount: number | null;
  /** 涵蓋天數（數值化，供「還差幾天」計算與顯示）。 */
  coveredDays: number | null;
  /** 涵蓋天數原始字串（不失真，供 title 屬性）。 */
  coveredDaysRaw: string | null;
  skippedIntervals: number | null;
  shown: PerfShown;
}

/** 合法的 Decimal 字串？空字串／空白／非有限數 → false（不可顯示）。 */
function isDecimalString(v: unknown): v is string {
  return typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v));
}

function toFiniteNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * 一個績效窗 → 顯示判定。`undefined`／形狀不對 → level `none`（不顯示任何數字）。
 *
 * ⭐ 刻意接受 `unknown`：這份資料來自網路，型別宣告只是宣告。形狀不符時
 * 回一個「什麼都不顯示」的 view，而不是讓 JSX 去讀 undefined 的屬性。
 */
export function perfView(raw: unknown): LeaderPerfView {
  const w = (raw && typeof raw === "object" ? raw : {}) as Partial<LeaderPerfWindow>;
  const tierRaw = typeof w.disclosure_tier === "string" ? w.disclosure_tier : "";
  const tier: PerfTier | "unknown" =
    (TIERS as readonly string[]).includes(tierRaw) ? (tierRaw as PerfTier) : "unknown";
  const rank = TIER_RANK[tierRaw] ?? 0;

  const shown: PerfShown = {};
  let level: PerfLevel = "none";
  let degraded = false;

  // --- 由低往高逐級開放；任一級的資料不齊就停在前一級（且記 degraded）。 ---
  if (rank >= 1) {
    if (isDecimalString(w.cum_pnl)) {
      shown.cum_pnl = w.cum_pnl;
      level = "pnl";
    } else {
      degraded = true;
    }
  }
  // twr 與 max_drawdown 一起給或一起不給：只有其中一個時，畫面會變成
  // 「有報酬率、沒有回撤」——那是最容易被讀成「這個 leader 沒有回撤」的形狀。
  if (rank >= 2 && level === "pnl") {
    if (isDecimalString(w.twr) && isDecimalString(w.max_drawdown)) {
      shown.twr = w.twr;
      shown.max_drawdown = w.max_drawdown;
      level = "window";
    } else {
      degraded = true;
    }
  } else if (rank >= 2) {
    degraded = true;
  }
  if (rank >= 3 && level === "window") {
    if (isDecimalString(w.annualized_return)) {
      shown.annualized_return = w.annualized_return;
      level = "annualized";
    } else {
      degraded = true;
    }
  } else if (rank >= 3) {
    degraded = true;
  }

  const coveredRaw = isDecimalString(w.covered_days) ? w.covered_days : null;
  return {
    period: typeof w.period === "string" ? w.period : "",
    tier,
    status: typeof w.status === "string" ? w.status : "",
    reason: typeof w.reason === "string" ? w.reason : null,
    level,
    degraded,
    sampleCount: toFiniteNumber(w.sample_count),
    coveredDays: coveredRaw === null ? null : Number(coveredRaw),
    coveredDaysRaw: coveredRaw,
    skippedIntervals: toFiniteNumber(w.skipped_intervals),
    shown,
  };
}

/**
 * 距門檻還差幾天（無條件進位到 0.1 天；已達門檻或天數未知 → null）。
 * ⭐ 「還差多少天」是把「這格為什麼空著」變成一個**有內容的狀態**的關鍵：
 * 「尚無年化」是空格，「涵蓋 45.2 天，距 90 天門檻還差 44.8 天」是狀態。
 */
export function daysShortOf(coveredDays: number | null, threshold: number): number | null {
  if (coveredDays === null || coveredDays >= threshold) return null;
  return Math.round((threshold - coveredDays) * 10) / 10;
}

/** 天數顯示（一位小數）。 */
export function fmtDays(days: number): string {
  return days.toFixed(1);
}

/** 解析後的揭露警語：一定有值（缺席由前端等義文案遞補）。 */
export interface PerfNotesResolved {
  basis: string;
  upperBound: string;
  maxDrawdown: string;
  sufficiency: string;
}

/**
 * 空白即缺席。⭐ 刻意**不用** `??`：`??` 只擋 null／undefined，擋不掉空字串——
 * 後端送來一個 `""`，`??` 會照單全收，於是數字旁邊留下一則**空的**警語，
 * 畫面上等同於沒有警語，而程式碼看起來完全正常。警語的缺席必須用「有沒有字」判定，
 * 不是「是不是 null」。（這個洞是 redline.test.ts 的缺鍵防線先攔下來的。）
 */
function textOr(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() !== "" ? value : fallback;
}

/**
 * 揭露警語：後端 `performance_notes` 原文優先（單一來源：計算的極限與呈現的警語
 * 必須出自同一處），缺席或空白才用前端等義文案遞補。
 *
 * ⭐⭐ 警語與數字的規則**相反**，這是本函式存在的理由：
 *   - **數字**缺了就不顯示——補一個預設值等於憑空造出後端不打算給的資訊；
 *   - **警語**缺了要補上——少一句警語等於把數字送出去而沒有它的極限說明。
 * 兩者都叫「缺了」，處理方式相反，所以分兩個函式、不共用一套「填預設值」的直覺。
 */
export function resolvePerfNotes(
  notes: LeaderPerfNotes | undefined,
  fallbacks: PerfNotesResolved,
): PerfNotesResolved {
  return {
    basis: textOr(notes?.basis, fallbacks.basis),
    upperBound: textOr(notes?.upper_bound, fallbacks.upperBound),
    maxDrawdown: textOr(notes?.max_drawdown, fallbacks.maxDrawdown),
    sufficiency: textOr(notes?.sufficiency, fallbacks.sufficiency),
  };
}
