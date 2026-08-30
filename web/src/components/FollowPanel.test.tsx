/**
 * `FollowPanel` — 跟單設定右欄面板元件測試（M3 round4 Task R4-11 項目 1）。
 *
 * 涵蓋：策略模式（`disabledState` 缺席／`pending`）、進階模式（`advancedNote`／
 * `blocked`）兩種形態的渲染，以及 CTA click 回呼、slider 互動。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { FollowPanel } from "./FollowPanel";

const PANEL_COPY = COPY.strategyDetail.panel;

function baseProps() {
  return {
    heading: "跟隨此策略",
    copy: PANEL_COPY,
    leverageDisplay: "3x",
    leverageInfoPrefix: COPY.wizard.leverageInfoPrefix,
    leverageInfoSuffix: COPY.wizard.leverageInfoSuffix,
    scalePct: 25,
    onScalePctChange: vi.fn(),
    ddEnabled: false,
    onDdEnabledChange: vi.fn(),
    ddPct: 20,
    onDdPctChange: vi.fn(),
    phase: "idle" as const,
    error: null,
    onCta: vi.fn(),
  };
}

describe("FollowPanel", () => {
  it("策略模式（無 disabledState）→ 渲染 heading／CTA／footnote，不含 advancedNote", () => {
    render(<FollowPanel {...baseProps()} />);
    expect(screen.getByText("跟隨此策略")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: PANEL_COPY.cta })).toBeInTheDocument();
    expect(screen.getByText(PANEL_COPY.footnote)).toBeInTheDocument();
    expect(screen.getByText("3x")).toBeInTheDocument();
    expect(screen.queryByText(COPY.advanced.gate.body)).not.toBeInTheDocument();
  });

  it("進階模式（advancedNote＋leverageDisplay=—）→ 面板頂部顯示無背書說明", () => {
    render(
      <FollowPanel
        {...baseProps()}
        heading={COPY.traders.panel.heading}
        leverageDisplay="—"
        advancedNote={COPY.advanced.gate.body}
      />,
    );
    expect(screen.getByText(COPY.traders.panel.heading)).toBeInTheDocument();
    expect(screen.getByText(COPY.advanced.gate.body)).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("disabledState=pending（策略頁 listable:false）→ CTA 換成 disabled 按鈕＋說明，不觸發 onCta", () => {
    const onCta = vi.fn();
    render(
      <FollowPanel
        {...baseProps()}
        onCta={onCta}
        disabledState={{ kind: "pending", cta: "暫不開放新跟單", note: "此策略目前暫不開放新跟單" }}
      />,
    );
    const btn = screen.getByTestId("follow-panel-disabled");
    expect(btn).toBeDisabled();
    expect(screen.getByText("此策略目前暫不開放新跟單")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: PANEL_COPY.cta })).not.toBeInTheDocument();
  });

  it("disabledState=blocked（交易員頁 follow_blocked:true）→ 只顯示提示，不渲染任何按鈕", () => {
    render(
      <FollowPanel
        {...baseProps()}
        disabledState={{ kind: "blocked", note: COPY.traders.panel.followBlocked }}
      />,
    );
    expect(screen.getByText(COPY.traders.panel.followBlocked)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("CTA click → 呼叫 onCta", () => {
    const onCta = vi.fn();
    render(<FollowPanel {...baseProps()} onCta={onCta} />);
    fireEvent.click(screen.getByRole("button", { name: PANEL_COPY.cta }));
    expect(onCta).toHaveBeenCalledTimes(1);
  });

  it("投入比例 slider 互動 → 呼叫 onScalePctChange", () => {
    const onScalePctChange = vi.fn();
    render(<FollowPanel {...baseProps()} onScalePctChange={onScalePctChange} />);
    fireEvent.change(screen.getByLabelText(PANEL_COPY.scaleLabel), { target: { value: "60" } });
    expect(onScalePctChange).toHaveBeenCalledWith(60);
  });

  it("phase=connecting → CTA 文字換成連接中", () => {
    render(<FollowPanel {...baseProps()} phase="connecting" />);
    expect(screen.getByRole("button", { name: PANEL_COPY.ctaConnecting })).toBeDisabled();
  });

  it("error 存在 → 顯示錯誤訊息", () => {
    render(<FollowPanel {...baseProps()} error="拒絕簽署" />);
    expect(screen.getByText("拒絕簽署")).toBeInTheDocument();
  });
});
