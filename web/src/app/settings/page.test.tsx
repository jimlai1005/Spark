/**
 * `/settings` 頁測試（Task 16）。涵蓋：未登入 redirect；四段（風控／資金配置／
 * 授權管理／目前跟隨的策略）各自渲染假資料；風控啟用開關狀態對應 API 回傳；
 * 「暫停跟單」按鈕呼叫 `postPause`；平倉並撤銷入口重用 `CloseAllModal`。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DashboardResp,
  DashboardStatus,
  MyCapitalResp,
  MyLeaderResp,
  MyRiskResp,
  OnboardStatus,
  PauseResp,
  RiskParamSpec,
} from "@/lib/api";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
}));

const getMyRisk = vi.fn();
const getMyCapital = vi.fn();
const getStatus = vi.fn();
const getDashboard = vi.fn();
const getMyLeader = vi.fn();
const postPause = vi.fn<(a0: string) => Promise<PauseResp>>();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
  getStatus: (...a: unknown[]) => getStatus(...a),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
  getMyLeader: (...a: unknown[]) => getMyLeader(...a),
  postPause: (...a: [string]) => postPause(...a),
}));

vi.mock("wagmi", () => ({
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: vi.fn(async () => ADDR.toLowerCase()),
}));

import { COPY_ZH as COPY } from "@/lib/copy";
import SettingsPage from "./page";

const ADDR = "0xAbC0000000000000000000000000000000000001";
const ME = { address: ADDR, account_id: "fabc" };

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const SPECS: RiskParamSpec[] = [
  {
    name: "size_tolerance", env: "COPY_SIZE_TOLERANCE", type: "decimal", group: "tracking",
    default: "0.1", recommended: "0.1", min: "0.01", max: "0.5",
    label: "部位比例容忍度", help: "容忍度說明",
  },
  {
    name: "max_drawdown_pct", env: "COPY_MAX_DRAWDOWN_PCT", type: "decimal", group: "risk",
    default: "0.2", recommended: "0.2", min: "0.05", max: "0.5",
    label: "最大回撤", help: "回撤說明",
  },
  {
    name: "max_total_drawdown_pct", env: "COPY_MAX_TOTAL_DRAWDOWN_PCT", type: "decimal", group: "risk",
    default: "0.3", recommended: "0.3", min: "0.05", max: "0.6",
    label: "總回撤上限", help: "總回撤說明",
  },
  {
    name: "flatten_on_breach", env: "COPY_FLATTEN_ON_BREACH", type: "bool", group: "risk",
    default: true, recommended: true, min: null, max: null,
    label: "觸發時平倉", help: "平倉說明",
  },
  {
    name: "cooldown_hours", env: "COPY_COOLDOWN_HOURS", type: "decimal", group: "risk", unit: "hours",
    default: "12", recommended: "12", min: "1", max: "72",
    label: "冷靜期", help: "冷靜期說明",
  },
];

function riskResp(overrides: Partial<MyRiskResp> = {}): MyRiskResp {
  return {
    prefs: {
      enabled: false, size_tolerance: "0.1", max_drawdown_pct: "0.2",
      max_total_drawdown_pct: "0.3", flatten_on_breach: true, cooldown_hours: "12",
    },
    specs: SPECS,
    defaults: {
      enabled: false, size_tolerance: "0.1", max_drawdown_pct: "0.2",
      max_total_drawdown_pct: "0.3", flatten_on_breach: true, cooldown_hours: "12",
    },
    submitted: { issued_at: null },
    applied: null,
    halted: { tripped: false, reason: null, tripped_at: null, resumable: null, residual_exposure: null, cooldown_hours: null, resume_at: null },
    editable: true,
    ...overrides,
  };
}

const CAPITAL: MyCapitalResp = {
  account_id: "fabc",
  status: "effective",
  effective: {
    allocated_capital: "0", capital_utilization: "0.25", use_full_equity: true,
    source: "customer_signed", changed_at: "2026-08-01T00:00:00Z", as_of: "2026-08-28T00:00:00Z",
  },
  pending: null,
  heartbeat: { status: "ok", at: "2026-08-28T00:00:00Z", age_s: 5, stale_after_s: 90 },
  note: "投入比例目前生效中。",
};

const STATUS: OnboardStatus = {
  address: ADDR, account_id: "fabc", agent_address: "0xAgEnT000000000000000000000000000000001",
  agent_generated: true, builder_fee_approved: true, agent_approved: true, funded: true,
  perp_account_value: "1200", min_deposit: "100", deposit_shortfall: "0",
  spot_stranded: null, state: "READY",
};

function dashboardResp(state: DashboardStatus["state"]): DashboardResp {
  return {
    status: {
      strategy_name: "Filet Core", state, following_days: 10, signal_source_ok: true,
      guards: {
        scale: { now: "0.2", max: "0.25" }, leverage: { now: "1.2", max: "3.0" },
        drawdown: { now: null, max: "-0.1", enabled: false },
      },
    },
    equity: null, exposure: null, pnl: null, sync: null, fees_month: null,
    positions: [{ symbol: "ETH", side: "long", leverage: "5", margin_mode: "cross", value: "100", upnl: "1", entry: "10", mark: "10.1", deviation_pct: null }],
    updated_at: 1724900000,
  };
}

const LEADER: MyLeaderResp = {
  account_id: "fabc", status: "following",
  leader_address: "0x1111111111111111111111111111111111111111",
  leader_name: "Alpha", pending_change: null,
  note: "你目前跟隨 Alpha。",
};

beforeEach(() => {
  push.mockReset();
  getMyRisk.mockReset();
  getMyCapital.mockReset();
  getStatus.mockReset();
  getDashboard.mockReset();
  getMyLeader.mockReset();
  postPause.mockReset();
  mockMe = { data: ME, isLoading: false };

  getMyRisk.mockResolvedValue(riskResp());
  getMyCapital.mockResolvedValue(CAPITAL);
  getStatus.mockResolvedValue(STATUS);
  getDashboard.mockResolvedValue(dashboardResp("following"));
  getMyLeader.mockResolvedValue(LEADER);
});

describe("SettingsPage — guard", () => {
  it("未登入 → redirect /strategies", async () => {
    mockMe = { data: null, isLoading: false };
    render(wrap(<SettingsPage />));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
  });
});

describe("SettingsPage — 四段渲染", () => {
  it("風控設定／資金配置／授權管理／目前跟隨的策略 都渲染出對應內容", async () => {
    render(wrap(<SettingsPage />));

    expect(await screen.findByRole("heading", { name: COPY.settings.risk.title })).toBeInTheDocument();
    expect(screen.getByText(SPECS[0].label)).toBeInTheDocument(); // tracking 組，不受風控開關影響

    expect(await screen.findByRole("heading", { name: COPY.settings.capital.title })).toBeInTheDocument();
    expect(screen.getByText("25%")).toBeInTheDocument();

    expect(await screen.findByRole("heading", { name: COPY.settings.auth.title })).toBeInTheDocument();
    expect(screen.getByText(/0xAgEnT/)).toBeInTheDocument();

    expect(await screen.findByRole("heading", { name: COPY.settings.leader.title })).toBeInTheDocument();
    expect(screen.getAllByText(/Alpha/).length).toBeGreaterThan(0);

    expect(screen.getByRole("link", { name: COPY.settings.leader.changeStrategyBtn }))
      .toHaveAttribute("href", "/strategies");
    expect(screen.getByRole("link", { name: COPY.settings.leader.advancedModeBtn }))
      .toHaveAttribute("href", "/advanced");
  });
});

describe("SettingsPage — 風控開關狀態對應 API mock", () => {
  it("prefs.enabled=false → 啟用 checkbox 未勾選", async () => {
    getMyRisk.mockResolvedValue(riskResp({ prefs: { ...riskResp().prefs, enabled: false } }));
    render(wrap(<SettingsPage />));
    const checkbox = await screen.findByRole("checkbox", { name: COPY.settings.risk.enableLabel });
    expect(checkbox).not.toBeChecked();
  });

  it("prefs.enabled=true → 啟用 checkbox 勾選，且風控細項展開", async () => {
    getMyRisk.mockResolvedValue(riskResp({ prefs: { ...riskResp().prefs, enabled: true } }));
    render(wrap(<SettingsPage />));
    const checkbox = await screen.findByRole("checkbox", { name: COPY.settings.risk.enableLabel });
    expect(checkbox).toBeChecked();
    expect(screen.getByText(SPECS[2].label)).toBeInTheDocument(); // max_total_drawdown_pct 只在展開時顯示
  });
});

describe("SettingsPage — 暫停跟單（Task 15 postPause）", () => {
  it("點擊「暫停跟單」→ 呼叫 postPause('pause')", async () => {
    postPause.mockResolvedValue({ ok: true, paused: true, effective: "next_engine_cycle", effective_note: "" });
    render(wrap(<SettingsPage />));

    const btn = await screen.findByRole("button", { name: COPY.settings.auth.pauseBtn });
    fireEvent.click(btn);

    await waitFor(() => expect(postPause).toHaveBeenCalledWith("pause"));
  });

  it("state=paused → 顯示「恢復跟單」，點擊呼叫 postPause('resume')", async () => {
    getDashboard.mockResolvedValue(dashboardResp("paused"));
    postPause.mockResolvedValue({ ok: true, paused: false, effective: "next_engine_cycle", effective_note: "" });
    render(wrap(<SettingsPage />));

    const btn = await screen.findByRole("button", { name: COPY.settings.auth.resumeBtn });
    fireEvent.click(btn);

    await waitFor(() => expect(postPause).toHaveBeenCalledWith("resume"));
  });

  it("state=inactive → 不渲染暫停/平倉按鈕，顯示「沒有可操作的動作」", async () => {
    getDashboard.mockResolvedValue(dashboardResp("inactive"));
    render(wrap(<SettingsPage />));

    await screen.findByRole("heading", { name: COPY.settings.auth.title });
    expect(screen.queryByRole("button", { name: COPY.settings.auth.pauseBtn })).not.toBeInTheDocument();
    expect(screen.getByText(COPY.settings.auth.noEngineNote)).toBeInTheDocument();
  });
});

describe("SettingsPage — 平倉並撤銷入口複用 CloseAllModal", () => {
  it("點擊「平倉並撤銷授權」→ 開啟同一個 CloseAllModal（列出持倉＋不可逆警語）", async () => {
    render(wrap(<SettingsPage />));

    const btn = await screen.findByRole("button", { name: COPY.settings.auth.closeAllBtn });
    fireEvent.click(btn);

    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText(COPY.dashboard.status.closeAllModal.title)).toBeInTheDocument();
    expect(screen.getByText(/ETH/)).toBeInTheDocument();
  });
});
