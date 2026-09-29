/**
 * EquityCard 保證金數字變色（2026-09-29 移除紅／黃框與告警文案，使用者裁決：
 * 跟單用戶無法自行調整可用保證金，警示文案嚇人多於有用；僅保留可用保證金
 * **數字**在低水位時變色）。門檻常數見
 * `LOW_MARGIN_THRESHOLD`／`CRITICAL_MARGIN_THRESHOLD`（本檔 export，僅本檔使用）。
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

/**
 * 用「可用保證金」標籤反查同一列的數字 span——不用位置（最後一列）定位，
 * 否則日後在下方多加一列時 helper 會漂移到別的元素而讓變色斷言靜默通過
 * （2026-09-29 reviewer 變異實測抓到）。
 */
function availableMarginValueSpan(): Element | null {
  const label = screen.getByText(c.availableMargin);
  const row = label.closest(".dash-margin-row");
  return row?.querySelector("span.mono") ?? null;
}

describe("EquityCard — 保證金門檻常數", () => {
  it("黃色門檻 5%、紅色門檻 2%", () => {
    expect(LOW_MARGIN_THRESHOLD).toBe(0.05);
    expect(CRITICAL_MARGIN_THRESHOLD).toBe(0.02);
  });
});

describe("EquityCard — 保證金分級樣式", () => {
  it("≥5%（0.051）→ 無 data-margin 屬性、無告警卡、數字無變色", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.051")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).not.toHaveAttribute("data-margin");
    expect(container.querySelector(".dash-low-margin-card")).not.toBeInTheDocument();
    const span = availableMarginValueSpan();
    expect(span).not.toBeNull();
    expect((span as HTMLElement).style.color).toBe("");
  });

  it("<5%（0.049）→ 無 data-margin 屬性、無告警卡、數字變黃色", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.049")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).not.toHaveAttribute("data-margin");
    expect(container.querySelector(".dash-low-margin-card")).not.toBeInTheDocument();
    const span = availableMarginValueSpan();
    expect((span as HTMLElement).style.color).toBe("var(--warn)");
  });

  it("<2%（0.019）→ 無 data-margin 屬性、無告警卡、數字變紅色（不是黃色）", () => {
    const { container } = render(<EquityCard equity={equityWithPct("0.019")} />);
    const card = container.querySelector(".dash-card-equity");
    expect(card).not.toHaveAttribute("data-margin");
    expect(container.querySelector(".dash-low-margin-card")).not.toBeInTheDocument();
    const span = availableMarginValueSpan();
    expect((span as HTMLElement).style.color).toBe("var(--neg)");
  });

  it("邊界：恰好 2%（0.02）→ 仍是黃色（門檻用 < 不用 <=，非紅色）", () => {
    render(<EquityCard equity={equityWithPct("0.02")} />);
    const span = availableMarginValueSpan();
    expect((span as HTMLElement).style.color).toBe("var(--warn)");
  });
});
