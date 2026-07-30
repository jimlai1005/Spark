import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardStatus } from "@/lib/api";

const postVerify = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  postVerify: (...a: unknown[]) => postVerify(...a),
}));

import { StepDeposit } from "./StepDeposit";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: "0xabc", account_id: "fabc", agent_address: "0xa",
    agent_generated: true, builder_fee_approved: true,
    agent_approved: true, funded: false, state: "IN_PROGRESS",
    ...over,
  };
}

beforeEach(() => vi.clearAllMocks());

describe("StepDeposit", () => {
  it("未入金：顯示待入金文案、完成綁定鈕 disabled", () => {
    render(<StepDeposit status={status()} refetchStatus={() => undefined} />);
    expect(screen.getByText(/尚未偵測到足額資金/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "完成綁定" })).toBeDisabled();
  });

  it("已入金：完成綁定 → READY → 顯示自動啟用說明＋前往選 leader（2026-07-30 移除人工審核）", async () => {
    postVerify.mockResolvedValue({ state: "READY" });
    render(<StepDeposit status={status({ funded: true, state: "READY" })}
      refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "完成綁定" }));
    expect(await screen.findByText(/選定後系統會在約一分鐘內自動開始跟單/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往選擇 leader" }))
      .toHaveAttribute("href", "/leaders");
    expect(postVerify).toHaveBeenCalledTimes(1);
  });
});
