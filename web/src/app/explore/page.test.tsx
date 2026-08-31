/**
 * `/explore` — 探索榜頁測試（M3 round3 Task 4；R4-10 2026-08-31 使用者裁決：
 * chip UI 保留，期間 chip 從固定 30D 改四窗全開，原「樣本門檻」合併 chip 拆成
 * 兩顆獨立布林 chip）。驗證頁面職責：抓資料、三態區分（building／fetch 失敗／
 * 正常）、表格渲染（含勝率 null→「—」、所選窗缺席→「—」）、「跟單 →」連結帶
 * `?leader=`、分頁換頁打 API 帶 `page` 參數、四顆 chip 各自獨立映射固定門檻、
 * 四窗切換立即打新 window 參數。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { fmtUpdatedAtUtc } from "@/lib/format";
import type { ExploreResp, ExploreRow } from "@/lib/publicApi";
import ExplorePage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function stubFetch(impl: (url: string) => Response) {
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(impl(url))));
}

const ROW_A: ExploreRow = {
  address: "0xaaaa00000000000000000000000000000000aaaa",
  display_name: "Alice",
  label: "Alice",
  coins: ["BTC", "ETH"],
  account_bucket: "$10K–$100K",
  windows: {
    day: { ret_pct: 2.1, max_dd_pct: -1.0, spark: [1, 1.02] },
    week: { ret_pct: 12.0, max_dd_pct: -4.0, spark: [1, 1.05, 1.12] },
    month: { ret_pct: 38.4, max_dd_pct: -11.2, spark: [1, 1.1, 1.05, 1.2] },
    allTime: { ret_pct: 90.0, max_dd_pct: -20.0, spark: [1, 1.9] },
  },
  live_days: 118,
  fill_count_30d: 250,
  close_win_rate_pct: 61.2,
  concentration_pct: 40.0,
  exposure: { dir: "long", pct: 72.0 },
  tags: ["low_drawdown"],
};

const ROW_B: ExploreRow = {
  address: "0xbbbb00000000000000000000000000000000bbbb",
  display_name: null,
  label: "0xbbbb…bbbb",
  coins: [],
  account_bucket: "$100K–$1M",
  windows: {
    day: null,
    week: null,
    month: { ret_pct: -5.0, max_dd_pct: -20.0, spark: [] },
    allTime: { ret_pct: -5.0, max_dd_pct: -20.0, spark: [] },
  },
  live_days: 90,
  fill_count_30d: 210,
  close_win_rate_pct: null,
  concentration_pct: 95.0,
  exposure: { dir: null, pct: null },
  tags: ["concentrated"],
};

function buildResp(over: Partial<ExploreResp> = {}): ExploreResp {
  return {
    rows: [ROW_A, ROW_B],
    page: 1,
    page_size: 25,
    total_qualified: 2,
    total_scanned: 100,
    pool: 100,
    updated_at: 1_700_000_000,
    building: false,
    ...over,
  };
}

describe("ExplorePage — 三態", () => {
  it("building:true → 顯示建置中文案（R2·C 態二），不顯示表格或空態", async () => {
    stubFetch(() => jsonResponse(buildResp({
      rows: [], building: true, total_qualified: 0, total_scanned: 0, updated_at: null,
    })));
    render(<ExplorePage />);
    expect(await screen.findByText(COPY.explore.building)).toBeInTheDocument();
    expect(screen.queryByText(COPY.explore.empty)).not.toBeInTheDocument();
  });

  it("fetch 失敗 → 顯示時間戳＋重試（R2·C 態三），點重試會重新打 API", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ detail: "boom" }, false)));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);

    await waitFor(() => {
      expect(
        screen.getByText((_, el) => el?.tagName === "P"
          && (el?.textContent ?? "").startsWith(COPY.explore.errorPrefix)),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/UTC/)).toBeInTheDocument();

    const retryBtn = screen.getByRole("button", { name: COPY.common.retry });
    fetchMock.mockClear();
    fetchMock.mockResolvedValue(jsonResponse(buildResp()));
    await userEvent.click(retryBtn);

    await screen.findByText("Alice");
    expect(fetchMock).toHaveBeenCalled();
  });

  it("成功但零筆合格 → 顯示空態文案", async () => {
    stubFetch(() => jsonResponse(buildResp({ rows: [], total_qualified: 0 })));
    render(<ExplorePage />);
    expect(await screen.findByText(COPY.explore.empty)).toBeInTheDocument();
  });
});

describe("ExplorePage — 表格渲染", () => {
  it("渲染帳戶列（預設 30D／month 窗），勝率 null → 「—」，count 摘要顯示 total_scanned → total_qualified", async () => {
    stubFetch(() => jsonResponse(buildResp()));
    const { container } = render(<ExplorePage />);
    await screen.findByText("Alice");
    expect(screen.getByText(ROW_B.label)).toBeInTheDocument();

    const retCells = container.querySelectorAll(".explore-ret");
    expect(retCells[0].textContent).toBe("+38.4%"); // ROW_A month
    expect(retCells[1].textContent).toBe("-5.0%");  // ROW_B month

    const wrCells = container.querySelectorAll(".explore-wr");
    expect(wrCells).toHaveLength(2);
    expect(wrCells[0].textContent).toBe("61.2%");
    expect(wrCells[1].textContent).toBe("—");

    const dayCells = container.querySelectorAll(".explore-days");
    expect(dayCells[0].textContent).toBe("118");
    expect(dayCells[1].textContent).toBe("90");

    expect(screen.getByText((_, el) => (el?.textContent ?? "") === "100 個帳戶 → 符合 2")).toBeInTheDocument();
  });

  it("I-17：榜首常駐一行顯示候選池與合格數，數字來自後端回應（不寫死）", async () => {
    stubFetch(() => jsonResponse(buildResp({ pool: 300, total_qualified: 7 })));
    render(<ExplorePage />);
    await screen.findByText("Alice");
    expect(screen.getByText((_, el) => (el?.textContent ?? "")
      === `${COPY.explore.poolNotePrefix}300${COPY.explore.poolNoteMid}7${COPY.explore.poolNoteSuffix}`,
    )).toBeInTheDocument();
  });

  it("所選窗對某列缺席（day/week best-effort）→ 該列該窗誠實顯示「—」，不回退借用其他窗", async () => {
    stubFetch(() => jsonResponse(buildResp()));
    const { container } = render(<ExplorePage />);
    await screen.findByText("Alice");

    await userEvent.click(screen.getByRole("button", { name: COPY.explore.windows.day }));

    await waitFor(() => {
      const retCells = container.querySelectorAll(".explore-ret");
      expect(retCells[0].textContent).toBe("+2.1%"); // ROW_A day
      expect(retCells[1].textContent).toBe("—");     // ROW_B day 缺席
    });
    const ddCells = container.querySelectorAll(".explore-dd");
    expect(ddCells[1].textContent).toBe("—");
  });

  it("「跟單 →」連結帶 ?leader=<address>；「查看」連向 /traders/{address}", async () => {
    stubFetch(() => jsonResponse(buildResp()));
    render(<ExplorePage />);
    await screen.findByText("Alice");

    const followLinks = screen.getAllByRole("link", { name: COPY.explore.follow });
    expect(followLinks[0]).toHaveAttribute("href", `/advanced?leader=${ROW_A.address}`);
    expect(followLinks[1]).toHaveAttribute("href", `/advanced?leader=${ROW_B.address}`);

    const viewLinks = screen.getAllByRole("link", { name: COPY.explore.view });
    expect(viewLinks[0]).toHaveAttribute("href", `/traders/${ROW_A.address}`);
  });

  it("D14：tag／曝險方向代碼對映成 copy.ts 顯示文案；未知代碼防禦性顯示原字串", async () => {
    stubFetch(() => jsonResponse(buildResp({
      rows: [
        { ...ROW_A, tags: ["low_drawdown", "some_future_tag"] },
        { ...ROW_B, exposure: { dir: "short", pct: 66.0 }, tags: ["concentrated"] },
      ],
    })));
    render(<ExplorePage />);
    await screen.findByText("Alice");

    // 已知代碼 → COPY.explore.tags.* 文案（非後端原始代碼字串）。
    expect(screen.getByText(COPY.explore.tags.lowDrawdown)).toBeInTheDocument();
    expect(screen.getByText(COPY.explore.tags.concentrated)).toBeInTheDocument();
    // 未知代碼 → 防禦性顯示原字串，不吞掉、不當機。
    expect(screen.getByText("some_future_tag")).toBeInTheDocument();

    // 曝險方向：long/short 代碼對映成中文「多」/「空」，不是英文代碼本身。
    expect(screen.getByText(`${COPY.explore.exposureDir.long} 72.0%`)).toBeInTheDocument();
    expect(screen.getByText(`${COPY.explore.exposureDir.short} 66.0%`)).toBeInTheDocument();
  });
});

// R-C／W3（2026-08-30 審查修正）：後端 index TTL 是 10min，文案已同步改
// 「每 10 分鐘更新」；頁面另外渲染 `updated_at` 讓用戶自己核對這份榜單多新，
// 不只是相信文案描述的更新頻率。
describe("ExplorePage — updated_at（R-C/W3）", () => {
  it("成功且非 building → 渲染「資料更新於 …」＋ fmtUpdatedAtUtc 格式化的時間戳", async () => {
    stubFetch(() => jsonResponse(buildResp({ updated_at: 1_700_000_000 })));
    render(<ExplorePage />);
    await screen.findByText("Alice");
    expect(screen.getByText((_, el) => el?.tagName === "P"
      && (el?.textContent ?? "") === `${COPY.explore.updatedAtPrefix}${fmtUpdatedAtUtc(1_700_000_000)}`,
    )).toBeInTheDocument();
  });

  it("building:true（`updated_at: null`）→ 不渲染「資料更新於」列", async () => {
    stubFetch(() => jsonResponse(buildResp({
      rows: [], building: true, total_qualified: 0, total_scanned: 0, updated_at: null,
    })));
    render(<ExplorePage />);
    await screen.findByText(COPY.explore.building);
    expect(screen.queryByText(new RegExp(COPY.explore.updatedAtPrefix))).not.toBeInTheDocument();
  });
});


describe("ExplorePage — 分頁", () => {
  it("點「下一頁」→ 打 API 帶新的 page 參數並渲染新一頁資料", async () => {
    const fetchMock = vi.fn((url: string) => {
      const page = new URL(url, "https://x.test").searchParams.get("page");
      return Promise.resolve(jsonResponse(buildResp({
        rows: page === "2" ? [ROW_B] : [ROW_A],
        page: page === "2" ? 2 : 1,
        total_qualified: 50,
        page_size: 25,
      })));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");

    await userEvent.click(screen.getByRole("button", { name: COPY.explore.pagination.next }));

    await waitFor(() => {
      const lastUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("page=2");
    });
    await screen.findByText(ROW_B.label);
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
  });
});

// R4-10（2026-08-31 使用者裁決）：期間 chip 從固定 30D 改四窗全開，切換立即
// 打新 window 參數，不 debounce（離散選擇）。
describe("ExplorePage — R4-10 期間窗切換", () => {
  it("四鈕全部可點（不再 disabled）；切換立即打新 window 參數並回第一頁", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(buildResp())));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");

    const dayBtn = screen.getByRole("button", { name: COPY.explore.windows.day });
    expect(dayBtn).not.toBeDisabled();

    fetchMock.mockClear();
    await userEvent.click(dayBtn);

    await waitFor(() => {
      const lastUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("window=day");
      expect(lastUrl).toContain("page=1");
    });
  });

  it("預設 window=month（30D）打第一次請求", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(buildResp())));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");

    const firstUrl = fetchMock.mock.calls[0]?.[0] as string;
    expect(firstUrl).toContain("window=month");
  });
});

// R4-10：原「僅顯示達樣本門檻（實盤 ≥ 30 天 · ≥ 200 筆成交）」合併 chip 拆成
// 兩顆獨立布林 chip（實盤天數／成交筆數），各自獨立映射固定門檻值（不是自由
// 填寫輸入框——R4-3 那版已被 revert，見 page.tsx 檔頭）。
describe("ExplorePage — R4-10 qualified chip 拆分", () => {
  it("預設兩顆 chip 皆開 → 打 API 帶 min_live_days=30 & min_fills=200", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(buildResp())));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");

    const firstUrl = fetchMock.mock.calls[0]?.[0] as string;
    expect(firstUrl).toContain("min_live_days=30");
    expect(firstUrl).toContain("min_fills=200");
  });

  it("關閉「實盤 ≥ 30 天」chip → 只送 min_live_days=0，min_fills 不受影響", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(buildResp())));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");

    fetchMock.mockClear();
    await userEvent.click(screen.getByRole("button", { name: COPY.explore.filters.liveDays }));

    await waitFor(() => {
      const lastUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("min_live_days=0");
      expect(lastUrl).toContain("min_fills=200");
    });
  });

  it("關閉「成交 ≥ 200 筆」chip → 只送 min_fills=0，min_live_days 不受影響", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(buildResp())));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");

    fetchMock.mockClear();
    await userEvent.click(screen.getByRole("button", { name: COPY.explore.filters.fills }));

    await waitFor(() => {
      const lastUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("min_fills=0");
      expect(lastUrl).toContain("min_live_days=30");
    });
  });

  it("回撤/集中度 chip 預設關閉（2026-08-31 裁決：避免空榜），首次請求送 100/100；點開才收緊為 30/90", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(buildResp())));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExplorePage />);
    await screen.findByText("Alice");
    // 預設 off → 首次請求即為不過濾邊界值
    const firstUrl = fetchMock.mock.calls[0]?.[0] as string;
    expect(firstUrl).toContain("max_dd_pct=100");
    expect(firstUrl).toContain("max_concentration_pct=100");

    fetchMock.mockClear();
    await userEvent.click(screen.getByRole("button", { name: COPY.explore.filters.maxDd }));
    await waitFor(() => {
      const lastUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("max_dd_pct=30");
    });

    fetchMock.mockClear();
    await userEvent.click(screen.getByRole("button", { name: COPY.explore.filters.concentrated }));
    await waitFor(() => {
      const lastUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("max_concentration_pct=90");
    });
  });
});
