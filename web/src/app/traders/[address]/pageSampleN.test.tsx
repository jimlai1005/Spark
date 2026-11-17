/**
 * `/traders/[address]` — 區間報酬樣本數 N（E2）頁面層測試（plan
 * 2026-11-17-perf-followups §1 A2）。交易員頁不畫 `EquityCurve`（用 `PnlCurve`），
 * 故只有 N 的變更。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { NO_VALUE } from "@/lib/format";

const push = vi.fn();
let paramsAddress = "0xfefefefefefefefefefefefefefefefefefefefe";
let searchParamsValue = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ address: paramsAddress }),
  useSearchParams: () => searchParamsValue,
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
import TraderDetailPage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}


const METRICS_DAY = {
  total_return_pct: null, total_return_pct_insufficient: true,
  max_drawdown_pct: null, max_drawdown_pct_insufficient: true,
  sharpe: null, sharpe_insufficient: true,
  sharpe_se: null, sharpe_se_insufficient: true,
  win_rate_pct: "64.86", win_rate_pct_insufficient: false,
  annualized_vol_pct: null, annualized_vol_pct_insufficient: true,
  sortino: null, sortino_insufficient: true,
  best_day_pct: null, best_day_pct_insufficient: true,
  worst_day_pct: null, worst_day_pct_insufficient: true,
  sample_count: 30,
};

function detail(dayMetrics: Record<string, unknown>) {
  return {
    address: "0xfefefefefefefefefefefefefefefefefefefefe",
    account_value: "5000.00", follow_blocked: false, live_days: 1003,
    exposure: null,
    windows: {
      day: { pnl_usd: 1, max_dd_pct: null, max_dd_reason: null, spark: [0, 1] },
      week: null, month: null, allTime: null,
    },
    metrics: { day: dayMetrics, week: METRICS_DAY, month: METRICS_DAY, allTime: METRICS_DAY },
    fills_30d: null,
    methodology: {
      basis: "combined", updated_at: 1756000000, start_equity_usd: "1000.00",
      end_equity_usd: "5000.00", initial_deposit_usd: "1000", mdd_note: "x",
    },
    sample_days: 29, sample_threshold: 30,
  };
}

beforeEach(() => {
  paramsAddress = "0xfefefefefefefefefefefefefefefefefefefefe";
  searchParamsValue = new URLSearchParams("window=day");
  getMe.mockReset();
  getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
  accountState = { isConnected: false };
});

describe("TraderDetailPage — N＝區間報酬樣本數", () => {
  it("有 return_sample_count（29）→ 勝率註記顯示 N=29（不是取樣點數 30）", async () => {
    stubFetch(() => jsonResponse(detail({ ...METRICS_DAY, return_sample_count: 29 })));
    render(wrap(<TraderDetailPage />));
    const mc = COPY.strategyDetail.metrics;
    expect(await screen.findByText(`${mc.winRateNotePrefix}29${mc.winRateNoteSuffix}`)).toBeInTheDocument();
  });

  it("舊後端缺 return_sample_count → 退回 sample_count − 1（29）", async () => {
    stubFetch(() => jsonResponse(detail(METRICS_DAY)));
    render(wrap(<TraderDetailPage />));
    const mc = COPY.strategyDetail.metrics;
    expect(await screen.findByText(`${mc.winRateNotePrefix}29${mc.winRateNoteSuffix}`)).toBeInTheDocument();
  });
});
