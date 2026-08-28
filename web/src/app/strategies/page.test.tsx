/**
 * `/strategies` — 策略列表頁測試（Task 9）。復用 Task 8 的 `StrategyCard`，
 * 這裡只驗證列表頁本身的職責：抓資料、渲染卡片網格、進階模式卡永遠在。
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import StrategiesPage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

const STRATEGY = {
  slug: "core", name: "Filet Core", tagline: "多資產動能 · 永續合約", featured: true,
  leader_address: "0xfeed000000000000000000000000000000f00d",
  status: "running", listable: true, live_days: 91, follower_count: 9,
  min_notional_usd: "500", max_leverage: "3",
  metrics: {
    total_return_pct: "17.77", total_return_pct_insufficient: false,
    max_drawdown_pct: "-3.33", max_drawdown_pct_insufficient: false,
    sharpe: "7.71", sharpe_insufficient: false,
    sharpe_se: "2.22", sharpe_se_insufficient: false,
    win_rate_pct: "55.55", win_rate_pct_insufficient: false,
    annualized_vol_pct: "20.20", annualized_vol_pct_insufficient: false,
    sortino: "8.88", sortino_insufficient: false,
    best_day_pct: "5.05", best_day_pct_insufficient: false,
    worst_day_pct: "-3.33", worst_day_pct_insufficient: false,
    sample_count: 91,
  },
};

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

describe("StrategiesPage", () => {
  it("渲染 API 策略卡＋進階模式卡", async () => {
    stubFetch(() => jsonResponse({ strategies: [STRATEGY], updated_at: 1 }));
    render(<StrategiesPage />);
    await screen.findByText("Filet Core");
    const card = document.querySelector('[data-slug="core"]') as HTMLElement;
    expect(within(card).getByRole("link", { name: COPY.home.strategies.cta }))
      .toHaveAttribute("href", "/strategies/core");
    expect(screen.getByRole("link", { name: COPY.home.strategies.advancedCta }))
      .toHaveAttribute("href", "/advanced");
  });

  it("清單為空 → 顯示空態文字，進階模式卡仍在", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    render(<StrategiesPage />);
    await waitFor(() => {
      expect(screen.getByText(COPY.home.strategies.empty)).toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: COPY.home.strategies.advancedCta })).toBeInTheDocument();
  });

  it("標題與說明文字存在", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    render(<StrategiesPage />);
    expect(screen.getByRole("heading", { level: 1, name: COPY.home.strategies.heading })).toBeInTheDocument();
    expect(screen.getByText(COPY.home.strategies.sub)).toBeInTheDocument();
  });
});
