import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type BillingStatusResp, type BillingStatusValue } from "@/lib/api";

const getBillingStatus = vi.fn();
const postBillingPortal = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getBillingStatus: (...a: unknown[]) => getBillingStatus(...a),
  postBillingPortal: (...a: unknown[]) => postBillingPortal(...a),
}));

import BillingPage from "./page";

const ME = { address: "0xabc0000000000000000000000000000000000001", account_id: "fabc" };

function wrap(children: ReactNode, me: typeof ME | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function status(s: BillingStatusValue): BillingStatusResp {
  return { account_id: "fabc", status: s, active: s === "active" };
}

function stubLocation(): { href: string } {
  const fake = { href: "" };
  Object.defineProperty(window, "location", { value: fake, configurable: true, writable: true });
  return fake;
}

beforeEach(() => {
  getBillingStatus.mockReset();
  postBillingPortal.mockReset();
});

describe("BillingPage", () => {
  it("未登入 → 提示並給回登入頁的連結（不打 billing 端點）", async () => {
    render(wrap(<BillingPage />, null));
    expect(await screen.findByText(/尚未登入/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "回登入頁" })).toHaveAttribute("href", "/");
    expect(getBillingStatus).not.toHaveBeenCalled();
  });

  it("active → 顯示訂閱中狀態與「管理訂閱」按鈕", async () => {
    getBillingStatus.mockResolvedValue(status("active"));
    render(wrap(<BillingPage />, ME));

    expect(await screen.findByText("訂閱中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "管理訂閱" })).toBeInTheDocument();
    // 取消／改付款方式交給 Stripe，不自建
    expect(screen.getByText(/Stripe 的付款入口/)).toBeInTheDocument();
  });

  it("past_due → 顯示逾期提醒，管理入口仍在（讓使用者能去更新付款方式）", async () => {
    getBillingStatus.mockResolvedValue(status("past_due"));
    render(wrap(<BillingPage />, ME));

    expect(await screen.findByText("付款逾期")).toBeInTheDocument();
    expect(screen.getByText(/最近一次扣款未成功/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "管理訂閱" })).toBeInTheDocument();
  });

  it("status=none → 未訂閱狀態＋尚無訂閱空狀態＋前往 /pricing 的連結", async () => {
    getBillingStatus.mockResolvedValue(status("none"));
    render(wrap(<BillingPage />, ME));

    expect(await screen.findByText("未訂閱")).toBeInTheDocument();
    expect(screen.getByText("尚無訂閱")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看方案" })).toHaveAttribute("href", "/pricing");
    expect(screen.queryByRole("button", { name: "管理訂閱" })).not.toBeInTheDocument();
  });

  it("canceled → 顯示已取消，並導向方案頁（不留死路）", async () => {
    getBillingStatus.mockResolvedValue(status("canceled"));
    render(wrap(<BillingPage />, ME));

    expect(await screen.findByText("已取消")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "管理訂閱" })).toBeInTheDocument();
  });

  it("501（billing 未啟用）→「訂閱功能即將開放」，不顯示狀態面板", async () => {
    getBillingStatus.mockRejectedValue(new ApiError("client", "計費未啟用", 501, "計費未啟用"));
    render(wrap(<BillingPage />, ME));

    expect(await screen.findByText("訂閱功能即將開放")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "管理訂閱" })).not.toBeInTheDocument();
    expect(screen.queryByText("尚無訂閱")).not.toBeInTheDocument();
  });

  it("portal 成功 → 導向 Stripe 的 url", async () => {
    getBillingStatus.mockResolvedValue(status("active"));
    postBillingPortal.mockResolvedValue({ url: "https://portal.stripe.test/p1" });
    const loc = stubLocation();
    render(wrap(<BillingPage />, ME));

    await userEvent.click(await screen.findByRole("button", { name: "管理訂閱" }));

    expect(postBillingPortal).toHaveBeenCalledTimes(1);
    expect(loc.href).toBe("https://portal.stripe.test/p1");
  });

  it("portal 409（無訂閱記錄）→ 顯示「尚無訂閱」＋方案頁連結，不當成錯誤", async () => {
    getBillingStatus.mockResolvedValue(status("active"));
    postBillingPortal.mockRejectedValue(
      new ApiError("client", "尚無訂閱記錄，請先訂閱", 409, "尚無訂閱記錄，請先訂閱"),
    );
    const loc = stubLocation();
    render(wrap(<BillingPage />, ME));

    await userEvent.click(await screen.findByRole("button", { name: "管理訂閱" }));

    expect(await screen.findByText("尚無訂閱")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看方案" })).toHaveAttribute("href", "/pricing");
    expect(loc.href).toBe("");
  });

  it("portal 其他失敗 → 顯示可重試訊息，按鈕回到可按狀態", async () => {
    getBillingStatus.mockResolvedValue(status("active"));
    postBillingPortal.mockRejectedValue(new ApiError("upstream", "計費服務錯誤", 502));
    stubLocation();
    render(wrap(<BillingPage />, ME));

    await userEvent.click(await screen.findByRole("button", { name: "管理訂閱" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("無法開啟付款入口");
    expect(screen.getByRole("button", { name: "管理訂閱" })).toBeEnabled();
  });
});
