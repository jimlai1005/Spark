import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type PendingEntry } from "@/lib/api";

const getAdminPending = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
}));

import AdminPage from "./page";

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const ENTRY: PendingEntry = {
  account_id: "fabc0000000000000000000000000000000000000001",
  user_address: "0xabc0000000000000000000000000000000000001",
  builder_address: "0xbbb0000000000000000000000000000000000002",
  network: "testnet",
  agent_address: "0x1111111111111111111111111111111111111111",
  label: "<script>alert(1)</script>",
};

describe("AdminPage", () => {
  it("渲染 pending 表、六欄齊備；label 以純文字呈現（stored XSS 防護，spec 資料模型）", async () => {
    getAdminPending.mockResolvedValue({ pending: [ENTRY] });
    const { container } = render(wrap(<AdminPage />));
    expect(await screen.findByText(ENTRY.user_address)).toBeInTheDocument();
    expect(screen.getByText(ENTRY.builder_address)).toBeInTheDocument();
    // XSS：label 文字原樣顯示、不注入 DOM
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText(/人工 CLI/)).toBeInTheDocument();
  });

  it("403 → 僅限管理員文案", async () => {
    getAdminPending.mockRejectedValue(new ApiError("client", "非管理員", 403, "非管理員"));
    render(wrap(<AdminPage />));
    expect(await screen.findByText("此頁僅限管理員。")).toBeInTheDocument();
  });

  it("空清單 → 空狀態文案", async () => {
    getAdminPending.mockResolvedValue({ pending: [] });
    render(wrap(<AdminPage />));
    expect(await screen.findByText("目前沒有待核准的項目。")).toBeInTheDocument();
  });
});
