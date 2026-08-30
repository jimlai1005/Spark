/**
 * `/dashboard` — EN 模式無繁中殘留（M3 round4 Task R4-4 規格 6）。
 * 新檔（不改既有 `page.test.tsx`）：`localStorage.filet_lang=en` 下渲染整頁
 * （六塊皆有值＋kill switch 暫停確認彈窗開啟時），斷言 `container.textContent`
 * 不含 CJK 字元（白名單：位址/數字/「繁中」語言切換標籤本身，此頁未渲染 Header
 * 故不會出現）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardResp, PauseResp } from "@/lib/api";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
}));

const getDashboard = vi.fn();
const postPause = vi.fn<(a0: string) => Promise<PauseResp>>();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
  postPause: (...a: [string]) => postPause(...a),
}));

vi.mock("wagmi", () => ({
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
}));

vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: vi.fn(),
}));

import { LangProvider } from "@/lib/lang";
import DashboardPage from "./page";

const ADDR = "0xAbC0000000000000000000000000000000000001";

const CJK = /[一-鿿]/;

function wrapEn(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <LangProvider>{children}</LangProvider>
    </QueryClientProvider>
  );
}

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
    missed_reason: null, last_recon_ts: 1724805060,
    data_state: "ok", since_ts: null,
  },
  fees_month: {
    routed_volume: "128300.00", builder_fees: "25.66", fill_count: 96,
    avg_fee: "0.27", effective_rate_bps: "2.00",
    daily_bars: [
      { date: "2026-08-01", fill_count: 3, routed_volume: "6000", builder_fee: "1.20", effective_rate_bps: "2.00" },
    ],
  },
  positions: [
    {
      symbol: "ETH", side: "long", leverage: "25", margin_mode: "cross",
      value: "2492.50", upnl: "1.59", entry: "2452.76", mark: "2453.1575",
      deviation_pct: "0.4",
    },
  ],
  risk_controls_enabled: true,
  updated_at: 1724805063,
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("filet_lang", "en");
  push.mockReset();
  getDashboard.mockReset();
  postPause.mockReset();
  getDashboard.mockResolvedValue(FULL);
  mockMe = { data: { address: ADDR, account_id: "fabc" }, isLoading: false };
});

describe("DashboardPage — EN 模式無 CJK 殘留", () => {
  it("六塊渲染完成後，textContent 不含任何 CJK 字元", async () => {
    render(wrapEn(<DashboardPage />));
    await screen.findByText(/Filet Core/);

    expect(document.body.textContent ?? "").not.toMatch(CJK);
  });

  it("暫停確認彈窗開啟時，彈窗文案也不含 CJK 字元", async () => {
    render(wrapEn(<DashboardPage />));
    await screen.findByText(/Filet Core/);

    await userEvent.click(screen.getByRole("button", { name: /pause/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    expect(document.body.textContent ?? "").not.toMatch(CJK);
  });
});
