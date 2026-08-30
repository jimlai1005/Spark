/**
 * `/settings` — EN 模式無繁中殘留（M3 round4 Task R4-4 規格 6）。
 * 新檔（不改既有 `page.test.tsx`）：`localStorage.filet_lang=en` 下渲染四段
 * （風控／資金配置／授權管理／目前跟隨的策略，風控展開含 `paramLabels` 五個
 * 參數），斷言 `container.textContent` 不含 CJK 字元。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DashboardResp, DashboardStatus, MyCapitalResp, MyLeaderResp, MyRiskResp, OnboardStatus, RiskParamSpec,
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
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
  getStatus: (...a: unknown[]) => getStatus(...a),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
  getMyLeader: (...a: unknown[]) => getMyLeader(...a),
}));

vi.mock("wagmi", () => ({
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: vi.fn(),
}));

import { LangProvider } from "@/lib/lang";
import SettingsPage from "./page";

const ADDR = "0xAbC0000000000000000000000000000000000001";
const ME = { address: ADDR, account_id: "fabc" };
const CJK = /[一-鿿]/;

function wrapEn(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <LangProvider>{children}</LangProvider>
    </QueryClientProvider>
  );
}

// ⭐ 五個封閉列舉 name 全部到齊——確認 `paramLabels` 的 EN 對照表在展開態
// （enabled: true）逐一渲染，label/help 皆走 copy.ts。
const SPECS: RiskParamSpec[] = [
  {
    name: "size_tolerance", env: "COPY_SIZE_TOLERANCE", type: "decimal", group: "tracking",
    default: "0.08", recommended: "0.08", min: "0.02", max: "0.25", label: "占位", help: "占位",
  },
  {
    name: "max_drawdown_pct", env: "COPY_MAX_DRAWDOWN_PCT", type: "decimal", group: "risk",
    default: "0.2", recommended: "0.2", min: "0.05", max: "0.5", label: "占位", help: "占位",
  },
  {
    name: "max_total_drawdown_pct", env: "COPY_MAX_TOTAL_DRAWDOWN_PCT", type: "decimal", group: "risk",
    default: "0.4", recommended: "0.4", min: "0", max: "0.8", label: "占位", help: "占位",
  },
  {
    name: "flatten_on_breach", env: "COPY_FLATTEN_ON_BREACH", type: "bool", group: "risk",
    default: true, recommended: true, min: null, max: null, label: "占位", help: "占位",
  },
  {
    name: "cooldown_hours", env: "COPY_RISK_COOLDOWN_HOURS", type: "decimal", group: "risk", unit: "hours",
    default: "12", recommended: "12", min: "0", max: "168", label: "占位", help: "占位",
  },
];

const RISK: MyRiskResp = {
  prefs: {
    enabled: true, size_tolerance: "0.08", max_drawdown_pct: "0.2",
    max_total_drawdown_pct: "0.4", flatten_on_breach: true, cooldown_hours: "12",
  },
  specs: SPECS,
  defaults: {
    enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
    max_total_drawdown_pct: "0.4", flatten_on_breach: true, cooldown_hours: "12",
  },
  submitted: { issued_at: "2026-08-29T00:00:00Z" },
  applied: {
    controls_enabled: true, source: "customer_signed", changed_at: "2026-08-29T00:00:00Z",
    prefs: {
      enabled: true, size_tolerance: "0.08", max_drawdown_pct: "0.2",
      max_total_drawdown_pct: "0.4", flatten_on_breach: true, cooldown_hours: "12",
    },
  },
  halted: { tripped: false, reason: null, tripped_at: null, resumable: null, residual_exposure: null, cooldown_hours: null, resume_at: null },
  editable: true,
};

const CAPITAL: MyCapitalResp = {
  account_id: "fabc", status: "effective",
  effective: {
    allocated_capital: "0", capital_utilization: "0.25", use_full_equity: true,
    source: "customer_signed", changed_at: "2026-08-01T00:00:00Z", as_of: "2026-08-28T00:00:00Z",
  },
  pending: null,
  heartbeat: { status: "ok", at: "2026-08-28T00:00:00Z", age_s: 5, stale_after_s: 90 },
  note: "占位（不應渲染，已改用 copy.ts）",
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
        drawdown: { now: null, max: "-0.1", enabled: true },
      },
    },
    equity: null, exposure: null, pnl: null, sync: null, fees_month: null,
    positions: [], risk_controls_enabled: true, updated_at: 1724900000,
  };
}

const LEADER: MyLeaderResp = {
  account_id: "fabc", status: "following",
  leader_address: "0x1111111111111111111111111111111111111111",
  leader_name: "Alpha", pending_change: null,
  note: "占位（不應渲染，已改用 copy.ts）",
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("filet_lang", "en");
  push.mockReset();
  getMyRisk.mockReset();
  getMyCapital.mockReset();
  getStatus.mockReset();
  getDashboard.mockReset();
  getMyLeader.mockReset();
  mockMe = { data: ME, isLoading: false };

  getMyRisk.mockResolvedValue(RISK);
  getMyCapital.mockResolvedValue(CAPITAL);
  getStatus.mockResolvedValue(STATUS);
  getDashboard.mockResolvedValue(dashboardResp("following"));
  getMyLeader.mockResolvedValue(LEADER);
});

describe("SettingsPage — EN 模式無 CJK 殘留", () => {
  it("四段（含展開的風控細項）渲染完成後，textContent 不含任何 CJK 字元", async () => {
    render(wrapEn(<SettingsPage />));

    await screen.findByRole("heading", { name: "Risk controls" });
    await screen.findByRole("heading", { name: "Capital allocation" });
    await screen.findByRole("heading", { name: "Authorization" });
    await screen.findByRole("heading", { name: "Strategy you're currently following" });
    // paramLabels 五個參數皆已渲染（展開態，enabled:true）。
    await screen.findByText("7-day rolling drawdown limit");

    expect(document.body.textContent ?? "").not.toMatch(CJK);
  });
});
