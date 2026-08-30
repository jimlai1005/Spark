/**
 * `components/Toast.tsx`（M3 round3 Task 8，R2·P0）：右下角浮動通知——
 * 8 秒自動消失（fake timers）＋手動關閉皆呼叫 `onDismiss`；`message` 更新
 * 重新起算 8 秒。父層（`settings/page.tsx`）負責在 `onDismiss` 裡把錯誤狀態
 * 清空，本測試只驗證 `Toast` 自己的計時與互動邏輯，不牽扯簽章流程。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Toast } from "./Toast";

describe("Toast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("8 秒後自動呼叫 onDismiss", () => {
    const onDismiss = vi.fn();
    render(<Toast message="測試錯誤訊息" onDismiss={onDismiss} dismissLabel="關閉" />);
    expect(screen.getByText("測試錯誤訊息")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(7999);
    });
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("手動點擊關閉按鈕 → 立即呼叫 onDismiss（不等 8 秒）", () => {
    const onDismiss = vi.fn();
    render(<Toast message="測試錯誤訊息" onDismiss={onDismiss} dismissLabel="關閉" />);

    fireEvent.click(screen.getByRole("button", { name: "關閉" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("message 變了 → 重新起算 8 秒（舊 timer 不會提早觸發）", () => {
    const onDismiss = vi.fn();
    const { rerender } = render(<Toast message="第一則" onDismiss={onDismiss} dismissLabel="關閉" />);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    rerender(<Toast message="第二則" onDismiss={onDismiss} dismissLabel="關閉" />);

    act(() => {
      vi.advanceTimersByTime(5000); // 累計 10s，但第二則只過了 5s
    });
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(3000); // 第二則滿 8s
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
