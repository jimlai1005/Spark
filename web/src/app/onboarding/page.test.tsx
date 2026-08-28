/**
 * `/onboarding` — 統一四步 wizard 測試（Task 10）。
 * 涵蓋：未登入／無 strategy 參數 redirect；步驟條狀態；step3 回撤開關預設關＋
 * 關閉時零 risk API 請求；step4 未全勾送出鈕 disabled；localStorage 續作；
 * `advanced:0x…` 顯示無背書標示；spot_stranded 提示（沿舊版語意保留）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
const getRiskSettingsMessage = vi.fn();
const postMyRisk = vi.fn();
const getMyCapital = vi.fn();
const getCapitalSettingsMessage = vi.fn();
const postCapitalSettings = vi.fn();
const getLeaderSelectMessage = vi.fn();
const postLeaderSelect = vi.fn();
const postVerify = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createAgent: (...a: unknown[]) => createAgent(...a),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getRiskSettingsMessage: (...a: unknown[]) => getRiskSettingsMessage(...a),
  postMyRisk: (...a: unknown[]) => postMyRisk(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
  getCapitalSettingsMessage: (...a: unknown[]) => getCapitalSettingsMessage(...a),
  postCapitalSettings: (...a: unknown[]) => postCapitalSettings(...a),
  getLeaderSelectMessage: (...a: unknown[]) => getLeaderSelectMessage(...a),
  postLeaderSelect: (...a: unknown[]) => postLeaderSelect(...a),
  postVerify: (...a: unknown[]) => postVerify(...a),
}));

const getPublicStrategy = vi.fn();
vi.mock("@/lib/publicApi", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getPublicStrategy: (...a: unknown[]) => getPublicStrategy(...a),
}));

const recoverPersonalSigner = vi.fn();
vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: (...a: unknown[]) => recoverPersonalSigner(...a),
}));

/** 逐字照抄後端 `build_capital_settings_message`（filet/capital_settings.py）版型。 */
function buildCapMessage(
  accountId: string, allocatedCapital: string, capitalUtilization: string, useFullEquity: boolean,
): string {
  const capLine = useFullEquity ? "full account equity" : `${allocatedCapital} USDC`;
  return [
    "Filet: update copy-trading capital allocation", "",
    `Account: ${accountId}`,
    `Allocated Capital: ${capLine}`,
    `Capital Utilization: ${capitalUtilization}`,
    "Nonce: n-cap", "Issued At: 2026-08-28T00:00:00Z",
  ].join("\n");
}

import OnboardingPage from "./page";

const ADDR = "0xabc0000000000000000000000000000000000001";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: ADDR, account_id: "fabc",
    agent_address: null, agent_generated: false, builder_fee_approved: false,
    agent_approved: false, funded: false, spot_stranded: null, state: "IN_PROGRESS",
    perp_account_value: "1000", min_deposit: "100", deposit_shortfall: "100",
    ...over,
  };
}

const READY_STATUS = status({
  agent_address: "0xa", agent_generated: true,
  agent_approved: true, builder_fee_approved: true, funded: true, state: "READY",
});

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
  specs: [
    {
      name: "max_drawdown_pct", env: "COPY_MAX_DRAWDOWN_PCT", type: "decimal", group: "risk",
      default: "0.2", recommended: "0.2", min: "0.05", max: "0.5",
      label: "7 天滾動回撤上限", help: "",
    },
  ],
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
  currentSearch = new URLSearchParams({ strategy: "core" });
  mockWagmiAccount = { isConnected: true, address: "0xAbC0000000000000000000000000000000000001", chainId: 42161 };
  mockMe = { data: { address: ADDR, account_id: "fabc" }, isLoading: false };
  mockStatus = { data: status(), refetch: () => undefined };
  createAgent.mockResolvedValue({ agent_address: "0xa" });
  getPublicStrategy.mockResolvedValue(STRATEGY_DETAIL);
  getMyRisk.mockResolvedValue(RISK);
  recoverPersonalSigner.mockResolvedValue(ADDR.toLowerCase());
  signMessageAsync.mockResolvedValue(`0x${"ab".repeat(65)}`);
  getMyCapital.mockResolvedValue({
    account_id: "fabc", status: "not_activated", effective: null, pending: null,
    heartbeat: null, note: "尚未活化，之後這裡會顯示引擎採用的資金配置。",
  });
  // ⭐ 動態回聲：不論呼叫端送出什麼 scale，都原樣回聲對應的待簽原文——這是
  // 「內容預驗必須通過」的前提（見 lib/capitalSettingsFlow.ts），而非投入比例
  // 這條測試本身要驗的東西。
  getCapitalSettingsMessage.mockImplementation(
    async (allocatedCapital: string, capitalUtilization: string, useFullEquity: boolean) => ({
      message: buildCapMessage("fabc", allocatedCapital, capitalUtilization, useFullEquity),
      nonce: "n-cap", issued_at: "2026-08-28T00:00:00Z", account_id: "fabc",
      allocated_capital: allocatedCapital, capital_utilization: capitalUtilization,
      use_full_equity: useFullEquity,
    }),
  );
  postCapitalSettings.mockResolvedValue({
    ok: true, account_id: "fabc", allocated_capital: "0.00", capital_utilization: "0.2500",
    use_full_equity: true, effective: "next_engine_cycle", effective_note: "下一輪生效。",
    consequences: "不會立即強制再平衡。",
  });
});

describe("OnboardingPage — guard（NOTE 10）", () => {
  it("未登入 → redirect /strategies", async () => {
    mockMe = { data: null, isLoading: false };
    render(wrap(<OnboardingPage />));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
  });

  it("無 strategy 參數 → redirect /strategies", async () => {
    currentSearch = new URLSearchParams();
    render(wrap(<OnboardingPage />));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
  });
});

describe("OnboardingPage — 步驟條狀態", () => {
  it("state 未 READY → step 2 為目前步，step 1 顯示為已完成", async () => {
    render(wrap(<OnboardingPage />));
    const nav = await screen.findByRole("navigation", { name: "開通步驟" });
    const items = nav.querySelectorAll("li");
    expect(items[0].className).toContain("is-done");
    expect(items[1].className).toContain("is-current");
  });
});

describe("OnboardingPage — step 3 風險限制（裁決 1：opt-in）", () => {
  it("回撤開關預設關；關閉狀態下點「前往費用與風險確認」不呼叫任何 risk API", async () => {
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    const nextBtn = await screen.findByRole("button", { name: "前往費用與風險確認" });
    const ddToggle = screen.getByRole("checkbox", { name: /最大回撤自動停止/ });
    expect(ddToggle).not.toBeChecked();

    await userEvent.click(nextBtn);

    expect(getMyRisk).not.toHaveBeenCalled();
    expect(getRiskSettingsMessage).not.toHaveBeenCalled();
    expect(postMyRisk).not.toHaveBeenCalled();
    // 送出後前進到 step 4（確認頁）。
    expect(await screen.findByRole("heading", { name: "確認" })).toBeInTheDocument();
  });
});

describe("OnboardingPage — step 4 費用與風險確認（NOTE 12）", () => {
  function seedAtStep4() {
    localStorage.setItem("filet_onboarding", JSON.stringify({
      address: ADDR, strategy: "core", scale: 25,
      ddEnabled: false, ddPct: 20, step3Confirmed: true,
    }));
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
  }

  it("三條 checkbox 未全勾 → 送出鈕 disabled；全勾後才可送出", async () => {
    seedAtStep4();
    render(wrap(<OnboardingPage />));
    const submitBtn = await screen.findByRole("button", { name: "確認並開始跟單" });
    expect(submitBtn).toBeDisabled();

    const boxes = screen.getAllByRole("checkbox");
    for (const box of boxes) await userEvent.click(box);

    expect(submitBtn).not.toBeDisabled();
  });
});

describe("OnboardingPage — localStorage 續作（NOTE 11）", () => {
  it("已存的 step3Confirmed=true → 重新進入直接停在 step 4（不必重簽風險限制）", async () => {
    localStorage.setItem("filet_onboarding", JSON.stringify({
      address: ADDR, strategy: "core", scale: 40,
      ddEnabled: false, ddPct: 15, step3Confirmed: true,
    }));
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    expect(await screen.findByRole("heading", { name: "確認" })).toBeInTheDocument();
    expect(screen.queryByText("設定你的風險限制")).not.toBeInTheDocument();
  });

  it("續作進度不含任何簽章欄位（不變量 1 的前端鏡射）", () => {
    localStorage.setItem("filet_onboarding", JSON.stringify({
      address: ADDR, strategy: "core", scale: 40,
      ddEnabled: false, ddPct: 15, step3Confirmed: true,
    }));
    const raw = localStorage.getItem("filet_onboarding")!;
    expect(raw).not.toMatch(/signature|message/i);
  });
});

describe("OnboardingPage — advanced:0x… 形式（Task 11 會用）", () => {
  it("顯示位址與「進階模式（無背書）」標示，不當成精選策略卡渲染", async () => {
    currentSearch = new URLSearchParams({ strategy: "advanced:0xdead000000000000000000000000000000dead" });
    render(wrap(<OnboardingPage />));
    expect(await screen.findByText("進階模式（無背書）")).toBeInTheDocument();
    expect(screen.getByText(/dead\.{0,3}|0xde/i)).toBeInTheDocument();
    expect(getPublicStrategy).not.toHaveBeenCalled();
  });
});

describe("OnboardingPage — 資金卡在 spot 的提示（沿舊版語意）", () => {
  it("spot_stranded 為 null → 完全不顯示", async () => {
    mockStatus = { data: status({ spot_stranded: null }), refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    await screen.findByRole("navigation", { name: "開通步驟" });
    expect(document.querySelectorAll(".spot-stranded")).toHaveLength(0);
  });

  it("有卡住的資金 → 顯示金額、門檻與外部連結，且不含任何按鈕", async () => {
    mockStatus = {
      data: status({
        spot_stranded: {
          usdc: "250.5", threshold: "10",
          action_required: "manual_transfer_spot_to_perp",
          note: "你有 250.5 USDC 在 **spot** 錢包。",
        },
      }),
      refetch: () => undefined,
    };
    render(wrap(<OnboardingPage />));
    const box = await screen.findByRole("status", { name: "你有資金停在 spot 錢包" });
    expect(box.textContent).toContain("250.50 USDC");
    expect(box.querySelectorAll("button")).toHaveLength(0);
    const link = screen.getByRole("link", { name: "前往 Hyperliquid 進行劃轉" });
    expect(link).toHaveAttribute("target", "_blank");
  });
});

describe("OnboardingPage — step 3 投入比例（Task 10b：真實簽章流）", () => {
  it("送出走 getCapitalSettingsMessage → 簽名 → postCapitalSettings，簽文原樣傳遞", async () => {
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    const nextBtn = await screen.findByRole("button", { name: "前往費用與風險確認" });
    await userEvent.click(nextBtn);

    await waitFor(() => expect(postCapitalSettings).toHaveBeenCalledTimes(1));
    expect(getCapitalSettingsMessage).toHaveBeenCalledWith("0", "0.25", true);
    const [payload, sig] = postCapitalSettings.mock.calls[0];
    const expectedMessage = buildCapMessage("fabc", "0", "0.25", true);
    // ⭐ 伺服器回聲的原文原樣進錢包、原樣送出——不變量 1（前端不組字串、不改一個字元）。
    expect(payload.message).toBe(expectedMessage);
    expect(signMessageAsync).toHaveBeenCalledWith({ message: expectedMessage });
    expect(sig).toBe(await signMessageAsync.mock.results[0].value);
  });

  it("槓桿改唯讀資訊列——onboarding step 3 不存在任何槓桿 slider", async () => {
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    await screen.findByRole("heading", { name: "設定你的風險限制" });
    expect(document.getElementById("onboard-lev-slider")).toBeNull();
    expect(document.querySelectorAll('input[type="range"]')).toHaveLength(2); // 投入比例 + 回撤
    expect(screen.getByText(/本策略槓桿上限 3x/)).toBeInTheDocument();
  });

  it("GET /api/me/capital 顯示 effective 狀態——生效值以後端投影為準", async () => {
    getMyCapital.mockResolvedValue({
      account_id: "fabc", status: "effective",
      effective: {
        allocated_capital: "0.00", capital_utilization: "0.3000", use_full_equity: true,
        source: "customer_signed", changed_at: "2026-08-27T00:00:00Z", as_of: "2026-08-28T00:00:00Z",
      },
      pending: null, heartbeat: { status: "fresh", at: "2026-08-28T00:00:00Z", age_s: 5, stale_after_s: 120 },
      note: "這是引擎目前實際採用的本金與使用比例。",
    });
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    await screen.findByRole("heading", { name: "設定你的風險限制" });
    expect(await screen.findByText(/30\.0%/)).toBeInTheDocument();
    expect(screen.queryByText("已提交，待引擎套用")).not.toBeInTheDocument();
  });

  it("GET /api/me/capital 顯示 pending 狀態——已提交但尚未確認生效", async () => {
    getMyCapital.mockResolvedValue({
      account_id: "fabc", status: "not_activated",
      effective: null,
      pending: {
        allocated_capital: "0.00", capital_utilization: "0.2500", use_full_equity: true,
        submitted_at: "2026-08-28T00:00:00Z", state: "unconfirmed",
        effective_when: "next_engine_cycle", note: "已簽署，尚未確認生效。",
      },
      heartbeat: null, note: "你的帳號尚未啟用跟單，因此還沒有生效中的資金設定。",
    });
    mockStatus = { data: READY_STATUS, refetch: () => undefined };
    render(wrap(<OnboardingPage />));
    await screen.findByRole("heading", { name: "設定你的風險限制" });
    expect(await screen.findByText("已提交，待引擎套用")).toBeInTheDocument();
  });
});
