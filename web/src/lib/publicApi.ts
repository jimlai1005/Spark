/**
 * lib/publicApi.ts — `/api/public/*` 的 fetch helpers（無需登入，Task 6 後端）。
 *
 * ⭐ 與 lib/api.ts 的 `request()` 刻意分開：`/api/public/*` 不需要 session cookie，
 * 且後端對這幾支端點承諾**永遠 200**（子來源失敗時整段降級為 null/unknown，見
 * publicapi/app.py 對應端點的檔頭註解），呼叫端因此不需要 lib/api.ts 的
 * ApiError 分類——直接 fetch，任何非預期失敗（網路、非 200、格式異常）一律
 * 折疊成呼叫端能安全顯示的保守值（fail-safe：讀不到就說讀不到，不偽裝成健康）。
 */

/** `/api/public/strategies` 單一策略卡的 `metrics` 子物件（見 `filet/strategies.py`
 * `build_metrics`）。每個公開指標都伴隨一個 `_insufficient` 布林旗標——`null` 值
 * 一律代表「這裡沒有看起來夠格的數字」，前端不得對 `null` 做任何算術或當 0 顯示。 */
export interface PublicStrategyMetrics {
  total_return_pct: string | null;
  total_return_pct_insufficient: boolean;
  max_drawdown_pct: string | null;
  max_drawdown_pct_insufficient: boolean;
  sharpe: string | null;
  sharpe_insufficient: boolean;
  sharpe_se: string | null;
  sharpe_se_insufficient: boolean;
  win_rate_pct: string | null;
  win_rate_pct_insufficient: boolean;
  annualized_vol_pct: string | null;
  annualized_vol_pct_insufficient: boolean;
  sortino: string | null;
  sortino_insufficient: boolean;
  best_day_pct: string | null;
  best_day_pct_insufficient: boolean;
  worst_day_pct: string | null;
  worst_day_pct_insufficient: boolean;
  sample_count: number;
}

export interface PublicStrategy {
  slug: string;
  name: string;
  tagline: string | null;
  /** 英文版一行文案（2026-08-30 新增）。缺席（`null`）＝白名單未填英文版——
   * 呼叫端一律用 `lib/format.ts` 的 `resolveTagline()` 決定顯示哪一個，
   * 不得自己重寫 fallback 邏輯（EN 模式殘留繁中修法，見該函式檔頭）。 */
  tagline_en: string | null;
  featured: boolean;
  leader_address: string;
  status: "running" | "paused";
  listable: boolean;
  live_days: number;
  follower_count: number | null;
  min_notional_usd: string | null;
  max_leverage: string | null;
  metrics: PublicStrategyMetrics;
  /** M3 round3 Task 3（D5 數字一致性）：perf 快照的快取時間戳，列表與詳情共用同一份
   * `_strategy_perf_with_as_of`，同一 60s 快取窗內兩端點的值相等。可能為 `null`
   * （上游查詢失敗）。標成 optional——`PublicStrategy` 也是列表項的型別，既有測試
   * fixture（`StrategyCard.test.tsx` 等，非本 task 範圍）未必帶這個欄位。 */
  as_of?: number | null;
}

export interface PublicStrategiesResp {
  strategies: PublicStrategy[];
  updated_at: number;
}

const EMPTY_STRATEGIES: PublicStrategiesResp = { strategies: [], updated_at: 0 };

const EMPTY_METRICS: PublicStrategyMetrics = {
  total_return_pct: null,
  total_return_pct_insufficient: true,
  max_drawdown_pct: null,
  max_drawdown_pct_insufficient: true,
  sharpe: null,
  sharpe_insufficient: true,
  sharpe_se: null,
  sharpe_se_insufficient: true,
  win_rate_pct: null,
  win_rate_pct_insufficient: true,
  annualized_vol_pct: null,
  annualized_vol_pct_insufficient: true,
  sortino: null,
  sortino_insufficient: true,
  best_day_pct: null,
  best_day_pct_insufficient: true,
  worst_day_pct: null,
  worst_day_pct_insufficient: true,
  sample_count: 0,
};

/** `/api/public/strategies/{slug}` 的 `methodology` 子物件（見 `filet/strategies.py`
 * `build_methodology`）。任何欄位都可能是 `null`（perf 不可用）——呼叫端一律走
 * null → `NO_VALUE` 的既有路徑。 */
export interface PublicStrategyMethodology {
  start_date: string | null;
  end_date: string | null;
  initial_deposit_usd: string | null;
  /** M3 round4 Task R4-2：`accountValueHistory` 首個非零值（前導 0 點跳過）。
   * 與 `end_equity_usd` 同源同一次 `hl.portfolio()` 回應。 */
  start_equity_usd: string | null;
  /** 同一份 `accountValueHistory` 的最後一點（不過濾，可能是 0）。 */
  end_equity_usd: string | null;
  sample_count: number | null;
  annualization_days: number;
  risk_free_rate: string;
  basis: string;
  updated_at: number;
}

const EMPTY_METHODOLOGY: PublicStrategyMethodology = {
  start_date: null,
  end_date: null,
  initial_deposit_usd: null,
  start_equity_usd: null,
  end_equity_usd: null,
  sample_count: null,
  annualization_days: 365,
  risk_free_rate: "0",
  basis: "perp",
  updated_at: 0,
};

export interface PublicStrategyDetail extends PublicStrategy {
  equity_index: string[];
  methodology: PublicStrategyMethodology;
  /** M3 round3 Task 3：`live_days` 的同一個值（結構性防呆用途，前端據此門檻
   * 決定是否摺疊次要指標——不得另外重算，見 `strategies.py` 檔頭）。 */
  sample_days: number;
  /** 目前恆為 30（`filet.strategies.CAGR_SAMPLE_THRESHOLD_DAYS`，
   * 2026-08-30 D15 裁決原 60 降為 30）。 */
  sample_threshold: number;
  /** `sample_days < sample_threshold` 時後端**整個不回傳這個鍵**——結構性防呆：
   * `null` 代表「鍵不存在或值非字串」，呼叫端一律用它判斷是否渲染 CagrCard，
   * 不得自己另外算門檻。 */
  cagr_pct: string | null;
}

function normalizeMetrics(v: unknown): PublicStrategyMetrics {
  if (v == null || typeof v !== "object") return EMPTY_METRICS;
  const m = v as Partial<PublicStrategyMetrics>;
  return {
    total_return_pct: m.total_return_pct ?? null,
    total_return_pct_insufficient: !!m.total_return_pct_insufficient,
    max_drawdown_pct: m.max_drawdown_pct ?? null,
    max_drawdown_pct_insufficient: !!m.max_drawdown_pct_insufficient,
    sharpe: m.sharpe ?? null,
    sharpe_insufficient: !!m.sharpe_insufficient,
    sharpe_se: m.sharpe_se ?? null,
    sharpe_se_insufficient: !!m.sharpe_se_insufficient,
    win_rate_pct: m.win_rate_pct ?? null,
    win_rate_pct_insufficient: !!m.win_rate_pct_insufficient,
    annualized_vol_pct: m.annualized_vol_pct ?? null,
    annualized_vol_pct_insufficient: !!m.annualized_vol_pct_insufficient,
    sortino: m.sortino ?? null,
    sortino_insufficient: !!m.sortino_insufficient,
    best_day_pct: m.best_day_pct ?? null,
    best_day_pct_insufficient: !!m.best_day_pct_insufficient,
    worst_day_pct: m.worst_day_pct ?? null,
    worst_day_pct_insufficient: !!m.worst_day_pct_insufficient,
    sample_count: typeof m.sample_count === "number" ? m.sample_count : 0,
  };
}

function normalizeMethodology(v: unknown): PublicStrategyMethodology {
  if (v == null || typeof v !== "object") return EMPTY_METHODOLOGY;
  const m = v as Partial<PublicStrategyMethodology>;
  return {
    start_date: m.start_date ?? null,
    end_date: m.end_date ?? null,
    initial_deposit_usd: m.initial_deposit_usd ?? null,
    start_equity_usd: m.start_equity_usd ?? null,
    end_equity_usd: m.end_equity_usd ?? null,
    sample_count: typeof m.sample_count === "number" ? m.sample_count : null,
    annualization_days: typeof m.annualization_days === "number" ? m.annualization_days : 365,
    risk_free_rate: typeof m.risk_free_rate === "string" ? m.risk_free_rate : "0",
    basis: typeof m.basis === "string" ? m.basis : "perp",
    updated_at: typeof m.updated_at === "number" ? m.updated_at : 0,
  };
}

export interface PublicStats {
  routed_volume_usd_total: string | null;
  builder_fee_bps: number | null;
  live_days: number | null;
  updated_at: number;
}

const UNKNOWN_STATS: PublicStats = {
  routed_volume_usd_total: null,
  builder_fee_bps: null,
  live_days: null,
  updated_at: 0,
};

export type PublicComponentStatus = "ok" | "degraded" | "unknown";

export interface PublicStatusComponent {
  name: string;
  status: PublicComponentStatus;
}

export interface PublicStatus {
  status: PublicComponentStatus;
  components: PublicStatusComponent[];
  updated_at: number;
}

const UNKNOWN_STATUS: PublicStatus = { status: "unknown", components: [], updated_at: 0 };

function isComponentStatus(v: unknown): v is PublicComponentStatus {
  return v === "ok" || v === "degraded" || v === "unknown";
}

/**
 * 讀取 `/api/public/status`。連線失敗、非 200、或回應格式不是預期的三態之一，
 * 一律降級為 `UNKNOWN_STATUS`——絕不把「讀不到」畫成 `"ok"`（工程原則 3 的
 * 前端鏡射：安全訊號讀不到時，寧可顯示保守值，不得偽裝成健康）。
 */
export async function getPublicStatus(): Promise<PublicStatus> {
  try {
    const res = await fetch("/api/public/status");
    if (!res.ok) return UNKNOWN_STATUS;
    const body = (await res.json()) as Partial<PublicStatus> | null;
    if (body == null || !isComponentStatus(body.status)) return UNKNOWN_STATUS;
    return {
      status: body.status,
      components: Array.isArray(body.components) ? body.components : [],
      updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
    };
  } catch {
    return UNKNOWN_STATUS;
  }
}

/**
 * 讀取 `/api/public/strategies`（首頁與策略列表頁）。連線失敗、非 200、或回應
 * 形狀不是陣列 → 降級為空清單（`EMPTY_STRATEGIES`）——**不得**在讀不到資料時
 * 顯示假策略卡；空清單讓呼叫端自然渲染「目前沒有策略」或乾脆略過該區塊。
 */
export async function getPublicStrategies(): Promise<PublicStrategiesResp> {
  try {
    const res = await fetch("/api/public/strategies");
    if (!res.ok) return EMPTY_STRATEGIES;
    const body = (await res.json()) as Partial<PublicStrategiesResp> | null;
    if (body == null || !Array.isArray(body.strategies)) return EMPTY_STRATEGIES;
    return {
      strategies: body.strategies,
      updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
    };
  } catch {
    return EMPTY_STRATEGIES;
  }
}

/**
 * 讀取 `/api/public/strategies/{slug}`（策略詳情頁，Task 9）。
 *
 * 回傳 `null` 代表「這頁沒有東西可畫」，呼叫端一律渲染 404 空態——不區分
 * 「後端明確 404」與「連線失敗／回應格式異常」：對使用者來說兩者都是
 * 「這個策略目前看不到」，且**不得**在讀不到資料時偽造一個看起來像有效的
 * 策略物件（工程原則 3 的前端鏡射）。
 */
export async function getPublicStrategy(slug: string): Promise<PublicStrategyDetail | null> {
  try {
    const res = await fetch(`/api/public/strategies/${encodeURIComponent(slug)}`);
    if (!res.ok) return null;
    const body = (await res.json()) as Partial<PublicStrategyDetail> | null;
    if (body == null || typeof body.slug !== "string") return null;
    return {
      slug: body.slug,
      name: typeof body.name === "string" ? body.name : "",
      tagline: body.tagline ?? null,
      tagline_en: body.tagline_en ?? null,
      featured: !!body.featured,
      leader_address: typeof body.leader_address === "string" ? body.leader_address : "",
      status: body.status === "paused" ? "paused" : "running",
      listable: !!body.listable,
      live_days: typeof body.live_days === "number" ? body.live_days : 0,
      follower_count: typeof body.follower_count === "number" ? body.follower_count : null,
      min_notional_usd: body.min_notional_usd ?? null,
      max_leverage: body.max_leverage ?? null,
      metrics: normalizeMetrics(body.metrics),
      equity_index: Array.isArray(body.equity_index) ? body.equity_index.map(String) : [],
      methodology: normalizeMethodology(body.methodology),
      as_of: typeof body.as_of === "number" ? body.as_of : null,
      sample_days: typeof body.sample_days === "number" ? body.sample_days : 0,
      // ⭐ R4-11：修正陳舊的 fallback 值——後端 `CAGR_SAMPLE_THRESHOLD_DAYS`
      // 2026-08-30 D15 裁決已由 60 降為 30（見 c948d6c），這個防禦性 fallback
      // （只在後端回應缺鍵時才會用到）先前漏改，與新增 `PublicTraderDetail`
      // 同款欄位時一併發現、一併修正（同一函式內的同型欄位，不留兩種預設值）。
      sample_threshold: typeof body.sample_threshold === "number" ? body.sample_threshold : 30,
      // ⭐ Task 7（CAGR 結構性 gating 的前端鏡射）：缺鍵／null／非字串一律視為
      // 「不顯示」，防後端序列化差異（見 delegation prompt）。
      cagr_pct: typeof body.cagr_pct === "string" ? body.cagr_pct : null,
    };
  } catch {
    return null;
  }
}

/**
 * `/api/public/leaderboard`（M3 round2 Task 5）。Hyperliquid 主網公開排行榜的
 * 展示資料——與本站策略／客戶資料無關，欄位全是字串（保留原精度，顯示層才
 * 格式化，見 `hl_leaderboard.top_rows` 檔頭）。
 */
export type LeaderboardWindow = "day" | "week" | "month" | "allTime";

export const LEADERBOARD_WINDOWS: LeaderboardWindow[] = ["day", "week", "month", "allTime"];

export interface PublicLeaderboardRow {
  address: string;
  display_name: string | null;
  account_value: string | null;
  pnl: string | null;
  roi: string | null;
  vlm: string | null;
}

export interface PublicLeaderboardResp {
  window: LeaderboardWindow;
  updated_at: number;
  rows: PublicLeaderboardRow[];
}

/**
 * 讀取 `/api/public/leaderboard?window=…`。與其他 `getPublic*` helper 刻意不同：
 * 這裡**不吞錯**（連線失敗、非 200、格式異常一律 throw）——呼叫端（leaderboard 頁）
 * 需要區分「讀取失敗」與「讀取成功但剛好零筆」兩種不同的空畫面（load/error/empty
 * 三態，plan Task 5 明訂），吞成統一的空清單會讓這兩種情況在 UI 上無法分辨。
 */
export async function getPublicLeaderboard(
  window: LeaderboardWindow,
): Promise<PublicLeaderboardResp> {
  const res = await fetch(`/api/public/leaderboard?window=${encodeURIComponent(window)}`);
  if (!res.ok) throw new Error(`leaderboard fetch failed: ${res.status}`);
  const body = (await res.json()) as { window?: unknown; updated_at?: unknown; rows?: unknown } | null;
  if (body == null || !Array.isArray(body.rows)) {
    throw new Error("leaderboard: unexpected response shape");
  }
  const rows: PublicLeaderboardRow[] = (body.rows as unknown[])
    .filter((r): r is Record<string, unknown> => r != null && typeof r === "object")
    .filter((r) => typeof r.address === "string" && r.address !== "")
    .map((r) => ({
      address: r.address as string,
      display_name: (r.display_name as string | null | undefined) ?? null,
      account_value: (r.account_value as string | null | undefined) ?? null,
      pnl: (r.pnl as string | null | undefined) ?? null,
      roi: (r.roi as string | null | undefined) ?? null,
      vlm: (r.vlm as string | null | undefined) ?? null,
    }));
  return {
    window: LEADERBOARD_WINDOWS.includes(body.window as LeaderboardWindow)
      ? (body.window as LeaderboardWindow)
      : window,
    updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
    rows,
  };
}

/**
 * `/api/public/explore`（M3 round3 Task 1／4：可跟單對象探索榜，`hl_explore.py`
 * `ExploreRow.to_dict()`）。排序與資格過濾全在後端（R2-01）。R4-10（2026-08-31
 * 使用者裁決）：期間 chip 改四窗全開（`ExploreWindow`）；原「樣本門檻」合併
 * chip 拆成兩顆獨立布林 chip（實盤天數／成交筆數），仍是布林開關送給後端固定
 * 數值門檻——不是 R4-3 revert 掉的自由數值輸入框（見 `explore/page.tsx`）。
 */
export type ExploreWindow = "day" | "week" | "month" | "allTime";

export const EXPLORE_WINDOWS: readonly ExploreWindow[] = ["day", "week", "month", "allTime"];

/** 單一窗的報酬／回撤／sparkline；`null`＝該窗對這一列缺席（day/week
 * best-effort，見 `hl_explore.py` 檔頭「R4-3」節）——前端誠實顯示「—」，
 * 不得回退借用其他窗的數字冒充。 */
export interface ExploreWindowStats {
  ret_pct: number;
  max_dd_pct: number;
  spark: number[];
}

export interface ExploreRow {
  address: string;
  display_name: string | null;
  label: string;
  coins: string[];
  account_bucket: string;
  windows: Record<ExploreWindow, ExploreWindowStats | null>;
  /** 實盤天數＝perpAllTime 首末快照的日曆跨距（後端欄位 `live_days`）。 */
  live_days: number;
  fill_count_30d: number;
  /** R-A/W2：30D fills 讀到分頁上限仍滿頁 → true（fill_count 為下限值）。 */
  fills_truncated?: boolean;
  close_win_rate_pct: number | null;
  concentration_pct: number | null;
  exposure: { dir: string | null; pct: number | null };
  tags: string[];
}

export interface ExploreResp {
  rows: ExploreRow[];
  page: number;
  page_size: number;
  total_qualified: number;
  total_scanned: number;
  updated_at: number | null;
  building: boolean;
}

/**
 * R4-10：呼叫端（`explore/page.tsx`）把兩顆布林 chip（實盤天數／成交筆數）與
 * 既有兩顆布林 chip（最大回撤／集中度）各自映射成後端的固定數值門檻
 * （on→門檻值／off→邊界值＝不過濾，映射常數集中在 page.tsx），本介面只描述
 * 「已決定要送出的數值」，不管 chip 開關本身。
 */
export interface ExploreFilters {
  window: ExploreWindow;
  minLiveDays: number;
  minFills: number;
  maxDdPct: number;
  maxConcentrationPct: number;
}

function toNumberOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function normalizeWindowStats(v: unknown): ExploreWindowStats | null {
  if (v == null || typeof v !== "object") return null;
  const r = v as Record<string, unknown>;
  const retPct = toNumberOrNull(r.ret_pct);
  const maxDdPct = toNumberOrNull(r.max_dd_pct);
  if (retPct == null || maxDdPct == null) return null;
  return {
    ret_pct: retPct,
    max_dd_pct: maxDdPct,
    spark: Array.isArray(r.spark)
      ? r.spark.filter((n): n is number => typeof n === "number" && Number.isFinite(n))
      : [],
  };
}

function normalizeExploreRow(v: unknown): ExploreRow | null {
  if (v == null || typeof v !== "object") return null;
  const r = v as Record<string, unknown>;
  if (typeof r.address !== "string" || r.address === "") return null;
  const exposure = (r.exposure && typeof r.exposure === "object")
    ? r.exposure as Record<string, unknown>
    : {};
  const rawWindows = (r.windows && typeof r.windows === "object")
    ? r.windows as Record<string, unknown>
    : {};
  const windows = Object.fromEntries(
    EXPLORE_WINDOWS.map((w) => [w, normalizeWindowStats(rawWindows[w])]),
  ) as Record<ExploreWindow, ExploreWindowStats | null>;
  return {
    address: r.address,
    display_name: typeof r.display_name === "string" ? r.display_name : null,
    label: typeof r.label === "string" ? r.label : r.address,
    coins: Array.isArray(r.coins) ? r.coins.filter((c): c is string => typeof c === "string") : [],
    account_bucket: typeof r.account_bucket === "string" ? r.account_bucket : NO_VALUE_PLACEHOLDER,
    windows,
    live_days: typeof r.live_days === "number" ? r.live_days : 0,
    fill_count_30d: typeof r.fill_count_30d === "number" ? r.fill_count_30d : 0,
    close_win_rate_pct: toNumberOrNull(r.close_win_rate_pct),
    concentration_pct: toNumberOrNull(r.concentration_pct),
    exposure: {
      dir: typeof exposure.dir === "string" ? exposure.dir : null,
      pct: toNumberOrNull(exposure.pct),
    },
    tags: Array.isArray(r.tags) ? r.tags.filter((t): t is string => typeof t === "string") : [],
  };
}

// 後端 `_account_bucket` 讀不到 accountValue 時本身就回傳 "—"；這裡只是型別上的
// 保底字面值（正常情況不會走到），避免與 `NO_VALUE`（`lib/format.ts`）耦合造成
// 這個檔案額外 import 一個純顯示常數。
const NO_VALUE_PLACEHOLDER = "—";

/**
 * 讀取 `/api/public/explore`。與 `getPublicLeaderboard` 同一套「不吞錯」設計：
 * 連線失敗、非 200、格式異常一律 throw——呼叫端（explore 頁）需要區分
 * 「fetch 失敗」（R2·C 態三：時間戳＋重試）與「成功但 `building:true`」
 * （R2·C 態二：建置中）两種不同的空狀態，吞掉會讓兩者在 UI 上無法分辨。
 */
export async function getPublicExplore(
  page: number, filters: ExploreFilters,
): Promise<ExploreResp> {
  const params = new URLSearchParams({
    window: filters.window,
    page: String(page),
    min_live_days: String(filters.minLiveDays),
    min_fills: String(filters.minFills),
    max_dd_pct: String(filters.maxDdPct),
    max_concentration_pct: String(filters.maxConcentrationPct),
  });
  const res = await fetch(`/api/public/explore?${params.toString()}`);
  if (!res.ok) throw new Error(`explore fetch failed: ${res.status}`);
  const body = (await res.json()) as Partial<ExploreResp> | null;
  if (body == null || !Array.isArray(body.rows)) {
    throw new Error("explore: unexpected response shape");
  }
  const rows = body.rows.map(normalizeExploreRow).filter((r): r is ExploreRow => r !== null);
  return {
    rows,
    page: typeof body.page === "number" ? body.page : page,
    page_size: typeof body.page_size === "number" ? body.page_size : rows.length,
    total_qualified: typeof body.total_qualified === "number" ? body.total_qualified : 0,
    total_scanned: typeof body.total_scanned === "number" ? body.total_scanned : 0,
    updated_at: typeof body.updated_at === "number" ? body.updated_at : null,
    building: !!body.building,
  };
}

/**
 * `/api/public/traders/{address}`（M3 round2 Task 6：交易員詳情頁）。任意 HL
 * 地址的鏈上績效——**不受精選白名單管轄**，`metrics`／`equity_index`／
 * `methodology` 與 `/api/public/strategies/{slug}` 同一份形狀（後端共用同一批
 * 純函式），故前端可重用同一個 `EquityCurve` 元件與指標卡渲染邏輯。
 * `account_value` 來自另一個端點（`clearinghouseState`，工程原則 1：與
 * `equity_index` 不同源，不得放進同一個對比），可能單獨為 `null`。
 */
export interface PublicTraderDetail {
  address: string;
  account_value: string | null;
  // ⭐ [W4] 2026-08-29 opus 審查修正：地址若被平台安全撤銷（精選白名單
  // enabled=false），後端回 true——前端隱藏跟單 CTA，不讓新客戶點進去。
  follow_blocked: boolean;
  metrics: PublicStrategyMetrics;
  equity_index: string[];
  methodology: PublicStrategyMethodology;
  /** M3 round4 Task R4-11：與 `PublicStrategyDetail` 同一套組裝規則
   * （後端 `build_cagr_fields`，見 `filet/strategies.py` 檔頭），供交易員詳情頁
   * 補齊 CAGR 收合卡。 */
  sample_days: number;
  sample_threshold: number;
  /** `sample_days < sample_threshold` 時後端整個不回傳這個鍵——同
   * `PublicStrategyDetail.cagr_pct` 的結構性防呆。 */
  cagr_pct: string | null;
}

/**
 * 讀取 `/api/public/traders/{address}`。回傳 `null` 代表「這頁沒有東西可畫」
 * ——404（壞位址格式）、503（上游暫時不可用）、連線失敗、格式異常一律折疊成
 * 同一種呼叫端渲染（沿 `getPublicStrategy` 的既有慣例，不區分「明確不存在」
 * 與「讀不到」：對使用者都是「這個地址目前看不到」）。
 */
export async function getPublicTraderDetail(address: string): Promise<PublicTraderDetail | null> {
  try {
    const res = await fetch(`/api/public/traders/${encodeURIComponent(address)}`);
    if (!res.ok) return null;
    const body = (await res.json()) as Partial<PublicTraderDetail> | null;
    if (body == null || typeof body.address !== "string") return null;
    return {
      address: body.address,
      account_value: body.account_value ?? null,
      // ⭐ [8b-7] 2026-08-29 二輪複審 Suggestion：fail-closed，與後端
      // `_trader_follow_blocked` 同方向——欄位缺漏或格式異常一律視為「已封鎖」
      // （隱藏 CTA），不得因為解析失敗就預設放行去跟一個可能已被撤銷的地址。
      follow_blocked: body.follow_blocked !== false,
      metrics: normalizeMetrics(body.metrics),
      equity_index: Array.isArray(body.equity_index) ? body.equity_index.map(String) : [],
      methodology: normalizeMethodology(body.methodology),
      sample_days: typeof body.sample_days === "number" ? body.sample_days : 0,
      sample_threshold: typeof body.sample_threshold === "number" ? body.sample_threshold : 30,
      cagr_pct: typeof body.cagr_pct === "string" ? body.cagr_pct : null,
    };
  } catch {
    return null;
  }
}

/**
 * 讀取 `/api/public/stats`（首頁證據列）。後端承諾任一子項取不到就回 `null`、
 * 端點恆 200；這裡再加一層防禦——連線失敗或非 200 一律降級為全 `null`
 * （`UNKNOWN_STATS`），呼叫端一律走「null → 顯示 `—`」的既有路徑，不特殊處理。
 */
export async function getPublicStats(): Promise<PublicStats> {
  try {
    const res = await fetch("/api/public/stats");
    if (!res.ok) return UNKNOWN_STATS;
    const body = (await res.json()) as Partial<PublicStats> | null;
    if (body == null) return UNKNOWN_STATS;
    return {
      routed_volume_usd_total:
        typeof body.routed_volume_usd_total === "string" ? body.routed_volume_usd_total : null,
      builder_fee_bps: typeof body.builder_fee_bps === "number" ? body.builder_fee_bps : null,
      live_days: typeof body.live_days === "number" ? body.live_days : null,
      updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
    };
  } catch {
    return UNKNOWN_STATS;
  }
}
