/**
 * `/strategies/[slug]` — 策略詳情頁測試（Task 9）。
 *
 * 涵蓋：404 空態、insufficient 指標「樣本不足」、slider 未連錢包可互動、
 * CTA 依登入狀態分流（未登入→connect+SIWE→帶參數導向；已登入→直接導向）、
 * 回撤開關預設關＋關閉時查詢字串不帶 dd、listable:false 的 CTA disabled 態。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { fmtUpdatedAtUtc } from "@/lib/format";

const push = vi.fn();
let paramsSlug = "core";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ slug: paramsSlug }),
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
import StrategyDetailPage from "./page";

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => body } as Response;
}

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const DETAIL = {
  slug: "core", name: "Filet Core", tagline: "多資產動能 · 永續合約", featured: true,
  leader_address: "0xfeed000000000000000000000000000000f00d",
  status: "running", listable: true, live_days: 72, follower_count: 3,
  min_notional_usd: "500", max_leverage: "3",
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
    sample_count: 38,
  },
  equity_index: Array.from({ length: 38 }, (_, i) => String(1 + i * 0.005)),
  methodology: {
    start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
    sample_count: 38, annualization_days: 365, risk_free_rate: "0", basis: "perp",
    updated_at: 1756000000,
  },
  as_of: 1756000500,
  sample_days: 72,
  sample_threshold: 60,
  cagr_pct: "45.23",
};

beforeEach(() => {
  paramsSlug = "core";
  push.mockReset();
  connectAsync.mockReset();
  signMessageAsync.mockReset();
  loginWithSiwe.mockReset();
  getMe.mockReset();
  accountState = { isConnected: false };
});

describe("StrategyDetailPage", () => {
  it("404 → 顯示空態與回列表連結", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({ detail: "策略不存在" }, false, 404));
    render(wrap(<StrategyDetailPage />));
    expect(await screen.findByText(COPY.strategyDetail.notFoundTitle)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.strategyDetail.backToList }))
      .toHaveAttribute("href", "/strategies");
  });

  it("Task 17：策略載入後 document.title 更新為 `{name}｜Filet`", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    expect(document.title).toBe("Filet Core｜Filet");
  });

  it("insufficient 指標 → 渲染「樣本不足」而非數字", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({
      ...DETAIL,
      metrics: { ...DETAIL.metrics, sharpe: null, sharpe_insufficient: true, sharpe_se: null, sharpe_se_insufficient: true },
    }));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    expect(screen.getAllByText(COPY.strategyDetail.metrics.insufficientLabel).length).toBeGreaterThan(0);
  });

  it("slider 未連錢包（未登入）仍可互動", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    const scaleSlider = screen.getByLabelText(COPY.strategyDetail.panel.scaleLabel) as HTMLInputElement;
    fireEvent.change(scaleSlider, { target: { value: "60" } });
    expect(screen.getByText("60%")).toBeInTheDocument();
  });

  it("最大回撤開關預設關閉；關閉狀態下 CTA 導向的查詢字串不含 dd", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    const ddToggle = screen.getByLabelText(COPY.strategyDetail.panel.ddEnableLabel) as HTMLInputElement;
    expect(ddToggle.checked).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    expect(url).toMatch(/^\/onboarding\?/);
    expect(url).not.toMatch(/dd=/);
    expect(url).toMatch(/scale=25/);
  });

  it("啟用回撤開關後，CTA 查詢字串帶 dd", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    fireEvent.click(screen.getByLabelText(COPY.strategyDetail.panel.ddEnableLabel));
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    expect(url).toMatch(/dd=\d+/);
  });

  // ⭐ Task 10b（主線程裁決 2026-08-28）：槓桿沒有 per-user 簽章通道，slider 移除、
  // 改唯讀資訊列；CTA 查詢字串同步移除 `lev` 參數，只剩 scale／(選填) dd。
  it("槓桿改唯讀資訊列（無 slider），CTA 查詢字串只剩 scale/dd，不含 lev", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });

    expect(screen.queryByRole("slider", { name: COPY.strategyDetail.panel.leverageLabel }))
      .not.toBeInTheDocument();
    expect(screen.getByText("3x")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(COPY.strategyDetail.panel.ddEnableLabel));
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    const params = new URLSearchParams(url.split("?")[1]);
    expect([...params.keys()].sort()).toEqual(["dd", "scale", "strategy"]);
  });

  it("未登入點 CTA → 觸發連接錢包＋SIWE 登入，成功後帶參數導向 /onboarding", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalled());
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    expect(url).toMatch(/^\/onboarding\?strategy=core/);
  });

  it("已登入點 CTA → 不觸發連線/簽署，直接帶參數導向", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(connectAsync).not.toHaveBeenCalled();
    expect(loginWithSiwe).not.toHaveBeenCalled();
  });

  it("listable:false → CTA disabled＋「暫不開放新跟單」，不出現可跟單按鈕", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({ ...DETAIL, listable: false }));
    render(wrap(<StrategyDetailPage />));
    await screen.findByRole("heading", { level: 1, name: "Filet Core" });
    expect(screen.queryByRole("button", { name: COPY.strategyDetail.panel.cta })).not.toBeInTheDocument();
    const disabledBtn = screen.getByTestId("follow-panel-disabled");
    expect(disabledBtn).toBeDisabled();
    expect(screen.getByText(COPY.strategyDetail.panel.pendingNote)).toBeInTheDocument();
  });

  // ⭐ M3 round3 Task 7（R2-P0 指標收斂＋CAGR gating＋回撤改名）
  describe("Task 7：指標收斂與 CAGR 結構性 gating", () => {
    it("sample_days < sample_threshold → 摺成一行小字，大字只剩 4 張（含最佳/最差日）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse({ ...DETAIL, sample_days: 10, sample_threshold: 60 }));
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });

      // 摺疊行：Sharpe／Sortino／年化波動／起訖淨值：樣本不足（10/60 天），達門檻後顯示
      const c = COPY.strategyDetail.metrics;
      const expectedNote = `${c.insufficientGroupLabel}${c.insufficientGroupPrefix}10`
        + `${c.insufficientGroupMid}60${c.insufficientGroupSuffix}`;
      expect(screen.getByText((_, node) => node?.textContent === expectedNote)).toBeInTheDocument();

      // 個別小卡只剩 4 張：總報酬／策略期間回撤／日勝率／最佳最差日。
      expect(screen.queryByText(c.sharpeLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.sortinoLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.annualizedVolLabel)).not.toBeInTheDocument();
      expect(screen.queryByText(c.startEndEquityLabel)).not.toBeInTheDocument();
      expect(screen.getByText(c.totalReturnLabel)).toBeInTheDocument();
      expect(screen.getByText(c.maxDrawdownLabel)).toBeInTheDocument();
      expect(screen.getByText(c.winRateLabel)).toBeInTheDocument();
      expect(screen.getByText(c.bestWorstLabel)).toBeInTheDocument();
    });

    it("sample_days ≥ sample_threshold → 恢復完整格，不出現摺疊行", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL)); // sample_days:72 >= sample_threshold:60
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });
      const c = COPY.strategyDetail.metrics;
      expect(screen.getByText(c.sharpeLabel)).toBeInTheDocument();
      expect(screen.getByText(c.sortinoLabel)).toBeInTheDocument();
      expect(screen.getByText(c.annualizedVolLabel)).toBeInTheDocument();
      expect(screen.getByText(c.startEndEquityLabel)).toBeInTheDocument();
      expect(screen.queryByText((_, node) => (node?.textContent ?? "").includes(c.insufficientGroupSuffix)))
        .not.toBeInTheDocument();
    });

    it("回應無 cagr_pct 鍵 → 不渲染 CagrCard", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      const { cagr_pct: _drop, ...withoutCagr } = DETAIL;
      stubFetch(() => jsonResponse(withoutCagr));
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });
      expect(screen.queryByText(COPY.strategyDetail.cagr.heading)).not.toBeInTheDocument();
    });

    it("cagr_pct 為 null（防後端序列化差異）→ 不渲染 CagrCard", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse({ ...DETAIL, cagr_pct: null }));
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });
      expect(screen.queryByText(COPY.strategyDetail.cagr.heading)).not.toBeInTheDocument();
    });

    it("有 cagr_pct → 渲染 CagrCard，灰階＋樣本標注", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });
      expect(screen.getByText(COPY.strategyDetail.cagr.heading)).toBeInTheDocument();
      expect(screen.getByText("45.23%")).toBeInTheDocument();
      const cc = COPY.strategyDetail.cagr;
      const expectedNote = `${cc.notePrefix}${DETAIL.sample_days}${cc.noteSuffix}`;
      expect(screen.getByText((_, node) => node?.textContent === expectedNote)).toBeInTheDocument();
    });

    it("as_of 顯示為 UTC 時間戳（取代 methodology.updated_at）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });
      expect(screen.getByText(fmtUpdatedAtUtc(DETAIL.as_of), { exact: false })).toBeInTheDocument();
    });

    it("回撤 label 為「策略期間回撤」（策略頁／首頁／traders 頁三處同一 key）", async () => {
      getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
      stubFetch(() => jsonResponse(DETAIL));
      render(wrap(<StrategyDetailPage />));
      await screen.findByRole("heading", { level: 1, name: "Filet Core" });
      expect(COPY.strategyDetail.metrics.maxDrawdownLabel).toBe("策略期間回撤");
      expect(screen.getByText("策略期間回撤")).toBeInTheDocument();
    });
  });
});
