import { describe, expect, it, vi } from "vitest";
import {
  fillsIncomplete,
  getPublicExplore,
  getPublicLeaderboard,
  getPublicStats,
  getPublicStatus,
  getPublicStrategies,
  getPublicStrategy,
  getPublicTraderDetail,
} from "./publicApi";
import type { ExploreFilters } from "./publicApi";

function mockFetchOnce(impl: () => Response | Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: async () => body,
  } as Response;
}

describe("getPublicStatus", () => {
  it("回傳後端的三態之一（ok）", async () => {
    mockFetchOnce(() => jsonResponse({ status: "ok", components: [{ name: "api", status: "ok" }], updated_at: 123 }));
    const s = await getPublicStatus();
    expect(s).toEqual({ status: "ok", components: [{ name: "api", status: "ok" }], updated_at: 123 });
  });

  it("degraded 原樣回傳", async () => {
    mockFetchOnce(() => jsonResponse({ status: "degraded", components: [], updated_at: 1 }));
    const s = await getPublicStatus();
    expect(s.status).toBe("degraded");
  });

  it("非 200 → 降級為 unknown（不得偽裝成 ok）", async () => {
    mockFetchOnce(() => jsonResponse({ status: "ok" }, false));
    const s = await getPublicStatus();
    expect(s.status).toBe("unknown");
    expect(s.components).toEqual([]);
  });

  it("網路例外 → 降級為 unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    const s = await getPublicStatus();
    expect(s.status).toBe("unknown");
  });

  it("回應格式不是預期三態之一 → 降級為 unknown", async () => {
    mockFetchOnce(() => jsonResponse({ status: "totally-broken" }));
    const s = await getPublicStatus();
    expect(s.status).toBe("unknown");
  });
});

describe("getPublicStrategies", () => {
  const ONE = {
    slug: "core", name: "Filet Core", tagline: "多資產動能 · 永續合約", featured: true,
    leader_address: "0xfeed000000000000000000000000000000f00d",
    status: "running", listable: true, live_days: 99, follower_count: 3,
    min_notional_usd: "500", max_leverage: "3",
    metrics: {
      total_return_pct: "13.37", total_return_pct_insufficient: false,
      max_drawdown_pct: "-1.02", max_drawdown_pct_insufficient: false,
      sharpe: "5.55", sharpe_insufficient: false,
      sharpe_se: "1.11", sharpe_se_insufficient: false,
      win_rate_pct: "61.11", win_rate_pct_insufficient: false,
      annualized_vol_pct: "22.20", annualized_vol_pct_insufficient: false,
      sortino: "9.99", sortino_insufficient: false,
      best_day_pct: "2.02", best_day_pct_insufficient: false,
      worst_day_pct: "-1.02", worst_day_pct_insufficient: false,
      sample_count: 99,
    },
  };

  it("原樣回傳策略清單", async () => {
    mockFetchOnce(() => jsonResponse({ strategies: [ONE], updated_at: 42 }));
    const r = await getPublicStrategies();
    expect(r.strategies).toEqual([ONE]);
    expect(r.updated_at).toBe(42);
  });

  it("非 200 → 降級為空清單", async () => {
    mockFetchOnce(() => jsonResponse({ strategies: [ONE] }, false));
    const r = await getPublicStrategies();
    expect(r.strategies).toEqual([]);
  });

  it("網路例外 → 降級為空清單", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    const r = await getPublicStrategies();
    expect(r.strategies).toEqual([]);
  });

  it("回應形狀不是陣列 → 降級為空清單", async () => {
    mockFetchOnce(() => jsonResponse({ strategies: "not-an-array" }));
    const r = await getPublicStrategies();
    expect(r.strategies).toEqual([]);
  });
});

describe("getPublicStrategy", () => {
  const DETAIL = {
    slug: "core", name: "Filet Core", tagline: "多資產動能 · 永續合約",
    tagline_en: "Multi-asset momentum · Perpetuals", featured: true,
    leader_address: "0xfeed000000000000000000000000000000f00d",
    status: "running", listable: true, live_days: 72, follower_count: 3,
    min_notional_usd: "500", max_leverage: "3",
    metrics: {
      total_return_pct: "17.77", total_return_pct_insufficient: false,
      max_drawdown_pct: "-0.80", max_drawdown_pct_insufficient: false,
      sharpe: "5.55", sharpe_insufficient: false,
      sharpe_se: "3.36", sharpe_se_insufficient: false,
      win_rate_pct: "64.86", win_rate_pct_insufficient: false,
      annualized_vol_pct: "18.05", annualized_vol_pct_insufficient: false,
      sortino: "43.42", sortino_insufficient: false,
      best_day_pct: "3.01", best_day_pct_insufficient: false,
      worst_day_pct: "-0.80", worst_day_pct_insufficient: false,
      sample_count: 38,
    },
    equity_index: ["1", "1.01", "1.206"],
    methodology: {
      start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
      start_equity_usd: "1000", end_equity_usd: "1200",
      sample_count: 38, annualization_days: 365, risk_free_rate: "0", basis: "perp",
      updated_at: 999,
    },
    as_of: 995,
    sample_days: 72,
    sample_threshold: 60,
    cagr_pct: "45.23",
  };

  it("原樣回傳策略詳情", async () => {
    mockFetchOnce(() => jsonResponse(DETAIL));
    const r = await getPublicStrategy("core");
    expect(r).toEqual(DETAIL);
  });

  it("tagline_en 鍵不存在 → 降級為 null（白名單未填英文版）", async () => {
    const { tagline_en: _drop, ...withoutTaglineEn } = DETAIL;
    mockFetchOnce(() => jsonResponse(withoutTaglineEn));
    const r = await getPublicStrategy("core");
    expect(r?.tagline_en).toBeNull();
  });

  it("cagr_pct 鍵不存在（樣本不足）→ 降級為 null，不臆造", async () => {
    const { cagr_pct: _drop, ...withoutCagr } = DETAIL;
    mockFetchOnce(() => jsonResponse(withoutCagr));
    const r = await getPublicStrategy("core");
    expect(r?.cagr_pct).toBeNull();
  });

  it("as_of 為 null（上游查詢失敗）→ 原樣透傳 null", async () => {
    mockFetchOnce(() => jsonResponse({ ...DETAIL, as_of: null }));
    const r = await getPublicStrategy("core");
    expect(r?.as_of).toBeNull();
  });

  it("404 → null（呼叫端渲染空態，不偽造策略物件）", async () => {
    mockFetchOnce(() => jsonResponse({ detail: "策略不存在" }, false));
    const r = await getPublicStrategy("nope");
    expect(r).toBeNull();
  });

  it("網路例外 → null", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    const r = await getPublicStrategy("core");
    expect(r).toBeNull();
  });

  it("回應缺 slug → null", async () => {
    mockFetchOnce(() => jsonResponse({ name: "broken" }));
    const r = await getPublicStrategy("core");
    expect(r).toBeNull();
  });

  it("metrics／methodology 缺席 → 降級為空殼而非拋錯", async () => {
    mockFetchOnce(() => jsonResponse({ slug: "core", name: "Filet Core" }));
    const r = await getPublicStrategy("core");
    expect(r?.metrics.sharpe_insufficient).toBe(true);
    expect(r?.methodology.initial_deposit_usd).toBeNull();
    expect(r?.equity_index).toEqual([]);
    expect(r?.as_of).toBeNull();
    expect(r?.sample_days).toBe(0);
    // ⚠️ 2026-08-30 D15 裁決原 60 降為 30——fallback 值須與後端
    // `CAGR_SAMPLE_THRESHOLD_DAYS` 同步，見 publicApi.ts 該行註解。
    expect(r?.sample_threshold).toBe(30);
    expect(r?.cagr_pct).toBeNull();
  });
});

describe("getPublicLeaderboard", () => {
  const ROW = {
    address: "0xfeed000000000000000000000000000000f00d",
    display_name: "Alice", account_value: "58675737.76",
    pnl: "1234.56", roi: "0.0842", vlm: "999000.0",
  };

  it("原樣回傳 rows（不吞錯，與其他 getPublic* helper 刻意不同）", async () => {
    mockFetchOnce(() => jsonResponse({ window: "month", updated_at: 42, rows: [ROW] }));
    const r = await getPublicLeaderboard("month");
    expect(r).toEqual({ window: "month", updated_at: 42, rows: [ROW] });
  });

  it("非 200 → 拋出（呼叫端據此區分 error 態與 empty 態）", async () => {
    mockFetchOnce(() => jsonResponse({ detail: "壞 window" }, false));
    await expect(getPublicLeaderboard("month")).rejects.toThrow();
  });

  it("網路例外 → 拋出", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    await expect(getPublicLeaderboard("day")).rejects.toThrow();
  });

  it("rows 不是陣列 → 拋出（不得偽裝成空清單，那會被 UI 顯示成 empty 而非 error）", async () => {
    mockFetchOnce(() => jsonResponse({ window: "month", updated_at: 1, rows: "nope" }));
    await expect(getPublicLeaderboard("month")).rejects.toThrow();
  });

  it("成功時空 rows → 空陣列（合法的 empty 態，不是錯誤）", async () => {
    mockFetchOnce(() => jsonResponse({ window: "week", updated_at: 1, rows: [] }));
    const r = await getPublicLeaderboard("week");
    expect(r.rows).toEqual([]);
  });

  it("列缺 address → 過濾掉，不讓一筆壞資料整批拋錯", async () => {
    mockFetchOnce(() =>
      jsonResponse({ window: "month", updated_at: 1, rows: [{ ...ROW, address: undefined }, ROW] }));
    const r = await getPublicLeaderboard("month");
    expect(r.rows).toEqual([ROW]);
  });

  it("window 帶入請求 query", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ window: "allTime", updated_at: 1, rows: [] })));
    vi.stubGlobal("fetch", fetchMock);
    await getPublicLeaderboard("allTime");
    expect(fetchMock).toHaveBeenCalledWith("/api/public/leaderboard?window=allTime");
  });
});

// Task 6.6（P6 契約 A）：`live_days`／`closed_positions_30d`／`realized_pnl_30d_usd`
// 後端送 `null`（pending／portfolio_missing、coverage≠complete）時前端不得補 0——
// 那會讓探索頁顯示「實盤天數 0 天」這種假數字。三欄非 number 一律 normalize 成 null。
const EXPLORE_FILTERS: ExploreFilters = {
  window: "month", minLiveDays: 0, minFills: 0, maxDdPct: 100, maxConcentrationPct: 100,
  sort: "pnl", order: "desc",
};

function exploreRowBody(over: Record<string, unknown> = {}) {
  return {
    address: "0xaaaa00000000000000000000000000000000aaaa",
    display_name: null,
    label: "0xaaaa…aaaa",
    coins: [],
    account_bucket: "$10K–$100K",
    windows: {},
    live_days: null,
    order_count_30d: 5,
    closed_positions_30d: null,
    realized_pnl_30d_usd: null,
    close_win_rate_pct: null,
    concentration_pct: null,
    exposure: { dir: null, pct: null },
    tags: [],
    ...over,
  };
}

describe("getPublicExplore — normalizeExploreRow null 欄位不得轉 0（P6 契約 A / Task 6.6）", () => {
  it("live_days: null → 保持 null（不得補 0）", async () => {
    mockFetchOnce(() => jsonResponse({ rows: [exploreRowBody({ live_days: null })] }));
    const r = await getPublicExplore(1, EXPLORE_FILTERS);
    expect(r.rows[0].live_days).toBeNull();
  });

  it("closed_positions_30d: null → 保持 null（不得補 0）", async () => {
    mockFetchOnce(() => jsonResponse({ rows: [exploreRowBody({ closed_positions_30d: null })] }));
    const r = await getPublicExplore(1, EXPLORE_FILTERS);
    expect(r.rows[0].closed_positions_30d).toBeNull();
  });

  it("realized_pnl_30d_usd: null → 保持 null（不得補 0）", async () => {
    mockFetchOnce(() => jsonResponse({ rows: [exploreRowBody({ realized_pnl_30d_usd: null })] }));
    const r = await getPublicExplore(1, EXPLORE_FILTERS);
    expect(r.rows[0].realized_pnl_30d_usd).toBeNull();
  });

  it("三欄為正常 number 時原樣保留", async () => {
    mockFetchOnce(() => jsonResponse({
      rows: [exploreRowBody({ live_days: 118, closed_positions_30d: 20, realized_pnl_30d_usd: 5000 })],
    }));
    const r = await getPublicExplore(1, EXPLORE_FILTERS);
    expect(r.rows[0].live_days).toBe(118);
    expect(r.rows[0].closed_positions_30d).toBe(20);
    expect(r.rows[0].realized_pnl_30d_usd).toBe(5000);
  });
});

describe("getPublicTraderDetail", () => {
  // 2026-09-05（explore/trader 指標統一 plan Task 4/5）：形狀改為與
  // `/api/public/explore`（`ExploreRow`）同源的 `windows`／`live_days`／
  // `fills_30d`／`exposure`；`metrics` 保留但改逐窗；`equity_index` 移除。
  const METRICS_OK = {
    total_return_pct: "20.00", total_return_pct_insufficient: false,
    max_drawdown_pct: "-0.80", max_drawdown_pct_insufficient: false,
    sharpe: "5.55", sharpe_insufficient: false,
    sharpe_se: "3.36", sharpe_se_insufficient: false,
    win_rate_pct: "64.86", win_rate_pct_insufficient: false,
    annualized_vol_pct: "18.05", annualized_vol_pct_insufficient: false,
    sortino: "43.42", sortino_insufficient: false,
    best_day_pct: "3.01", best_day_pct_insufficient: false,
    worst_day_pct: "-0.80", worst_day_pct_insufficient: false,
    sample_count: 38,
  };
  const WINDOW_OK = { pnl_usd: 1200.5, max_dd_pct: -8.0, max_dd_reason: null, spark: [0, 600, 1200.5] };
  const DETAIL = {
    address: "0xfeed000000000000000000000000000000f00d",
    account_value: "5000.00",
    follow_blocked: false,
    live_days: 120,
    exposure: { dir: "long", pct: 60.0 },
    windows: { day: WINDOW_OK, week: WINDOW_OK, month: WINDOW_OK, allTime: WINDOW_OK },
    metrics: { day: METRICS_OK, week: METRICS_OK, month: METRICS_OK, allTime: METRICS_OK },
    fills_30d: {
      order_count: 221, closed_positions: 27, wins: 15, win_rate_pct: 55.56,
      realized_pnl_usd: 40225.79, concentration_pct: 62.5, coins: ["BTC", "ETH"], truncated: false,
    },
    methodology: {
      basis: "combined", updated_at: 999,
      start_equity_usd: "1000", end_equity_usd: "1200",
      initial_deposit_usd: "1000", mdd_note: "MDD 採樣說明",
    },
    // ⭐ M3 round4 Task R4-11：與 `PublicStrategyDetail` 同一套組裝規則
    // （後端 `build_cagr_fields`）。
    sample_days: 72,
    sample_threshold: 30,
    cagr_pct: "45.23",
  };

  it("原樣回傳交易員詳情", async () => {
    mockFetchOnce(() => jsonResponse(DETAIL));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r).toEqual(DETAIL);
  });

  it("422/503 → null（呼叫端渲染空態，不偽造交易員物件）", async () => {
    mockFetchOnce(() => jsonResponse({ detail: "位址格式不合法" }, false));
    const r = await getPublicTraderDetail("not-an-address");
    expect(r).toBeNull();
  });

  it("網路例外 → null", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r).toBeNull();
  });

  it("回應缺 address → null", async () => {
    mockFetchOnce(() => jsonResponse({ account_value: "1" }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r).toBeNull();
  });

  it("account_value 為 null（clearinghouseState 查詢失敗降級）時原樣保留", async () => {
    mockFetchOnce(() => jsonResponse({ ...DETAIL, account_value: null }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.account_value).toBeNull();
  });

  it("metrics／methodology 缺席 → 降級為空殼而非拋錯", async () => {
    mockFetchOnce(() => jsonResponse({ address: DETAIL.address }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.metrics.month.sharpe_insufficient).toBe(true);
    expect(r?.methodology.initial_deposit_usd).toBeNull();
    expect(r?.windows.month).toBeNull();
    expect(r?.exposure).toBeNull();
  });

  it("[W4] follow_blocked: true 原樣保留", async () => {
    mockFetchOnce(() => jsonResponse({ ...DETAIL, follow_blocked: true }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.follow_blocked).toBe(true);
  });

  it("[8b-7] follow_blocked 缺席 → fail-closed 視為 true（與後端方向一致）", async () => {
    mockFetchOnce(() => jsonResponse({ address: DETAIL.address }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.follow_blocked).toBe(true);
  });

  it("[8b-7] follow_blocked: false → 明確保留為 false（唯一放行的值）", async () => {
    mockFetchOnce(() => jsonResponse({ ...DETAIL, follow_blocked: false }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.follow_blocked).toBe(false);
  });

  it("Task 10 Step 4：fills_30d 畸形 payload（字串）→ null，不偽造成四個 0", async () => {
    mockFetchOnce(() => jsonResponse({ ...DETAIL, fills_30d: "x" }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.fills_30d).toBeNull();
  });

  // Task 4.2（2026-09-20）：新欄位（第二次部署前後端混跑）——舊後端不回這些鍵
  // 時全部 `undefined`（見「原樣回傳交易員詳情」，DETAIL 沒帶這些鍵仍
  // `toEqual` 通過），新後端回傳時要原樣解析出來。
  it("Task 4.2：source／refreshing／as_of／fills_coverage 有回傳時原樣解析", async () => {
    mockFetchOnce(() => jsonResponse({
      ...DETAIL,
      source: "upstream",
      refreshing: true,
      as_of: { portfolio: 111, state: 111, fills: null },
      fills_coverage: { state: "partial", observed_from: 1, observed_to: null, reason: "page_limit" },
    }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.source).toBe("upstream");
    expect(r?.refreshing).toBe(true);
    expect(r?.as_of).toEqual({ portfolio: 111, state: 111, fills: null });
    // Task 7.1（2026-09-21）：`normalizeFillsCoverage` 一律補上 `synced_through`／
    // `last_success_at`（缺席時 null），即使後端沒回這兩個新鍵也一樣。
    expect(r?.fills_coverage).toEqual({
      state: "partial", observed_from: 1, observed_to: null, reason: "page_limit",
      synced_through: null, last_success_at: null,
    });
  });

  it("Task 7.1：後端回傳 synced_through／last_success_at 時原樣解析", async () => {
    mockFetchOnce(() => jsonResponse({
      ...DETAIL,
      fills_coverage: {
        state: "partial", observed_from: 1, observed_to: 2, reason: "page_limit",
        synced_through: 1_700_000_000_000, last_success_at: 1_700_000_500,
      },
    }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.fills_coverage?.synced_through).toBe(1_700_000_000_000);
    expect(r?.fills_coverage?.last_success_at).toBe(1_700_000_500);
  });

  it("Task 4.2：source 非法值／fills_coverage 畸形 → undefined，不拋錯", async () => {
    mockFetchOnce(() => jsonResponse({ ...DETAIL, source: "weird", fills_coverage: "x" }));
    const r = await getPublicTraderDetail(DETAIL.address);
    expect(r?.source).toBeUndefined();
    expect(r?.fills_coverage).toBeUndefined();
  });
});

// Task 4.2（2026-09-20）：`fills_coverage` 上線後的單一判準——存在時優先讀它，
// 只有整體缺席（舊後端）才 fallback 到 `fills_truncated`。
describe("fillsIncomplete", () => {
  it("fills_coverage.state === \"complete\" → false（即使 fills_truncated 為 true 也不理它）", () => {
    expect(fillsIncomplete({
      fills_truncated: true,
      fills_coverage: { state: "complete", observed_from: 1, observed_to: 2, reason: null },
    })).toBe(false);
  });

  it("fills_coverage.state === \"partial\" → true", () => {
    expect(fillsIncomplete({
      fills_coverage: { state: "partial", observed_from: null, observed_to: null, reason: "page_limit" },
    })).toBe(true);
  });

  it("無 fills_coverage 且 fills_truncated: true → true（fallback）", () => {
    expect(fillsIncomplete({ fills_truncated: true })).toBe(true);
  });

  it("無 fills_coverage 且 fills_truncated 缺席／false → false", () => {
    expect(fillsIncomplete({})).toBe(false);
    expect(fillsIncomplete({ fills_truncated: false })).toBe(false);
  });
});

describe("getPublicStats", () => {
  it("原樣回傳（含 null 欄位）", async () => {
    mockFetchOnce(() => jsonResponse({
      routed_volume_usd_total: null, builder_fee_bps: 2, live_days: null, updated_at: 7,
    }));
    const s = await getPublicStats();
    expect(s).toEqual({
      routed_volume_usd_total: null, builder_fee_bps: 2, live_days: null, updated_at: 7,
    });
  });

  it("非 200 → 全欄降級為 null", async () => {
    mockFetchOnce(() => jsonResponse({ routed_volume_usd_total: "1" }, false));
    const s = await getPublicStats();
    expect(s.routed_volume_usd_total).toBeNull();
    expect(s.builder_fee_bps).toBeNull();
  });

  it("網路例外 → 全欄降級為 null", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    const s = await getPublicStats();
    expect(s.live_days).toBeNull();
  });
});
