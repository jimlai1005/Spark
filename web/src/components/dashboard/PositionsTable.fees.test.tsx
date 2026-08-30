/**
 * PositionsTable —「費用明細」tab（M3 round3 Task 5，R2·B 重構；M3 round4
 * Task R4-9，2026-08-31 使用者裁決：移除前端補整月日曆「—」列）。
 *
 * 涵蓋 plan 驗收清單：倒序渲染（最新在最上）；空日**不出列**（後端本來就
 * 只回有成交的日子，前端不再補日曆湊出整月）；期間切換打 API 帶 period；
 * 合計四格渲染（`pnl_share_pct` null → 「—」）；CSV 內容正確（含跳脫、
 * 同樣不含空日列）；「載入更早」擴窗。
 *
 * 純函式（`sortFeesRowsDesc`／`buildFeesCsv`）直接單測，數值錨例精確斷言；
 * 元件層再補一組 RTL 整合測試驗證資料流與畫面渲染的接線正確（mock `getMyFees`，
 * 沿 `PositionsTable.history.test.tsx` 既有慣例）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LangProvider } from "@/lib/lang";
import type { DashboardPosition, MyFeesDailyRow, MyFeesPeriod, MyFeesResp } from "@/lib/api";
import { buildFeesCsv, PositionsTable, sortFeesRowsDesc } from "./PositionsTable";

// ── 純函式：sortFeesRowsDesc ─────────────────────────────────────────────

function dailyRow(over: Partial<MyFeesDailyRow>): MyFeesDailyRow {
  return {
    date: "2026-08-01", fill_count: 1, routed_volume: "100",
    builder_fee: "1.00", effective_rate_bps: "10.00", ...over,
  };
}

describe("sortFeesRowsDesc", () => {
  it("新→舊排序，不補任何空日", () => {
    const rows = sortFeesRowsDesc([
      dailyRow({ date: "2026-08-01" }),
      dailyRow({ date: "2026-08-10" }),
      dailyRow({ date: "2026-08-05" }),
    ]);
    expect(rows.map((r) => r.date)).toEqual(["2026-08-10", "2026-08-05", "2026-08-01"]);
    // 三個有成交的日子輸入 → 三筆輸出，日期之間沒有被插入的「空日」
    expect(rows).toHaveLength(3);
  });

  it("空輸入 → 空輸出（不臆造任何日期範圍）", () => {
    expect(sortFeesRowsDesc([])).toEqual([]);
  });

  it("不修改原陣列（回傳新陣列）", () => {
    const input = [dailyRow({ date: "2026-08-01" }), dailyRow({ date: "2026-08-02" })];
    const sorted = sortFeesRowsDesc(input);
    expect(sorted).not.toBe(input);
    expect(input.map((r) => r.date)).toEqual(["2026-08-01", "2026-08-02"]); // 原陣列順序不變
  });
});

// ── 純函式：buildFeesCsv（含跳脫） ────────────────────────────────────────

const FEES_COPY = {
  colDate: "日期 ↓", colFillCount: "成交筆數", colRoutedVolume: "路由交易量",
  colBuilderFee: "Builder fee", colEffectiveRate: "實際費率",
};

describe("buildFeesCsv", () => {
  it("數值錨例：千分位逗號的金額欄位被引號包住並雙寫內部引號跳脫規則不誤傷；不含空日列", () => {
    const rows = sortFeesRowsDesc([
      dailyRow({
        date: "2026-08-03", fill_count: 5, routed_volume: "102680.00",
        builder_fee: "20.54", effective_rate_bps: "0.02",
      }),
      dailyRow({
        date: "2026-08-01", fill_count: 1, routed_volume: "50",
        builder_fee: "0", effective_rate_bps: "0.00",
      }),
    ]);
    const csv = buildFeesCsv(rows, FEES_COPY);
    const lines = csv.split("\n");
    expect(lines).toEqual([
      "日期 ↓,成交筆數,路由交易量,Builder fee,實際費率",
      '2026-08-03,5,"$102,680.00",$20.54,0.02 bps', // 含逗號的金額欄位被引號包住
      "2026-08-01,1,$50.00,$0.00,0.00 bps", // 有成交、fee=0 照實列出（非「—」）
      // 8/2 無成交 → R4-9 起不再補「—」列，CSV 只有兩天有值的資料列
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
    await waitFor(() => expect(screen.getByText("$1,050.00")).toBeInTheDocument());
    // "$1.20" 同時出現在合計格與 8/1 那列（builder_fee 恰好同值）——兩處都要在。
    expect(screen.getAllByText("$1.20")).toHaveLength(2);
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("佔已實現淨 PnL")).toBeInTheDocument();
    // pnl_share_pct null → 合計格顯示「—」，且只有這一格（逐日列已無空日「—」）
    expect(screen.getAllByText("—")).toHaveLength(1);
  });

  it("R4-9：只渲染兩筆有成交的日子（新→舊），空日完全不出列", async () => {
    renderFeesTab();
    await waitFor(() => expect(screen.getByText("2026-08-10")).toBeInTheDocument());

    const dateCells = screen
      .getAllByText(/^2026-08-\d{2}$/)
      .map((el) => el.textContent);
    // this_month 回應只有 8/1 與 8/10 兩筆——沒有任何「補出來」的中間空日。
    expect(dateCells).toEqual(["2026-08-10", "2026-08-01"]);

    // 逐日列完全沒有「—」（空日消失）；唯一的「—」是合計格 pnl_share_pct（null）。
    expect(screen.getAllByText("—")).toHaveLength(1);

    // Aug 10 有成交但 fee=0 → 顯示 $0.00
    const aug10Row = screen.getByText("2026-08-10").closest("div")!.parentElement!;
    expect(aug10Row.textContent).toContain("$0.00");

    // 沒有「載入更早」按鈕（只有 2 筆，遠低於預設可見上限 10）
    expect(screen.queryByText("載入更早的 20 天")).not.toBeInTheDocument();
  });

  it("「載入更早的 20 天」擴窗：超過預設可見上限時才出現，點擊後展開", async () => {
    getMyFees.mockImplementation((period) => {
      if ((period ?? "this_month") !== "all") return Promise.resolve(respFor("this_month"));
      return Promise.resolve({
        summary: { builder_fees: "12.00", routed_volume: "12000", fill_count: 12, pnl_share_pct: "10.00" },
        daily: Array.from({ length: 12 }, (_, i) => dailyRow({
          date: `2026-07-${String(i + 1).padStart(2, "0")}`, fill_count: 1,
        })),
      });
    });
    renderFeesTab();
    fireEvent.click(screen.getByText("全部"));
    await waitFor(() => expect(screen.getByText("2026-07-12")).toBeInTheDocument());
    expect(screen.queryByText("2026-07-01")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("載入更早的 20 天"));
    expect(screen.getByText("2026-07-01")).toBeInTheDocument();
    expect(screen.queryByText("載入更早的 20 天")).not.toBeInTheDocument();
  });

  it("匯出 CSV：Blob 內容正確（含跳脫，不含空日列），檔名 filet-fees-<period>.csv", async () => {
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
      // $9,000.00＝合計格 routed_volume（"9000"），只在合計格出現一次（不會與逐日列的
      // $100.00 routed_volume 混淆），用來確認 period=all 的資料已載入完成。
      await waitFor(() => expect(screen.getByText("$9,000.00")).toBeInTheDocument());

      fireEvent.click(screen.getByText("匯出 CSV"));

      await waitFor(() => expect(capturedParts).not.toBeNull());
      expect(capturedFilename).toBe("filet-fees-all.csv");
      const text = (capturedParts as unknown as string[]).join("");
      const lines = text.split("\n");
      expect(lines[0]).toBe("日期 ↓,成交筆數,路由交易量,Builder fee,實際費率");
      // period=all 回應只有 7/1 一筆有成交的日子 → CSV 恰好一筆資料列，不含任何空日
      expect(lines).toEqual([
        "日期 ↓,成交筆數,路由交易量,Builder fee,實際費率",
        "2026-07-01,9,$100.00,$9.00,10.00 bps",
      ]);
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
