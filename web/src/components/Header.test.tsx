import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/onboarding" }));

const logout = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  logout: (...a: unknown[]) => logout(...a),
}));

import { Header } from "./Header";

function wrap(children: ReactNode, qc: QueryClient) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function qcWithMe(me: { address: string; account_id: string } | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return qc;
}

describe("Header", () => {
  it("渲染 wordmark 與三個 tab，當前頁帶 aria-current", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.getByText("FILET")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "登入" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "開通" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "績效" })).toBeInTheDocument();
    // admin 不在 tabs（設計定案 8）
    expect(screen.queryByRole("link", { name: /admin/i })).not.toBeInTheDocument();
  });

  it("未登入 → 不顯示登出鈕", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.queryByRole("button", { name: "登出" })).not.toBeInTheDocument();
  });

  it("已登入 → 顯示登出鈕；點擊呼叫 logout 並清空 [\"me\"] 快取", async () => {
    logout.mockResolvedValue({ ok: true });
    const qc = qcWithMe({ address: "0xabc", account_id: "fabc" });
    render(wrap(<Header />, qc));

    const btn = screen.getByRole("button", { name: "登出" });
    await userEvent.click(btn);

    expect(logout).toHaveBeenCalledTimes(1);
    expect(qc.getQueryState(["me"])?.isInvalidated).toBe(true);
  });
});
