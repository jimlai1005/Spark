import { describe, expect, it, vi } from "vitest";
import {
  getPublicLeaderboard,
  getPublicStats,
  getPublicStatus,
  getPublicStrategies,
  getPublicStrategy,
  getPublicTraderDetail,
} from "./publicApi";

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

describe("getPublicTraderDetail", () => {
  const DETAIL = {
    address: "0xfeed000000000000000000000000000000f00d",
    account_value: "5000.00",
    follow_blocked: false,
    metrics: {
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
    },
    equity_index: ["1", "1.2"],
    methodology: {
      start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
      start_equity_usd: "1000", end_equity_usd: "1200",
      sample_count: 38, annualization_days: 365, risk_free_rate: "0", basis: "perp",
      updated_at: 999,
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
    expect(r?.metrics.sharpe_insufficient).toBe(true);
    expect(r?.methodology.initial_deposit_usd).toBeNull();
    expect(r?.equity_index).toEqual([]);
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
