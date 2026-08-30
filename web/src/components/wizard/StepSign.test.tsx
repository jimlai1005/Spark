import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type OnboardStatus } from "@/lib/api";

vi.mock("wagmi", () => ({
  useAccount: () => ({ address: "0xAbC0000000000000000000000000000000000001", chainId: 42161 }),
  useConnectorClient: () => ({ data: { request: vi.fn() } }),
}));
const runApprovalFlow = vi.fn();
vi.mock("@/lib/approvalFlow", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  runApprovalFlow: (...a: unknown[]) => runApprovalFlow(...a),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createAgent: vi.fn(async () => ({ agent_address: "0xa" })),
}));

import { StepSign } from "./StepSign";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: "0xabc0000000000000000000000000000000000001", account_id: "fabc",
    agent_address: "0x1111111111111111111111111111111111111111",
    agent_generated: true, builder_fee_approved: false,
    agent_approved: false, funded: false, state: "IN_PROGRESS",
    perp_account_value: "0", min_deposit: "100", deposit_shortfall: "100",
    spot_stranded: over.spot_stranded ?? null,
    ...over,
  };
}

beforeEach(() => vi.clearAllMocks());

describe("StepSign ⭐", () => {
  it("兩張卡各有以錢包簽署鈕；點擊跑 approvalFlow，成功顯示已送出", async () => {
    runApprovalFlow.mockResolvedValue({ ok: true });
    render(
      <StepSign status={status()} loginAddress="0xabc0000000000000000000000000000000000001"
        refetchStatus={() => undefined} />,
    );
    const buttons = screen.getAllByRole("button", { name: "以錢包簽署" });
    expect(buttons).toHaveLength(2);
    await userEvent.click(buttons[0]);
    expect(runApprovalFlow).toHaveBeenCalledWith(
      expect.objectContaining({ fetchPayload: expect.any(Function) }),
      { expectedSigner: "0xabc0000000000000000000000000000000000001" },
    );
    expect(await screen.findByText("已送出，等待鏈上確認…")).toBeInTheDocument();
  });

  it("signer-mismatch → 顯示帳號不符文案（且有重試鈕）", async () => {
    runApprovalFlow.mockResolvedValue({ ok: false, kind: "signer-mismatch" });
    render(
      <StepSign status={status()} loginAddress="0xabc0000000000000000000000000000000000001"
        refetchStatus={() => undefined} />,
    );
    await userEvent.click(screen.getAllByRole("button", { name: "以錢包簽署" })[0]);
    expect(await screen.findByText(/簽名帳號與登入帳號不符/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重試" })).toBeInTheDocument();
  });

  it("鏈上已生效的卡顯示已生效、無簽署鈕", () => {
    render(
      <StepSign status={status({ agent_approved: true })}
        loginAddress="0xabc0000000000000000000000000000000000001"
        refetchStatus={() => undefined} />,
    );
    expect(screen.getByText("已生效")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "以錢包簽署" })).toHaveLength(1);
  });

  it("紅線 1：本步無任何文字輸入框", () => {
    render(
      <StepSign status={status()} loginAddress="0xabc0000000000000000000000000000000000001"
        refetchStatus={() => undefined} />,
    );
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("agent 未生成 → 進場自動呼叫 createAgent（設計定案 15；8 步動作 4）", async () => {
    const { createAgent } = await import("@/lib/api");
    const refetch = vi.fn();
    render(
      <StepSign status={status({ agent_generated: false, agent_address: null })}
        loginAddress="0xabc0000000000000000000000000000000000001" refetchStatus={refetch} />,
    );
    expect(await screen.findByText("正在準備 agent 金鑰…")).toBeInTheDocument();
    expect(vi.mocked(createAgent)).toHaveBeenCalledTimes(1);
  });

  // 2026-08-29 M3 round2 Task 3：後端兩種 409 語意不同，前端必須分流
  // （之前無條件把兩種 409 都當成功是隱藏 bug）。
  it("409 + code=agent_exists → 視為成功，呼叫 refetchStatus，不顯示錯誤", async () => {
    const { createAgent } = await import("@/lib/api");
    vi.mocked(createAgent).mockRejectedValueOnce(
      new ApiError("client", "已有 agent，不重生", 409, "已有 agent，不重生",
        undefined, "agent_exists"),
    );
    const refetch = vi.fn();
    render(
      <StepSign status={status({ agent_generated: false, agent_address: null })}
        loginAddress="0xabc0000000000000000000000000000000000001" refetchStatus={refetch} />,
    );
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/agent 金鑰狀態不一致/)).not.toBeInTheDocument();
  });

  it("409 + 無 code（向後相容）→ 視為成功，呼叫 refetchStatus", async () => {
    const { createAgent } = await import("@/lib/api");
    vi.mocked(createAgent).mockRejectedValueOnce(
      new ApiError("client", "已有 agent，不重生", 409, "已有 agent，不重生"),
    );
    const refetch = vi.fn();
    render(
      <StepSign status={status({ agent_generated: false, agent_address: null })}
        loginAddress="0xabc0000000000000000000000000000000000001" refetchStatus={refetch} />,
    );
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
  });

  it("409 + code=agent_conflict → 顯示不一致文案，不呼叫 refetchStatus，無重試鈕", async () => {
    const { createAgent } = await import("@/lib/api");
    vi.mocked(createAgent).mockRejectedValueOnce(
      new ApiError("client", "keystore 與 DB 狀態不一致且無法自動復原，請聯絡管理員", 409,
        "keystore 與 DB 狀態不一致且無法自動復原，請聯絡管理員", undefined, "agent_conflict"),
    );
    const refetch = vi.fn();
    render(
      <StepSign status={status({ agent_generated: false, agent_address: null })}
        loginAddress="0xabc0000000000000000000000000000000000001" refetchStatus={refetch} />,
    );
    expect(await screen.findByText(/agent 金鑰狀態不一致/)).toBeInTheDocument();
    expect(refetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "重試" })).not.toBeInTheDocument();
  });

  it("502（keysvc 不可用）→ 顯示可重試文案＋重試鈕，點擊重跑 createAgent", async () => {
    const { createAgent } = await import("@/lib/api");
    vi.mocked(createAgent)
      .mockRejectedValueOnce(new ApiError("upstream", "金鑰服務暫時不可用", 502))
      .mockResolvedValueOnce({ agent_address: "0xa" });
    const refetch = vi.fn();
    render(
      <StepSign status={status({ agent_generated: false, agent_address: null })}
        loginAddress="0xabc0000000000000000000000000000000000001" refetchStatus={refetch} />,
    );
    expect(await screen.findByText(/請點「重試」再試一次/)).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", { name: "重試" });
    await userEvent.click(retryBtn);
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgent)).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/請點「重試」再試一次/)).not.toBeInTheDocument();
  });
});
