import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  type OpsCustomerRow,
  type OpsCustomersResp,
  type OpsHealthFollower,
  type OpsHealthResp,
  type OpsRevenueResp,
  type OpsSubscriptionsResp,
  type OpsTradeQualityResp,
  type OpsTradeQualityRow,
} from "@/lib/api";

const getOpsCustomers = vi.fn();
const getOpsRevenue = vi.fn();
const getOpsSubscriptions = vi.fn();
const getOpsTradeQuality = vi.fn();
const getOpsHealth = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getOpsCustomers: (...a: unknown[]) => getOpsCustomers(...a),
  getOpsRevenue: (...a: unknown[]) => getOpsRevenue(...a),
  getOpsSubscriptions: (...a: unknown[]) => getOpsSubscriptions(...a),
  getOpsTradeQuality: (...a: unknown[]) => getOpsTradeQuality(...a),
  getOpsHealth: (...a: unknown[]) => getOpsHealth(...a),
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

/**
 * 預設樣本＝同基準模式（window=accrued）。窗口刻意與 REVENUE_OK 完全相同——
 * 後端讓兩個端點共用同一個窗口推導函式，fixture 必須反映那個保證。
 */
const CUSTOMERS: OpsCustomersResp = {
  window: "accrued",
  basis_unknown: false,
  start: "2026-07-18T00:00:00+00:00",
  end: "2026-07-19T00:00:00+00:00",
  window_start: "2026-07-18T00:00:00+00:00",
  window_end: "2026-07-19T00:00:00+00:00",
  customers: [ROW],
  manifest_errors: [],
};

/** 自由檢視窗（days）：與對帳窗必定錯開，這個模式下兩張表本來就不可相減。 */
const CUSTOMERS_DAYS: OpsCustomersResp = {
  window: "days",
  days: 7,
  start: "2026-07-12T00:00:00+00:00",
  end: "2026-07-19T00:00:00+00:00",
  window_start: "2026-07-12T00:00:00+00:00",
  window_end: "2026-07-19T00:00:00+00:00",
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

/**
 * 成交品質／系統健康的**預設**樣本＝「空但形狀完整」。
 * 刻意不放任何客戶列：既有測試用 getByText 斷言單一命中（例如 taker 佔比 "25.0%"），
 * 新區塊若預設就渲染另一組客戶數字，會把既有斷言變成「找到多個」而誤紅——
 * 那是測試互相污染，不是被測行為改變。各自的行為由下方新增的測試釘住。
 */
const TQ_EMPTY: OpsTradeQualityResp = {
  window: "accrued",
  basis_unknown: false,
  window_start: "2026-07-18T00:00:00+00:00",
  window_end: "2026-07-19T00:00:00+00:00",
  skipped_days: [],
  followers: [],
  summary: {
    followers: 0, quality_available_count: 0, te_available_count: 0,
    skipped_available_count: 0, worst_median_delay_s: null, delay_sample: 0,
    worst_taker_slippage_bp_median: null, slippage_sample: 0,
  },
  manifest_errors: [],
};

const HEALTH_EMPTY: OpsHealthResp = {
  checked_at: "2026-07-19T00:10:00+00:00",
  engine_stale_after_s: 600,
  followers: [],
  unapplied_leader_changes: [],
  summary: {
    followers: 0, engine_alive_count: 0, engine_stale_count: 0,
    engine_unknown_count: 0, killswitch_tripped_count: 0,
    killswitch_unknown_count: 0, coverage_insufficient_count: 0,
    coverage_unknown_count: 0, alerts_total: 0, alerts_unknown_count: 0,
    heartbeat_ok_count: 0, heartbeat_stale_count: 0, heartbeat_missing_count: 0,
    unapplied_leader_changes: 0, leader_change_errors: [],
  },
  manifest_errors: [],
};

/** 健康列的基準：心跳新鮮、全部可讀且健康。各測試以 spread 只覆寫它要考的那一格。 */
const H_ROW: OpsHealthFollower = {
  account_id: "fh010000000000000000000000000000000000001",
  label: "alice",
  network: "mainnet",
  liveness_basis: "equity_sample",
  state_root: "/var/lib/filet/fh01/",
  heartbeat_status: "ok",
  heartbeat_at: "2026-07-19T00:09:30+00:00",
  heartbeat_age_s: 30,
  heartbeat_stale_after_s: 600,
  basis: "heartbeat",
  leader_address: "0xlead000000000000000000000000000000000001",
  leader_source: "signed",
  leader_kind: "standard",
  capital: {
    allocated_capital: "10000.00",
    capital_utilization: "0.2500",
    use_full_equity: false,
    source: "signed",
    changed_at: "2026-07-18T09:00:00+00:00",
  },
  last_cycle: { result: "ok", detail: null },
  coverage_known: true,
  sample_count: 120,
  sample_coverage_sufficient: true,
  last_sample_age_s: 30,
  engine_alive: true,
  killswitch_tripped: false,
  killswitch_known: true,
  alerts: 0,
  error: null,
};

/**
 * ⭐ 每一格都讀不到：狀態根讀不到（0700，面板跑在 filet-api）**且**心跳也收不到。
 * 這是預設部署下最該被正確呈現的形狀——什麼都看不見。
 */
const H_ROW_UNKNOWN: OpsHealthFollower = {
  ...H_ROW,
  account_id: "fh990000000000000000000000000000000000009",
  label: "zoe",
  heartbeat_status: "missing",
  heartbeat_at: null,
  heartbeat_age_s: null,
  basis: "unreadable",
  leader_address: null,
  leader_source: null,
  leader_kind: null,
  capital: null,
  last_cycle: null,
  coverage_known: false,
  sample_count: null,
  sample_coverage_sufficient: null,
  last_sample_age_s: null,
  engine_alive: null,
  killswitch_tripped: null,
  killswitch_known: false,
  alerts: null,
  error: "狀態根讀不到（/var/lib/filet/fh99/）——kill switch、覆蓋度、告警數"
    + "改由引擎發布的健康心跳提供；心跳也讀不到時整列維持未知",
};

/**
 * ⭐⭐ 心跳過期：後端在過期時**結構性地不回傳 payload**，所以 leader／資金設定／
 * kill switch 全是 null——只剩最後心跳時刻與年齡。這正是「過期的綠燈比沒有燈更
 * 危險」要防的形狀：40 分鐘前的「未觸發」不得被畫成現在的狀態。
 */
const H_ROW_STALE: OpsHealthFollower = {
  ...H_ROW_UNKNOWN,
  account_id: "fh500000000000000000000000000000000000005",
  label: "stale",
  heartbeat_status: "stale",
  heartbeat_at: "2026-07-18T23:10:00+00:00",
  heartbeat_age_s: 3600,
  error: null,
};

function healthWith(
  rows: OpsHealthFollower[],
  summary: Partial<OpsHealthResp["summary"]> = {},
  rest: Partial<OpsHealthResp> = {},
): OpsHealthResp {
  return {
    ...HEALTH_EMPTY,
    followers: rows,
    ...rest,
    summary: { ...HEALTH_EMPTY.summary, followers: rows.length, ...summary },
  };
}

/** 成交品質列的基準：全部量測皆可得。 */
const Q_ROW: Extract<OpsTradeQualityRow, { quality_available: true }> = {
  account_id: "fq010000000000000000000000000000000000001",
  label: "alice",
  network: "mainnet",
  quality_available: true,
  fills: 8,
  taker_share: "0.5",
  te_available: true,
  pair_count: 6,
  median_delay_s: "2",
  taker_slippage_bp_median: "12.5",
  skipped_available: true,
  skipped_small_notional: "150",
  skipped_small_ratio: "0.02",
  error: null,
};

function qualityWith(
  rows: OpsTradeQualityRow[],
  summary: Partial<Extract<OpsTradeQualityResp, { basis_unknown: false }>["summary"]> = {},
): Extract<OpsTradeQualityResp, { basis_unknown: false }> {
  const tq = TQ_EMPTY as Extract<OpsTradeQualityResp, { basis_unknown: false }>;
  return {
    window: "accrued" as const,
    basis_unknown: false as const,
    window_start: tq.window_start,
    window_end: tq.window_end,
    skipped_days: ["2026-07-18", "2026-07-19"],
    followers: rows,
    summary: { ...tq.summary, followers: rows.length, ...summary },
    manifest_errors: [],
  };
}

describe("OpsPage", () => {
  // billing 預設啟用且零漂移：既有測試的斷言不因新區塊改變。
  beforeEach(() => {
    getOpsSubscriptions.mockResolvedValue(SUBS_CLEAN);
    getOpsTradeQuality.mockResolvedValue(TQ_EMPTY);
    getOpsHealth.mockResolvedValue(HEALTH_EMPTY);
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

  // ---------- 共用比較窗口（同基準的可視化防線） ----------
  // 背景：收入對帳與客戶損益的窗口曾錯開一整天，健康帳戶被誤判 199 倍差異並觸發告警。
  // 後端已讓兩個端點共用同一個窗口推導函式；以下三條測試釘住「前端把那個保證顯示出來，
  // 而且在保證失效時大聲說出來」——保證本身在後端，可視化防線在這裡。

  it("⭐ 預設 window=accrued → 兩張表共用一個窗口標頭（顯示一次），無不一致警告", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    render(wrap(<OpsPage />));

    expect(await screen.findByText(ROW.account_id)).toBeInTheDocument();
    // 預設就是同基準模式（不是 days）——這是本頁可相減的唯一模式
    expect(getOpsCustomers).toHaveBeenCalledWith({ window: "accrued" });

    // 共用標頭只出現一次，且明說兩張表同窗口
    expect(screen.getByText("以下兩張表使用同一比較窗口")).toBeInTheDocument();
    const banner = document.querySelector(".ops-window-banner");
    expect(banner?.textContent).toMatch(/可直接對照相減/);
    expect(banner?.textContent).toContain("2026-07-18T00:00:00+00:00");
    expect(banner?.textContent).toContain("2026-07-19T00:00:00+00:00");
    expect(document.querySelectorAll(".ops-window-banner").length).toBe(1);

    // 窗口一致 → 不得出現任何警告
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("兩張表的比較窗口不一致，數字不可相減")).toBeNull();
  });

  it("⭐ 兩張表窗口竟然不同 → role=alert 警告數字不可相減（前端不盲信後端保證）", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue({
      ...CUSTOMERS,
      // 錯開一整天——正是那個 Critical 的形狀。後端保證不會發生，但保證退化時
      // 前端必須是第二道防線，而不是安靜地照樣把兩張表並排。
      window_start: "2026-07-17T00:00:00+00:00",
      window_end: "2026-07-18T00:00:00+00:00",
    });
    render(wrap(<OpsPage />));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("兩張表的比較窗口不一致，數字不可相減")).toBeInTheDocument();
    // 警告要直接指出差在哪：兩組窗口並排列出，不必自己翻兩張表
    expect(alert.textContent).toContain("2026-07-19T00:00:00+00:00"); // 收入對帳側
    expect(alert.textContent).toContain("2026-07-17T00:00:00+00:00"); // 客戶損益側
    // 不得一邊警告不一致、一邊還宣稱同基準
    expect(screen.queryByText("以下兩張表使用同一比較窗口")).toBeNull();
  });

  it("customers basis_unknown → 顯示後端 note，該區塊不出現任何數字也不畫空表", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue({
      window: "accrued",
      basis_unknown: true,
      window_start: null,
      window_end: null,
      note: "歷史資料缺快照時刻（captured_at），無法對齊窗口；本次客戶損益無法與收入對帳同基準，故不計算。",
      manifest_errors: [],
    } satisfies OpsCustomersResp);
    render(wrap(<OpsPage />));

    expect(await screen.findByText(/無法與收入對帳同基準/)).toBeInTheDocument();
    expect(screen.getByText("快照時刻無法對齊，本表跳過計算。")).toBeInTheDocument();

    const section = screen.getByRole("region", { name: "每客戶損益" });
    // ⭐ 沿 revenue basis_unknown 分支的嚴格度：不畫表（空表會被讀成「今天沒人成交」），
    // 說明面板內不得出現任何數字。（數字檢查限定在面板內：本區塊有 1/7/30 切換鈕。）
    expect(section.querySelector(".ops-table")).toBeNull();
    const notice = section.querySelector(".ops-notice");
    expect(notice?.textContent).not.toMatch(/\d/);
    expect(notice?.textContent).not.toMatch(/undefined|NaN/);
    expect(screen.queryByText("125,000.50")).toBeNull();
    expect(screen.queryByText(ROW.account_id)).toBeNull();
    // 窗口算不出來 → 不渲染共用標頭（不謊報一致），也不誤報不一致
    expect(screen.queryByText("以下兩張表使用同一比較窗口")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
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

  it("期間切換 → days 模式送 {days}，切回對帳窗口送 {window:'accrued'}", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockImplementation((q: { days?: number; window?: string }) =>
      Promise.resolve(q?.window === "accrued" ? CUSTOMERS : { ...CUSTOMERS_DAYS, days: q?.days }),
    );
    render(wrap(<OpsPage />));
    expect(await screen.findByText(ROW.account_id)).toBeInTheDocument();
    // ⭐ 預設是同基準模式，不是 1 天自由窗
    expect(getOpsCustomers).toHaveBeenCalledWith({ window: "accrued" });

    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    expect(getOpsCustomers).toHaveBeenCalledWith({ days: 7 });
    await userEvent.click(screen.getByRole("button", { name: "30 天" }));
    expect(getOpsCustomers).toHaveBeenCalledWith({ days: 30 });

    // ⭐ days 模式下兩張表本來就不同基準：改口說「不可相減」，且**不**誤報窗口不一致。
    // 每次切天數都跳紅色警告會訓練人忽略它，等於親手拆掉這道防線。
    expect(await screen.findByText(/不可直接相減/)).toBeInTheDocument();
    expect(screen.queryByText("以下兩張表使用同一比較窗口")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "對帳窗口（同基準）" }));
    expect(getOpsCustomers).toHaveBeenCalledWith({ window: "accrued" });
    expect(await screen.findByText("以下兩張表使用同一比較窗口")).toBeInTheDocument();
  });

  // ---------- ⭐ 系統健康（謊報健康比沒有面板更危險） ----------
  // 本組測試的核心不是「有沒有畫出來」，而是「讀不到的格子有沒有被畫成健康值」。
  // 健康字樣（一切良好／未觸發／已生效）在 COPY.ops.health 裡刻意只出現在對應的
  // 健康分支，不出現在任何說明文字裡——所以 not.toMatch 才能整段掃而不誤傷。

  /**
   * 取某個 account_id 的第 n 張表裡那一列（<tr>）。
   * 系統健康區塊有兩張表（健康列、引擎現況），同一個 account_id 會出現兩次，
   * 所以要能指名是哪一張——預設取第一張（健康列）。
   */
  function rowOf(section: HTMLElement, accountId: string, nth = 0): HTMLElement {
    const cells = within(section).getAllByText(accountId);
    const tr = cells[nth]?.closest("tr");
    if (!tr) throw new Error(`找不到 ${accountId} 的第 ${nth} 列`);
    return tr as HTMLElement;
  }

  it("⭐⭐ 健康：每一格都讀不到 → 一律顯示未知，畫面不得出現任何健康字樣", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith([H_ROW_UNKNOWN], {
      engine_unknown_count: 1, killswitch_unknown_count: 1,
      coverage_unknown_count: 1, alerts_unknown_count: 1,
      heartbeat_missing_count: 1,
    }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    const row = rowOf(section, H_ROW_UNKNOWN.account_id);

    // ⭐ 核心斷言：讀不到的三態一律是「未知」，絕不是健康值。
    // 只斷言「未知有出現」不夠——同時把健康值畫出來一樣會通過。
    expect(row.textContent).toMatch(/樣本未知/);
    expect(row.textContent).toMatch(/狀態未知/);   // kill switch
    expect(row.textContent).toMatch(/覆蓋未知/);
    expect(row.textContent).toMatch(/從未收到心跳/);
    expect(row.textContent).not.toMatch(/一切良好|未觸發|已生效|心跳新鮮/);
    // ⭐ 整個區塊掃一遍：健康字樣一個都不准出現（含 summary 與所有說明文字）。
    expect(section.textContent).not.toMatch(/一切良好|未觸發|已生效|心跳新鮮/);

    // 告警數讀不到 → 顯示「未知」，不是 0（0 是面板上最令人安心的數字）
    const cells = row.querySelectorAll("td");
    expect(cells[6].textContent).toBe("未知");
    // 沒有心跳 → 時刻欄留佔位符，不畫一個「剛剛」的時刻
    expect(cells[2].textContent).toBe("—");
    // 讀不到必須大聲：未知不是「沒有問題」
    expect(within(section).getByText("有格子讀不到，這些項目的狀態無從確認")).toBeInTheDocument();
    // 後端的原文照樣上呈（不 log 完就吞）
    expect(within(section).getByText(/心跳也讀不到時整列維持未知/)).toBeInTheDocument();
    // 來源欄說明這一格出自哪裡（兩個來源並存卻不標示 → 讀者不知道值有多舊）
    expect(row.querySelectorAll("td")[7].textContent).toBe("狀態根讀不到");
    // ⭐ 引擎現況（leader／資金設定）只可能來自心跳 → 整列未知，不留白也不猜
    expect(rowOf(section, H_ROW_UNKNOWN.account_id, 1).textContent).toMatch(/未知/);
    expect(section.textContent).not.toMatch(/10,000\.00/);
  });

  it("⭐⭐ 健康：心跳過期 → 標明過期＋最後心跳時刻，過期心跳裡的值一律不當現況", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith([H_ROW_STALE], {
      heartbeat_stale_count: 1, engine_unknown_count: 1,
      killswitch_unknown_count: 1, coverage_unknown_count: 1, alerts_unknown_count: 1,
    }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    const row = rowOf(section, H_ROW_STALE.account_id);

    // ⭐ 明確標示過期，且最後心跳時刻與年齡都看得到
    expect(row.textContent).toMatch(/心跳過期/);
    expect(row.textContent).toContain("2026-07-18T23:10:00+00:00");
    expect(row.textContent).toMatch(/1 小時前/);
    // ⭐⭐ 過期的心跳不是現況：kill switch／覆蓋度一律未知，絕不出現健康字樣
    expect(row.textContent).not.toMatch(/一切良好|未觸發|已生效|心跳新鮮/);
    expect(section.textContent).not.toMatch(/一切良好|未觸發|已生效|心跳新鮮/);
    // 大聲：心跳停了代表跟單與回撤保護都可能已停止運作
    expect(within(section).getByText("有客戶的引擎心跳已過期")).toBeInTheDocument();
    expect(within(section).getByText(/不是它現在的狀態/)).toBeInTheDocument();
    // ⭐ 引擎現況（leader／資金設定）整列未知：後端在過期時結構上不回傳這些值
    expect(section.textContent).not.toMatch(/0xlead/);
    expect(section.textContent).not.toMatch(/10,000\.00/);
  });

  it("健康：全部可讀且健康 → 顯示健康值且無任何告警（未知斷言的對照組）", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith([H_ROW], { engine_alive_count: 1 }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    const row = rowOf(section, H_ROW.account_id);
    expect(row.textContent).toMatch(/一切良好/);
    expect(row.textContent).toMatch(/未觸發/);
    expect(row.textContent).toMatch(/已生效/);
    expect(row.textContent).not.toMatch(/未知/);
    expect(screen.queryByRole("alert")).toBeNull();
    // ⚠️ 誠實標註：這是 equity 樣本的代理，不是 process 存活檢查
    expect(within(section).getByText(/不是 process 存活檢查/)).toBeInTheDocument();
  });

  it("健康：心跳新鮮但權益樣本已過期 → 樣本欄標過期，不因心跳新鮮就報一切良好", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith(
      [{ ...H_ROW, engine_alive: false, last_sample_age_s: 3600 }],
      { heartbeat_ok_count: 1, engine_stale_count: 1 },
    ));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    const row = rowOf(section, H_ROW.account_id);
    // ⚠️ 心跳與權益樣本是**兩個**新鮮度，刻意不合併：引擎可以還在發心跳，卻已經
    // 很久沒有寫權益樣本（回撤保護的分母停止更新）。合併會讓後者被前者蓋掉。
    expect(row.textContent).toMatch(/心跳新鮮/);
    expect(row.textContent).toMatch(/樣本過期/);
    expect(row.textContent).not.toMatch(/一切良好/);
    expect(row.textContent).toMatch(/1 小時前/);
  });

  it("⭐ 健康：kill switch 已觸發 → 視覺上明顯（紅底列＋實心 chip）並顯著告警", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith(
      [{ ...H_ROW, killswitch_tripped: true, killswitch_known: true }],
      { engine_alive_count: 1, killswitch_tripped_count: 1 },
    ));
    const { container } = render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    const row = rowOf(section, H_ROW.account_id);
    expect(row.textContent).toMatch(/已觸發/);
    expect(row.textContent).not.toMatch(/未觸發/);
    // ⭐ 不只是文字：整列標紅 ＋ 實心紅 chip（掃過一整頁時要自己跳出來）
    expect(container.querySelector(".ops-row-tripped")).not.toBeNull();
    expect(row.querySelector(".ops-chip-bad")).not.toBeNull();
    // 告警要說出後果：該客戶已停止跟單
    const alert = within(section).getByText(/kill switch 已觸發/);
    expect(alert.textContent).toMatch(/已停止跟單/);
  });

  it("⭐ 健康：換 leader 積壓查不下去 → 顯示未知＋原因，絕不顯示 0", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith([H_ROW], {
      engine_alive_count: 1,
      unapplied_leader_changes: null,
      leader_change_errors: ["換 leader 記錄檔讀取失敗（/exchange/leader_changes.json）"],
    }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    expect(within(section).getByText("換 leader 積壓無從得知")).toBeInTheDocument();
    expect(within(section).getByText(/leader_changes\.json/)).toBeInTheDocument();
    // ⭐ 積壓的統計格顯示「未知」而不是 0（0 會被讀成「沒有積壓」，正好相反）
    const stat = within(section).getByText("換 leader 未套用積壓").parentElement;
    expect(stat?.querySelector("dd")?.textContent).toBe("未知");
    // 查不下去時不得畫出一個看起來已清點過的筆數 chip
    expect(section.querySelector(".ops-drift-count")).toBeNull();
  });

  it("健康：心跳新鮮 → 列出目前 leader、來源與引擎當輪採用的資金設定", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    // leader_kind: vault → 來源欄多標 vault（vault 保護是否生效的唯一觀測面）
    getOpsHealth.mockResolvedValue(healthWith([{ ...H_ROW, leader_kind: "vault" }], {
      heartbeat_ok_count: 1, engine_alive_count: 1,
    }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    const row = rowOf(section, H_ROW.account_id, 1);   // 引擎現況表
    expect(row.textContent).toContain("0xlead000000000000000000000000000000000001");
    expect(row.textContent).toMatch(/signed · vault/);
    // ⚠️ 兩個資金數值直接乘進部位大小：本金以字串來、顯示層只在最後一刻格式化
    expect(row.textContent).toMatch(/10,000\.00/);
    expect(row.textContent).toMatch(/25\.0%/);
    // 來源欄說明這一列的 kill switch／覆蓋度出自心跳而非直讀
    expect(rowOf(section, H_ROW.account_id).querySelectorAll("td")[7].textContent)
      .toBe("引擎心跳");
  });

  it("健康：積壓有筆數 → 逐筆列出並把 reason 代碼翻成人話", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith(
      [H_ROW],
      { engine_alive_count: 1, unapplied_leader_changes: 1 },
      {
        unapplied_leader_changes: [{
          account_id: H_ROW.account_id, nonce: "n1", age_s: 7200, reason: "not_redeemed",
        }],
      },
    ));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    expect(within(section).getByText(/引擎帳本裡沒有兌現記錄/)).toBeInTheDocument();
    expect(within(section).getByText("n1")).toBeInTheDocument();
    expect(within(section).getByText("2 小時")).toBeInTheDocument();
  });

  it("健康：liveness_basis 換成未知代碼 → 原樣顯示，不沿用已不成立的說明", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsHealth.mockResolvedValue(healthWith(
      [{ ...H_ROW, liveness_basis: "engine_heartbeat" }], { engine_alive_count: 1 },
    ));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "系統健康" });
    // 後端換掉判定基準時顯示新代碼並標明本頁尚無說明——過期的說明比沒有說明更危險
    expect(within(section).getByText(/engine_heartbeat/)).toBeInTheDocument();
    expect(section.textContent).not.toMatch(/equity 樣本（引擎每 cycle 寫一筆）/);
  });

  // ---------- ⭐ 成交品質 ----------

  it("成交品質：正常資料 → 指標齊備，且窗口併入既有的共用標頭", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue(qualityWith([Q_ROW], {
      quality_available_count: 1, te_available_count: 1, skipped_available_count: 1,
      worst_median_delay_s: "2", delay_sample: 1,
      worst_taker_slippage_bp_median: "12.5", slippage_sample: 1,
    }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "成交品質" });
    expect(getOpsTradeQuality).toHaveBeenCalledWith({ window: "accrued" });
    const row = rowOf(section, Q_ROW.account_id);
    expect(row.textContent).toMatch(/2/);            // 配對延遲中位數（秒）
    expect(row.textContent).toMatch(/12\.5/);        // taker 滑價中位數（bp）
    expect(row.textContent).toMatch(/50\.0%/);       // taker 佔比
    expect(row.textContent).toMatch(/2\.0%/);        // 跳過小額佔比
    // ⭐ 指標名稱是「配對延遲中位數」，不得被改寫成「速度」之類意義不同的詞
    expect(within(section).getByRole("columnheader", { name: "配對延遲中位數（秒）" }))
      .toBeInTheDocument();
    expect(section.textContent).not.toMatch(/速度|反應時間/);
    // 三張表同窗口 → 併入同一個標頭，不另外印一次窗口
    const banner = document.querySelector(".ops-window-banner");
    expect(banner?.textContent).toMatch(/成交品質面板也取自同一組快照時刻/);
    expect(document.querySelectorAll(".ops-window-banner").length).toBe(1);
  });

  it("⭐⭐ 成交品質：skipped_small_ratio 為 null → 顯示「此窗口無法計算」，不是 0 也不留白", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue(qualityWith([{
      ...Q_ROW,
      skipped_available: true,
      skipped_small_notional: "150",
      skipped_small_ratio: null,   // 窗口非整個 UTC 日 → 分子分母不同基準
      skipped_note: "skipped 小額以日曆日落檔（檔內無逐筆時間戳），本窗口非整個 UTC 日 → "
        + "比例的分子與分母不同基準，故不計算；名目為窗口涵蓋日的合計",
    }], { quality_available_count: 1, te_available_count: 1, skipped_available_count: 1 }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "成交品質" });
    expect(within(section).getByText("此窗口無法計算")).toBeInTheDocument();
    // ⭐ 該欄位（列的最後一格）絕不退化成 0、0.0% 或空白——那個商看起來完全
    // 像一個正常的比例，而留白在一整欄數字裡同樣會被讀成 0。
    const cells = rowOf(section, Q_ROW.account_id).querySelectorAll("td");
    expect(cells[cells.length - 1].textContent).toBe("此窗口無法計算");
    expect(within(section).queryByText("0.0%")).toBeNull();
    // 名目仍然有意義，照樣顯示（「讀不到」與「算不出比例」是兩件事）
    expect(within(section).getByText("150.00")).toBeInTheDocument();
    expect(within(section).queryByText("讀不到")).toBeNull();
    // 原因來自後端原文（掛在 title 上，讀者可查證為什麼那一格是空的）
    expect(within(section).getByText("此窗口無法計算").getAttribute("title"))
      .toMatch(/不同基準/);
  });

  it("⭐ 成交品質：skipped 檔讀不到 → 名目與比例都顯示「讀不到」，不填 0", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue(qualityWith([{
      ...Q_ROW, skipped_available: false,
      skipped_small_notional: null, skipped_small_ratio: null,
    }], { quality_available_count: 1, te_available_count: 1 }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "成交品質" });
    // 兩格（名目、比例）都是「讀不到」——與「此窗口無法計算」刻意用不同的字
    expect(within(section).getAllByText("讀不到")).toHaveLength(2);
    expect(within(section).queryByText("此窗口無法計算")).toBeNull();
    expect(within(section).queryByText("0.00")).toBeNull();
  });

  it("⭐ 成交品質：不知道跟誰（te_available=false）→ 延遲與滑價顯示「無法配對」，不填 0", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue(qualityWith([{
      ...Q_ROW, te_available: false,
      pair_count: null, median_delay_s: null, taker_slippage_bp_median: null,
      te_note: "manifest 未記錄該 follower 的 leader_address，無法配對成交 → "
        + "配對延遲與滑價無從計算",
    }], { quality_available_count: 1, te_available_count: 0 }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "成交品質" });
    // 配對筆數／延遲／滑價三格皆「無法配對」——0 會被讀成完美的跟單品質
    const unpaired = within(section).getAllByText("無法配對");
    expect(unpaired).toHaveLength(3);
    // 每一格都附後端寫的原因（沒有解釋的空格會被當成「這個客戶沒事」）
    for (const el of unpaired) expect(el.getAttribute("title")).toMatch(/leader_address/);
    // taker 佔比只由我方成交算出，與 leader 無關 → 仍然有效
    expect(within(section).getByText("50.0%")).toBeInTheDocument();
  });

  it("成交品質：某列 fills 查詢失敗 → 該列標錯，其餘列照常（跨客戶隔離）", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue(qualityWith([
      Q_ROW,
      {
        account_id: "fq990000000000000000000000000000000000009",
        label: "bob", network: "mainnet",
        quality_available: false, te_available: false, skipped_available: false,
        error: "fills 查詢失敗: upstream timeout",
      },
    ], { quality_available_count: 1, te_available_count: 1, skipped_available_count: 1 }));
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "成交品質" });
    expect(within(section).getByText(/fills 查詢失敗: upstream timeout/)).toBeInTheDocument();
    // 失敗列不影響正常列的數字
    expect(within(section).getByText("50.0%")).toBeInTheDocument();
    expect(within(section).getByText(Q_ROW.account_id)).toBeInTheDocument();
  });

  it("成交品質：basis_unknown → 顯示後端 note，整段不出現任何數字也不畫空表", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue({
      window: "accrued",
      basis_unknown: true,
      window_start: null,
      window_end: null,
      note: "歷史資料缺快照時刻（captured_at），無法對齊窗口；本次成交品質無法與收入對帳同基準，故不計算。",
      manifest_errors: [],
    } satisfies OpsTradeQualityResp);
    render(wrap(<OpsPage />));

    const section = await screen.findByRole("region", { name: "成交品質" });
    expect(within(section).getByText("快照時刻無法對齊，本次成交品質跳過計算。")).toBeInTheDocument();
    expect(section.querySelector(".ops-table")).toBeNull();
    const notice = section.querySelector(".ops-notice");
    expect(notice?.textContent).not.toMatch(/\d/);
    expect(notice?.textContent).not.toMatch(/undefined|NaN/);
    // 窗口算不出來 → 不併入共用標頭（不謊報同基準），也不誤報不一致
    expect(screen.getByText("以下兩張表使用同一比較窗口")).toBeInTheDocument();
    expect(document.querySelector(".ops-window-banner")?.textContent)
      .not.toMatch(/成交品質面板也取自同一組快照時刻/);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("⭐ 成交品質窗口竟與對帳窗口不同 → role=alert 警告不可並排相減", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockResolvedValue({
      ...TQ_EMPTY,
      window_start: "2026-07-17T00:00:00+00:00",
      window_end: "2026-07-18T00:00:00+00:00",
    });
    render(wrap(<OpsPage />));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("成交品質的比較窗口與對帳表不一致，數字不可並排相減"))
      .toBeInTheDocument();
    // 差在哪要直接列出來，不必自己翻幾張表
    expect(alert.textContent).toContain("2026-07-17T00:00:00+00:00");
    expect(alert.textContent).toContain("2026-07-19T00:00:00+00:00");
    // 不得一邊警告不一致、一邊還宣稱同基準
    expect(screen.queryByText("以下兩張表使用同一比較窗口")).toBeNull();
  });

  it("成交品質：切到自由檢視窗 → 送 {days} 且改口說不可相減", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockImplementation((q: { days?: number; window?: string }) =>
      Promise.resolve(q?.window === "accrued" ? CUSTOMERS : { ...CUSTOMERS_DAYS, days: q?.days }),
    );
    render(wrap(<OpsPage />));
    expect(await screen.findByText(ROW.account_id)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    // ⭐ 品質面板與客戶表共用同一個窗口控制項：兩個獨立窗口才是誤讀的來源
    expect(getOpsTradeQuality).toHaveBeenCalledWith({ days: 7 });
    const section = screen.getByRole("region", { name: "成交品質" });
    expect(within(section).getByText(/不可並排相減/)).toBeInTheDocument();
  });

  // ---------- ⭐ 各自降級 ----------

  it("⭐ 兩個新面板各自載入失敗 → 各自顯示錯誤，其餘區塊完全不受影響", async () => {
    getOpsRevenue.mockResolvedValue(REVENUE_OK);
    getOpsCustomers.mockResolvedValue(CUSTOMERS);
    getOpsTradeQuality.mockRejectedValue(new ApiError("upstream", "上游服務暫時不可用", 503));
    getOpsHealth.mockRejectedValue(new ApiError("network", "無法連線到伺服器，請檢查網路後重試"));
    render(wrap(<OpsPage />));

    // 兩個區塊各自明確報錯，不靜默空白
    const q = await screen.findByRole("region", { name: "成交品質" });
    expect(within(q).getByText(/成交品質載入失敗/)).toBeInTheDocument();
    expect(within(q).getByText(/上游服務暫時不可用/)).toBeInTheDocument();
    const h = screen.getByRole("region", { name: "系統健康" });
    expect(within(h).getByText(/系統健康載入失敗/)).toBeInTheDocument();
    expect(within(h).getByText(/無法連線到伺服器/)).toBeInTheDocument();

    // 其餘區塊照常：對帳數字、客戶表、訂閱對帳都在
    expect(screen.getByText("25.0101")).toBeInTheDocument();
    expect(screen.getByText(ROW.account_id)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "訂閱對帳" })).toBeInTheDocument();
    // 品質窗口取不到 → 共用標頭照舊只描述另外兩張表（不因此消失、也不誤報不一致）
    expect(screen.getByText("以下兩張表使用同一比較窗口")).toBeInTheDocument();
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
