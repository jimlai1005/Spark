/**
 * PositionsTable —「成交記錄・授權歷程」tab（M3 round2 Task 7）。
 * 資料**直取 Hyperliquid**（`getMyFills`／`getMyAuthorizations`），不讀自家 DB
 * ——本檔只驗證前端組裝層：tab 不再 disabled、lazy fetch（切到 tab 才打 API）、
 * load/error/empty 三態各自獨立。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LangProvider } from "@/lib/lang";
import type { DashboardPosition, MyAuthorizationRow, MyFillRow } from "@/lib/api";

const getMyFills = vi.fn<() => Promise<{ fills: MyFillRow[] }>>();
const getMyAuthorizations = vi.fn<() => Promise<{ authorizations: MyAuthorizationRow[] }>>();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMyFills: () => getMyFills(),
  getMyAuthorizations: () => getMyAuthorizations(),
}));

import { PositionsTable } from "./PositionsTable";

const POSITION: DashboardPosition = {
  symbol: "ETH", side: "long", leverage: "3", margin_mode: "cross",
  value: "2492.50", upnl: "-0.16", entry: "2452.76", mark: "2453.1575",
  deviation_pct: "0.02",
};

const FILL: MyFillRow = {
  time: 1774926504932, coin: "ETH", side: "B", px: "2074.9", sz: "41.4803",
  fee: "-2.582024", closed_pnl: "217.356772",
  hash: "0x317e78012add56b532f80438128ac402033900e6c5d07587d5472353e9d1309f",
};

const AUTH: MyAuthorizationRow = {
  time: 1787752386163, action_type: "approveAgent",
  agent_address: "0xaf2292a19d2b144f17115be0775851cd878ef72c",
  builder: null, max_fee_rate: null,
  hash: "0x78421cd43cf39c2079bb04430552e5020c4600b9d7f6baf21c0ac826fbf7760b",
};

const AUTH_BUILDER_FEE: MyAuthorizationRow = {
  time: 1787375746030, action_type: "approveBuilderFee",
  agent_address: null,
  builder: "0x5af1b5f44207784dcb850bbb4143c5dcd1885f71", max_fee_rate: "0.095%",
  hash: "0xadde8c810af9aa5aaf580442b4463e0207a90066a5fcc92c51a737d3c9fd8445",
};

function renderTable() {
  return render(
    <LangProvider>
      <PositionsTable positions={[POSITION]} feesMonth={null} />
    </LangProvider>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("PositionsTable — history tab", () => {
  it("history tab 不再 disabled，可以點擊切換", () => {
    getMyFills.mockResolvedValue({ fills: [] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [] });
    renderTable();
    const tab = screen.getByText("成交記錄・授權歷程");
    expect(tab.closest("button")).not.toBeDisabled();
  });

  it("lazy fetch：未切到 history tab 前不打 API", () => {
    getMyFills.mockResolvedValue({ fills: [] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [] });
    renderTable();
    expect(getMyFills).not.toHaveBeenCalled();
    expect(getMyAuthorizations).not.toHaveBeenCalled();
  });

  it("切到 history tab 才打兩支 API（各一次）", async () => {
    getMyFills.mockResolvedValue({ fills: [] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [] });
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    await waitFor(() => expect(getMyFills).toHaveBeenCalledTimes(1));
    expect(getMyAuthorizations).toHaveBeenCalledTimes(1);
  });

  it("載入中顯示 loading 文案", () => {
    getMyFills.mockReturnValue(new Promise(() => {})); // 永不 resolve
    getMyAuthorizations.mockReturnValue(new Promise(() => {}));
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    expect(screen.getByText("讀取中…")).toBeInTheDocument();
  });

  it("兩者皆空 → 各自顯示 empty 文案", async () => {
    getMyFills.mockResolvedValue({ fills: [] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [] });
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    await waitFor(() => expect(screen.getByText("近期沒有成交紀錄。")).toBeInTheDocument());
    expect(screen.getByText("沒有查到授權紀錄。")).toBeInTheDocument();
  });

  it("成交記錄查詢失敗 → 顯示錯誤態；授權歷程不受拖累照常顯示", async () => {
    getMyFills.mockRejectedValue(new Error("hl 503"));
    getMyAuthorizations.mockResolvedValue({ authorizations: [AUTH] });
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    await waitFor(() =>
      expect(screen.getAllByText("資料暫時讀不到（直接查詢 Hyperliquid 失敗），請稍後重試。").length)
        .toBeGreaterThan(0));
    expect(screen.getByText("approveAgent")).toBeInTheDocument();
  });

  it("兩者皆有資料 → 渲染成交列與授權列", async () => {
    getMyFills.mockResolvedValue({ fills: [FILL] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [AUTH] });
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    await waitFor(() => expect(screen.getByText("ETH")).toBeInTheDocument());
    expect(screen.getByText("approveAgent")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "查看" });
    expect(links.some((a) =>
      a.getAttribute("href") === `https://app.hyperliquid.xyz/explorer/tx/${FILL.hash}`)).toBe(true);
    expect(links.some((a) =>
      a.getAttribute("href") === `https://app.hyperliquid.xyz/explorer/tx/${AUTH.hash}`)).toBe(true);
  });

  it("[W2] approveAgent → 前端用結構化欄位組出中文摘要（不吃後端 summary 字串）", async () => {
    getMyFills.mockResolvedValue({ fills: [] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [AUTH] });
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    expect(await screen.findByText(
      `授權 API wallet ${AUTH.agent_address}`)).toBeInTheDocument();
  });

  it("[W2] approveBuilderFee → 前端組出「授權 builder fee 費率 給 位址」", async () => {
    getMyFills.mockResolvedValue({ fills: [] });
    getMyAuthorizations.mockResolvedValue({ authorizations: [AUTH_BUILDER_FEE] });
    renderTable();
    fireEvent.click(screen.getByText("成交記錄・授權歷程"));
    expect(await screen.findByText(
      `授權 builder fee ${AUTH_BUILDER_FEE.max_fee_rate} 給 ${AUTH_BUILDER_FEE.builder}`,
    )).toBeInTheDocument();
  });
});
