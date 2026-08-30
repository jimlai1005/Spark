/**
 * PositionsTable —「費用明細」tab（M3 round3 Task 5，R2·B 重構）。
 *
 * 涵蓋 plan 驗收清單：倒序渲染（最新在最上）；無成交日「—」列與
 * `builder_fee=0` 但有成交日的區分；期間切換打 API 帶 period；合計四格渲染
 * （`pnl_share_pct` null → 「—」）；CSV 內容正確（含跳脫）；「載入更早」擴窗。
 *
 * 純函式（`buildFeesCalendarRows`／`buildFeesCsv`）直接單測，數值錨例精確斷言；
 * 元件層再補一組 RTL 整合測試驗證資料流與畫面渲染的接線正確（mock `getMyFees`，
 * 沿 `PositionsTable.history.test.tsx` 既有慣例）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LangProvider } from "@/lib/lang";
import type { DashboardPosition, MyFeesDailyRow, MyFeesPeriod, MyFeesResp } from "@/lib/api";
import { buildFeesCalendarRows, buildFeesCsv, PositionsTable } from "./PositionsTable";

// ── 純函式：buildFeesCalendarRows ────────────────────────────────────────

function dailyRow(over: Partial<MyFeesDailyRow>): MyFeesDailyRow {
  return {
    date: "2026-08-01", fill_count: 1, routed_volume: "100",
    builder_fee: "1.00", effective_rate_bps: "10.00", ...over,
  };
}

describe("buildFeesCalendarRows", () => {
  it("this_month：補整個月 1 號到今天，新→舊排序", () => {
    const now = new Date("2026-08-15T12:00:00Z");
    const rows = buildFeesCalendarRows("this_month", [dailyRow({ date: "2026-08-01" })], now);
    expect(rows).toHaveLength(15); // Aug 1..15
    expect(rows[0].date).toBe("2026-08-15"); // 最新在最上
    expect(rows[rows.length - 1].date).toBe("2026-08-01");
  });

  it("last_month：補上個月整月（跨年邊界照算）", () => {
    const now = new Date("2026-01-15T00:00:00Z");
    const rows = buildFeesCalendarRows("last_month", [], now);
    expect(rows).toHaveLength(31); // Dec 2025 有 31 天
    expect(rows[0].date).toBe("2025-12-31");
    expect(rows[rows.length - 1].date).toBe("2025-12-01");
  });

  it("all：只從 daily 裡最早一天補起，不補到帳戶誕生前", () => {
    const now = new Date("2026-07-22T00:00:00Z");
    const rows = buildFeesCalendarRows(
      "all",
      [dailyRow({ date: "2026-07-20" }), dailyRow({ date: "2026-07-05" })],
      now,
    );
    expect(rows).toHaveLength(18); // Jul 5..22
    expect(rows[0].date).toBe("2026-07-22");
    expect(rows[rows.length - 1].date).toBe("2026-07-05");
  });

  it("all：daily 為空 → 回傳空陣列（不臆造日期範圍）", () => {
    expect(buildFeesCalendarRows("all", [], new Date("2026-07-22T00:00:00Z"))).toEqual([]);
  });

  it("無成交日 hasFill=false 且全部欄位為 null；builder_fee=0 但有成交的日子 hasFill=true", () => {
    const now = new Date("2026-08-03T00:00:00Z");
    const rows = buildFeesCalendarRows(
      "all",
      [dailyRow({ date: "2026-08-01", fill_count: 1, builder_fee: "0", effective_rate_bps: "0.00" })],
      now,
    );
    const aug1 = rows.find((r) => r.date === "2026-08-01")!;
    const aug2 = rows.find((r) => r.date === "2026-08-02")!;
    expect(aug1).toMatchObject({ hasFill: true, fill_count: 1, builder_fee: "0" });
    expect(aug2).toMatchObject({
      hasFill: false, fill_count: null, routed_volume: null,
      builder_fee: null, effective_rate_bps: null,
    });
  });
});

// ── 純函式：buildFeesCsv（含跳脫） ────────────────────────────────────────

const FEES_COPY = {
  colDate: "日期 ↓", colFillCount: "成交筆數", colRoutedVolume: "路由交易量",
  colBuilderFee: "Builder fee", colEffectiveRate: "實際費率",
};

describe("buildFeesCsv", () => {
  it("數值錨例：千分位逗號的金額欄位被引號包住並雙寫內部引號跳脫規則不誤傷", () => {
    const rows = buildFeesCalendarRows(
      "all",
      [
        dailyRow({
          date: "2026-08-03", fill_count: 5, routed_volume: "102680.00",
          builder_fee: "20.54", effective_rate_bps: "0.02",
        }),
        dailyRow({
          date: "2026-08-01", fill_count: 1, routed_volume: "50",
          builder_fee: "0", effective_rate_bps: "0.00",
        }),
      ],
      new Date("2026-08-03T00:00:00Z"),
    );
    const csv = buildFeesCsv(rows, FEES_COPY);
    const lines = csv.split("\n");
    expect(lines).toEqual([
      "日期 ↓,成交筆數,路由交易量,Builder fee,實際費率",
      '2026-08-03,5,"$102,680.00",$20.54,0.02 bps', // 含逗號的金額欄位被引號包住
      "2026-08-02,—,—,—,—", // 無成交日整列「—」
      "2026-08-01,1,$50.00,$0.00,0.00 bps", // 有成交、fee=0 照實列出（非「—」）
    ]);
  });

  it("欄位本身含引號時雙寫跳脫", () => {
    const copyWithQuote = { ...FEES_COPY, colDate: 'Date "UTC"' };
    const csv = buildFeesCsv([], copyWithQuote);
    expect(csv.split("\n")[0]).toBe('"Date ""UTC""",成交筆數,路由交易量,Builder fee,實際費率');
  });
});

// ── 元件整合：資料流與畫面接線 ────────────────────────────────────────────

const getMyFees = vi.fn<(period?: MyFeesPeriod) => Promise<MyFeesResp>>();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMyFees: (period?: MyFeesPeriod) => getMyFees(period),
}));

const POSITION: DashboardPosition = {
  symbol: "ETH", side: "long", leverage: "3", margin_mode: "cross",
  value: "2492.50", upnl: "-0.16", entry: "2452.76", mark: "2453.1575",
  deviation_pct: "0.02",
};

function respFor(period: MyFeesPeriod): MyFeesResp {
  if (period === "this_month") {
    return {
      summary: { builder_fees: "1.20", routed_volume: "1050", fill_count: 3, pnl_share_pct: null },
      daily: [
        dailyRow({
          date: "2026-08-01", fill_count: 2, routed_volume: "1000",
          builder_fee: "1.20", effective_rate_bps: "12.00",
        }),
        dailyRow({
          date: "2026-08-10", fill_count: 1, routed_volume: "50",
          builder_fee: "0", effective_rate_bps: "0.00",
        }),
      ],
    };
  }
  return {
    summary: { builder_fees: "9.00", routed_volume: "9000", fill_count: 9, pnl_share_pct: "30.77" },
    daily: [dailyRow({ date: "2026-07-01", fill_count: 9, builder_fee: "9.00" })],
  };
}

function renderFeesTab() {
  const utils = render(
    <LangProvider>
      <PositionsTable positions={[POSITION]} feesMonth={null} />
    </LangProvider>,
  );
  fireEvent.click(screen.getByText("費用明細"));
  return utils;
}

beforeEach(() => {
  // `shouldAdvanceTime`：凍結 `Date`（讓元件內 `new Date()` 決定期間邊界可預期），
  // 但仍讓計時器隨真實時間前進——RTL 的 `waitFor` 靠 `setInterval` 輪詢，純
  // `vi.useFakeTimers()` 會把它一起凍住，導致每個 `await waitFor(...)` 逾時。
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-08-15T12:00:00Z"));
  getMyFees.mockImplementation((period) => Promise.resolve(respFor((period ?? "this_month") as MyFeesPeriod)));
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("PositionsTable — 費用明細 tab", () => {
  it("切到費用明細 tab 才打 API，預設 period=this_month", async () => {
    renderFeesTab();
    await waitFor(() => expect(getMyFees).toHaveBeenCalledWith("this_month"));
  });

  it("切換期間（上月/全部）→ 打 API 帶對應 period", async () => {
    renderFeesTab();
    await waitFor(() => expect(getMyFees).toHaveBeenCalledWith("this_month"));

    fireEvent.click(screen.getByText("全部"));
    await waitFor(() => expect(getMyFees).toHaveBeenCalledWith("all"));

    fireEvent.click(screen.getByText("上月"));
    await waitFor(() => expect(getMyFees).toHaveBeenCalledWith("last_month"));
  });

  it("合計四格渲染；pnl_share_pct null → 「—」", async () => {
    renderFeesTab();
    await waitFor(() => expect(screen.getByText("$1.20")).toBeInTheDocument());
    expect(screen.getByText("$1,050.00")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("佔已實現淨 PnL")).toBeInTheDocument();
    // 合計格與表格內的「—」都存在；用 getAllByText 確認合計格那顆存在即可
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("表格新→舊排序；無成交日整列「—」，與有成交但 fee=0 的日子區分", async () => {
    renderFeesTab();
    await waitFor(() => expect(screen.getByText("2026-08-15")).toBeInTheDocument());

    const dateCells = screen
      .getAllByText(/^2026-08-\d{2}$/)
      .map((el) => el.textContent);
    // 預設只顯示前 10 列（Aug 15 → Aug 6），且新到舊
    expect(dateCells).toEqual([
      "2026-08-15", "2026-08-14", "2026-08-13", "2026-08-12", "2026-08-11",
      "2026-08-10", "2026-08-09", "2026-08-08", "2026-08-07", "2026-08-06",
    ]);

    // Aug 10 有成交但 fee=0 → 顯示 $0.00，不是「—」
    const aug10Row = screen.getByText("2026-08-10").closest("div")!.parentElement!;
    expect(aug10Row.textContent).toContain("$0.00");
    expect(aug10Row.textContent).not.toContain("—");

    // Aug 15 當天無成交 → 整列「—」
    const aug15Row = screen.getByText("2026-08-15").closest("div")!.parentElement!;
    expect((aug15Row.textContent!.match(/—/g) || []).length).toBe(4); // 四個資料欄皆「—」
  });

  it("「載入更早的 20 天」擴窗：點擊後 Aug 1 那列（原本被 10 列上限擋住）出現", async () => {
    renderFeesTab();
    await waitFor(() => expect(screen.getByText("2026-08-15")).toBeInTheDocument());
    expect(screen.queryByText("2026-08-01")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("載入更早的 20 天"));
    expect(screen.getByText("2026-08-01")).toBeInTheDocument();
    // this_month 只有 15 列，全部已展開 → 按鈕消失
    expect(screen.queryByText("載入更早的 20 天")).not.toBeInTheDocument();
  });

  it("匯出 CSV：Blob 內容正確（含跳脫），檔名 filet-fees-<period>.csv", async () => {
    // jsdom 的 Blob 實作不一定有 `.text()`（實測 `capturedBlob.text is not a
    // function`）——改攔截 `Blob` 建構子本身的 parts 引數，繞開這個環境限制，
    // 只驗證本元件真正寫進去的內容，不依賴 jsdom 對 Blob 的完整度。
    let capturedParts: string[] | null = null;
    let capturedFilename: string | null = null;
    const OriginalBlob = globalThis.Blob;
    class CapturingBlob extends OriginalBlob {
      constructor(parts: BlobPart[], options?: BlobPropertyBag) {
        super(parts, options);
        capturedParts = parts as string[];
      }
    }
    vi.stubGlobal("Blob", CapturingBlob);
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:mock-url") as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        capturedFilename = this.download;
      });

    try {
      renderFeesTab();
      fireEvent.click(screen.getByText("全部"));
      await waitFor(() => expect(getMyFees).toHaveBeenCalledWith("all"));
      // 「全部」期間從 daily 最早一天（7/1）補到今天（8/15），預設只顯示前 10 列，
      // 7/1 那列不在可見視窗內——用合計格（period="all" 專屬數字）確認資料已載入。
      await waitFor(() => expect(screen.getByText("$9.00")).toBeInTheDocument());

      fireEvent.click(screen.getByText("匯出 CSV"));

      await waitFor(() => expect(capturedParts).not.toBeNull());
      expect(capturedFilename).toBe("filet-fees-all.csv");
      const text = (capturedParts as unknown as string[]).join("");
      expect(text.split("\n")[0]).toBe("日期 ↓,成交筆數,路由交易量,Builder fee,實際費率");
      // CSV 匯出的是完整期間（不受畫面 10 列上限限制），7/1 那列必須存在
      expect(text).toContain("2026-07-01,9,$100.00,$9.00,10.00 bps");
    } finally {
      clickSpy.mockRestore();
      vi.unstubAllGlobals();
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
    }
  });

  it("上游查詢失敗 → 顯示錯誤文案", async () => {
    getMyFees.mockRejectedValueOnce(new Error("503"));
    renderFeesTab();
    await waitFor(() =>
      expect(screen.getByText("費用明細暫時讀不到，請稍後重試。")).toBeInTheDocument());
  });
});
