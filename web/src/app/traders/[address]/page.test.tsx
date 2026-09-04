/**
 * `/traders/[address]` — 交易員詳情頁測試（M3 round2 Task 6 首建；M3 round4
 * Task R4-11 版型對齊 `/strategies/[slug]`；2026-09-05 explore/trader 指標統一
 * plan Task 6 改四窗切換＋與探索清單同源欄位，測試同步改寫）。
 *
 * 涵蓋：404/讀不到空態、insufficient 指標「樣本不足」、CTA 依登入狀態分流、
 * account_value 為 null 時降級顯示、投入比例／回撤 slider、槓桿唯讀列「—」、
 * 面板頂部無背書說明、CAGR 卡渲染（sample_days/sample_threshold）、
 * 四窗切換（預設 month／`?window=` 帶入）、損益／回撤／實盤天數／成交統計
 * 與探索清單同源欄位、指標網格不重複渲染 total_return_pct／max_drawdown_pct。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";

const push = vi.fn();
let paramsAddress = "0xfefefefefefefefefefefefefefefefefefefefe";
let searchParamsValue = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ address: paramsAddress }),
  useSearchParams: () => searchParamsValue,
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

const getMe = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
}));

import { ApiError } from "@/lib/api";
import TraderDetailPage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// ── 指標（逐窗，D6：全部不足——0x6648 錨例 month/week/allTime 三窗都被
//    leader_perf 閘門判無效，見 docs/superpowers/plans/2026-09-04-...md Task 4）──
const METRICS_ALL_INSUFFICIENT = {
  total_return_pct: null, total_return_pct_insufficient: true,
  max_drawdown_pct: null, max_drawdown_pct_insufficient: true,
  sharpe: null, sharpe_insufficient: true,
  sharpe_se: null, sharpe_se_insufficient: true,
  win_rate_pct: null, win_rate_pct_insufficient: true,
  annualized_vol_pct: null, annualized_vol_pct_insufficient: true,
  sortino: null, sortino_insufficient: true,
  best_day_pct: null, best_day_pct_insufficient: true,
  worst_day_pct: null, worst_day_pct_insufficient: true,
  sample_count: 0,
};

// day 窗 perf ok（覆蓋天數 < 30 仍標比率型指標不足），但 win_rate_pct 不受
// RATIO_MIN_DAYS 門檻限制（N>=1 即存在，見 plan Task 4 測試註解）。
const METRICS_DAY = {
  ...METRICS_ALL_INSUFFICIENT,
  win_rate_pct: "64.86", win_rate_pct_insufficient: false,
  sample_count: 1,
};

const METRICS_MONTH_FULL = {
  ...METRICS_ALL_INSUFFICIENT,
  sharpe: "5.55", sharpe_insufficient: false,
  sharpe_se: "3.36", sharpe_se_insufficient: false,
  win_rate_pct: "64.86", win_rate_pct_insufficient: false,
  annualized_vol_pct: "18.05", annualized_vol_pct_insufficient: false,
  sortino: "43.42", sortino_insufficient: false,
  best_day_pct: "3.01", best_day_pct_insufficient: false,
  worst_day_pct: "-0.80", worst_day_pct_insufficient: false,
  sample_count: 29,
};

const DETAIL = {
  address: "0xfefefefefefefefefefefefefefefefefefefefe",
  account_value: "5000.00",
  follow_blocked: false,
  live_days: 1003,
  exposure: { dir: "long" as const, pct: 62.5 },
  // 錨例來自 0x6648…b1f3（plan Task 4/Task 2 fixture 錨例）。
  windows: {
    day: { pnl_usd: -2181.94, max_dd_pct: -74.07, max_dd_reason: null, spark: [0, -800, -2181.94] },
    week: { pnl_usd: 764.18, max_dd_pct: null, max_dd_reason: "too_many_skipped_intervals", spark: [0, 300, 764.18] },
    month: { pnl_usd: 33055.26, max_dd_pct: null, max_dd_reason: "too_many_skipped_intervals", spark: [0, 12000, 33055.26] },
    allTime: { pnl_usd: 27504.48, max_dd_pct: null, max_dd_reason: "flow_dominated_interval", spark: [0, 9000, 27504.48] },
  },
  metrics: {
    day: METRICS_DAY,
    week: METRICS_ALL_INSUFFICIENT,
    month: METRICS_ALL_INSUFFICIENT,
    allTime: METRICS_ALL_INSUFFICIENT,
  },
  fills_30d: {
    order_count: 221, closed_positions: 27, wins: 15, win_rate_pct: 55.56,
    realized_pnl_usd: 40225.79, concentration_pct: 41.2, coins: ["BTC", "ETH"], truncated: false,
  },
  methodology: {
    basis: "combined",
    updated_at: 1756000000,
    start_equity_usd: "1000.00",
    end_equity_usd: "5000.00",
    initial_deposit_usd: "1000",
    mdd_note: "回撤以權益指數計算",
  },
  // ⭐ M3 round4 Task R4-11：與 `/api/public/strategies/{slug}` 共用同一套
  // `build_cagr_fields`（後端）。DETAIL 的 `sample_days:29`（差門檻一天）刻意
  // 用來驗證「摺疊」是預設路徑；`DETAIL_FULL_SAMPLE` 覆寫成 30 驗證「完整格」。
  sample_days: 29,
  sample_threshold: 30,
};

const DETAIL_FULL_SAMPLE = {
  ...DETAIL,
  metrics: { ...DETAIL.metrics, month: METRICS_MONTH_FULL },
  sample_days: 30,
  cagr_pct: "45.23",
};

beforeEach(() => {
  paramsAddress = DETAIL.address;
  searchParamsValue = new URLSearchParams();
  push.mockReset();
  connectAsync.mockReset();
  signMessageAsync.mockReset();
  loginWithSiwe.mockReset();
  getMe.mockReset();
  accountState = { isConnected: false };
});

describe("TraderDetailPage", () => {
  it("讀不到（422/503/網路異常）→ 顯示空態與回排行榜連結", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({ detail: "位址格式不合法" }, false));
    render(wrap(<TraderDetailPage />));
    expect(await screen.findByText(COPY.traders.notFoundTitle)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.traders.backToList }))
      .toHaveAttribute("href", "/leaderboard");
  });

  it("無 displayName 查詢參數 → 標題顯示 shortAddr", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    expect(await screen.findByRole("heading", { level: 1, name: "0xfefe…efe" })).toBeInTheDocument();
  });

  it("[W3] 不再信任 ?name= 查詢參數——一律顯示 shortAddr", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    expect(await screen.findByRole("heading", { level: 1, name: "0xfefe…efe" })).toBeInTheDocument();
    expect(document.title).toBe("0xfefe…efe｜Filet");
  });

  it("insufficient 指標 → 渲染「樣本不足」而非數字", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    // DETAIL 預設 month 窗（too_many_skipped_intervals）全部指標不足，headline
    // 卡（win_rate）驗證「樣本不足」文字確實渲染出來，而非數字。
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getAllByText(COPY.strategyDetail.metrics.insufficientLabel).length).toBeGreaterThan(0);
  });

  it("methodology.end_equity_usd 為 null（allTime 查無資料降級）→ 帳戶價值顯示 —", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({
      ...DETAIL,
      methodology: { ...DETAIL.methodology, end_equity_usd: null },
    }));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(COPY.traders.accountValueLabel).nextSibling?.textContent).toBe("—");
  });

  // ⭐ R4-11：版型對齊策略頁——投入比例／回撤 slider 與策略頁同款可互動。
  it("R4-11：投入比例／回撤 slider 與策略頁同款，未連錢包仍可互動", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    const scaleSlider = screen.getByLabelText(COPY.strategyDetail.panel.scaleLabel) as HTMLInputElement;
    fireEvent.change(scaleSlider, { target: { value: "60" } });
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByLabelText(COPY.strategyDetail.panel.ddEnableLabel)).toBeInTheDocument();
  });

  // ⭐ R4-11：本頁沒有平台審核過的槓桿上限（任意鏈上地址，非策展）——唯讀列
  // 顯示 `NO_VALUE`（「—」），不臆造數字。
  it("R4-11：槓桿唯讀列顯示「—」（本頁無平台層帽）", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(COPY.strategyDetail.panel.leverageLabel)).toBeInTheDocument();
    const leverageRow = screen.getByText(COPY.strategyDetail.panel.leverageLabel).closest(".risk-slider-row");
    expect(leverageRow?.textContent).toContain("—");
  });

  // ⭐ R4-11：面板頂部進階模式無背書說明，沿用 `/advanced` 頁同一句。
  it("R4-11：面板頂部顯示進階模式無背書說明", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(COPY.advanced.gate.body)).toBeInTheDocument();
  });

  it("未登入點 CTA → 觸發連接錢包＋SIWE 登入，成功後帶 advanced: 前綴＋scale 導向 /onboarding", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalled());
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    expect(url).toMatch(/^\/onboarding\?/);
    const params = new URLSearchParams(url.split("?")[1]);
    expect(params.get("strategy")).toBe(`advanced:${DETAIL.address}`);
    expect(params.get("scale")).toBe("25");
    expect(params.has("dd")).toBe(false);
  });

  it("已登入點 CTA → 不觸發連線/簽署，直接帶 advanced: 前綴＋scale 導向", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(connectAsync).not.toHaveBeenCalled();
    expect(loginWithSiwe).not.toHaveBeenCalled();
    const url = push.mock.calls[0][0] as string;
    const params = new URLSearchParams(url.split("?")[1]);
    expect(params.get("strategy")).toBe(`advanced:${DETAIL.address}`);
    expect(params.get("scale")).toBe("25");
  });

  it("啟用回撤開關後，CTA 查詢字串帶 dd", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByLabelText(COPY.strategyDetail.panel.ddEnableLabel));
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    expect(url).toMatch(/dd=\d+/);
  });

  it("錢包拒簽 → 顯示拒簽文案，不導向", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    connectAsync.mockRejectedValue({ name: "UserRejectedRequestError" });
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    expect(await screen.findByText(COPY.login.rejected)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("[W4] follow_blocked=true → 隱藏 CTA，顯示不可跟單提示", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({ ...DETAIL, follow_blocked: true }));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(COPY.traders.panel.followBlocked)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.strategyDetail.panel.cta })).not.toBeInTheDocument();
  });

  it("[W4] follow_blocked=false → 正常顯示 CTA（跟單面板仍渲染）", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta })).toBeInTheDocument();
    expect(screen.queryByText(COPY.traders.panel.followBlocked)).not.toBeInTheDocument();
  });

  // ⭐ M3 round3 Task 7；R4-11 起改用後端直接供給的 sample_days／
  // sample_threshold（與策略詳情頁同一套 build_cagr_fields），不再鏡射常數。
  describe("Task 7：指標收斂（比照策略詳情頁）", () => {
    it("sample_days < sample_threshold（DETAIL 預設 29，差門檻一天）→ 摺成一行小字，個別小卡只剩 1 張（win_rate）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      const c = COPY.strategyDetail.metrics;
      const expectedNote = `${c.insufficientGroupLabel}${c.insufficientGroupPrefix}29`
        + `${c.insufficientGroupMid}30${c.insufficientGroupSuffix}`;
      expect(screen.getByText((_, node) => node?.textContent === expectedNote)).toBeInTheDocument();
      expect(screen.queryByText(c.sharpeLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.sortinoLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.annualizedVolLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.bestWorstLabel)).not.toBeInTheDocument();
      // D6：total_return/max_drawdown 卡已移除（由窗卡取代），指標網格只剩 win_rate。
      expect(screen.queryByText(c.totalReturnLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.maxDrawdownLabel)).not.toBeInTheDocument();
      expect(screen.getByText(c.winRateLabel)).toBeInTheDocument();
    });

    it("sample_days ≥ sample_threshold（恰在門檻 30）→ 恢復完整格，不出現摺疊行", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL_FULL_SAMPLE));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      const c = COPY.strategyDetail.metrics;
      expect(screen.getByText(c.sharpeLabel)).toBeInTheDocument();
      expect(screen.getByText(c.sortinoLabel)).toBeInTheDocument();
      expect(screen.getByText(c.annualizedVolLabel)).toBeInTheDocument();
      expect(screen.getByText(c.bestWorstLabel)).toBeInTheDocument();
      expect(screen.queryByText((_, node) => (node?.textContent ?? "").includes(c.insufficientGroupSuffix)))
        .not.toBeInTheDocument();
    });

    // D6（2026-09-05 Task 6）：指標網格不再渲染 total_return_pct／max_drawdown_pct
    // ——同頁只有窗卡（`windows[w]`）一個回撤數字，避免兩處回撤語意不同卻同時出現。
    it("指標網格不渲染 total_return_pct／max_drawdown_pct（同頁只有窗卡一個回撤數字）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL_FULL_SAMPLE));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      const c = COPY.strategyDetail.metrics;
      expect(screen.queryByText(c.totalReturnLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.maxDrawdownLabel)).not.toBeInTheDocument();
    });
  });

  // ⭐ R4-11：CAGR 卡——兩頁版型對齊，後端同一套 `build_cagr_fields`。
  describe("R4-11：CAGR 卡（版型對齊策略詳情頁）", () => {
    it("無 cagr_pct 鍵（sample_days 未達門檻）→ 不渲染 CagrCard", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.queryByText(COPY.strategyDetail.cagr.heading)).not.toBeInTheDocument();
    });

    it("有 cagr_pct → 渲染 CagrCard", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL_FULL_SAMPLE));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText(COPY.strategyDetail.cagr.heading)).toBeInTheDocument();
      expect(screen.getByText("45.23%")).toBeInTheDocument();
    });
  });

  // ⭐⭐ 2026-09-05 Task 6（explore/trader 指標統一 plan）：四窗切換＋與探索清單
  // 同源的損益／回撤／實盤天數／成交統計。
  describe("Task 6：四窗切換＋與探索清單同源欄位", () => {
    it("預設 month 窗：顯示 +$33,055、回撤「—」（too_many_skipped_intervals）、實盤 1003 天", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText("+$33,055")).toBeInTheDocument();
      const ddCell = screen.getByText(COPY.traders.ddLabel).closest(".metric-card");
      expect(ddCell?.textContent).toContain("—");
      expect(screen.getByText("1003")).toBeInTheDocument();
    });

    it("?window=day → 顯示 −$2,182 與回撤 -74.1%，指標網格切到 metrics.day", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      searchParamsValue = new URLSearchParams("window=day");
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText("−$2,182")).toBeInTheDocument();
      expect(screen.getByText("-74.1%")).toBeInTheDocument();
      // metrics.day 的 win_rate_pct 有值（64.86%），與 month（不足）不同——證明網格切窗了。
      expect(screen.getByText("64.86%")).toBeInTheDocument();
    });

    it("成交統計卡：221 / 27 / 55.56% / +$40,226", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText(COPY.traders.fillsHeading)).toBeInTheDocument();
      expect(screen.getByText("221")).toBeInTheDocument();
      expect(screen.getByText("27")).toBeInTheDocument();
      expect(screen.getByText("55.56%")).toBeInTheDocument();
      expect(screen.getByText("+$40,226")).toBeInTheDocument();
    });

    it("fills_30d.truncated → 顯示下限值提示", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse({
        ...DETAIL,
        fills_30d: { ...DETAIL.fills_30d, truncated: true },
      }));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText(COPY.traders.fillsTruncatedNote)).toBeInTheDocument();
    });
  });
});
