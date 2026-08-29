/**
 * `/dashboard` — Opus 審查 Critical 2(c)：平倉並撤銷輪詢逾時的明確失敗路徑。
 * 過去無限輪詢，引擎離線或請求過期時畫面永遠停在「約一分鐘內完成」——安全動作
 * 失敗不能只有前端沉默轉圈（工程原則 3）。本檔驗證：後端判定 `close_request.state
 * === "expired"` → 停止輪詢＋顯示明確失敗卡（不再顯示「即將完成」的進度卡）。
 *
 * 沿 `page.test.tsx` 既有的 mock 慣例（同一組 vi.mock，避免兩份定義漂移）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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

const FULL: DashboardResp = {
  status: {
    strategy_name: "Filet Core", state: "following", following_days: 41,
    signal_source_ok: true, close_request: null,
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
  exposure: null, pnl: null, sync: null, fees_month: null,
  positions: [],
  updated_at: 1724805063,
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

function openModalAndConfirm() {
  fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.closeAllBtn }));
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: COPY.dashboard.status.closeAllModal.confirmBtn }));
}

describe("DashboardPage — 平倉並撤銷輪詢逾時的明確失敗路徑（opus Critical 2c）", () => {
  it("close_request.state==='expired' → 停止輪詢＋顯示明確失敗卡，不再顯示進度卡", async () => {
    getDashboard.mockResolvedValueOnce(FULL);
    const message =
      "Filet: close all positions and revoke copy-trading\n\nAccount: fabc\nNonce: n1\nIssued At: 2026-08-28T00:00:00Z";
    getCloseAllMessage.mockResolvedValue({
      message, nonce: "n1", issued_at: "2026-08-28T00:00:00Z", account_id: "fabc",
    });
    postCloseAll.mockResolvedValue({
      ok: true, account_id: "fabc", effective: "next_engine_cycle", effective_note: "",
    });
    // ⭐ 在送出前就把「引擎已判定過期」設成後續所有呼叫的預設值——
    // `onCloseAllSubmitted` 送出成功後會立即 `dash.refetch()`，那一次呼叫必須
    // 已經拿得到這份資料，不能依賴之後才會發生的下一輪輪詢（真實計時器下測試
    // 不會等 10 秒）。`mockResolvedValueOnce(FULL)` 只吃掉第一次（初始載入）。
    getDashboard.mockResolvedValue({
      ...FULL,
      status: { ...FULL.status!, close_request: { state: "expired" } },
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    openModalAndConfirm();
    await waitFor(() => expect(postCloseAll).toHaveBeenCalled());

    expect(await screen.findByText(COPY.dashboard.status.closeAllFailed.title)).toBeInTheDocument();
    expect(screen.getByText(COPY.dashboard.status.closeAllFailed.note)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.dashboard.status.closeAllFailed.linkLabel }))
      .toHaveAttribute("href", "https://app.hyperliquid.xyz/API");
    // 進度卡不該與失敗卡並存（不再暗示「即將完成」）。
    expect(screen.queryByText(COPY.dashboard.status.closeAllProgress.title)).not.toBeInTheDocument();
  });

  it("state 一路維持 pending（無 close_request）→ 持續顯示進度卡，不誤判失敗", async () => {
    getDashboard.mockResolvedValue(FULL);
    getCloseAllMessage.mockResolvedValue({
      message: "Filet: close all positions and revoke copy-trading\n\nAccount: fabc\nNonce: n1\nIssued At: x",
      nonce: "n1", issued_at: "x", account_id: "fabc",
    });
    postCloseAll.mockResolvedValue({
      ok: true, account_id: "fabc", effective: "next_engine_cycle", effective_note: "",
    });
    render(wrap(<DashboardPage />));
    await screen.findByText(/Filet Core/);
    openModalAndConfirm();
    await waitFor(() => expect(postCloseAll).toHaveBeenCalled());

    expect(await screen.findByText(COPY.dashboard.status.closeAllProgress.title)).toBeInTheDocument();
    expect(screen.queryByText(COPY.dashboard.status.closeAllFailed.title)).not.toBeInTheDocument();
  });
});
