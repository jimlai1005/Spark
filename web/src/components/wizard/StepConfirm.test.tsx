import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MyRiskResp, ReferralStatusResp } from "@/lib/api";

vi.mock("wagmi", () => ({
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

const getReferralStatus = vi.fn();
const getMyRisk = vi.fn();
const getReferralMessage = vi.fn();
const submitReferralOptin = vi.fn();
const getLeaderSelectMessage = vi.fn();
const postLeaderSelect = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getReferralStatus: (...a: unknown[]) => getReferralStatus(...a),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getReferralMessage: (...a: unknown[]) => getReferralMessage(...a),
  submitReferralOptin: (...a: unknown[]) => submitReferralOptin(...a),
  getLeaderSelectMessage: (...a: unknown[]) => getLeaderSelectMessage(...a),
  postLeaderSelect: (...a: unknown[]) => postLeaderSelect(...a),
}));

const runReferralOptinFlow = vi.fn();
vi.mock("@/lib/referralFlow", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  runReferralOptinFlow: (...a: unknown[]) => runReferralOptinFlow(...a),
}));

const runLeaderSelectFlow = vi.fn();
vi.mock("@/lib/leaderSelectFlow", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  runLeaderSelectFlow: (...a: unknown[]) => runLeaderSelectFlow(...a),
}));

import { StepConfirm } from "./StepConfirm";

function referralStatus(over: Partial<ReferralStatusResp> = {}): ReferralStatusResp {
  return {
    enabled: true,
    code: "JIMLAI1005",
    signed: false,
    signed_at: null,
    onchain_code: null,
    onchain_error: false,
    ...over,
  };
}

function myRisk(): MyRiskResp {
  return {
    prefs: {} as MyRiskResp["prefs"],
    specs: [],
    defaults: {} as MyRiskResp["defaults"],
    submitted: {} as MyRiskResp["submitted"],
    applied: null,
    halted: null,
    editable: true,
  };
}

function renderStepConfirm() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <StepConfirm
        me={{ address: "0xabc0000000000000000000000000000000000001", account_id: "fabc" }}
        leaderAddress="0xLeader0000000000000000000000000000000001"
        estimatedNotional={1000}
        onDone={() => undefined}
      />
    </QueryClientProvider>,
  );
  return qc;
}

/** 負向斷言前先等 `me-referral` 真的落地，避免只驗到 loading 態而靜默常綠。 */
async function waitReferralLoaded(qc: QueryClient) {
  await waitFor(() => expect(qc.getQueryData(["me-referral"])).toBeDefined());
}

beforeEach(() => {
  vi.clearAllMocks();
  getReferralStatus.mockResolvedValue(referralStatus());
});

describe("StepConfirm ⭐ 推薦碼區塊移進主卡片", () => {
  it("推薦碼區塊在主按鈕之前、且與主按鈕同屬一個 .step-card（DOM 順序驗證）", async () => {
    renderStepConfirm();
    const referralNode = await screen.findByText("推薦碼（選填）");
    const lastCheck = screen.getByText("我理解可隨時撤銷");
    const primaryBtn = screen.getByRole("button", { name: "確認並開始跟單" });
    expect(screen.getByRole("button", { name: "簽署啟用" })).toBeInTheDocument();
    // 勾選框之後、主按鈕之前
    expect(
      lastCheck.compareDocumentPosition(referralNode) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      referralNode.compareDocumentPosition(primaryBtn) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(primaryBtn.closest(".step-card")).not.toBeNull();
    expect(primaryBtn.closest(".step-card")).toBe(referralNode.closest(".step-card"));
  });

  it("enabled:false → 查無「推薦碼（選填）」；主按鈕仍在", async () => {
    getReferralStatus.mockResolvedValue(referralStatus({ enabled: false }));
    const qc = renderStepConfirm();
    await waitReferralLoaded(qc);
    expect(screen.queryByText("推薦碼（選填）")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "確認並開始跟單" })).toBeInTheDocument();
  });

  it("enabled:true, signed:true → 查無「推薦碼（選填）」；主按鈕仍在", async () => {
    getReferralStatus.mockResolvedValue(referralStatus({ signed: true }));
    const qc = renderStepConfirm();
    await waitReferralLoaded(qc);
    expect(screen.queryByText("推薦碼（選填）")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "確認並開始跟單" })).toBeInTheDocument();
  });

  it("點擊「簽署啟用」成功 → 顯示已簽署提示；不呼叫 runLeaderSelectFlow；主按鈕仍在", async () => {
    getMyRisk.mockResolvedValue(myRisk());
    runReferralOptinFlow.mockResolvedValue({ ok: true });
    renderStepConfirm();
    const signBtn = await screen.findByRole("button", { name: "簽署啟用" });
    await userEvent.click(signBtn);
    expect(await screen.findByText("已簽署，引擎會在下一輪套用。")).toBeInTheDocument();
    expect(runLeaderSelectFlow).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "確認並開始跟單" })).toBeInTheDocument();
  });

  it("推薦碼簽署進行中 → 主按鈕停用；簽完（含失敗）解鎖", async () => {
    getMyRisk.mockResolvedValue(myRisk());
    let resolveFlow!: (r: { ok: boolean; kind?: string }) => void;
    runReferralOptinFlow.mockImplementation(() => new Promise((r) => { resolveFlow = r; }));
    renderStepConfirm();
    await checkAll();
    const primaryBtn = screen.getByRole("button", { name: "確認並開始跟單" });
    expect(primaryBtn).toBeEnabled();
    await userEvent.click(await screen.findByRole("button", { name: "簽署啟用" }));
    await waitFor(() => expect(primaryBtn).toBeDisabled());
    resolveFlow({ ok: false, kind: "wallet-rejected" });
    await waitFor(() => expect(primaryBtn).toBeEnabled());
    expect(runLeaderSelectFlow).not.toHaveBeenCalled();
  });

  it("主按鈕簽署進行中 → 「簽署啟用」停用", async () => {
    let resolveFlow!: (r: { ok: boolean }) => void;
    runLeaderSelectFlow.mockImplementation(() => new Promise((r) => { resolveFlow = r; }));
    renderStepConfirm();
    const signBtn = await screen.findByRole("button", { name: "簽署啟用" });
    await checkAll();
    await userEvent.click(screen.getByRole("button", { name: "確認並開始跟單" }));
    await waitFor(() => expect(signBtn).toBeDisabled());
    resolveFlow({ ok: true });
    await waitFor(() => expect(signBtn).toBeEnabled());
    expect(runReferralOptinFlow).not.toHaveBeenCalled();
  });
});

async function checkAll() {
  for (const box of screen.getAllByRole("checkbox")) await userEvent.click(box);
}
