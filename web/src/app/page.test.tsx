/**
 * `/` — 首頁（Task 8 大改版）測試。舊版是 SIWE 登入頁，測試涵蓋 connect→sign→
 * redirect；本頁完全不觸碰錢包，測試改為：無錢包按鈕、證據列 null→「—」、
 * 策略卡渲染、錨點 id 存在。SIWE 流程搬到 Task 9 的策略詳情頁，屆時另有測試。
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import HomePage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

const STRATEGY = {
  slug: "core",
  name: "Filet Core",
  tagline: "多資產動能 · 永續合約",
  featured: true,
  leader_address: "0xfeed000000000000000000000000000000f00d",
  status: "running",
  listable: true,
  live_days: 91,
  follower_count: 9,
  min_notional_usd: "500",
  max_leverage: "3",
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

function stubFetch(impl: (url: string) => Response) {
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(impl(url))));
}

describe("HomePage", () => {
  it("零錢包按鈕：不出現任何連接錢包的互動元素", async () => {
    stubFetch((url) => {
      if (url.includes("/api/public/strategies")) return jsonResponse({ strategies: [STRATEGY], updated_at: 1 });
      return jsonResponse({ routed_volume_usd_total: "4280000", builder_fee_bps: 2, live_days: 91, updated_at: 1 });
    });
    render(<HomePage />);
    await screen.findByText("Filet Core");
    // "連接錢包並授權" 合法出現在步驟區（描述後續流程），但不得出現任何按鈕/輸入框
    // 型態的錢包連線互動元素——本頁不 import wagmi，結構上不可能有這種元素。
    expect(screen.queryByRole("button", { name: /連接錢包/ })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("button").length).toBe(0);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("hero 與 CTA：H1、主 CTA 連向 /strategies", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    render(<HomePage />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(COPY.home.hero.title);
    const ctas = screen.getAllByRole("link", { name: COPY.home.hero.ctaPrimary });
    expect(ctas.some((a) => a.getAttribute("href") === "/strategies")).toBe(true);
  });

  it("證據列：API 回傳 null 欄位 → 顯示「—」並保留欄位（不隱藏整列）", async () => {
    stubFetch((url) => {
      if (url.includes("/api/public/strategies")) return jsonResponse({ strategies: [], updated_at: 1 });
      return jsonResponse({ routed_volume_usd_total: null, builder_fee_bps: null, live_days: null, updated_at: 1 });
    });
    render(<HomePage />);
    await waitFor(() => {
      expect(screen.getByText(COPY.home.evidence.routedVolumeLabel)).toBeInTheDocument();
    });
    expect(screen.getByText(COPY.home.evidence.liveDaysLabel)).toBeInTheDocument();
    expect(screen.getByText(COPY.home.evidence.builderFeeLabel)).toBeInTheDocument();
    // 三個 null 欄位（routed volume／live days／builder fee）都顯示佔位符；
    // 託管資產（custody）為靜態 "0"，不受這組 null mock 影響。
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("策略卡：渲染 API 回傳的策略並附「查看策略與風險」CTA", async () => {
    stubFetch((url) => {
      if (url.includes("/api/public/strategies")) return jsonResponse({ strategies: [STRATEGY], updated_at: 1 });
      return jsonResponse({ routed_volume_usd_total: "4280000", builder_fee_bps: 2, live_days: 91, updated_at: 1 });
    });
    render(<HomePage />);
    await screen.findByText("Filet Core");
    const card = document.querySelector('[data-slug="core"]') as HTMLElement;
    const link = within(card).getByRole("link", { name: COPY.home.strategies.cta });
    expect(link).toHaveAttribute("href", "/strategies/core");
    // 進階模式卡永遠存在，即便策略清單非空。
    expect(screen.getByRole("link", { name: COPY.home.strategies.advancedCta })).toHaveAttribute("href", "/advanced");
  });

  it("錨點 id：#security（授權能力矩陣）與 #how（步驟）皆存在", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    const { container } = render(<HomePage />);
    await waitFor(() => {
      expect(container.querySelector("#security")).not.toBeNull();
    });
    expect(container.querySelector("#how")).not.toBeNull();
  });

  it("錨點 id：#strategies（策略區，供 header「策略」導覽跳轉）存在", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    const { container } = render(<HomePage />);
    await waitFor(() => {
      expect(container.querySelector("#strategies")).not.toBeNull();
    });
  });

  it("「全部策略 →」連向 /leaderboard（round2：leaderboard 頁尚未建，僅先接連結）", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    render(<HomePage />);
    const link = await screen.findByRole("link", { name: COPY.home.strategies.viewAll });
    expect(link).toHaveAttribute("href", "/leaderboard");
  });

  it("不寫死設計稿佔位數字（20.35 / 4.28M / 10.24 一律來自 API 狀態）", async () => {
    stubFetch(() => jsonResponse({ strategies: [], updated_at: 1 }));
    const { container } = render(<HomePage />);
    await waitFor(() => expect(container.textContent).not.toMatch(/20\.35|10\.24/));
  });
});
