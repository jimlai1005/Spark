import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type OpsCustomerRow, type OpsCustomersResp, type OpsRevenueResp } from "@/lib/api";

const getOpsCustomers = vi.fn();
const getOpsRevenue = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getOpsCustomers: (...a: unknown[]) => getOpsCustomers(...a),
  getOpsRevenue: (...a: unknown[]) => getOpsRevenue(...a),
}));

import OpsPage from "./page";

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const ROW: OpsCustomerRow = {
  account_id: "fabc0000000000000000000000000000000000000001",
  user_address: "0xabc0000000000000000000000000000000000001",
  label: "alice",
  network: "mainnet",
  fills: 12,
  notional: "125000.5",
  builder_fee: "25.0101",
  taker_share: "0.25",
  account_value: "9876.54",
  subscription: "active",
  error: null,
};

const FAILED_ROW: OpsCustomerRow = {
  ...ROW,
  account_id: "fdef0000000000000000000000000000000000000002",
  user_address: "0xdef0000000000000000000000000000000000002",
  label: "bob",
  fills: 0,
  notional: "0",
  builder_fee: "0",
  taker_share: "0",
  account_value: null,
  subscription: "unknown",
  error: "account_value 查詢失敗: upstream timeout",
};

const CUSTOMERS: OpsCustomersResp = {
  days: 1,
  start: "2026-07-18T00:00:00+00:00",
  end: "2026-07-19T00:00:00+00:00",
  customers: [ROW],
  manifest_errors: [],
};

const REVENUE_OK: OpsRevenueResp = {
  insufficient_accrued_history: false,
  attributed: "25.0101",
  accrued_delta: "25.0301",
  accrued_now: "1025.0301",
  accrued_prev: "1000.0",
  discrepancy: "0.02",
  discrepancy_pct: "0.0008",
  over_threshold: false,
  threshold_pct: "0.01",
  rows: 1,
  day: "2026-07-18",
  prev_day: "2026-07-17",
  window_start: "2026-07-18T00:00:00+00:00",
  window_end: "2026-07-19T00:00:00+00:00",
  customers: [ROW],
  manifest_errors: [],
};

describe("OpsPage", () => {
  it("403 → 僅限管理員（授權在後端；本頁只負責講清楚）", async () => {
    const forbidden = new ApiError("client", "非管理員", 403, "非管理員");
    getOpsRevenue.mockRejectedValue(forbidden);
    getOpsCustomers.mockRejectedValue(forbidden);
    render(wrap(<OpsPage />));
    expect(await screen.findByText("此頁僅限管理員。")).toBeInTheDocument();
  });

  it("正常資料 → 對帳數字與客戶表齊備", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    render(wrap(<OpsPage />));

    // 收入對帳：應收／實收／差額／百分比
    expect(await screen.findByText("25.0101")).toBeInTheDocument();
    expect(screen.getByText("25.0301")).toBeInTheDocument();
    expect(screen.getByText("0.0200")).toBeInTheDocument();
    expect(screen.getByText("0.08%")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();

    // 客戶表：名目、fee、taker 佔比、淨值、訂閱狀態
    expect(screen.getByText("125,000.50")).toBeInTheDocument();
    expect(screen.getByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText("9,876.54")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    // 地址縮寫（shortAddr），非完整位址
    expect(screen.getByText("0xabc0…001")).toBeInTheDocument();
  });

  it("over_threshold → 顯著告警並說明可能原因（modify／非我方路由／鏈上延遲）", async () => {
    getOpsRevenue.mockResolvedValue({
      ...REVENUE_OK, discrepancy: "5.5", discrepancy_pct: "0.22", over_threshold: true,
    });
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    const { container } = render(wrap(<OpsPage />));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("收入對帳超出門檻")).toBeInTheDocument();
    expect(alert.textContent).toMatch(/modify/);
    expect(alert.textContent).toMatch(/非經我方路由/);
    expect(alert.textContent).toMatch(/延遲/);
    // 標紅：差額欄位帶 is-bad（紅色 token --neg）
    expect(container.querySelectorAll("dd.is-bad").length).toBeGreaterThan(0);
  });

  it("insufficient_accrued_history → 顯示累積中訊息，且不把缺值顯示成 0", async () => {
    getOpsRevenue.mockResolvedValue({
      insufficient_accrued_history: true,
      history_points: 1,
      detail: "accrued 歷史不足兩點",
      manifest_errors: [],
    } satisfies OpsRevenueResp);
    getOpsCustomers.mockResolvedValue({ ...CUSTOMERS, customers: [] });
    render(wrap(<OpsPage />));

    expect(await screen.findByText("歷史資料累積中，需至少兩日快照才能對帳。")).toBeInTheDocument();
    // ⭐ 缺 accrued 歷史時絕不出現金額 0.00：0 會被讀成「已對帳且無差額」
    expect(screen.queryByText("0.00")).toBeNull();
    expect(screen.queryByText("收入對帳超出門檻")).toBeNull();
  });

  it("某列 error → 該列標錯，其他列照常渲染（跨客戶隔離）", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue({ ...CUSTOMERS, customers: [ROW, FAILED_ROW] });
    render(wrap(<OpsPage />));

    expect(await screen.findByText(/account_value 查詢失敗: upstream timeout/)).toBeInTheDocument();
    // 失敗列不影響正常列的數字
    expect(screen.getByText("125,000.50")).toBeInTheDocument();
    expect(screen.getByText(ROW.account_id)).toBeInTheDocument();
    expect(screen.getByText(FAILED_ROW.account_id)).toBeInTheDocument();
    // 失敗列的 account_value 為 null → 佔位符，不顯示 0
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("manifest_errors 非空 → 表格上方警示，客戶列照常顯示", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue({
      ...CUSTOMERS, manifest_errors: ["followers[2]: 缺 user_address"],
    });
    render(wrap(<OpsPage />));
    expect(await screen.findByText("followers[2]: 缺 user_address")).toBeInTheDocument();
    expect(screen.getByText(ROW.account_id)).toBeInTheDocument();
  });

  it("天數切換 → 以新的 days 重新查詢", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    render(wrap(<OpsPage />));
    expect(await screen.findByText(ROW.account_id)).toBeInTheDocument();
    expect(getOpsCustomers).toHaveBeenCalledWith(1);

    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    expect(getOpsCustomers).toHaveBeenCalledWith(7);
    await userEvent.click(screen.getByRole("button", { name: "30 天" }));
    expect(getOpsCustomers).toHaveBeenCalledWith(30);
  });

  it("XSS：user_address 與 error 含 <script> 時以純文字呈現（React 轉義）", async () => {
    const evil = "<script>alert(1)</script>";
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue({
      ...CUSTOMERS,
      customers: [{ ...ROW, user_address: evil, error: `boom ${evil}` }],
      manifest_errors: [`manifest ${evil}`],
    });
    const { container } = render(wrap(<OpsPage />));

    expect(await screen.findByText(evil)).toBeInTheDocument();
    expect(screen.getByText(`boom ${evil}`)).toBeInTheDocument();
    expect(screen.getByText(`manifest ${evil}`)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });
});
