/**
 * 全站錯誤頁（error.tsx）：使用者裁決（2026-08-30）——任何 runtime 錯誤都不得
 * 把 stack／錯誤訊息／程式碼曝露到 DOM，只顯示固定文案＋重試。
 */
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LangProvider } from "@/lib/lang";
import { COPY_ZH } from "@/lib/copy";
import ErrorPage from "./error";

function renderPage(err: Error, reset: () => void) {
  return render(
    <LangProvider>
      <ErrorPage error={err} reset={reset} />
    </LangProvider>,
  );
}

describe("ErrorPage — 不曝露錯誤內容", () => {
  it("渲染固定文案，錯誤訊息與 stack 都不出現在 DOM", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const err = new Error("SECRET-INTERNAL param is not iterable");
    err.stack = "SECRET-STACK at FeesCard.tsx:19";
    const { container } = renderPage(err, () => {});
    expect(screen.getByText(COPY_ZH.errorPage.title)).toBeInTheDocument();
    expect(screen.getByText(COPY_ZH.errorPage.desc)).toBeInTheDocument();
    expect(container.textContent).not.toContain("SECRET-INTERNAL");
    expect(container.textContent).not.toContain("SECRET-STACK");
    expect(container.textContent).not.toContain("FeesCard");
    spy.mockRestore();
  });

  it("點「重試」呼叫 reset；「回首頁」連向 /", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const reset = vi.fn();
    renderPage(new Error("boom"), reset);
    fireEvent.click(screen.getByText(COPY_ZH.errorPage.retry));
    expect(reset).toHaveBeenCalledTimes(1);
    expect(screen.getByText(COPY_ZH.errorPage.home).closest("a")).toHaveAttribute("href", "/");
    spy.mockRestore();
  });
});
