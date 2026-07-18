import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardStatus } from "@/lib/api";

let mockWagmiAccount: { isConnected: boolean; address?: string; chainId?: number };
const connect = vi.fn();
vi.mock("wagmi", () => ({
  useAccount: () => mockWagmiAccount,
  useConnect: () => ({ connect, connectors: [{ id: "injected" }], isPending: false }),
  useConnectorClient: () => ({ data: { request: vi.fn() } }),
}));
let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
let mockStatus: { data: OnboardStatus | null; refetch: () => void };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
  useOnboardingStatus: () => mockStatus,
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createAgent: vi.fn(async () => ({ agent_address: "0xa" })),
}));

import OnboardingPage from "./page";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: "0xabc0000000000000000000000000000000000001", account_id: "fabc",
    agent_address: null, agent_generated: false, builder_fee_approved: false,
    agent_approved: false, funded: false, state: "IN_PROGRESS",
    ...over,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mockWagmiAccount = { isConnected: true, address: "0xAbC0000000000000000000000000000000000001", chainId: 42161 };
  mockMe = { data: { address: "0xabc0000000000000000000000000000000000001", account_id: "fabc" }, isLoading: false };
  mockStatus = { data: status(), refetch: () => undefined };
});

describe("OnboardingPage 斷點續走渲染", () => {
  it("未登入 → 導回登入的提示", () => {
    mockMe = { data: null, isLoading: false };
    render(<OnboardingPage />);
    expect(screen.getByText(/尚未登入/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "回登入頁" })).toHaveAttribute("href", "/");
  });

  it("已登入未勾風險 → step 2（風險確認）", () => {
    render(<OnboardingPage />);
    expect(screen.getByText("請確認以下事項")).toBeInTheDocument();
  });

  it("勾過風險 → step 3（簽署授權）", () => {
    localStorage.setItem("filet.risk-confirmed.0xabc0000000000000000000000000000000000001", "1");
    render(<OnboardingPage />);
    expect(screen.getByText("簽署兩筆授權")).toBeInTheDocument();
  });

  it("鏈上雙授權已生效 → step 4（入金），未勾風險也直達", () => {
    mockStatus = {
      data: status({ agent_generated: true, agent_address: "0xa", agent_approved: true, builder_fee_approved: true }),
      refetch: () => undefined,
    };
    render(<OnboardingPage />);
    expect(screen.getByText("入金檢查")).toBeInTheDocument();
  });

  it("⭐ session 有效但錢包未連（隔天回來錢包鎖住）→ step 3 顯示重連閘，非死路（Finding 1）", async () => {
    mockWagmiAccount = { isConnected: false };
    localStorage.setItem("filet.risk-confirmed.0xabc0000000000000000000000000000000000001", "1");
    render(<OnboardingPage />);
    // 仍在 step 3（不回退 step 1），但內容是重連閘而非 disabled 簽署鈕
    expect(screen.getByText("錢包未連接")).toBeInTheDocument();
    expect(screen.queryByText("簽署兩筆授權")).not.toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "重新連接錢包" });
    const userEvent = (await import("@testing-library/user-event")).default;
    await userEvent.click(btn);
    expect(connect).toHaveBeenCalledWith({ connector: expect.objectContaining({ id: "injected" }) });
  });

  it("session 有效但錢包未連、鏈上雙授權已生效 → step 4 同樣顯示重連閘", () => {
    mockWagmiAccount = { isConnected: false };
    mockStatus = {
      data: status({ agent_generated: true, agent_address: "0xa", agent_approved: true, builder_fee_approved: true }),
      refetch: () => undefined,
    };
    render(<OnboardingPage />);
    expect(screen.getByText("錢包未連接")).toBeInTheDocument();
    expect(screen.queryByText("入金檢查")).not.toBeInTheDocument();
  });
});
