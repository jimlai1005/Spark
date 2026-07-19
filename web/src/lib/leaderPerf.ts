/**
 * leader 績效的「這個窗到底能顯示什麼」判定——顯示層的**單一判定點**。
 *
 * ⭐⭐ 2026-07-19 揭露模型改版，本模組的職責跟著**反過來**了：
 *
 * **舊模型**：後端用「鍵存不存在」承載分級揭露，缺鍵＝不該顯示。本模組的工作是
 * 擋住「前端憑空造出後端不打算給的數字」。
 *
 * **新模型**：資料不足時後端**照樣給數字**，只是附上 `*_insufficient_data` 標記。
 * 於是最危險的失敗方向換了一邊——不再是造出數字，而是**把數字送出去而沒有它的
 * 警示**。一個 7 天 +3% 的 leader 年化算出來是 365%：數字本身完全「正常」，
 * 少掉標記則畫面上一點都看不出它是把 7 天的雜訊複利放大 52 倍的結果。
 *
 * 所以新的判定規則是**成對到齊**（fail closed）：
 *   - 數字有、標記沒有 → **不顯示數字** ＋ `degraded`。我們無法陳述這個數字的
 *     基礎，就不該把它送出去；沉默地當它「資料充足」是本模組要防的頭號錯誤。
 *   - 標記有、數字沒有 → 不顯示（**絕不單獨顯示標記**）＋ `degraded`。
 *   - 兩個都沒有 → 這一級乾淨地缺席（例如年化在數學上無定義），**不算** degraded：
 *     那是後端的合法狀態，不是漂移。
 *   - `twr`／`max_drawdown` 仍然一起給或一起不給：只有其中一個時，畫面會變成
 *     「有報酬率、沒有回撤」——最容易被讀成「這個 leader 沒有回撤」的形狀。
 *
 * ⚠️ `disclosure_tier` **不再**是顯示授權（改版後它是 `covered_days` 的純函式，
 * 與鍵的存在與否無關）。它只剩兩個用途：(1) 畫面上的層級標籤；(2) rank 0
 * （`insufficient`／無法判讀）代表後端說「這個窗根本沒有可用資料」→ 什麼都不顯示。
 * 拿 tier 去擋 rank 2／3 會讓薄資料的數字被藏起來，那正是本次改版要停止的行為。
 *
 * ⚠️ 型別註記（實測後端，非推測）：Decimal 欄位序列化後是 **string**
 * （`jsonable_performance`：`str(v) if isinstance(v, Decimal)`），
 * 所以 twr／max_drawdown／annualized_return／cum_pnl／covered_days／
 * annualized_return_extrapolated_from_days 都是字串；三個 `*_insufficient_data`
 * 是**原生 bool**（後端刻意不轉字串：`"False"` 的真值是 true）。
 */
import type { LeaderPerfNotes, LeaderPerfWindow } from "./api";

/** 後端 `leader_perf` 的四個揭露層級（TIER_* 常數，改一邊要改兩邊）。 */
export const TIERS = ["insufficient", "pnl_only", "window_return", "annualizable"] as const;
export type PerfTier = (typeof TIERS)[number];

/**
 * 天數門檻。⭐ 與 filet/leader_perf.py 的 `MIN_DAYS_FOR_RETURN`／
 * `MIN_DAYS_FOR_ANNUALIZATION` 同源（改一邊要改兩邊）。前端只拿它算「還差幾天」
 * 這種**說明文字**，不用它決定顯示什麼——顯示與否一律看後端送來的數字與標記是否
 * 成對到齊。前端拿自己的門檻去重算「這筆資料到底夠不夠」，等於在後端之外開第二個
 * 判定來源，兩邊一旦漂移就會出現「標記說不足、畫面卻沒警語」這種無聲的矛盾。
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
 * 可顯示的數值 ＋ **與它們同生共死的標記**。
 *
 * ⭐ 欄位名刻意與後端原名一字不差（`cum_pnl` 而非 `cumPnl`、
 * `twr_insufficient_data` 而非 `twrInsufficient`）：`lib/redline.test.ts` 的兩條
 * 結構性斷言擋的是這些**識別字**本身，沿用原名等於讓那道防線從 api.ts 一路
 * 涵蓋到 JSX。改成駝峰命名會讓紅線在顯示層失效，而且失效得無聲無息。
 *
 * ⭐⭐ 標記與數字**同時存在或同時不存在**——這是本介面唯一重要的不變式，由
 * `perfView()` 維持（見檔頭「成對到齊」）。它讓 JSX 可以直接
 * `{s.twr_insufficient_data && <警示/>}`：標記在，就一定是個 bool，不會有
 * 「undefined 被當成 false 於是靜默宣告資料充足」的路徑。
 */
export interface PerfShown {
  cum_pnl?: string;
  twr?: string;
  twr_insufficient_data?: boolean;
  max_drawdown?: string;
  max_drawdown_insufficient_data?: boolean;
  annualized_return?: string;
  annualized_return_insufficient_data?: boolean;
  annualized_return_extrapolated_from_days?: string;
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
 * 標記必須是**原生 boolean**。
 *
 * ⭐⭐ 刻意寫成型別檢查而不是 `Boolean(v)`／`v ?? false`／`!!v`：後三者會把
 * `undefined`（標記缺席）壓成 `false`，而 `false` 在這裡的意思是**「資料充足」**——
 * 那是一句我們沒有根據的斷言，也是本次改版最危險的失敗方向。標記缺席不是
 * 「不足為 false」，是「我們不知道」，而不知道的時候不准替後端說話。
 * （`lib/redline.test.ts` 的紅線 (a) 就在擋 `?? false` 這類寫法。）
 */
function isBooleanMarker(v: unknown): v is boolean {
  return typeof v === "boolean";
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

  // rank 0（tier=insufficient 或無法判讀）＝ 後端說「本窗沒有可用資料」。這是 tier
  // 唯一還保留的擋門作用；rank 1/2/3 之間**不再**分級擋數字（見檔頭）。
  if (rank >= 1) {
    if (isDecimalString(w.cum_pnl)) {
      shown.cum_pnl = w.cum_pnl;
      level = "pnl";
    } else {
      degraded = true;
    }

    // --- 窗口報酬與回撤：兩個數字 ＋ 兩個標記，四者到齊才顯示 ---
    // ⭐ 標記與數字綁在同一個判斷式裡，而不是「先決定顯示數字、之後再找標記」：
    // 後者一旦標記缺席，最自然的補救寫法就是給個預設值，而那正是要防的事。
    const twrPair = isDecimalString(w.twr) && isBooleanMarker(w.twr_insufficient_data);
    const mddPair = isDecimalString(w.max_drawdown)
      && isBooleanMarker(w.max_drawdown_insufficient_data);
    const windowAnyPresent = w.twr !== undefined || w.max_drawdown !== undefined
      || w.twr_insufficient_data !== undefined
      || w.max_drawdown_insufficient_data !== undefined;
    if (twrPair && mddPair && level === "pnl") {
      shown.twr = w.twr;
      shown.twr_insufficient_data = w.twr_insufficient_data;
      shown.max_drawdown = w.max_drawdown;
      shown.max_drawdown_insufficient_data = w.max_drawdown_insufficient_data;
      level = "window";
    } else if (windowAnyPresent) {
      // 有東西但不成對（只有數字沒標記／只有標記沒數字／只有 twr 沒 MDD）→ 出聲。
      degraded = true;
    }

    // --- 年化：數字 ＋ 不足標記 ＋ 外推天數，三者到齊才顯示 ---
    // ⭐ `annualized_return_extrapolated_from_days` 也是**必要**條件，不是裝飾：
    // 沒有它就寫不出「由 N 天外推」，而一個沒有標明外推基礎的年化數字，正是這次
    // 改版最想避免的東西。缺了寧可整格不畫（後端令三鍵同生共死）。
    const annualPair = isDecimalString(w.annualized_return)
      && isBooleanMarker(w.annualized_return_insufficient_data)
      && isDecimalString(w.annualized_return_extrapolated_from_days);
    const annualAnyPresent = w.annualized_return !== undefined
      || w.annualized_return_insufficient_data !== undefined
      || w.annualized_return_extrapolated_from_days !== undefined;
    if (annualPair && level === "window") {
      shown.annualized_return = w.annualized_return;
      shown.annualized_return_insufficient_data = w.annualized_return_insufficient_data;
      shown.annualized_return_extrapolated_from_days =
        w.annualized_return_extrapolated_from_days;
      level = "annualized";
    } else if (annualAnyPresent) {
      degraded = true;
    }
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
