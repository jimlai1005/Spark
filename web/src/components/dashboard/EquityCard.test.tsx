/**
 * EquityCard 保證金三級分色（M3 round3 Task 6，R2 P2「Dashboard 保證金」）：
 * ≥5% 無框；<5% 黃框＋黃文案；<2% 紅框＋紅文案。門檻常數見
 * `LOW_MARGIN_THRESHOLD`／`CRITICAL_MARGIN_THRESHOLD`（本檔 export，Header.tsx 沿用同一個）。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DashboardEquity } from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { EquityCard, CRITICAL_MARGIN_THRESHOLD, LOW_MARGIN_THRESHOLD } from "./EquityCard";

const c = COPY.dashboard.equity;

function equityWithPct(pct: string): DashboardEquity {
  return {
    account_value: "1000.00", margin_used: "100.00", withdrawable: "900.00",
    available_pct: pct, ret_30d_pct: "1.0",
  };
}

describe("EquityCard — 保證金門檻常數", () => {
  it("黃色門檻 5%、紅色門檻 2%", () => {
    expect(LOW_MARGIN_THRESHOLD).toBe(0.05);
    expect(CRITICAL_MARGIN_THRESHOLD).toBe(0.02);
  });
});

describe("EquityCard — 保證金分級樣式", () => {
  it("≥5%（0.051）→ 無 data-margin 屬性，不出現任何告警文案", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.051")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).not.toHaveAttribute("data-margin");
    expect(screen.queryByText(c.lowMarginWarning)).not.toBeInTheDocument();
    expect(screen.queryByText(c.criticalMarginWarning)).not.toBeInTheDocument();
  });

  it("<5%（0.049）→ data-margin=\"warning\"，黃色文案", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.049")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).toHaveAttribute("data-margin", "warning");
    expect(screen.getByText(c.lowMarginWarning)).toBeInTheDocument();
    const warnCard = container.querySelector(".dash-low-margin-card");
    expect(warnCard).toHaveAttribute("data-level", "warning");
  });

  it("<2%（0.019）→ data-margin=\"critical\"，紅色文案（不是黃色）", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.019")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).toHaveAttribute("data-margin", "critical");
    expect(screen.getByText(c.criticalMarginWarning)).toBeInTheDocument();
    expect(screen.queryByText(c.lowMarginWarning)).not.toBeInTheDocument();
    const critCard = container.querySelector(".dash-low-margin-card");
    expect(critCard).toHaveAttribute("data-level", "critical");
  });

  it("邊界：恰好 2%（0.02）→ 仍是 warning（門檻用 < 不用 <=）", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.02")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).toHaveAttribute("data-margin", "warning");
  });
});
