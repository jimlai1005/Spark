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
import type { LeadersResp, OnboardStatus } from "@/lib/api";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
  usePathname: () => "/",
  // ⭐ Task 10：OnboardingPage 改讀 `?strategy=`；本檔的通用 ROUTES 迴圈不帶任何
  // 查詢參數，這對 /onboarding 剛好是它自己的「無 strategy 參數」guard 路徑
  // （見下方對 /onboarding 的排除說明與專屬斷言）。
  useSearchParams: () => new URLSearchParams(),
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
const getAdminPending = vi.fn();
const getOpsCustomers = vi.fn();
const getOpsRevenue = vi.fn();
const getOpsSubscriptions = vi.fn();
const getOpsTradeQuality = vi.fn();
const getOpsHealth = vi.fn();
const getDashboard = vi.fn();
const getMyRisk = vi.fn();
const getMyCapital = vi.fn();
const getMyLeader = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getStatus: (...a: unknown[]) => getStatus(...a),
  getLeaders: (...a: unknown[]) => getLeaders(...a),
  getAdminPending: (...a: unknown[]) => getAdminPending(...a),
  getOpsCustomers: (...a: unknown[]) => getOpsCustomers(...a),
  getOpsRevenue: (...a: unknown[]) => getOpsRevenue(...a),
  getOpsSubscriptions: (...a: unknown[]) => getOpsSubscriptions(...a),
  getOpsTradeQuality: (...a: unknown[]) => getOpsTradeQuality(...a),
  getOpsHealth: (...a: unknown[]) => getOpsHealth(...a),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
  getMyLeader: (...a: unknown[]) => getMyLeader(...a),
}));

/** 全部 mock 的集合，供 reset 與批次設定使用（在 factory 之外求值，不受提升影響）。 */
const api = {
  getMe, getStatus, getLeaders, getAdminPending,
  getOpsCustomers, getOpsRevenue, getOpsSubscriptions, getOpsTradeQuality, getOpsHealth,
  getDashboard, getMyRisk, getMyCapital, getMyLeader,
};

import { ApiError, type DashboardResp } from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { Header } from "@/components/Header";
import AdminPage from "./admin/page";
import AdvancedPage from "./advanced/page";
import DashboardPage from "./dashboard/page";
import DocsPage from "./docs/page";
import LeadersPage from "./leaders/page";
import HomePage from "./page";
import OnboardingPage from "./onboarding/page";
import OpsPage from "./ops/page";
import SettingsPage from "./settings/page";
import StrategiesPage from "./strategies/page";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const unauthorized = () => new ApiError("auth", "未登入", 401, "未登入");
const forbidden = () => new ApiError("client", "非管理員", 403, "非管理員");

/** 已登入但**尚未活化**：授權沒簽完、沒入金，後端 /api/onboard/status 照樣回進度物件。 */
const NOT_ACTIVATED: OnboardStatus = {
  address: ME.address,
  account_id: ME.account_id,
  agent_address: null,
  agent_generated: false,
  builder_fee_approved: false,
  agent_approved: false,
  funded: false,
  perp_account_value: "0",
  min_deposit: "100",
  deposit_shortfall: "100",
  state: "IN_PROGRESS",
};

/**
 * 已登入但未活化：`/api/me/dashboard` 照樣回 200＋全塊 null（`mine is None` 分支，
 * app.py::_dashboard_status），不是 401——只有真的沒登入才 401。用來覆蓋 Task 14
 * 的「null 塊顯示『—』不炸」驗收條件。
 */
const DASHBOARD_INACTIVE: DashboardResp = {
  status: {
    strategy_name: null, state: "inactive", following_days: null,
    signal_source_ok: null,
    guards: {
      scale: { now: null, max: null }, leverage: { now: null, max: null },
      drawdown: { now: null, max: null, enabled: null },
    },
  },
  equity: null, exposure: null, pnl: null, sync: null, fees_month: null,
  positions: null, updated_at: 1724800000,
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

/**
 * 導覽列點得到的路由 → 本檔受測的頁面元件。新增路由必須同步加進來。
 *
 * ⭐ /onboarding 頁面元件本身未變（Task 10 已完成），只是 Task 7 的新導覽不再把它
 * 掛在 nav 上——保留在本表是為了維持這頁既有的逐頁狀態覆蓋，不是漏改。
 * ⭐ Task 11：`/leaders` 改為純 redirect（見 `../leaders/page.test.tsx` 專屬測試），
 * 功能遷移至 `/advanced`，本表新增一筆。
 * ⭐ Task 16：`/settings` 頁面元件已建立並搬進本表——原本用來豁免尚未建立頁面的
 * 導覽涵蓋率白名單機制（其註解自述白名單即待辦，對應 task 完成時要清空）隨之
 * 整段移除，見下方「導覽涵蓋率」測試。
 */
const ROUTES: { path: string; name: string; el: () => ReactNode }[] = [
  { path: "/", name: "首頁", el: () => <HomePage /> },
  { path: "/onboarding", name: "開通頁", el: () => <OnboardingPage /> },
  { path: "/leaders", name: "跟單對象頁（舊路由，redirect）", el: () => <LeadersPage /> },
  { path: "/advanced", name: "進階模式頁", el: () => <AdvancedPage /> },
  { path: "/ops", name: "營運頁", el: () => <OpsPage /> },
  { path: "/admin", name: "待核准頁", el: () => <AdminPage /> },
  { path: "/strategies", name: "策略列表頁", el: () => <StrategiesPage /> },
  { path: "/docs", name: "文件頁", el: () => <DocsPage /> },
  { path: "/dashboard", name: "Dashboard 頁", el: () => <DashboardPage /> },
  { path: "/settings", name: "設定頁", el: () => <SettingsPage /> },
];

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  routerPush.mockReset();
});

/** 未登入：所有需要 session 的端點一律 401。 */
function mockLoggedOut() {
  api.getMe.mockRejectedValue(unauthorized());
  for (const k of ["getStatus", "getLeaders", "getAdminPending",
                   "getOpsCustomers", "getOpsRevenue", "getOpsSubscriptions",
                   "getOpsTradeQuality", "getOpsHealth", "getDashboard",
                   "getMyRisk", "getMyCapital", "getMyLeader"] as const) {
    api[k].mockRejectedValue(unauthorized());
  }
}

/** 已登入的一般客戶，尚未活化：session 有效，但沒有 follower、非管理員。
 * `/api/me/dashboard` 照樣 200＋全塊 null（見 DASHBOARD_INACTIVE 註解）。
 * `/settings` 的三段自有查詢（風控／資金配置／目前跟隨的 leader）在這個狀態下
 * 一律回 403（沿 `/ops`、`/admin` 未活化客戶的既有慣例：session 有效但功能性
 * 端點對還沒完成開通的帳號回拒），驗的是「讀不到不炸」，不是這幾段的正常路徑。 */
function mockLoggedInNotActivated() {
  api.getMe.mockResolvedValue(ME);
  api.getStatus.mockResolvedValue(NOT_ACTIVATED);
  api.getLeaders.mockResolvedValue(LEADERS);
  api.getDashboard.mockResolvedValue(DASHBOARD_INACTIVE);
  api.getAdminPending.mockRejectedValue(forbidden());
  for (const k of ["getOpsCustomers", "getOpsRevenue", "getOpsSubscriptions",
                   "getOpsTradeQuality", "getOpsHealth",
                   "getMyRisk", "getMyCapital", "getMyLeader"] as const) {
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

  it("⭐ Task 10／NOTE 10：/onboarding 未登入 → redirect /strategies（不留白畫面、不留返回按鈕）", async () => {
    render(wrap(<OnboardingPage />, null));
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/strategies"));
  });

  it("⭐ Task 11：/leaders 舊路由未登入照樣 redirect /advanced（功能已遷移，見該頁專屬測試）",
    async () => {
      render(wrap(<LeadersPage />, null));
      await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/advanced"));
    });

  it("⭐ Task 11：/advanced 未登入 → 顯示說明＋登入 CTA，不 redirect（進階用戶的直達入口）",
    async () => {
      render(wrap(<AdvancedPage />, null));
      expect(await screen.findByText(COPY.advanced.notLoggedIn.title)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: COPY.advanced.notLoggedIn.cta }))
        .toBeInTheDocument();
      expect(routerPush).not.toHaveBeenCalled();
    });

  it("管理端頁未登入 → 顯示提示（後端 401；前端不自行判斷授權）", async () => {
    const { unmount } = render(wrap(<OpsPage />, null));
    expect(await screen.findByText(COPY.common.notLoggedIn)).toBeInTheDocument();
    unmount();
    render(wrap(<AdminPage />, null));
    expect(await screen.findByText(COPY.common.notLoggedIn)).toBeInTheDocument();
  });

  it("公開頁（/ ）未登入仍正常呈現，不是錯誤畫面；且無任何錢包連線按鈕（Task 8：首頁改版）", async () => {
    const { unmount } = render(wrap(<HomePage />, null));
    expect(screen.getByRole("heading", { level: 1, name: COPY.home.hero.title })).toBeInTheDocument();
    expect(screen.queryAllByRole("button").length).toBe(0);
    unmount();
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

  it("⭐ Task 11：/leaders 舊路由已登入未活化照樣 redirect /advanced", async () => {
    render(wrap(<LeadersPage />, ME));
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/advanced"));
  });

  it("⭐ Task 11：/advanced 已登入未活化 → 地址輸入入口照樣呈現，不是白畫面", async () => {
    render(wrap(<AdvancedPage />, ME));
    expect(await screen.findByText(COPY.advanced.gate.title)).toBeInTheDocument();
    // 未活化不等於白畫面：輸入框存在（不依賴 /api/onboard/status），只是待勾選聲明後才可用。
    expect(screen.getByLabelText(/leader 錢包位址/)).toBeInTheDocument();
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
      document.querySelectorAll(`nav[aria-label='${COPY.nav.ariaLabel}'] a`),
    ).map((a) => a.getAttribute("href"));
    expect(hrefs.length).toBeGreaterThan(0);
    const tested = new Set(ROUTES.map((r) => r.path));
    for (const h of hrefs) {
      expect(tested, `導覽列有 ${h} 但本檔未測`).toContain(h);
    }
  });
});
