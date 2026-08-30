/**
 * `/dashboard` — 六塊 Dashboard 測試（Task 14）＋ kill switch 暫停/平倉並撤銷
 * （Task 15，接上 KILL_SWITCH_ENABLED 後的真實行為）。
 * 涵蓋：未登入 redirect；六塊渲染假資料；`available_pct` 0.05 告警閾值翻轉；
 * 全 null 塊渲染「—」不炸；kill switch 兩顆按鈕的渲染條件、暫停/恢復呼叫、
 * halted 態的官方介面指引卡、平倉並撤銷 modal 的二次確認閘門。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CloseAllMessageResp, CloseAllResp, DashboardResp, PauseResp } from "@/lib/api";

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
const getCloseAllMessage = vi.fn<() => Promise<CloseAllMessageResp>>();
const postCloseAll = vi.fn<(a0: CloseAllMessageResp, a1: string) => Promise<CloseAllResp>>();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getDashboard: (...a: unknown[]) => getDashboard(...a),
  postPause: (...a: [string]) => postPause(...a),
  getCloseAllMessage: (...a: []) => getCloseAllMessage(...a),
  postCloseAll: (...a: [CloseAllMessageResp, string]) => postCloseAll(...a),
}));

const signMessageAsync = vi.fn(async () => `0x${"ab".repeat(65)}`);
vi.mock("wagmi", () => ({
  useSignMessage: () => ({ signMessageAsync }),
}));

vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: vi.fn(async () => ADDR.toLowerCase()),
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
    data_state: "ok", since_ts: null,
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
  risk_controls_enabled: true,
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
  positions: null, risk_controls_enabled: false, updated_at: 1724805063,
};

beforeEach(() => {
  push.mockReset();
  getDashboard.mockReset();
  postPause.mockReset();
  getCloseAllMessage.mockReset();
  postCloseAll.mockReset();
  signMessageAsync.mockClear();
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

    // 狀態
    expect(await screen.findByText(/Filet Core/)).toBeInTheDocument();
    expect(screen.getByText(COPY.dashboard.status.stateFollowing, { exact: false })).toBeInTheDocument();
    // 淨值
    expect(screen.getByText("$1,206.67")).toBeInTheDocument();
    // 曝險
    expect(screen.getByText("$521.20")).toBeInTheDocument();
    expect(screen.getByText("29.1% (INTC)")).toBeInTheDocument();
    // PnL
    expect(screen.getByText("+$39.57")).toBeInTheDocument();
    // 同步
    expect(screen.getByText("512ms")).toBeInTheDocument();
    // 費用（builder_fees 累計列已依 M3 round2 Task 4 隱藏，改斷言仍保留的路由交易量）
    expect(screen.getByText("$128,300")).toBeInTheDocument();
    // 持倉表
    expect(screen.getByText("ETH")).toBeInTheDocument();
  });
});

describe("DashboardPage — 最後同步顯示（Task 19 修正）", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("<60s → 顯示「剛剛」，不帶「Xs 前」", async () => {
    vi.setSystemTime((FULL.updated_at + 30) * 1000);
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    expect(screen.getByText(`${COPY.dashboard.lastSyncPrefix}${COPY.dashboard.lastSyncJustNow}`)).toBeInTheDocument();
  });

  it("介於 1 分鐘到 24 小時 → 顯示「Xm 前」/「Xh 前」", async () => {
    vi.setSystemTime((FULL.updated_at + 3 * 3600) * 1000);
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    expect(screen.getByText(`${COPY.dashboard.lastSyncPrefix}3h${COPY.dashboard.lastSyncSuffix}`)).toBeInTheDocument();
  });

  it(">24h → 顯示日期（YYYY-MM-DD），不是「8766h 前」", async () => {
    vi.setSystemTime((FULL.updated_at + 8766 * 3600) * 1000);
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    const expectedDate = new Date(FULL.updated_at * 1000).toISOString().slice(0, 10);
    expect(screen.getByText(`${COPY.dashboard.lastSyncPrefix}${expectedDate}`)).toBeInTheDocument();
    expect(screen.queryByText(/8766h/)).not.toBeInTheDocument();
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

describe("DashboardPage — kill switch 按鈕渲染條件（Task 15）", () => {
  it("state=following → 暫停跟單／平倉並撤銷授權兩顆按鈕都渲染", async () => {
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    expect(screen.getByRole("button", { name: COPY.dashboard.status.pauseBtn }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: COPY.dashboard.status.closeAllBtn }))
      .toBeInTheDocument();
  });

  it("state=paused → 顯示「恢復跟單」而非「暫停跟單」", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, status: { ...FULL.status!, state: "paused" },
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    expect(screen.getByRole("button", { name: COPY.dashboard.status.resumeBtn }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.pauseBtn }))
      .not.toBeInTheDocument();
  });

  it("state=inactive → 兩顆按鈕都不渲染（沒有引擎可操作）", async () => {
    getDashboard.mockResolvedValue(ALL_NULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(COPY.dashboard.status.stateInactive, { exact: false });
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.pauseBtn }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.closeAllBtn }))
      .not.toBeInTheDocument();
  });

  it("state=halted → 兩顆按鈕不渲染，改顯示官方介面指引卡", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, status: { ...FULL.status!, state: "halted" },
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.pauseBtn }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: COPY.dashboard.status.closeAllBtn }))
      .not.toBeInTheDocument();
    expect(screen.getByText(COPY.dashboard.status.closeAllDone.title)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.dashboard.status.closeAllDone.linkLabel }))
      .toHaveAttribute("href", "https://app.hyperliquid.xyz/API");
  });
});

describe("DashboardPage — 暫停/恢復（無需簽章，Task 15）", () => {
  it("點擊「暫停跟單」→ 呼叫 postPause('pause')，成功後重新整理 dashboard", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, status: { ...FULL.status!, state: "following" },
    });
    postPause.mockResolvedValue({
      ok: true, paused: true, effective: "next_engine_cycle", effective_note: "",
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    const callsBefore = getDashboard.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.pauseBtn }));

    await waitFor(() => expect(postPause).toHaveBeenCalledWith("pause"));
    await waitFor(() =>
      expect(getDashboard.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it("點擊「恢復跟單」→ 呼叫 postPause('resume')", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, status: { ...FULL.status!, state: "paused" },
    });
    postPause.mockResolvedValue({
      ok: true, paused: false, effective: "next_engine_cycle", effective_note: "",
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);

    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.resumeBtn }));

    await waitFor(() => expect(postPause).toHaveBeenCalledWith("resume"));
  });

  it("postPause 失敗 → 顯示錯誤文案，不悄悄吞掉", async () => {
    getDashboard.mockResolvedValue({
      ...FULL, status: { ...FULL.status!, state: "following" },
    });
    postPause.mockRejectedValue(new Error("500"));
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);

    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.pauseBtn }));

    expect(await screen.findByText(COPY.dashboard.status.pauseErrorNote)).toBeInTheDocument();
  });
});

describe("DashboardPage — 平倉並撤銷 modal（Task 15 kill switch 第二級）", () => {
  function openModal() {
    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.closeAllBtn }));
  }

  it("點擊「平倉並撤銷授權」→ 開啟 modal，列出目前持倉＋不可逆警語", async () => {
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);

    openModal();

    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(COPY.dashboard.status.closeAllModal.title)).toBeInTheDocument();
    expect(dialog.getByText(COPY.dashboard.status.closeAllModal.warning)).toBeInTheDocument();
    expect(dialog.getByText(/ETH/)).toBeInTheDocument(); // FULL.positions[0].symbol
  });

  it("⭐ 二次確認閘門：勾選前確認鈕 disabled，勾選後才能點", async () => {
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    openModal();

    const confirmBtn = screen.getByRole("button", { name: COPY.dashboard.status.closeAllModal.confirmBtn });
    expect(confirmBtn).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(confirmBtn).not.toBeDisabled();
  });

  it("取消按鈕關閉 modal，不呼叫任何簽章流程", async () => {
    getDashboard.mockResolvedValue(FULL);
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    openModal();

    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.closeAllModal.cancelBtn }));

    expect(screen.queryByText(COPY.dashboard.status.closeAllModal.title)).not.toBeInTheDocument();
    expect(getCloseAllMessage).not.toHaveBeenCalled();
  });

  it("⭐⭐ 完整簽署流程：勾選 → 確認 → 簽名 → 送出成功 → modal 關閉並開始輪詢進度卡", async () => {
    getDashboard.mockResolvedValue(FULL);
    const message =
      "Filet: close all positions and revoke copy-trading\n\nAccount: fabc\nNonce: n1\nIssued At: 2026-08-28T00:00:00Z";
    getCloseAllMessage.mockResolvedValue({
      message, nonce: "n1", issued_at: "2026-08-28T00:00:00Z", account_id: "fabc",
    });
    postCloseAll.mockResolvedValue({
      ok: true, account_id: "fabc", effective: "next_engine_cycle", effective_note: "",
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    openModal();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.closeAllModal.confirmBtn }));

    await waitFor(() => expect(postCloseAll).toHaveBeenCalledWith(
      { message, nonce: "n1", issued_at: "2026-08-28T00:00:00Z", account_id: "fabc" },
      `0x${"ab".repeat(65)}`,
    ));
    await waitFor(() =>
      expect(screen.queryByText(COPY.dashboard.status.closeAllModal.title)).not.toBeInTheDocument());
    expect(await screen.findByText(COPY.dashboard.status.closeAllProgress.title)).toBeInTheDocument();
  });

  it("簽章者與登入帳號不符 → content-mismatch，不喚起錢包（域分隔前端防線）", async () => {
    getDashboard.mockResolvedValue(FULL);
    getCloseAllMessage.mockResolvedValue({
      message: "Filet: close all positions and revoke copy-trading\n\nAccount: fother\nNonce: n1\nIssued At: x",
      nonce: "n1", issued_at: "x", account_id: "fother", // 帳號不是我
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    openModal();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.closeAllModal.confirmBtn }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postCloseAll).not.toHaveBeenCalled();
  });
});
