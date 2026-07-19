import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  type OpsCustomerRow,
  type OpsCustomersResp,
  type OpsRevenueResp,
  type OpsSubscriptionsResp,
} from "@/lib/api";

const getOpsCustomers = vi.fn();
const getOpsRevenue = vi.fn();
const getOpsSubscriptions = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getOpsCustomers: (...a: unknown[]) => getOpsCustomers(...a),
  getOpsRevenue: (...a: unknown[]) => getOpsRevenue(...a),
  getOpsSubscriptions: (...a: unknown[]) => getOpsSubscriptions(...a),
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
  basis_unknown: false,
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

/** 訂閱對帳：零漂移的基準樣本（各測試以 spread 覆寫需要的欄位）。 */
const SUBS_CLEAN: OpsSubscriptionsResp = {
  local_active_stripe_not: [],
  stripe_active_local_not: [],
  status_mismatch: [],
  orphan_stripe: [],
  in_sync_count: 3,
  drift_count: 0,
  local_count: 3,
  stripe_count: 3,
  superseded_count: 0,
  truncated: false,
};

describe("OpsPage", () => {
  // billing 預設啟用且零漂移：既有測試的斷言不因新區塊改變。
  beforeEach(() => {
    getOpsSubscriptions.mockResolvedValue(SUBS_CLEAN);
  });

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

  it("basis_unknown → 顯示後端 note，且整段不出現任何數字（連區間都不給）", async () => {
    getOpsRevenue.mockResolvedValue({
      insufficient_accrued_history: false,
      basis_unknown: true,
      over_threshold: false,
      day: "2026-07-18",
      prev_day: "2026-07-17",
      window_start: null,
      window_end: null,
      note: "歷史資料缺快照時刻（captured_at），無法對齊窗口；本日對帳跳過。",
      manifest_errors: [],
    } satisfies OpsRevenueResp);
    getOpsCustomers.mockResolvedValue({ ...CUSTOMERS, customers: [] });
    render(wrap(<OpsPage />));

    expect(await screen.findByText(/無法對齊窗口/)).toBeInTheDocument();
    expect(screen.getByText("快照時刻無法對齊，本日對帳跳過。")).toBeInTheDocument();
    // ⭐ 核心斷言：窗口對不齊時整個對帳區塊不得出現**任何**數字。
    // 只斷言 note 有出現是不夠的——渲染出 undefined／0／猜出來的日期一樣會通過。
    // 後端在這個分支不給 attributed／accrued_delta／discrepancy／customers，
    // 前端連 day／prev_day 都不畫（看起來像區間的東西會被讀成已對帳的區間）。
    const section = screen.getByRole("region", { name: "收入對帳" });
    expect(section.textContent).not.toMatch(/\d/);
    expect(section.textContent).not.toMatch(/undefined|NaN/);
    expect(screen.queryByText("收入對帳超出門檻")).toBeNull();
  });

  it("正常路徑 → 顯示實際比較窗口（快照時刻，非日曆日）", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    render(wrap(<OpsPage />));

    // 窗口錯開曾是 Critical bug 且靜默；把兩端攤在畫面上是可視化防線。
    // （「對帳期間」一詞在客戶表的說明文字裡也出現，故限定在對帳區塊的 .ops-window。）
    await screen.findByText("25.0101");
    const section = screen.getByRole("region", { name: "收入對帳" });
    const win = section.querySelector(".ops-window");
    expect(win?.textContent).toContain("2026-07-18T00:00:00+00:00");
    expect(win?.textContent).toContain("2026-07-19T00:00:00+00:00");
  });

  it("訂閱對帳：零漂移 → 顯示一致訊息，四類清單皆為空且無警示", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsSubscriptions.mockResolvedValue(SUBS_CLEAN);
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "訂閱對帳" });
    expect(within(section).getByText("四類漂移皆為零，本地與 Stripe 一致。")).toBeInTheDocument();
    // 四類標題以危害敘述呈現（不是欄位名），且皆已檢查過
    expect(within(section).getByText(/客戶付了錢卻沒拿到權益/)).toBeInTheDocument();
    expect(within(section).getByText(/還在提供服務卻收不到錢/)).toBeInTheDocument();
    expect(within(section).getByText(/兩邊狀態不一致/)).toBeInTheDocument();
    expect(within(section).getByText(/對不到本地帳號的 Stripe 訂閱/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("訂閱對帳：有漂移 → 各清單列出明細，危害最高者排在最前", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsSubscriptions.mockResolvedValue({
      ...SUBS_CLEAN,
      stripe_active_local_not: [{
        account_id: "fpaid000000000000000000000000000000000001",
        local_status: null, stripe_status: "active", stripe_status_raw: "active",
        stripe_subscription_id: "sub_paid_no_entitlement", matched_by: null,
      }],
      local_active_stripe_not: [{
        account_id: "flost000000000000000000000000000000000002",
        local_status: "active", stripe_status: null, stripe_status_raw: null,
        stripe_subscription_id: "sub_gone", matched_by: null,
      }],
      status_mismatch: [{
        account_id: "fmism000000000000000000000000000000000003",
        local_status: "past_due", stripe_status: "canceled", stripe_status_raw: "canceled",
        stripe_subscription_id: "sub_mismatch", matched_by: "subscription_id",
      }],
      orphan_stripe: [{
        account_id: null, local_status: null,
        stripe_status: "canceled", stripe_status_raw: "incomplete_expired",
        stripe_subscription_id: "sub_orphan", matched_by: null,
      }],
      in_sync_count: 1, drift_count: 4, local_count: 3, stripe_count: 4, superseded_count: 2,
    } satisfies OpsSubscriptionsResp);
    const { container } = render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "訂閱對帳" });
    expect(within(section).getByText("sub_paid_no_entitlement")).toBeInTheDocument();
    expect(within(section).getByText("sub_gone")).toBeInTheDocument();
    expect(within(section).getByText("sub_mismatch")).toBeInTheDocument();
    expect(within(section).getByText("sub_orphan")).toBeInTheDocument();
    // Stripe 原始值與正規化值分開顯示（同基準比較的前後兩個值都要看得到）
    expect(within(section).getByText("incomplete_expired")).toBeInTheDocument();
    // ⭐ 清單順序＝危害順序：付了錢沒權益必須排在漏財之前
    const titles = Array.from(section.querySelectorAll(".ops-drift-title"))
      .map((el) => el.textContent ?? "");
    expect(titles[0]).toMatch(/客戶付了錢卻沒拿到權益/);
    expect(titles[1]).toMatch(/還在提供服務卻收不到錢/);
    // 漂移非零 → 不顯示「一致」訊息；superseded 有值 → 說明它不是漂移
    expect(within(section).queryByText("四類漂移皆為零，本地與 Stripe 一致。")).toBeNull();
    expect(within(section).getByText(/不計入漂移合計/)).toBeInTheDocument();
    // 漂移合計標紅
    expect(container.querySelectorAll("dd.is-bad").length).toBeGreaterThan(0);
  });

  it("訂閱對帳：truncated=true → role=alert 警告樣本不完整、結論不可信", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsSubscriptions.mockResolvedValue({
      ...SUBS_CLEAN,
      truncated: true,
      local_active_stripe_not: [{
        account_id: "fmaybe00000000000000000000000000000000004",
        local_status: "active", stripe_status: null, stripe_status_raw: null,
        stripe_subscription_id: "sub_maybe_false_drift", matched_by: null,
      }],
      drift_count: 1,
    } satisfies OpsSubscriptionsResp);
    render(wrap(<OpsPage />));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("樣本不完整，本區塊結論不可信")).toBeInTheDocument();
    // 假漂移的成因要講清楚，否則 admin 會照著這張表去停用正常付費的客戶
    expect(alert.textContent).toMatch(/1000 筆上限/);
    expect(alert.textContent).toMatch(/假漂移/);
  });

  it("訂閱對帳：billing 未啟用（501）→ 整個區塊隱藏，其餘區塊照常", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsSubscriptions.mockRejectedValue(
      new ApiError("client", "計費未啟用", 501, "計費未啟用"),
    );
    render(wrap(<OpsPage />));

    // 收入對帳與客戶表不受影響
    expect(await screen.findByText("25.0101")).toBeInTheDocument();
    expect(screen.getByText(ROW.account_id)).toBeInTheDocument();
    // 501 是「還沒開放」不是錯誤：整段不渲染，也不留一塊紅色錯誤
    expect(screen.queryByRole("region", { name: "訂閱對帳" })).toBeNull();
    expect(screen.queryByText("訂閱對帳")).toBeNull();
    expect(screen.queryByText(/計費未啟用/)).toBeNull();
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
