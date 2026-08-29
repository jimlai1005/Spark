import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type OnboardStatus } from "@/lib/api";

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

  // ⭐ 2026-08-29 裁決 6：完成綁定失敗逐條列出未滿足條件，取代單句籠統紅字。
  it("未 READY 且僅 agent 未核准 → 只顯示 agent 授權尚未生效", async () => {
    postVerify.mockResolvedValue(status({
      funded: true, agent_approved: false, builder_fee_approved: true,
      state: "IN_PROGRESS",
    }));
    render(<StepDeposit status={status({ funded: true })} refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "完成綁定" }));
    expect(await screen.findByText("agent 授權尚未生效")).toBeInTheDocument();
    expect(screen.queryByText("builder fee 尚未核准")).not.toBeInTheDocument();
    expect(screen.queryByText("入金未達門檻")).not.toBeInTheDocument();
  });

  it("未 READY 且多項未滿足 → 逐條全部顯示", async () => {
    postVerify.mockResolvedValue(status({
      funded: false, agent_approved: false, builder_fee_approved: false,
      state: "IN_PROGRESS",
    }));
    render(<StepDeposit status={status({ funded: true })} refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "完成綁定" }));
    expect(await screen.findByText("agent 授權尚未生效")).toBeInTheDocument();
    expect(screen.getByText("builder fee 尚未核准")).toBeInTheDocument();
    expect(screen.getByText("入金未達門檻")).toBeInTheDocument();
  });

  it("送出失敗（伺服器拒絕）且送出前三旗標皆已滿足 → 顯示伺服器 detail 原文", async () => {
    postVerify.mockRejectedValue(new ApiError("client", "帳號已被停用", 403, "帳號已被停用"));
    render(<StepDeposit
      status={status({ funded: true, agent_approved: true, builder_fee_approved: true })}
      refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "完成綁定" }));
    expect(await screen.findByText("帳號已被停用")).toBeInTheDocument();
    expect(screen.queryByText("agent 授權尚未生效")).not.toBeInTheDocument();
  });

  it("送出失敗且送出前有未滿足旗標（builder fee）→ 仍逐條列出未滿足條件（不是伺服器原文）", async () => {
    // ⭐ 按鈕的 disabled 條件只看 `status.funded`（既有行為），所以 funded 維持
    // true，用 builder_fee_approved=false 測「送出前已知有旗標未滿足」的分支。
    postVerify.mockRejectedValue(new ApiError("client", "拒絕", 400, "拒絕"));
    render(<StepDeposit
      status={status({ funded: true, agent_approved: true, builder_fee_approved: false })}
      refetchStatus={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "完成綁定" }));
    expect(await screen.findByText("builder fee 尚未核准")).toBeInTheDocument();
    expect(screen.queryByText("拒絕")).not.toBeInTheDocument();
  });
});
