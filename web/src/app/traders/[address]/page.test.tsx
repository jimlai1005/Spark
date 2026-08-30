/**
 * `/traders/[address]` — 交易員詳情頁測試（M3 round2 Task 6）。
 *
 * 涵蓋：404/讀不到空態、insufficient 指標「樣本不足」、displayName 查詢參數、
 * CTA 依登入狀態分流（未登入→connect+SIWE→帶 advanced: 前綴導向；已登入→直接
 * 導向）、account_value 為 null 時降級顯示、不含策略頁專屬的槓桿/回撤滑桿。
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
    sample_count: 38,
  },
  equity_index: Array.from({ length: 38 }, (_, i) => String(1 + i * 0.005)),
  methodology: {
    start_date: "2026-06-17", end_date: "2026-08-27", initial_deposit_usd: "1000",
    sample_count: 38, annualization_days: 365, risk_free_rate: "0", basis: "perp",
    updated_at: 1756000000,
  },
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
      ...DETAIL,
      metrics: { ...DETAIL.metrics, sharpe: null, sharpe_insufficient: true, sharpe_se: null, sharpe_se_insufficient: true },
    }));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getAllByText(COPY.strategyDetail.metrics.insufficientLabel).length).toBeGreaterThan(0);
  });

  it("account_value 為 null（clearinghouseState 查詢失敗降級）→ 顯示 —，equity 仍照常渲染", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({ ...DETAIL, account_value: null }));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(COPY.traders.accountValueLabel).nextSibling?.textContent).toBe("—");
  });

  it("不含策略頁專屬的投入比例／回撤滑桿", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByLabelText(COPY.strategyDetail.panel.scaleLabel)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(COPY.strategyDetail.panel.ddEnableLabel)).not.toBeInTheDocument();
  });

  it("未登入點 CTA → 觸發連接錢包＋SIWE 登入，成功後帶 advanced: 前綴導向 /onboarding", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 1 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "fabc" });
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByRole("button", { name: COPY.traders.panel.cta }));
    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalled());
    await waitFor(() => expect(push).toHaveBeenCalled());
    const url = push.mock.calls[0][0] as string;
    expect(url).toBe(`/onboarding?strategy=advanced:${DETAIL.address}`);
  });

  it("已登入點 CTA → 不觸發連線/簽署，直接帶 advanced: 前綴導向", async () => {
    getMe.mockResolvedValue({ address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" });
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByRole("button", { name: COPY.traders.panel.cta }));
    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(connectAsync).not.toHaveBeenCalled();
    expect(loginWithSiwe).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith(`/onboarding?strategy=advanced:${DETAIL.address}`);
  });

  it("錢包拒簽 → 顯示拒簽文案，不導向", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    connectAsync.mockRejectedValue({ name: "UserRejectedRequestError" });
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    fireEvent.click(screen.getByRole("button", { name: COPY.traders.panel.cta }));
    expect(await screen.findByText(COPY.login.rejected)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("[W4] follow_blocked=true → 隱藏 CTA，顯示不可跟單提示", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse({ ...DETAIL, follow_blocked: true }));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByText(COPY.traders.panel.followBlocked)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.traders.panel.cta })).not.toBeInTheDocument();
  });

  it("[W4] follow_blocked=false → 正常顯示 CTA", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    stubFetch(() => jsonResponse(DETAIL));
    render(wrap(<TraderDetailPage />));
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole("button", { name: COPY.traders.panel.cta })).toBeInTheDocument();
    expect(screen.queryByText(COPY.traders.panel.followBlocked)).not.toBeInTheDocument();
  });
});
