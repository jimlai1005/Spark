/**
 * `/dashboard` — 六塊 Dashboard 測試（Task 14）。
 * 涵蓋：未登入 redirect；六塊渲染假資料；`available_pct` 0.05 告警閾值翻轉；
 * 全 null 塊渲染「—」不炸；kill switch 兩顆按鈕在 feature flag 關閉時不出現。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardResp } from "@/lib/api";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
}));

const getDashboard = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
}));

import { COPY_ZH as COPY } from "@/lib/copy";
import DashboardPage from "./page";

const ADDR = "0xAbC0000000000000000000000000000000000001";

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** 六塊皆有值的假資料（數字刻意與設計稿佔位示意值不同，避免「假數字掃描」誤命中）。 */
const FULL: DashboardResp = {
  status: {
    strategy_name: "Filet Core", state: "following", following_days: 41,
    signal_source_ok: true,
    guards: {
      scale: { now: "0.241", max: "0.25" },
      leverage: { now: "1.25", max: "3.0" },
      drawdown: { now: null, max: "-0.10", enabled: true },
    },
  },
  equity: {
    account_value: "1206.67", margin_used: "418.05", withdrawable: "2.69",
    available_pct: "0.0064", ret_30d_pct: "2.4",
  },
  exposure: {
    notional: "521.20", leverage: "1.25", long_pct: "100.0", short_pct: "0.0",
    position_count: 6, max_position: { symbol: "INTC", pct: "29.1" },
  },
  pnl: {
    net: "39.57", realized: "31.48", unrealized: "8.09", fees_paid: "1.66",
    fee_share_of_pnl_pct: "4.2", win_rate_pct: "75.61", closed_positions: 41,
    max_drawdown_pct: "-0.64",
    series: [[1724500000000, "1000"], [1724580000000, "1010"], [1724660000000, "1039.57"]],
  },
  sync: {
    latency_median_ms: 512, latency_p95_ms: 900, price_diff_bp: "2.3",
    unsynced_positions: 0, scale_deviation_pct: "0.8", missed_signals_24h: 1,
    missed_reason: "insufficient_margin", last_recon_ts: 1724805060,
  },
  fees_month: {
    routed_volume: "128300.00", builder_fees: "25.66", fill_count: 96,
    avg_fee: "0.27", effective_rate_bps: "2.00",
    daily_bars: [["2026-08-01", "1.20"], ["2026-08-02", "2.50"]],
  },
  positions: [
    {
      symbol: "ETH", side: "long", leverage: "25", margin_mode: "cross",
      value: "2492.50", upnl: "1.59", entry: "2452.76", mark: "2453.1575",
      deviation_pct: "0.4",
    },
  ],
  updated_at: 1724805063,
};

const ALL_NULL: DashboardResp = {
  status: {
    strategy_name: null, state: "inactive", following_days: null, signal_source_ok: null,
    guards: {
      scale: { now: null, max: null }, leverage: { now: null, max: null },
      drawdown: { now: null, max: null, enabled: null },
    },
  },
  equity: null, exposure: null, pnl: null, sync: null, fees_month: null,
  positions: null, updated_at: 1724805063,
};

beforeEach(() => {
  push.mockReset();
  getDashboard.mockReset();
  mockMe = { data: { address: ADDR, account_id: "fabc" }, isLoading: false };
});

describe("DashboardPage — guard", () => {
  it("未登入 → redirect /strategies", async () => {
    mockMe = { data: null, isLoading: false };
    render(wrap(<DashboardPage />));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
  });
});

describe("DashboardPage — 六塊渲染假資料", () => {
  it("六塊＋持倉表都渲染出對應數字", async () => {
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));

    // ① 狀態
    expect(await screen.findByText(/Filet Core/)).toBeInTheDocument();
    expect(screen.getByText(COPY.dashboard.status.stateFollowing, { exact: false })).toBeInTheDocument();
    // ② 淨值
    expect(screen.getByText("$1,206.67")).toBeInTheDocument();
    // ③ 曝險
    expect(screen.getByText("$521.20")).toBeInTheDocument();
    expect(screen.getByText("29.1% (INTC)")).toBeInTheDocument();
    // ④ PnL
    expect(screen.getByText("+$39.57")).toBeInTheDocument();
    // ⑤ 同步
    expect(screen.getByText("512ms")).toBeInTheDocument();
    // ⑥ 費用
    expect(screen.getByText("$25.66")).toBeInTheDocument();
    // 持倉表
    expect(screen.getByText("ETH")).toBeInTheDocument();
  });
});

describe("DashboardPage — available_pct 0.05 低保證金告警閾值翻轉（NOTE 14）", () => {
  it("0.049（< 0.05）→ 出現告警卡", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, equity: { ...FULL.equity!, available_pct: "0.049" },
    });
    render(wrap(<DashboardPage />));
    expect(await screen.findByText(COPY.dashboard.equity.lowMarginWarning)).toBeInTheDocument();
  });

  it("0.051（≥ 0.05）→ 不出現告警卡", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, equity: { ...FULL.equity!, available_pct: "0.051" },
    });
    render(wrap(<DashboardPage />));
    await screen.findByText("$1,206.67");
    expect(screen.queryByText(COPY.dashboard.equity.lowMarginWarning)).not.toBeInTheDocument();
  });
});

describe("DashboardPage — 全 null 塊不炸（不變量 6）", () => {
  it("六塊全 null → 渲染保守空態，無 undefined/NaN/[object Object]", async () => {
    getDashboard.mockResolvedValue(ALL_NULL);
    const { container } = render(wrap(<DashboardPage />));
    await screen.findByText(COPY.dashboard.status.stateInactive, { exact: false });
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/undefined|NaN|\[object Object\]/);
    expect(text).toContain("—");
  });
});

describe("DashboardPage — kill switch（Task 15 未完成前 feature flag 隱藏）", () => {
  it("暫停跟單／平倉並撤銷授權兩顆按鈕不渲染", async () => {
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.pauseBtn }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.closeAllBtn }))
      .not.toBeInTheDocument();
  });
});
