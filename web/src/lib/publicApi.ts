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
  featured: boolean;
  leader_address: string;
  status: "running" | "paused";
  listable: boolean;
  live_days: number;
  follower_count: number | null;
  min_notional_usd: string | null;
  max_leverage: string | null;
  metrics: PublicStrategyMetrics;
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
  sample_count: null,
  annualization_days: 365,
  risk_free_rate: "0",
  basis: "perp",
  updated_at: 0,
};

export interface PublicStrategyDetail extends PublicStrategy {
  equity_index: string[];
  methodology: PublicStrategyMethodology;
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
