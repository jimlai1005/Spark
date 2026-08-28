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
    perp_account_value: "12.5", min_deposit: "100",
    deposit_shortfall: "87.5",
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

  // 警語必須明講「錢要放在 perps 錢包」：後端的 funded 查的是 perp 帳戶淨值，
  // 只說「轉進你的 Hyperliquid 帳戶」會讓客戶轉到 spot 之後依然被擋而無從診斷。
  it("未入金：警語明確要求 perps 錢包，並說明 spot 的錢不算", () => {
    render(<StepDeposit status={status()} refetchStatus={() => undefined} />);
    const pending = screen.getByText(/尚未偵測到足額資金/);
    expect(pending.textContent).toMatch(/perps（永續合約）/);
    expect(pending.textContent).toMatch(/spot/);
  });

  it("顯示 perps 餘額、門檻與差額（判定值本身，非另外查的數字）", () => {
    render(<StepDeposit status={status()} refetchStatus={() => undefined} />);
    // 金額由 {fmtAmount(x)} USDC 拆成兩個 text node，故斷言容器的 textContent
    // （沿 onboarding/page.test.tsx:126 既有寫法）。
    const facts = screen.getByText("perps 帳戶餘額").closest("dl");
    expect(facts?.textContent).toContain("12.50 USDC");   // 判定值
    expect(facts?.textContent).toContain("100.00 USDC");  // 門檻，取自後端
    expect(facts?.textContent).toContain("87.50 USDC");   // 還差
    expect(screen.getByText("還差")).toBeInTheDocument();
  });

  it("已入金：不顯示差額列（差額只在未達標時有意義）", () => {
    render(<StepDeposit status={status({
      funded: true, perp_account_value: "250", deposit_shortfall: "0",
    })} refetchStatus={() => undefined} />);
    const facts = screen.getByText("perps 帳戶餘額").closest("dl");
    expect(facts?.textContent).toContain("250.00 USDC");
    expect(screen.queryByText("還差")).not.toBeInTheDocument();
  });

  // ⭐ Task 10：不再有「前往選擇 leader」的站外連結——leader（即所選策略）已在
  // onboarding step 1 決定，父層頁面會依伺服器 state 自動把使用者帶往 step 3。
  it("已入金：完成綁定 → READY → 顯示完成訊息，不含任何導出連結", async () => {
    postVerify.mockResolvedValue({ state: "READY" });
    render(<StepDeposit status={status({ funded: true, state: "READY" })}
      refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "完成綁定" }));
    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(postVerify).toHaveBeenCalledTimes(1);
  });
});
