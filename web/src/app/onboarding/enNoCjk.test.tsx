/**
 * `/onboarding` step3（設定風險限制）— EN 模式無繁中殘留（M3 round4 Task R4-4
 * 規格 6）。新檔（不改既有 `page.test.tsx`）：`localStorage.filet_lang=en` 下
 * `READY_STATUS` 直接落在 step3，開啟回撤開關（觸發 `capital.data.note` 的
 * `notesByStatus` 查表路徑），斷言 `container.textContent` 不含 CJK 字元。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MyRiskResp, OnboardStatus } from "@/lib/api";

const push = vi.fn();
const currentSearch = new URLSearchParams({ strategy: "core" });
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useSearchParams: () => currentSearch,
}));

let mockWagmiAccount: { isConnected: boolean; address?: string; chainId?: number };
vi.mock("wagmi", () => ({
  useAccount: () => mockWagmiAccount,
  useConnect: () => ({ connect: vi.fn(), connectors: [{ id: "injected" }], isPending: false }),
  useConnectorClient: () => ({ data: { request: vi.fn() } }),
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
let mockStatus: { data: OnboardStatus | null; refetch: () => void };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
  useOnboardingStatus: () => mockStatus,
}));

const createAgent = vi.fn();
const getMyRisk = vi.fn();
const getMyCapital = vi.fn();
const getMyLeader = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createAgent: (...a: unknown[]) => createAgent(...a),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
  getMyLeader: (...a: unknown[]) => getMyLeader(...a),
}));

const getPublicStrategy = vi.fn();
vi.mock("@/lib/publicApi", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getPublicStrategy: (...a: unknown[]) => getPublicStrategy(...a),
}));

vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: vi.fn(),
}));

import { LangProvider } from "@/lib/lang";
import OnboardingPage from "./page";

const ADDR = "0xabc0000000000000000000000000000000000001";
const CJK = /[一-鿿]/;

function wrapEn(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <LangProvider>{children}</LangProvider>
    </QueryClientProvider>
  );
}

const READY_STATUS: OnboardStatus = {
  address: ADDR, account_id: "fabc",
  agent_address: "0xa", agent_generated: true, builder_fee_approved: true,
  agent_approved: true, funded: true, spot_stranded: null, state: "READY",
  perp_account_value: "1000", min_deposit: "100", deposit_shortfall: "0",
};

const STRATEGY_DETAIL = {
  slug: "core", name: "Filet Core", tagline: "Multi-asset momentum", featured: true,
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
  equity_index: [], methodology: {
    start_date: null, end_date: null, initial_deposit_usd: null,
    start_equity_usd: null, end_equity_usd: null, sample_count: null,
    annualization_days: 365, risk_free_rate: "0", basis: "perp", updated_at: 0,
  },
};

const RISK: MyRiskResp = {
  prefs: {
    enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
    max_total_drawdown_pct: "0", flatten_on_breach: true, cooldown_hours: "12",
  },
  specs: [
    {
      name: "max_drawdown_pct", env: "COPY_MAX_DRAWDOWN_PCT", type: "decimal", group: "risk",
      default: "0.2", recommended: "0.2", min: "0.05", max: "0.5", label: "占位", help: "占位",
    },
  ],
  defaults: {
    enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
    max_total_drawdown_pct: "0", flatten_on_breach: true, cooldown_hours: "12",
  },
  submitted: { issued_at: null }, applied: null, halted: null, editable: true,
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("filet_lang", "en");
  vi.clearAllMocks();
  mockWagmiAccount = { isConnected: true, address: ADDR, chainId: 42161 };
  mockMe = { data: { address: ADDR, account_id: "fabc" }, isLoading: false };
  mockStatus = { data: READY_STATUS, refetch: () => undefined };
  createAgent.mockResolvedValue({ agent_address: "0xa" });
  getPublicStrategy.mockResolvedValue(STRATEGY_DETAIL);
  getMyRisk.mockResolvedValue(RISK);
  // ⭐ status="effective"——命中 `notesByStatus` 查表路徑（同 settings 頁）。
  getMyCapital.mockResolvedValue({
    account_id: "fabc", status: "effective",
    effective: {
      allocated_capital: "0", capital_utilization: "0.25", use_full_equity: true,
      source: "customer_signed", changed_at: "2026-08-27T00:00:00Z", as_of: "2026-08-28T00:00:00Z",
    },
    pending: null, heartbeat: { status: "fresh", at: "2026-08-28T00:00:00Z", age_s: 5, stale_after_s: 120 },
    note: "placeholder（不應渲染，已改用 copy.ts）",
  });
  getMyLeader.mockResolvedValue({
    account_id: "fabc", status: "not_activated", leader_address: null,
    leader_name: null, pending_change: null, note: "placeholder",
  });
});

describe("OnboardingPage step3 — EN 模式無 CJK 殘留", () => {
  it("step3（含開啟回撤開關後的風控細項）渲染完成後，textContent 不含任何 CJK 字元", async () => {
    render(wrapEn(<OnboardingPage />));
    await screen.findByRole("heading", { name: "Set your risk limits" });

    await userEvent.click(screen.getByRole("checkbox", { name: "Enable max-drawdown auto-stop" }));
    await screen.findByText("Max-drawdown auto-stop");
    // capital.data.note → `settings.capital.notesByStatus.effective`（status="effective"）。
    await screen.findByText(
      "This is the principal and utilization ratio the engine is actually using right now.",
    );

    expect(document.body.textContent ?? "").not.toMatch(CJK);
  });
});
