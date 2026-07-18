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
  it("未入金：顯示待入金文案、送審鈕 disabled", () => {
    render(<StepDeposit status={status()} refetchStatus={() => undefined} />);
    expect(screen.getByText(/尚未偵測到足額資金/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "送出審核" })).toBeDisabled();
  });

  it("已入金：送審 → READY → 顯示已送審文案（設計定案 5：非「啟用」）", async () => {
    postVerify.mockResolvedValue({ state: "READY" });
    render(<StepDeposit status={status({ funded: true, state: "READY" })}
      refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "送出審核" }));
    expect(await screen.findByText(/已送出審核。管理員核准後開始跟單/)).toBeInTheDocument();
    expect(postVerify).toHaveBeenCalledTimes(1);
  });
});
