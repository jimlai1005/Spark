/** PositionsTable — upnl 金額格式（Task 19 修正：-$0.16 而非 -$0.1600）。 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LangProvider } from "@/lib/lang";
import type { DashboardPosition } from "@/lib/api";
import { PositionsTable } from "./PositionsTable";

const POSITION: DashboardPosition = {
  symbol: "ETH", side: "long", leverage: "3", margin_mode: "cross",
  value: "2492.50", upnl: "-0.16", entry: "2452.76", mark: "2453.1575",
  deviation_pct: "0.02",
};

function renderTable(positions: DashboardPosition[]) {
  return render(
    <LangProvider>
      <PositionsTable positions={positions} feesMonth={null} />
    </LangProvider>,
  );
}

describe("PositionsTable — upnl 格式", () => {
  it("負值小額 upnl 固定兩位小數（-$0.16，不是 -$0.1600）", () => {
    renderTable([POSITION]);
    expect(screen.getByText("-$0.16")).toBeInTheDocument();
    expect(screen.queryByText("-$0.1600")).not.toBeInTheDocument();
  });

  it("正值小額 upnl 同樣兩位小數", () => {
    renderTable([{ ...POSITION, upnl: "0.084" }]);
    expect(screen.getByText("+$0.08")).toBeInTheDocument();
  });

  it("整數金額 upnl 也是兩位小數", () => {
    renderTable([{ ...POSITION, upnl: "39.5" }]);
    expect(screen.getByText("+$39.50")).toBeInTheDocument();
  });
});
