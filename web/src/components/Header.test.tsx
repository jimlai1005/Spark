import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/onboarding" }));

const logout = vi.fn();
const getAdminPending = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  logout: (...a: unknown[]) => logout(...a),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
}));

import { ApiError } from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
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
  getAdminPending.mockReset();
  // 預設：一般客戶（後端 403）——ops／admin 連結不該出現
  getAdminPending.mockRejectedValue(new ApiError("client", "非管理員", 403, "非管理員"));
});

describe("Header", () => {
  it("渲染 wordmark 與三個公開 tab，當前頁帶 aria-current", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.getByText("FILET")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "開始" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "綁定錢包" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "跟單" })).toBeInTheDocument();
    // 付費功能全部下架：/capital／/performance／/pricing／/billing 不在導覽列
    expect(screen.queryByRole("link", { name: /方案/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /訂閱管理/ })).not.toBeInTheDocument();
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

  it("已登入且非管理員 → 不會出現訂閱 chip 這種已下架的元素（只有 wordmark／tabs／登出鈕）", () => {
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    expect(screen.queryByRole("link", { name: "專業" })).not.toBeInTheDocument();
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

  it("未登入 → 三個公開頁連結齊備，admin 頁不出現", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(navLabels()).toEqual([COPY.nav.login, COPY.nav.onboarding, COPY.nav.leaders]);
    expect(screen.getByRole("link", { name: COPY.nav.leaders })).toHaveAttribute("href", "/leaders");
    expect(screen.getByRole("link", { name: COPY.nav.onboarding })).toHaveAttribute("href", "/onboarding");
  });

  it("已登入的一般客戶（後端 admin 探測回 403）→ 仍只有三個公開 tab，無 ops／admin", async () => {
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    await screen.findByRole("button", { name: "登出" });
    expect(navLabels()).toEqual([COPY.nav.login, COPY.nav.onboarding, COPY.nav.leaders]);
    // 後端說 403 → 前端就不顯示。這是把後端的答案反映到 UI，不是前端自己判斷。
    expect(screen.queryByRole("link", { name: COPY.nav.ops })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: COPY.nav.admin })).not.toBeInTheDocument();
  });

  it("⭐ 後端放行 admin 探測 → ops／admin 連結出現，全部頁面皆可從導覽抵達", async () => {
    getAdminPending.mockResolvedValue({ pending: [] });
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));

    expect(await screen.findByRole("link", { name: COPY.nav.ops })).toHaveAttribute("href", "/ops");
    expect(screen.getByRole("link", { name: COPY.nav.admin })).toHaveAttribute("href", "/admin");
    // 涵蓋率：admin 身分下，全部 5 條路由都在導覽列上。
    const hrefs = within(screen.getByRole("navigation", { name: "頁面切換" }))
      .getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(new Set(hrefs)).toEqual(new Set(["/", "/onboarding", "/leaders", "/ops", "/admin"]));
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
