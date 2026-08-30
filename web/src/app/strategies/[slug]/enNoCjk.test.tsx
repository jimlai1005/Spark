/**
 * `/strategies/[slug]` — EN 模式無繁中殘留（M3 round4 Task R4-4 規格 6）。
 * 新檔（不改既有 `page.test.tsx`）：`localStorage.filet_lang=en` 下渲染策略
 * 詳情頁（含 R4-2 起訖淨值／真實入金句型），斷言 `container.textContent`
 * 不含 CJK 字元。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ slug: "core" }),
}));

let accountState: { address?: string; chainId?: number; isConnected: boolean } = { isConnected: false };
vi.mock("wagmi", () => ({
  useAccount: () => accountState,
  useConnect: () => ({ connectAsync: vi.fn(), connectors: [{ id: "injected" }] }),
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

vi.mock("@/lib/siwe", () => ({ loginWithSiwe: vi.fn() }));

const getMe = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
}));

import { ApiError } from "@/lib/api";
import { LangProvider } from "@/lib/lang";
import StrategyDetailPage from "./page";

const CJK = /[一-鿿]/;

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => body } as Response;
}

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

function wrapEn(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <LangProvider>{children}</LangProvider>
    </QueryClientProvider>
  );
}

const DETAIL = {
  slug: "core", name: "Filet Core", tagline: "Multi-asset momentum · perpetuals", featured: true,
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
  equity_index: Array.from({ length: 38 }, (_, i) => String(1 + i * 0.005)),
  methodology: {
    start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
    start_equity_usd: "1000.00", end_equity_usd: "1177.70",
    sample_count: 38, annualization_days: 365, risk_free_rate: "0", basis: "perp",
    updated_at: 1756000000,
  },
  as_of: 1756000500,
  sample_days: 72,
  sample_threshold: 30,
  cagr_pct: "45.23",
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("filet_lang", "en");
  push.mockReset();
  getMe.mockReset();
  accountState = { isConnected: false };
});

describe("StrategyDetailPage — EN 模式無 CJK 殘留", () => {
  it("詳情頁（含起訖淨值／真實入金句型）渲染完成後，textContent 不含任何 CJK 字元", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "not logged in", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrapEn(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    const body = document.body.textContent ?? "";
    // R4-2：真實入金本金句型（EN，`methodology.depositPrefix` 起算）。
    expect(body).toContain("$1,000");

    expect(body).not.toMatch(CJK);
  });
});
