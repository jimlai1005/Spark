import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { StepRisk } from "./StepRisk";

describe("StepRisk（spec 流程 2：四勾全勾才進；8 步動作 3）", () => {
  it("四個勾未全勾時下一步 disabled；全勾後可按 → onConfirm", async () => {
    const onConfirm = vi.fn();
    render(<StepRisk onConfirm={onConfirm} />);
    const next = screen.getByRole("button", { name: "下一步" });
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes).toHaveLength(4);
    expect(next).toBeDisabled();
    await userEvent.click(boxes[0]);
    await userEvent.click(boxes[1]);
    expect(next).toBeDisabled();
    await userEvent.click(boxes[2]);
    expect(next).toBeDisabled();
    await userEvent.click(boxes[3]);
    expect(next).toBeEnabled();
    await userEvent.click(next);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
