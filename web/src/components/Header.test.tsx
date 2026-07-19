import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/onboarding" }));

const logout = vi.fn();
const getBillingStatus = vi.fn();
const getAdminPending = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  logout: (...a: unknown[]) => logout(...a),
  getBillingStatus: (...a: unknown[]) => getBillingStatus(...a),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
}));

import { ApiError } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { Header } from "./Header";

function wrap(children: ReactNode, qc: QueryClient) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function qcWithMe(me: { address: string; account_id: string } | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return qc;
}

beforeEach(() => {
  getBillingStatus.mockReset();
  // 預設：billing 未啟用（501）——chip 不該出現，既有斷言不受新功能影響
  getBillingStatus.mockRejectedValue(new ApiError("client", "計費未啟用", 501, "計費未啟用"));
  getAdminPending.mockReset();
  // 預設：一般客戶（後端 403）——ops／admin 連結不該出現
  getAdminPending.mockRejectedValue(new ApiError("client", "非管理員", 403, "非管理員"));
});

describe("Header", () => {
  it("渲染 wordmark 與四個 tab，當前頁帶 aria-current", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.getByText("FILET")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "登入" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "開通" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "績效" })).toBeInTheDocument();
    // /pricing 是公開頁，放進導覽列曝光
    expect(screen.getByRole("link", { name: "方案" })).toHaveAttribute("href", "/pricing");
    // admin 不在 tabs（設計定案 8）；/billing 同理，只從 chip 進入
    expect(screen.queryByRole("link", { name: /admin/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "訂閱管理" })).not.toBeInTheDocument();
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

describe("Header 訂閱 chip", () => {
  it("已登入且 billing 啟用 → 顯示狀態 chip，連到 /billing", async () => {
    getBillingStatus.mockResolvedValue({ account_id: "fabc", status: "active", active: true });
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));

    const chip = await screen.findByRole("link", { name: "專業" });
    expect(chip).toHaveAttribute("href", "/billing");
  });

  it("無訂閱（status=none）→ chip 顯示未訂閱，仍可點進 /billing", async () => {
    getBillingStatus.mockResolvedValue({ account_id: "fabc", status: "none", active: false });
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));

    const chip = await screen.findByRole("link", { name: "未訂閱" });
    expect(chip).toHaveAttribute("href", "/billing");
  });

  it("⭐ billing 未啟用（501）→ chip 完全不顯示，Header 其餘元素不受影響", async () => {
    getBillingStatus.mockRejectedValue(new ApiError("client", "計費未啟用", 501, "計費未啟用"));
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));

    // 登出鈕仍在（版面不因缺少 chip 而變形／缺件）
    expect(await screen.findByRole("button", { name: "登出" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "/billing" })).not.toBeInTheDocument();
    for (const label of ["專業", "免費", "未訂閱", "訂閱中", "已取消", "付款逾期"]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });

  it("未登入 → 不打 billing 端點、不顯示 chip", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(getBillingStatus).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "未訂閱" })).not.toBeInTheDocument();
  });
});

/**
 * 導覽涵蓋率：使用者要能從導覽列走到每一頁，不必手打網址。
 * ⭐ 這裡同時釘住「可見性分組 ≠ 授權」：ops／admin 連結的顯示與否只影響看不看得見，
 * 兩頁的後端端點各自掛 `_require_admin`，手打網址仍會 403（各頁 403 分支已有測試）。
 */
describe("Header 導覽涵蓋率", () => {
  /** 導覽列上實際可見的連結文字。 */
  function navLabels(): string[] {
    const nav = screen.getByRole("navigation", { name: "頁面切換" });
    return within(nav).getAllByRole("link").map((a) => a.textContent ?? "");
  }

  it("未登入 → 公開頁連結齊備（含 /leaders 與 /capital），會員與管理頁不出現", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(navLabels()).toEqual([
      COPY.nav.login, COPY.nav.onboarding, COPY.nav.leaders,
      COPY.nav.capital, COPY.nav.performance, COPY.nav.pricing,
    ]);
    expect(screen.getByRole("link", { name: COPY.nav.leaders })).toHaveAttribute("href", "/leaders");
    expect(screen.getByRole("link", { name: COPY.nav.capital })).toHaveAttribute("href", "/capital");
  });

  it("已登入的一般客戶（後端 admin 探測回 403）→ 多出訂閱管理，仍無 ops／admin", async () => {
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    const billing = await screen.findByRole("link", { name: COPY.nav.billing });
    expect(billing).toHaveAttribute("href", "/billing");
    // 後端說 403 → 前端就不顯示。這是把後端的答案反映到 UI，不是前端自己判斷。
    expect(screen.queryByRole("link", { name: COPY.nav.ops })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: COPY.nav.admin })).not.toBeInTheDocument();
  });

  it("⭐ 後端放行 admin 探測 → ops／admin 連結出現，全部頁面皆可從導覽抵達", async () => {
    getAdminPending.mockResolvedValue({ pending: [] });
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));

    expect(await screen.findByRole("link", { name: COPY.nav.ops })).toHaveAttribute("href", "/ops");
    expect(screen.getByRole("link", { name: COPY.nav.admin })).toHaveAttribute("href", "/admin");
    // 涵蓋率：admin 身分下，全部 9 條路由都在導覽列上。
    const hrefs = within(screen.getByRole("navigation", { name: "頁面切換" }))
      .getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(new Set(hrefs)).toEqual(new Set([
      "/", "/onboarding", "/leaders", "/capital",
      "/performance", "/pricing", "/billing", "/ops", "/admin",
    ]));
  });

  it("未登入 → 不打 admin 探測端點（避免必然的 401 噪音）", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(getAdminPending).not.toHaveBeenCalled();
  });

  it("⭐ 導覽文案單一來源：每個 tab 的文字都等於 COPY.nav 的值（不得硬編字面值）", async () => {
    getAdminPending.mockResolvedValue({ pending: [] });
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    await screen.findByRole("link", { name: COPY.nav.admin });
    const allowed = new Set(Object.values(COPY.nav));
    for (const label of navLabels()) expect(allowed).toContain(label);
  });
});
