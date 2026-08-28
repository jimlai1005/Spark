import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/strategies" }));

const logout = vi.fn();
const getAdminPending = vi.fn();
const getDashboard = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  logout: (...a: unknown[]) => logout(...a),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
}));

import { ApiError, type DashboardResp, type DashboardStatus } from "@/lib/api";
import { COPY_EN, COPY_ZH } from "@/lib/copy";
import { LangProvider } from "@/lib/lang";
import { Header } from "./Header";

function wrap(children: ReactNode, qc: QueryClient) {
  return (
    <QueryClientProvider client={qc}>
      <LangProvider>{children}</LangProvider>
    </QueryClientProvider>
  );
}

function qcWithMe(me: { address: string; account_id: string } | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return qc;
}

function navLabels(): string[] {
  const nav = screen.getByRole("navigation", { name: COPY_ZH.nav.ariaLabel });
  return within(nav).getAllByRole("link").map((a) => a.textContent ?? "");
}

/** 固定其餘欄位、只變動 `status.state`，供 pill 三態測試使用。 */
function dashboardWithState(state: DashboardStatus["state"]): DashboardResp {
  return {
    status: {
      strategy_name: "Filet Core", state, following_days: null, signal_source_ok: true,
      guards: {
        scale: { now: null, max: null }, leverage: { now: null, max: null },
        drawdown: { now: null, max: null, enabled: null },
      },
    },
    equity: null, exposure: null, pnl: null, sync: null, fees_month: null,
    positions: null, updated_at: 1724800000,
  };
}

beforeEach(() => {
  getAdminPending.mockReset();
  // 預設：一般客戶（後端 403）——ops／admin 連結不該出現
  getAdminPending.mockRejectedValue(new ApiError("client", "非管理員", 403, "非管理員"));
  // 預設：尚未活化（state="inactive"）→ pill 保守顯示「未跟單」；下方 pill 三態
  // 測試個別覆寫 mockResolvedValue 驗證 following／paused 兩個非預設狀態。
  getDashboard.mockReset();
  getDashboard.mockResolvedValue(dashboardWithState("inactive"));
});

describe("Header — 未登入導覽（顧問 P1：導覽是信任訊號）", () => {
  it("渲染 wordmark ＋ 四個公開頁籤，當前頁帶 aria-current", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.getByText("FILET")).toBeInTheDocument();
    expect(navLabels()).toEqual([
      COPY_ZH.nav.strategies,
      COPY_ZH.nav.how,
      COPY_ZH.nav.security,
      COPY_ZH.nav.docs,
    ]);
    expect(screen.getByRole("navigation", { name: COPY_ZH.nav.ariaLabel }))
      .toBeInTheDocument();
    const strategiesLinks = screen.getAllByRole("link", { name: COPY_ZH.nav.strategies });
    // nav 裡的「策略」帶 aria-current（目前頁為 /strategies）；CTA 另有一顆按鈕不算。
    const navStrategies = strategiesLinks.find((l) => l.getAttribute("href") === "/strategies"
      && l.getAttribute("aria-current") === "page");
    expect(navStrategies).toBeDefined();
  });

  it("「綁定錢包」「跟單」頁籤不渲染，且沒有連回首頁的「開始」自我連結", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.queryByRole("link", { name: "開始" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "綁定錢包" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "跟單" })).not.toBeInTheDocument();
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/");
  });

  it("單一 CTA「查看策略與風險」→ /strategies", () => {
    render(wrap(<Header />, qcWithMe(null)));
    const cta = screen.getByRole("link", { name: COPY_ZH.nav.cta });
    expect(cta).toHaveAttribute("href", "/strategies");
  });

  it("未登入 → 不渲染 Dashboard／設定／跟單狀態 pill／地址／登出鈕", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.dashboard })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.settings })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY_ZH.common.logout })).not.toBeInTheDocument();
    expect(screen.queryByText(COPY_ZH.nav.pillNotFollowing)).not.toBeInTheDocument();
  });

  it("未登入 → 不打 admin 探測端點（避免必然的 401 噪音）", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(getAdminPending).not.toHaveBeenCalled();
  });
});

describe("Header — 已登入導覽", () => {
  it("渲染 Dashboard／策略／設定／文件；跟單狀態 pill 預設保守值「未跟單」；地址縮寫", () => {
    render(wrap(<Header />, qcWithMe({ address: "0x1A1d000000000000000000000000000000000111", account_id: "fabc" })));
    expect(navLabels()).toEqual([
      COPY_ZH.nav.dashboard,
      COPY_ZH.nav.strategies,
      COPY_ZH.nav.settings,
      COPY_ZH.nav.docs,
    ]);
    // TODO(Task 13) 之前，跟單狀態恆為保守值，不得偽造成「跟單中」。
    expect(screen.getByText(COPY_ZH.nav.pillNotFollowing)).toBeInTheDocument();
    expect(screen.queryByText(COPY_ZH.nav.pillFollowing)).not.toBeInTheDocument();
    expect(screen.getByText("0x1A1d…111")).toBeInTheDocument();
  });

  it("已登入 → 不出現未登入態的 CTA", () => {
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.cta })).not.toBeInTheDocument();
  });

  it("已登入 → 顯示登出鈕；點擊呼叫 logout 並清空 [\"me\"] 快取", async () => {
    logout.mockResolvedValue({ ok: true });
    const qc = qcWithMe({ address: "0xabc", account_id: "fabc" });
    render(wrap(<Header />, qc));

    const btn = screen.getByRole("button", { name: COPY_ZH.common.logout });
    await userEvent.click(btn);

    expect(logout).toHaveBeenCalledTimes(1);
    expect(qc.getQueryState(["me"])?.isInvalidated).toBe(true);
  });

  it("後端放行 admin 探測 → ops／admin 連結出現在已登入導覽裡", async () => {
    getAdminPending.mockResolvedValue({ pending: [] });
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));

    expect(await screen.findByRole("link", { name: COPY_ZH.nav.ops })).toHaveAttribute("href", "/ops");
    expect(screen.getByRole("link", { name: COPY_ZH.nav.admin })).toHaveAttribute("href", "/admin");
  });

  it("一般客戶（後端 403）→ 沒有 ops／admin 連結", async () => {
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    await screen.findByRole("button", { name: COPY_ZH.common.logout });
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.ops })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.admin })).not.toBeInTheDocument();
  });
});

describe("Header — 跟單狀態 pill 接上 /api/me/dashboard（Task 14）", () => {
  it("state=following → 「跟單中」", async () => {
    getDashboard.mockResolvedValue(dashboardWithState("following"));
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    expect(await screen.findByText(COPY_ZH.nav.pillFollowing)).toBeInTheDocument();
    expect(screen.queryByText(COPY_ZH.nav.pillNotFollowing)).not.toBeInTheDocument();
  });

  it("state=paused → 「已暫停」", async () => {
    getDashboard.mockResolvedValue(dashboardWithState("paused"));
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    expect(await screen.findByText(COPY_ZH.nav.pillPaused)).toBeInTheDocument();
  });

  it.each(["halted", "inactive"] as const)(
    "state=%s → 保守顯示「未跟單」（不是 following／paused 就不偽造綠燈）",
    async (state) => {
      getDashboard.mockResolvedValue(dashboardWithState(state));
      render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
      await screen.findByText(COPY_ZH.nav.pillNotFollowing);
      expect(screen.queryByText(COPY_ZH.nav.pillFollowing)).not.toBeInTheDocument();
      expect(screen.queryByText(COPY_ZH.nav.pillPaused)).not.toBeInTheDocument();
    },
  );
});

describe("Header — 語言切換", () => {
  it("點擊 EN 後 nav 字串變英文；點回繁中變回中文", async () => {
    const user = userEvent.setup();
    render(wrap(<Header />, qcWithMe(null)));
    expect(navLabels()).toEqual([
      COPY_ZH.nav.strategies, COPY_ZH.nav.how, COPY_ZH.nav.security, COPY_ZH.nav.docs,
    ]);

    await user.click(screen.getByRole("button", { name: "EN" }));

    const enNav = screen.getByRole("navigation", { name: COPY_EN.nav.ariaLabel });
    expect(within(enNav).getAllByRole("link").map((a) => a.textContent ?? "")).toEqual([
      COPY_EN.nav.strategies, COPY_EN.nav.how, COPY_EN.nav.security, COPY_EN.nav.docs,
    ]);
    expect(screen.getByRole("link", { name: COPY_EN.nav.cta })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "繁中" }));
    expect(screen.getByRole("navigation", { name: COPY_ZH.nav.ariaLabel })).toBeInTheDocument();
  });
});
