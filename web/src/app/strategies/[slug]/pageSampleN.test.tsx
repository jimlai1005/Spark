/**
 * `/strategies/[slug]` — 區間報酬樣本數 N（E2）與曲線時間戳（E1）的頁面層測試
 * （plan 2026-11-17-perf-followups §1 A2）。
 *
 * 38 個取樣點 → 37 個區間；「涵蓋 N 個取樣點」那句仍用 `methodology.sample_count`（38）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { fmtAmount, fmtUpdatedAtUtc } from "@/lib/format";

const push = vi.fn();
let paramsSlug = "core";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ slug: paramsSlug }),
}));

const connectAsync = vi.fn();
const signMessageAsync = vi.fn();
let accountState: { address?: string; chainId?: number; isConnected: boolean } = { isConnected: false };
vi.mock("wagmi", () => ({
  useAccount: () => accountState,
  useConnect: () => ({ connectAsync, connectors: [{ id: "injected" }] }),
  useSignMessage: () => ({ signMessageAsync }),
}));

const loginWithSiwe = vi.fn();
vi.mock("@/lib/siwe", () => ({ loginWithSiwe: (...a: unknown[]) => loginWithSiwe(...a) }));

const getMe = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
}));

import { ApiError } from "@/lib/api";
import StrategyDetailPage from "./page";

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => body } as Response;
}

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}


const DETAIL = {
  slug: "core", name: "Filet Core", tagline: "多資產動能 · 永續合約", featured: true,
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
    return_sample_count: 37,
  },
  equity_index: Array.from({ length: 38 }, (_, i) => String(1 + i * 0.005)),
  methodology: {
    start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
    sample_count: 38, annualization_days: 365, risk_free_rate: "0", basis: "perp",
    updated_at: 1756000000,
  },
  as_of: 1756000500,
  sample_days: 72,
  sample_threshold: 30,
  cagr_pct: "45.23",
};

beforeEach(() => {
  paramsSlug = "core";
  getMe.mockReset();
  getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
  accountState = { isConnected: false };
});

describe("StrategyDetailPage — N 與曲線時間戳", () => {
  it("勝率註記與方法論 Sharpe 句用 return_sample_count（37），涵蓋句仍是取樣點數（38）", async () => {
    stubFetch(() => jsonResponse(DETAIL));
    const { container } = render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    const mc = COPY.strategyDetail.metrics;
    const me = COPY.strategyDetail.methodology;
    expect(screen.getByText(`${mc.winRateNotePrefix}37${mc.winRateNoteSuffix}`)).toBeInTheDocument();
    const text = container.querySelector(".methodology-card")?.textContent ?? "";
    expect(text).toContain(`${me.sharpeSeSuffix}37${me.sampleSuffix}`);
    expect(text).toContain(`38${me.daysSuffix}2026-06-17 → 2026-08-27`);
  });

  it("舊後端缺 return_sample_count → 退回 sample_count − 1（37）", async () => {
    const { return_sample_count: _omit, ...legacyMetrics } = DETAIL.metrics;
    void _omit;
    stubFetch(() => jsonResponse({ ...DETAIL, metrics: legacyMetrics }));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    const mc = COPY.strategyDetail.metrics;
    expect(screen.getByText(`${mc.winRateNotePrefix}37${mc.winRateNoteSuffix}`)).toBeInTheDocument();
  });

  it("equity_index_ts 傳到 EquityCurve：每點相隔 10 天 → 7D 停用、30D 可用", async () => {
    const T = Date.UTC(2026, 7, 27);
    const ts = Array.from({ length: 38 }, (_, i) => T - (37 - i) * 10 * 86_400_000);
    stubFetch(() => jsonResponse({ ...DETAIL, equity_index_ts: ts }));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    expect(screen.getByRole("button", { name: /7D/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /30D/ })).not.toBeDisabled();
  });

  it("後端沒給 equity_index_ts（舊回應）→ 7D 不停用", async () => {
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    expect(screen.getByRole("button", { name: "7D" })).not.toBeDisabled();
  });
});
