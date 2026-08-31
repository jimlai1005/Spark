import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({ usePathname: () => "/strategies", useRouter: () => ({ push }) }));

const logout = vi.fn();
const getAdminPending = vi.fn();
const getDashboard = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  logout: (...a: unknown[]) => logout(...a),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
}));

const connectAsync = vi.fn();
const signMessageAsync = vi.fn();
let accountState: { address?: string; chainId?: number; isConnected: boolean } = { isConnected: false };
vi.mock("wagmi", () => ({
  useAccount: () => accountState,
  useConnect: () => ({ connectAsync, connectors: [{ id: "injected" }] }),
  useSignMessage: () => ({ signMessageAsync }),
}));

const loginWithSiwe = vi.fn();
vi.mock("@/lib/siwe", () => ({ loginWithSiwe: (...a: unknown[]) => loginWithSiwe(...a) }));

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
    positions: null, risk_controls_enabled: false, updated_at: 1724800000,
  };
}

/** 供保證金告警 pill 測試使用——固定其餘欄位、只變動 `equity.available_pct`。 */
function dashboardWithMargin(availablePct: string): DashboardResp {
  return {
    ...dashboardWithState("following"),
    equity: {
      account_value: "1000.00", margin_used: "100.00", withdrawable: "900.00",
      available_pct: availablePct, ret_30d_pct: "1.0",
    },
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
  push.mockReset();
  connectAsync.mockReset();
  signMessageAsync.mockReset();
  loginWithSiwe.mockReset();
  accountState = { isConnected: false };
});

describe("Header — 未登入導覽（顧問 P1：導覽是信任訊號）", () => {
  it("渲染 wordmark（連回首頁）＋ 三個公開頁籤（文件連結已隱藏）", () => {
    render(wrap(<Header />, qcWithMe(null)));
    const wordmark = screen.getByText("FILET");
    expect(wordmark).toBeInTheDocument();
    expect(wordmark.closest("a")).toHaveAttribute("href", "/");
    expect(navLabels()).toEqual([
      COPY_ZH.nav.strategies,
      COPY_ZH.nav.explore,
      COPY_ZH.nav.how,
      COPY_ZH.nav.security,
    ]);
    expect(screen.getByRole("navigation", { name: COPY_ZH.nav.ariaLabel }))
      .toBeInTheDocument();
    // 「策略」導覽現在跳首頁錨點，不是獨立頁面，故不再帶 aria-current。
    const navStrategies = screen.getAllByRole("link", { name: COPY_ZH.nav.strategies })
      .find((l) => l.getAttribute("href") === "/#strategies");
    expect(navStrategies).toBeDefined();
  });

  it("「綁定錢包」「跟單」頁籤不渲染，導覽 tab 裡沒有連回首頁的「開始」自我連結", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.queryByRole("link", { name: "開始" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "綁定錢包" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "跟單" })).not.toBeInTheDocument();
    // wordmark 本身現在故意連回首頁（Task 1），排除它後 nav tabs 不該再有另一個「/」自我連結。
    const nav = screen.getByRole("navigation", { name: COPY_ZH.nav.ariaLabel });
    const navHrefs = within(nav).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(navHrefs).not.toContain("/");
  });

  it("單一 CTA「登入」按鈕（不是連結——登入是動作，不是導覽）", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.getByRole("button", { name: COPY_ZH.nav.cta })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.cta })).not.toBeInTheDocument();
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
  it("渲染 Dashboard／策略／設定（文件連結已隱藏）；跟單狀態 pill 預設保守值「未跟單」；地址縮寫", () => {
    render(wrap(<Header />, qcWithMe({ address: "0x1A1d000000000000000000000000000000000111", account_id: "fabc" })));
    expect(navLabels()).toEqual([
      COPY_ZH.nav.dashboard,
      COPY_ZH.nav.strategies,
      COPY_ZH.nav.explore,
      COPY_ZH.nav.settings,
    ]);
    // TODO(Task 13) 之前，跟單狀態恆為保守值，不得偽造成「跟單中」。
    expect(screen.getByText(COPY_ZH.nav.pillNotFollowing)).toBeInTheDocument();
    expect(screen.queryByText(COPY_ZH.nav.pillFollowing)).not.toBeInTheDocument();
    expect(screen.getByText("0x1A1d…111")).toBeInTheDocument();
  });

  it("已登入 → 不出現未登入態的 CTA", () => {
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    expect(screen.queryByRole("link", { name: COPY_ZH.nav.cta })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY_ZH.nav.cta })).not.toBeInTheDocument();
  });

  // 2026-09-01 生產事故：登出只 invalidate ["me"] 會讓 admin 探測等其他快取
  // 跨錢包殘留（A 錢包 tabs 亮著、B 錢包點進去 403）→ 改整包 clear。
  it("已登入 → 顯示登出鈕；點擊呼叫 logout 並清空整個 query 快取", async () => {
    logout.mockResolvedValue({ ok: true });
    const qc = qcWithMe({ address: "0xabc", account_id: "fabc" });
    qc.setQueryData(["admin-pending"], { pending: [] }); // 模擬前一個身分的殘留
    render(wrap(<Header />, qc));

    const btn = screen.getByRole("button", { name: COPY_ZH.common.logout });
    await userEvent.click(btn);

    expect(logout).toHaveBeenCalledTimes(1);
    expect(qc.getQueryData(["me"])).toBeUndefined();
    expect(qc.getQueryData(["admin-pending"])).toBeUndefined();
    // 使用者裁決（2026-09-01）：登出一律回首頁。
    expect(push).toHaveBeenCalledWith("/");
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

describe("Header — 保證金告警 pill（M3 round3 Task 6，R2 P2）", () => {
  it("available_pct < 5% → 顯示保證金告警 pill，連向 /dashboard", async () => {
    getDashboard.mockResolvedValue(dashboardWithMargin("0.03"));
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    const pill = await screen.findByText(COPY_ZH.nav.marginAlertPill);
    expect(pill.closest("a")).toHaveAttribute("href", "/dashboard");
  });

  it("available_pct ≥ 5% → 不顯示保證金告警 pill", async () => {
    getDashboard.mockResolvedValue(dashboardWithMargin("0.10"));
    render(wrap(<Header />, qcWithMe({ address: "0xabc", account_id: "fabc" })));
    await screen.findByText(COPY_ZH.nav.pillFollowing);
    expect(screen.queryByText(COPY_ZH.nav.marginAlertPill)).not.toBeInTheDocument();
  });

  it("未登入 → 不顯示保證金告警 pill（不打 dashboard）", () => {
    render(wrap(<Header />, qcWithMe(null)));
    expect(screen.queryByText(COPY_ZH.nav.marginAlertPill)).not.toBeInTheDocument();
    expect(getDashboard).not.toHaveBeenCalled();
  });
});

describe("Header — CTA「登入」走 wagmi injected＋SIWE，依 dashboard 狀態導向（Task 2）", () => {
  it("state=following（非 inactive）→ 登入成功後導向 /dashboard", async () => {
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    getDashboard.mockResolvedValue(dashboardWithState("following"));
    render(wrap(<Header />, qcWithMe(null)));

    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.nav.cta }));

    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalled());
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
    expect(connectAsync).toHaveBeenCalledTimes(1);
  });

  it("state=inactive → 登入成功後導向 /strategies", async () => {
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    getDashboard.mockResolvedValue(dashboardWithState("inactive"));
    render(wrap(<Header />, qcWithMe(null)));

    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.nav.cta }));

    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalled());
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
  });

  it("登入成功但 dashboard 讀取失敗（404／例外）→ 保守導向 /strategies，不視為登入失敗", async () => {
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    getDashboard.mockRejectedValue(new ApiError("client", "not found", 404, "not found"));
    render(wrap(<Header />, qcWithMe(null)));

    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.nav.cta }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
  });

  it("已連錢包（isConnected=true）→ 不重複 connect，直接簽署登入", async () => {
    accountState = { address: "0xabc", chainId: 1, isConnected: true };
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    getDashboard.mockResolvedValue(dashboardWithState("inactive"));
    render(wrap(<Header />, qcWithMe(null)));

    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.nav.cta }));

    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalled());
    expect(connectAsync).not.toHaveBeenCalled();
  });

  it("錢包拒簽 → 顯示拒簽文案、不導向；console 不得洩漏簽章內容", async () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockRejectedValue(
      Object.assign(new Error("User rejected the request."), { name: "UserRejectedRequestError" }),
    );
    render(wrap(<Header />, qcWithMe(null)));

    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.nav.cta }));

    expect(await screen.findByText(COPY_ZH.login.rejected)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
    const loggedText = consoleSpy.mock.calls.flat().map((a) => String(a)).join(" ");
    expect(loggedText).not.toMatch(/signature|0x[0-9a-f]{20,}/i);
    consoleSpy.mockRestore();
  });
});

describe("Header — 語言切換", () => {
  it("點擊 EN 後 nav 字串變英文；點回繁中變回中文", async () => {
    const user = userEvent.setup();
    render(wrap(<Header />, qcWithMe(null)));
    expect(navLabels()).toEqual([
      COPY_ZH.nav.strategies, COPY_ZH.nav.explore, COPY_ZH.nav.how, COPY_ZH.nav.security,
    ]);

    await user.click(screen.getByRole("button", { name: "EN" }));

    const enNav = screen.getByRole("navigation", { name: COPY_EN.nav.ariaLabel });
    expect(within(enNav).getAllByRole("link").map((a) => a.textContent ?? "")).toEqual([
      COPY_EN.nav.strategies, COPY_EN.nav.explore, COPY_EN.nav.how, COPY_EN.nav.security,
    ]);
    expect(screen.getByRole("button", { name: COPY_EN.nav.cta })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "繁中" }));
    expect(screen.getByRole("navigation", { name: COPY_ZH.nav.ariaLabel })).toBeInTheDocument();
  });
});
