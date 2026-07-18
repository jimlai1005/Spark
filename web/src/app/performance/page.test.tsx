import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardStatus } from "@/lib/api";

let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
let mockStatus: { data: OnboardStatus | null };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
  useOnboardingStatus: () => mockStatus,
}));

import PerformancePage from "./page";

beforeEach(() => {
  mockMe = { data: { address: "0xabc0000000000000000000000000000000000001", account_id: "fabc" }, isLoading: false };
});

describe("PerformancePage", () => {
  it("READY：綠 chip、授權 pill 亮起、無「前往開通」", () => {
    mockStatus = {
      data: {
        address: "0xabc0000000000000000000000000000000000001", account_id: "fabc",
        agent_address: "0x1111111111111111111111111111111111111111",
        agent_generated: true, builder_fee_approved: true,
        agent_approved: true, funded: true, state: "READY",
      },
    };
    render(<PerformancePage />);
    expect(screen.getByText("已就緒")).toBeInTheDocument();
    expect(screen.getByText("trade-only・無提款權")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "前往開通" })).not.toBeInTheDocument();
  });

  it("進行中：中性 chip + 前往開通連結", () => {
    mockStatus = {
      data: {
        address: "0xabc0000000000000000000000000000000000001", account_id: "fabc",
        agent_address: null, agent_generated: false, builder_fee_approved: false,
        agent_approved: false, funded: false, state: "IN_PROGRESS",
      },
    };
    render(<PerformancePage />);
    expect(screen.getByText("開通進行中")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往開通" })).toHaveAttribute("href", "/onboarding");
  });

  it("未登入 → 導回登入提示", () => {
    mockMe = { data: null, isLoading: false };
    mockStatus = { data: null };
    render(<PerformancePage />);
    expect(screen.getByText(/尚未登入/)).toBeInTheDocument();
  });
});
