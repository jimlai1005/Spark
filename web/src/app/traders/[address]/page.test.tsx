/**
 * `/traders/[address]` — 交易員詳情頁測試（M3 round2 Task 6 首建；M3 round4
 * Task R4-11 版型對齊 `/strategies/[slug]`，測試同步改寫）。
 *
 * 涵蓋：404/讀不到空態、insufficient 指標「樣本不足」、displayName 查詢參數、
 * CTA 依登入狀態分流（未登入→connect+SIWE→帶 advanced: 前綴＋scale/dd 導向；
 * 已登入→直接導向）、account_value 為 null 時降級顯示、投入比例／回撤 slider
 * 與策略頁同款可互動、槓桿唯讀列顯示「—」（無平台層帽）、面板頂部無背書說明、
 * CAGR／方法論卡渲染（R4-11 起兩頁版型對齊，後端同一套 `build_cagr_fields`）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";

const push = vi.fn();
let paramsAddress = "0xfefefefefefefefefefefefefefefefefefefefe";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ address: paramsAddress }),
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

const DETAIL = {
  address: "0xfefefefefefefefefefefefefefefefefefefefe",
  account_value: "5000.00",
  follow_blocked: false,
  metrics: {
    total_return_pct: "17.77", total_return_pct_insufficient: false,
    max_drawdown_pct: "-0.80", max_drawdown_pct_insufficient: false,
    sharpe: "5.55", sharpe_insufficient: false,
    sharpe_se: "3.36", sharpe_se_insufficient: false,
    win_rate_pct: "64.86", win_rate_pct_insufficient: false,
    annualized_vol_pct: "18.05", annualized_vol_pct_insufficient: false,
    sortino: "43.42", sortino_insufficient: false,
    best_day_pct: "3.01", best_day_pct_insufficient: false,
    worst_day_pct: "-0.80", worst_day_pct_insufficient: false,
    sample_count: 29,
  },
  equity_index: Array.from({ length: 29 }, (_, i) => String(1 + i * 0.005)),
  methodology: {
    start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
    // ⭐ issue log I-19 附帶一致性修復：頁面「目前帳戶價值」改讀
    // `methodology.end_equity_usd`（與 equity_index 同源），不再讀
    // `account_value`（clearinghouseState，見 page.tsx 同款註解）。
    end_equity_usd: "5000.00",
    sample_count: 29, annualization_days: 365, risk_free_rate: "0", basis: "perp",
    updated_at: 1756000000,
  },
  // ⭐ M3 round4 Task R4-11：與 `/api/public/strategies/{slug}` 共用同一套
  // `build_cagr_fields`（後端）。DETAIL 的 `sample_days:29`（差門檻一天）刻意
  // 用來驗證「摺疊」是預設路徑；`DETAIL_FULL_SAMPLE` 覆寫成 30 驗證「完整格」。
  sample_days: 29,
  sample_threshold: 30,
};

const DETAIL_FULL_SAMPLE = {
  ...DETAIL,
  sample_days: 30,
  cagr_pct: "45.23",
};

beforeEach(() => {
  paramsAddress = DETAIL.address;
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
    stubFetch(() => jsonResponse({
      ...DETAIL_FULL_SAMPLE,
      metrics: {
        ...DETAIL_FULL_SAMPLE.metrics,
        sharpe: null, sharpe_insufficient: true, sharpe_se: null, sharpe_se_insufficient: true,
      },
    }));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getAllByText(COPY.strategyDetail.metrics.insufficientLabel).length).toBeGreaterThan(0);
  });

  it("methodology.end_equity_usd 為 null（allTime 查無資料降級）→ 顯示 —，equity 仍照常渲染", async () => {
    // ⭐ issue log I-19：本頁「目前帳戶價值」與 equity_index 同源
    // （methodology.end_equity_usd），不再讀 account_value（見 page.tsx 註解）。
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

  it("[W4] follow_blocked=false → 正常顯示 CTA", async () => {
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
    it("sample_days < sample_threshold（DETAIL 預設 29，差門檻一天）→ 摺成一行小字，個別小卡只剩 3 張", async () => {
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
      expect(screen.queryByText(c.startEndEquityLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.bestWorstLabel)).not.toBeInTheDocument();
      expect(screen.getByText(c.totalReturnLabel)).toBeInTheDocument();
      expect(screen.getByText(c.maxDrawdownLabel)).toBeInTheDocument();
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
      expect(screen.getByText(c.startEndEquityLabel)).toBeInTheDocument();
      expect(screen.getByText(c.bestWorstLabel)).toBeInTheDocument();
      expect(screen.queryByText((_, node) => (node?.textContent ?? "").includes(c.insufficientGroupSuffix)))
        .not.toBeInTheDocument();
    });

    it("回撤 label 為「策略期間回撤」（與策略詳情頁／首頁同一 key）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText("策略期間回撤")).toBeInTheDocument();
    });
  });

  // ⭐ R4-11：CAGR／方法論卡——兩頁版型對齊，後端同一套 `build_cagr_fields`。
  describe("R4-11：CAGR／方法論卡（版型對齊策略詳情頁）", () => {
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

    it("方法論卡渲染（真實入金句）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<TraderDetailPage />));
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByText(COPY.strategyDetail.methodology.heading)).toBeInTheDocument();
    });
  });
});
