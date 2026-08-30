/**
 * StatusCard — 暫停/恢復跟單二次確認彈窗（M3 round4 Task R4-4，使用者裁決 4）。
 * 新檔（不改既有 `StatusCard.test.tsx`）：驗證取消不呼叫 `postPause`、確認才呼叫，
 * 以及暫停態／恢復態各自顯示對應彈窗文案。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardStatus, PauseResp } from "@/lib/api";

const postPause = vi.fn<(a0: "pause" | "resume") => Promise<PauseResp>>();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  postPause: (...a: ["pause" | "resume"]) => postPause(...a),
}));

import { COPY_ZH as COPY } from "@/lib/copy";
import { StatusCard } from "./StatusCard";

const c = COPY.dashboard.status;
const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

function statusWith(overrides: Partial<DashboardStatus>): DashboardStatus {
  return {
    strategy_name: "Filet Alpha", state: "following", following_days: 12,
    signal_source_ok: true,
    guards: {
      scale: { now: "0.2", max: "0.25" },
      leverage: { now: "1.0", max: "3.0" },
      drawdown: { now: null, max: "-0.10", enabled: true },
    },
    ...overrides,
  };
}

function renderCard(status: DashboardStatus, onActionSettled = vi.fn()) {
  return render(
    <StatusCard
      status={status}
      me={ME}
      positions={null}
      closeAllPending={false}
      closeAllFailed={false}
      riskControlsEnabled={true}
      onActionSettled={onActionSettled}
      onCloseAllSubmitted={vi.fn()}
    />,
  );
}

beforeEach(() => {
  postPause.mockReset();
});

describe("StatusCard — 暫停確認彈窗", () => {
  it("點擊「暫停跟單」→ 顯示確認彈窗，尚未呼叫 postPause", async () => {
    renderCard(statusWith({ state: "following" }));
    await userEvent.click(screen.getByRole("button", { name: c.pauseBtn }));

    expect(screen.getByRole("dialog", { name: c.pauseConfirm.title })).toBeInTheDocument();
    expect(postPause).not.toHaveBeenCalled();
  });

  it("彈窗按「取消」→ 關閉彈窗，不呼叫 postPause", async () => {
    renderCard(statusWith({ state: "following" }));
    await userEvent.click(screen.getByRole("button", { name: c.pauseBtn }));
    await userEvent.click(screen.getByRole("button", { name: c.pauseConfirm.cancelBtn }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(postPause).not.toHaveBeenCalled();
  });

  it("彈窗按「確認暫停」→ 呼叫 postPause('pause')，並觸發 onActionSettled", async () => {
    postPause.mockResolvedValue({ ok: true, paused: true, effective: "next_engine_cycle", effective_note: "" });
    const onActionSettled = vi.fn();
    renderCard(statusWith({ state: "following" }), onActionSettled);
    await userEvent.click(screen.getByRole("button", { name: c.pauseBtn }));
    await userEvent.click(screen.getByRole("button", { name: c.pauseConfirm.confirmBtn }));

    await waitFor(() => expect(postPause).toHaveBeenCalledWith("pause"));
    await waitFor(() => expect(onActionSettled).toHaveBeenCalled());
  });

  it("state=paused → 顯示「確認恢復跟單」彈窗文案，確認呼叫 postPause('resume')", async () => {
    postPause.mockResolvedValue({ ok: true, paused: false, effective: "next_engine_cycle", effective_note: "" });
    renderCard(statusWith({ state: "paused" }));
    await userEvent.click(screen.getByRole("button", { name: c.resumeBtn }));

    expect(screen.getByRole("dialog", { name: c.resumeConfirm.title })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: c.resumeConfirm.confirmBtn }));

    await waitFor(() => expect(postPause).toHaveBeenCalledWith("resume"));
  });
});
