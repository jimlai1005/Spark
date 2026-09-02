/**
 * `/onboarding` — opus 審查 S5：query string 帶進來的 `scale` 未經任何驗證就
 * 原樣進 UI（`?scale=999`）。本檔驗證 step 3 的投入比例滑桿把它 clamp 到
 * 合法範圍 [5, 100]（同 `StepRiskLimits.tsx` 的 SCALE_MIN/SCALE_MAX）。
 *
 * mock 設定沿 `page.test.tsx` 既有的同一組（同一個 vi.mock 呼叫，避免兩份
 * 定義漂移；只保留 step 3 這條路徑用得到的部分）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MyRiskResp, OnboardStatus } from "@/lib/api";

const push = vi.fn();
let currentSearch = new URLSearchParams({ strategy: "core" });
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useSearchParams: () => currentSearch,
}));

let mockWagmiAccount: { isConnected: boolean; address?: string; chainId?: number };
const signMessageAsync = vi.fn();
vi.mock("wagmi", () => ({
  useAccount: () => mockWagmiAccount,
  useConnect: () => ({ connect: vi.fn(), connectors: [{ id: "injected" }], isPending: false }),
  useConnectorClient: () => ({ data: { request: vi.fn() } }),
  useSignMessage: () => ({ signMessageAsync }),
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
// ⭐ T10（2026-09-02）：page.tsx 現在會在 READY 且本地無 `step2Verified` 旗標時
// 自動補打一次 `postVerify()`（見該檔頭 `needsAutoVerify`）。這裡的兩段式 render
// 第二階段直接把 `state` 切到 READY（見 `renderReachingStep3`），若不 mock，
// `postVerify` 會落到真實實作打出一個 jsdom 沒有的網路請求並失敗，導致精靈卡在
// step 2、永遠到不了本檔要驗的 step 3——mock 只是補一個成功的 stub，不是本檔
// 要驗的行為。
const postVerify = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createAgent: (...a: unknown[]) => createAgent(...a),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
  postVerify: (...a: unknown[]) => postVerify(...a),
}));

const getPublicStrategy = vi.fn();
vi.mock("@/lib/publicApi", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getPublicStrategy: (...a: unknown[]) => getPublicStrategy(...a),
}));

vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: vi.fn(),
}));

import OnboardingPage from "./page";

const ADDR = "0xabc0000000000000000000000000000000000001";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: ADDR, account_id: "fabc",
    agent_address: "0xa", agent_generated: true, builder_fee_approved: true,
    agent_approved: true, funded: true, spot_stranded: null, state: "READY",
    perp_account_value: "1000", min_deposit: "100", deposit_shortfall: "100",
    ...over,
  };
}

const STRATEGY_DETAIL = {
  slug: "core", name: "Filet Core", tagline: "多資產動能", featured: true,
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
    start_date: null, end_date: null, initial_deposit_usd: null, sample_count: null,
    annualization_days: 365, risk_free_rate: "0", basis: "perp", updated_at: 0,
  },
};

const RISK: MyRiskResp = {
  prefs: {
    enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
    max_total_drawdown_pct: "0", flatten_on_breach: true, cooldown_hours: "12",
  },
  specs: [],
  defaults: {
    enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
    max_total_drawdown_pct: "0", flatten_on_breach: true, cooldown_hours: "12",
  },
  submitted: { issued_at: null },
  applied: null,
  halted: null,
  editable: true,
};

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mockWagmiAccount = { isConnected: true, address: "0xAbC0000000000000000000000000000000000001", chainId: 42161 };
  mockMe = { data: { address: ADDR, account_id: "fabc" }, isLoading: false };
  mockStatus = { data: status(), refetch: () => undefined };
  createAgent.mockResolvedValue({ agent_address: "0xa" });
  postVerify.mockResolvedValue(status());
  getPublicStrategy.mockResolvedValue(STRATEGY_DETAIL);
  getMyRisk.mockResolvedValue(RISK);
  getMyCapital.mockResolvedValue({
    account_id: "fabc", status: "not_activated", effective: null, pending: null,
    heartbeat: null, note: "尚未活化。",
  });
});

/**
 * ⭐ 兩段式 render：第一次渲染時 `state` 不是 READY（停在 step 2），讓「讀 query
 * scale 並 clamp」那個 effect（依賴 `[me.data?.address, strategyParam]`，兩者
 * 從第一幀就緒）先跑完，再把 mock 狀態切成 READY 並 rerender 進入 step 3。
 * 這模擬真實情境（`useOnboardingStatus` 是非同步 react-query，不會在首幀就
 * 是 READY）——`StepRiskLimits` 把 `scale` 存成自己 mount 時的一次性初始值
 * （`useState(initial.scale)`，無 prop-sync effect），若兩者同一幀渲染，
 * 子元件會凍結在 parent 尚未套用 query 值之前的預設值，不是本測試要驗的東西
 * （這個時序缺口在裁決記錄中列為觀察項，不在本次 clamp 修復範圍內）。
 */
async function renderReachingStep3() {
  mockStatus = { data: status({ state: "IN_PROGRESS" }), refetch: () => undefined };
  const view = render(wrap(<OnboardingPage />));
  await screen.findByText("02・連接與授權", { exact: false }).catch(() => undefined);
  mockStatus = { data: status(), refetch: () => undefined };
  view.rerender(wrap(<OnboardingPage />));
  await screen.findByRole("button", { name: "前往費用與風險確認" });
  return view;
}

describe("OnboardingPage — query scale clamp（opus 審查 S5）", () => {
  it("?scale=999（超出上限 100）→ 滑桿 clamp 到 100", async () => {
    currentSearch = new URLSearchParams({ strategy: "core", scale: "999" });
    const { container } = await renderReachingStep3();
    const slider = container.querySelector<HTMLInputElement>("#onboard-scale-slider")!;
    expect(slider.value).toBe("100");
  });

  it("?scale=1（低於下限 5）→ 滑桿 clamp 到 5", async () => {
    currentSearch = new URLSearchParams({ strategy: "core", scale: "1" });
    const { container } = await renderReachingStep3();
    const slider = container.querySelector<HTMLInputElement>("#onboard-scale-slider")!;
    expect(slider.value).toBe("5");
  });

  it("?scale=40（合法範圍內）→ 原樣採用，不被誤夾", async () => {
    currentSearch = new URLSearchParams({ strategy: "core", scale: "40" });
    const { container } = await renderReachingStep3();
    const slider = container.querySelector<HTMLInputElement>("#onboard-scale-slider")!;
    expect(slider.value).toBe("40");
  });
});
