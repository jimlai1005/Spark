import { describe, expect, it, vi } from "vitest";
import { getPublicStats, getPublicStatus, getPublicStrategies } from "./publicApi";

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
