/** ConfirmDialog — 通用輕量確認彈窗（M3 round4 Task R4-4）單元測試。 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog";

describe("ConfirmDialog", () => {
  it("渲染標題／內文／按鈕文案；按「取消」只呼叫 onCancel", async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        title="確認暫停跟單" body="暫停後不再開新倉。"
        confirmLabel="確認暫停" cancelLabel="取消"
        onConfirm={onConfirm} onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("dialog", { name: "確認暫停跟單" })).toBeInTheDocument();
    expect(screen.getByText("暫停後不再開新倉。")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("按「確認」只呼叫 onConfirm", async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        title="確認恢復跟單" body="恢復正常跟單。"
        confirmLabel="確認恢復" cancelLabel="取消"
        onConfirm={onConfirm} onCancel={onCancel}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "確認恢復" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("busy=true → 兩顆按鈕皆 disabled", () => {
    render(
      <ConfirmDialog
        title="t" body="b" confirmLabel="確認" cancelLabel="取消" busy
        onConfirm={vi.fn()} onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "確認" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
  });
});
