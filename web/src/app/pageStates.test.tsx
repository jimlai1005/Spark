/**
 * 逐頁狀態涵蓋：**每一條路由**在「未登入」與「已登入但未活化」兩種狀態下都必須
 * 給出可讀的畫面。
 *
 * ⭐ 為什麼獨立成一個檔案，而不是散進各頁的 test：這裡要驗的不是任何單一頁的功能，
 * 而是一條橫切的不變式——「導覽列上點得到的每一頁，在最常見的兩種未就緒狀態下都
 * 不是白畫面」。散進九個檔案就沒有人在看它是否還**齊全**：新增一條路由時，這裡的
 * ROUTES 少一筆會被 `導覽列上的每條路由都在本檔受測` 這條斷言抓出來。
 *
 * 兩條通用底線（每頁每狀態都跑）：
 *   1. 畫面有內容（不是白畫面）；
 *   2. 畫面上不出現 `undefined`／`NaN`／`[object Object]`——把缺席的資料渲染成字串，
 *      比留白更糟：它看起來像一個真的值。
 * 再加上每頁各自的具體文案斷言（光有「不是白畫面」會讓錯誤頁也算過關）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BillingPlansResp, LeadersResp, OnboardStatus } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/",
}));

vi.mock("wagmi", () => ({
  useAccount: () => ({ isConnected: false }),
  useConnect: () => ({ connectAsync: vi.fn(), connectors: [{ id: "injected" }] }),
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

vi.mock("@/lib/siwe", () => ({ loginWithSiwe: vi.fn() }));

// ⭐ 每支 mock 都獨立宣告、且以 `(...a) => fn(...a)` 轉呼：vi.mock 的 factory 會被
// 提升到 import 之前執行，任何在 factory 內**立即求值**的外部變數（例如遍歷一個
// 物件的鍵）都會炸 "Cannot access before initialization"。轉呼把求值延到呼叫時。
const getMe = vi.fn();
const getStatus = vi.fn();
const getLeaders = vi.fn();
const getBillingPlans = vi.fn();
const getBillingStatus = vi.fn();
const getAdminPending = vi.fn();
const getOpsCustomers = vi.fn();
const getOpsRevenue = vi.fn();
const getOpsSubscriptions = vi.fn();
const getOpsTradeQuality = vi.fn();
const getOpsHealth = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getStatus: (...a: unknown[]) => getStatus(...a),
  getLeaders: (...a: unknown[]) => getLeaders(...a),
  getBillingPlans: (...a: unknown[]) => getBillingPlans(...a),
  getBillingStatus: (...a: unknown[]) => getBillingStatus(...a),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
  getOpsCustomers: (...a: unknown[]) => getOpsCustomers(...a),
  getOpsRevenue: (...a: unknown[]) => getOpsRevenue(...a),
  getOpsSubscriptions: (...a: unknown[]) => getOpsSubscriptions(...a),
  getOpsTradeQuality: (...a: unknown[]) => getOpsTradeQuality(...a),
  getOpsHealth: (...a: unknown[]) => getOpsHealth(...a),
}));

/** 全部 mock 的集合，供 reset 與批次設定使用（在 factory 之外求值，不受提升影響）。 */
const api = {
  getMe, getStatus, getLeaders, getBillingPlans, getBillingStatus, getAdminPending,
  getOpsCustomers, getOpsRevenue, getOpsSubscriptions, getOpsTradeQuality, getOpsHealth,
};

import { ApiError } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { Header } from "@/components/Header";
import AdminPage from "./admin/page";
import BillingPage from "./billing/page";
import CapitalPage from "./capital/page";
import LeadersPage from "./leaders/page";
import LoginPage from "./page";
import OnboardingPage from "./onboarding/page";
import OpsPage from "./ops/page";
import PerformancePage from "./performance/page";
import PricingPage from "./pricing/page";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const unauthorized = () => new ApiError("auth", "未登入", 401, "未登入");
const forbidden = () => new ApiError("client", "非管理員", 403, "非管理員");
const billingOff = () => new ApiError("client", "計費未啟用", 501, "計費未啟用");

/** 已登入但**尚未活化**：授權沒簽完、沒入金，後端 /api/onboard/status 照樣回進度物件。 */
const NOT_ACTIVATED: OnboardStatus = {
  address: ME.address,
  account_id: ME.account_id,
  agent_address: null,
  agent_generated: false,
  builder_fee_approved: false,
  agent_approved: false,
  funded: false,
  state: "IN_PROGRESS",
};

/** 方案目錄。multiLeader／ratioSlider 已 shipped（後端 commit 0d68fcd）。 */
const PLANS: BillingPlansResp = {
  billing_enabled: false,
  plans: [
    {
      id: "pro",
      name_key: "plans.pro.name",
      price_display: "USD 29 / 月",
      purchasable: true,
      features: [
        { text_key: "plans.feature.copytrade", included: true, shipped: true },
        { text_key: "plans.feature.multiLeader", included: true, shipped: true },
        { text_key: "plans.feature.ratioSlider", included: true, shipped: true },
      ],
    },
  ],
};

const LEADERS = {
  leaders: [
    {
      address: "0x1111111111111111111111111111111111111111",
      name: "Alpha",
      description: "多幣種網格",
      account_value: "125000.5",
      total_ntl_pos: "340000",
      unrealized_pnl: "-1200.25",
      position_count: 4,
    },
  ],
  stats_available: true,
  stats_day: "2026-07-18",
  stats_as_of: "2026-07-18T00:10:03+00:00",
  note: null,
} as LeadersResp;

function wrap(children: ReactNode, me: typeof ME | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** 通用底線：有內容、且沒有把缺席資料渲染成字面值。 */
function assertReadable(container: HTMLElement) {
  const text = container.textContent ?? "";
  expect(text.trim().length).toBeGreaterThan(0);
  expect(text).not.toMatch(/undefined|NaN|\[object Object\]/);
}

/** 導覽列點得到的路由 → 本檔受測的頁面元件。新增路由必須同步加進來。 */
const ROUTES: { path: string; name: string; el: () => ReactNode }[] = [
  { path: "/", name: "登入頁", el: () => <LoginPage /> },
  { path: "/onboarding", name: "開通頁", el: () => <OnboardingPage /> },
  { path: "/leaders", name: "跟單對象頁", el: () => <LeadersPage /> },
  { path: "/capital", name: "資金配置頁", el: () => <CapitalPage /> },
  { path: "/performance", name: "績效頁", el: () => <PerformancePage /> },
  { path: "/pricing", name: "方案頁", el: () => <PricingPage /> },
  { path: "/billing", name: "訂閱管理頁", el: () => <BillingPage /> },
  { path: "/ops", name: "營運頁", el: () => <OpsPage /> },
  { path: "/admin", name: "待核准頁", el: () => <AdminPage /> },
];

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  // 方案目錄是公開端點：兩種狀態下都拿得到。
  api.getBillingPlans.mockResolvedValue(PLANS);
});

/** 未登入：所有需要 session 的端點一律 401。 */
function mockLoggedOut() {
  api.getMe.mockRejectedValue(unauthorized());
  for (const k of ["getStatus", "getLeaders", "getBillingStatus", "getAdminPending",
                   "getOpsCustomers", "getOpsRevenue", "getOpsSubscriptions",
                   "getOpsTradeQuality", "getOpsHealth"] as const) {
    api[k].mockRejectedValue(unauthorized());
  }
}

/** 已登入的一般客戶，尚未活化：session 有效，但沒有 follower、非管理員。 */
function mockLoggedInNotActivated() {
  api.getMe.mockResolvedValue(ME);
  api.getStatus.mockResolvedValue(NOT_ACTIVATED);
  api.getLeaders.mockResolvedValue(LEADERS);
  api.getBillingStatus.mockRejectedValue(billingOff());
  api.getAdminPending.mockRejectedValue(forbidden());
  for (const k of ["getOpsCustomers", "getOpsRevenue", "getOpsSubscriptions",
                   "getOpsTradeQuality", "getOpsHealth"] as const) {
    api[k].mockRejectedValue(forbidden());
  }
}

describe("逐頁狀態｜未登入", () => {
  beforeEach(mockLoggedOut);

  for (const r of ROUTES) {
    it(`${r.path}（${r.name}）→ 有可讀畫面，無 undefined`, async () => {
      const { container } = render(wrap(r.el(), null));
      await waitFor(() => assertReadable(container));
    });
  }

  it("需要登入的客戶頁一律給「尚未登入」提示，不是空白", async () => {
    for (const r of ROUTES.filter((x) => ["/onboarding", "/leaders", "/capital",
                                          "/performance", "/billing"].includes(x.path))) {
      const { unmount } = render(wrap(r.el(), null));
      expect(await screen.findByText(COPY.common.notLoggedIn),
             `${r.path} 應顯示尚未登入提示`).toBeInTheDocument();
      unmount();
    }
  });

  it("管理端頁未登入 → 顯示提示（後端 401；前端不自行判斷授權）", async () => {
    const { unmount } = render(wrap(<OpsPage />, null));
    expect(await screen.findByText(COPY.common.notLoggedIn)).toBeInTheDocument();
    unmount();
    render(wrap(<AdminPage />, null));
    expect(await screen.findByText(COPY.common.notLoggedIn)).toBeInTheDocument();
  });

  it("公開頁（/ 與 /pricing）未登入仍正常呈現，不是錯誤畫面", async () => {
    const { unmount } = render(wrap(<LoginPage />, null));
    expect(screen.getByRole("button", { name: COPY.login.connect })).toBeInTheDocument();
    unmount();
    render(wrap(<PricingPage />, null));
    expect(await screen.findByText(COPY.billing.pricingTitle)).toBeInTheDocument();
  });
});

describe("逐頁狀態｜已登入但未活化（沒有 follower）", () => {
  beforeEach(mockLoggedInNotActivated);

  for (const r of ROUTES) {
    it(`${r.path}（${r.name}）→ 有可讀畫面，無 undefined`, async () => {
      const { container } = render(wrap(r.el(), ME));
      await waitFor(() => assertReadable(container));
    });
  }

  it("⭐ /leaders 未活化 → 明說尚未開通並指路，同時仍看得到 leader 清單", async () => {
    render(wrap(<LeadersPage />, ME));
    expect(await screen.findByText(COPY.common.notActivated)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.common.goOnboarding }))
      .toHaveAttribute("href", "/onboarding");
    // 誠信不變式不受影響：清單照樣呈現，使用者看得到自己在選什麼。
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("⭐ /capital 未活化 → 明說尚未開通並指路，設定表單仍可瀏覽", async () => {
    render(wrap(<CapitalPage />, ME));
    expect(await screen.findByText(COPY.common.notActivated)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.common.goOnboarding }))
      .toHaveAttribute("href", "/onboarding");
  });

  it("/performance 未活化 → 顯示進行中狀態與前往開通的入口，不顯示假數字", async () => {
    const { container } = render(wrap(<PerformancePage />, ME));
    expect(await screen.findByText(COPY.perf.stateInProgress)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.perf.goOnboarding }))
      .toHaveAttribute("href", "/onboarding");
    // agent 尚未產生 → 顯示「尚未產生」而不是 null／undefined
    expect(container.textContent).toContain(COPY.perf.agentNone);
  });

  it("/billing 在 billing 未啟用（501）→ 降級為未啟用說明，不是錯誤畫面", async () => {
    render(wrap(<BillingPage />, ME));
    expect(await screen.findByText(COPY.billing.disabledNote)).toBeInTheDocument();
    expect(screen.queryByText(COPY.billing.errors.loadFailed)).not.toBeInTheDocument();
  });

  it("一般客戶開 /ops、/admin → 後端 403 → 「僅限管理員」，不是白畫面", async () => {
    const { unmount } = render(wrap(<OpsPage />, ME));
    expect(await screen.findByText(COPY.ops.forbidden)).toBeInTheDocument();
    unmount();
    render(wrap(<AdminPage />, ME));
    expect(await screen.findByText(COPY.admin.forbidden)).toBeInTheDocument();
  });
});

describe("導覽涵蓋率（與 Header 對齊）", () => {
  it("⭐ Header 導覽列上的每條路由都在本檔受測——新增路由不得漏掉狀態驗證", async () => {
    mockLoggedInNotActivated();
    api.getAdminPending.mockResolvedValue({ pending: [] }); // admin 身分 → 導覽列最全
    render(wrap(<Header />, ME));
    await screen.findByRole("link", { name: COPY.nav.admin });

    const hrefs = Array.from(
      document.querySelectorAll("nav[aria-label='頁面切換'] a"),
    ).map((a) => a.getAttribute("href"));
    expect(hrefs.length).toBeGreaterThan(0);
    const tested = new Set(ROUTES.map((r) => r.path));
    for (const h of hrefs) expect(tested, `導覽列有 ${h} 但本檔未測`).toContain(h);
  });
});
