import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type BillingPlansResp } from "@/lib/api";

const getBillingPlans = vi.fn();
const postBillingCheckout = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getBillingPlans: (...a: unknown[]) => getBillingPlans(...a),
  postBillingCheckout: (...a: unknown[]) => postBillingCheckout(...a),
}));

import PricingPage from "./page";

function wrap(children: ReactNode, me: { address: string; account_id: string } | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const ME = { address: "0xabc0000000000000000000000000000000000001", account_id: "fabc" };

/** 後端 plan_catalog 的形狀（free 不受限；pro 的兩項付費功能 shipped=false）。 */
function catalog(over: Partial<BillingPlansResp> = {}): BillingPlansResp {
  return {
    billing_enabled: true,
    plans: [
      {
        id: "free",
        name_key: "plans.free.name",
        price_display: null,
        purchasable: false,
        features: [
          { text_key: "plans.feature.copytrade", included: true, shipped: true },
          { text_key: "plans.feature.killswitch", included: true, shipped: true },
          { text_key: "plans.feature.multiLeader", included: false, shipped: false },
          { text_key: "plans.feature.ratioSlider", included: false, shipped: false },
        ],
      },
      {
        id: "pro",
        name_key: "plans.pro.name",
        price_display: "USD 29 / 月",
        purchasable: true,
        features: [
          { text_key: "plans.feature.copytrade", included: true, shipped: true },
          { text_key: "plans.feature.killswitch", included: true, shipped: true },
          { text_key: "plans.feature.multiLeader", included: true, shipped: false },
          { text_key: "plans.feature.ratioSlider", included: true, shipped: false },
        ],
      },
    ],
    ...over,
  };
}

/** jsdom 的 window.location 不可直接賦值 href（會 Not implemented），換成可寫的替身。 */
function stubLocation(): { href: string } {
  const fake = { href: "" };
  Object.defineProperty(window, "location", { value: fake, configurable: true, writable: true });
  return fake;
}

beforeEach(() => {
  getBillingPlans.mockReset();
  postBillingCheckout.mockReset();
});

describe("PricingPage", () => {
  it("⭐ 誠信紅線：included 但 shipped=false 的功能必須標「開發中」，不得看起來可用", async () => {
    getBillingPlans.mockResolvedValue(catalog());
    render(wrap(<PricingPage />, ME));

    // pro 卡的兩個未推出功能各帶一個徽章
    const badges = await screen.findAllByText("開發中，敬請期待");
    expect(badges).toHaveLength(2);

    // 未推出的功能列不得帶「已包含」的勾（那會讓人以為付錢就能用）
    for (const badge of badges) {
      const row = badge.closest("li")!;
      expect(row.className).toContain("is-unshipped");
      expect(row.querySelector('[aria-label="已包含"]')).toBeNull();
    }
    // 頁面另有整體說明，明講訂閱後也還不能使用
    expect(screen.getByText(/訂閱後也還不能使用/)).toBeInTheDocument();
  });

  it("免費層文案講明不限本金、不限跟單；free 卡不顯示「即將開放」按鈕", async () => {
    getBillingPlans.mockResolvedValue(catalog());
    render(wrap(<PricingPage />, ME));

    expect(await screen.findAllByText(/不限本金、不限跟單筆數/)).not.toHaveLength(0);
    expect(screen.getByText("免費使用中，無需訂閱")).toBeInTheDocument();
    // 免費層現在就能用，不得被畫成「即將開放」
    expect(screen.queryByRole("button", { name: "即將開放" })).not.toBeInTheDocument();
  });

  it("price_display 為 null（價格未拍板）→ 顯示「價格待定」，不崩潰、不顯示 0", async () => {
    const data = catalog();
    data.plans[1].price_display = null;
    getBillingPlans.mockResolvedValue(data);
    render(wrap(<PricingPage />, ME));

    expect(await screen.findByText("價格待定")).toBeInTheDocument();
    expect(screen.getByText(/價格尚未定案/)).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("billing_enabled=false → 訂閱鈕停用顯示「即將開放」＋頁面提示", async () => {
    const data = catalog({ billing_enabled: false });
    data.plans[1].purchasable = false; // 後端一併算好（purchasable = price_id && enabled）
    getBillingPlans.mockResolvedValue(data);
    render(wrap(<PricingPage />, ME));

    const btn = await screen.findByRole("button", { name: "即將開放" });
    expect(btn).toBeDisabled();
    expect(screen.getByText("訂閱功能即將開放")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "訂閱" })).not.toBeInTheDocument();
  });

  it("未登入 → 頁面照常渲染（公開端點），訂閱鈕變「請先登入」並連到 /", async () => {
    getBillingPlans.mockResolvedValue(catalog());
    render(wrap(<PricingPage />, null));

    const link = await screen.findByRole("link", { name: "請先登入" });
    expect(link).toHaveAttribute("href", "/");
    expect(screen.queryByRole("button", { name: "訂閱" })).not.toBeInTheDocument();
    // 公開頁：方案內容照樣看得到
    expect(screen.getByText("專業")).toBeInTheDocument();
  });

  it("checkout 成功 → 導向後端回的 checkout_url", async () => {
    getBillingPlans.mockResolvedValue(catalog());
    postBillingCheckout.mockResolvedValue({ checkout_url: "https://checkout.stripe.test/s1" });
    const loc = stubLocation();
    render(wrap(<PricingPage />, ME));

    await userEvent.click(await screen.findByRole("button", { name: "訂閱" }));

    expect(postBillingCheckout).toHaveBeenCalledTimes(1);
    expect(loc.href).toBe("https://checkout.stripe.test/s1");
  });

  it("checkout 409（已有訂閱）→ 顯示對應訊息、不崩潰、不導向", async () => {
    getBillingPlans.mockResolvedValue(catalog());
    postBillingCheckout.mockRejectedValue(new ApiError("client", "已有生效訂閱", 409, "已有生效訂閱"));
    const loc = stubLocation();
    render(wrap(<PricingPage />, ME));

    await userEvent.click(await screen.findByRole("button", { name: "訂閱" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("你已有生效中的訂閱");
    expect(loc.href).toBe("");
    // 失敗後按鈕回到可按狀態（非冪等寫入：重試由使用者決定）
    expect(screen.getByRole("button", { name: "訂閱" })).toBeEnabled();
  });

  it("checkout 501（billing 未啟用）→ 顯示即將開放訊息", async () => {
    getBillingPlans.mockResolvedValue(catalog());
    postBillingCheckout.mockRejectedValue(new ApiError("client", "計費未啟用", 501, "計費未啟用"));
    stubLocation();
    render(wrap(<PricingPage />, ME));

    await userEvent.click(await screen.findByRole("button", { name: "訂閱" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("訂閱功能即將開放");
  });

  it("方案載入失敗 → 顯示載入失敗文案而非空白頁", async () => {
    getBillingPlans.mockRejectedValue(new ApiError("network", "無法連線到伺服器"));
    render(wrap(<PricingPage />, ME));
    expect(await screen.findByText("載入方案資料失敗，請重新整理本頁。")).toBeInTheDocument();
  });
});
