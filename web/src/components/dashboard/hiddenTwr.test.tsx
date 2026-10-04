/**
 * 2026-10-04 使用者裁決：儀表板不顯示 30D TWR 報酬與最大回撤（對 follower 會被誇大），
 * 數值改放卡片根元素 data attribute 供 debug。
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DashboardEquity, DashboardPnl } from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { EquityCard } from "./EquityCard";
import { PnlCard } from "./PnlCard";

function equity(ret: string | null): DashboardEquity {
  return {
    account_value: "1000.00", margin_used: "100.00", withdrawable: "900.00",
    available_pct: "0.9", ret_30d_pct: ret,
  };
}

function pnl(dd: string | null): DashboardPnl {
  return {
    net: "10", realized: "8", unrealized: "2", fees_paid: "1",
    fee_share_of_pnl_pct: "10", win_rate_pct: "55", closed_positions: 7,
    max_drawdown_pct: dd, series: null,
  };
}

describe("EquityCard — 隱藏 30D TWR", () => {
  it("文字不含數字與 30D，值在 data-ret-30d-pct", () => {
    const { container } = render(<EquityCard equity={equity("-81.5")} />);
    expect(container.textContent).not.toContain("81.5");
    expect(container.textContent).not.toContain("30D");
    expect(container.textContent).not.toContain(COPY.dashboard.equity.retSuffix);
    expect(container.querySelector("[data-ret-30d-pct]")?.getAttribute("data-ret-30d-pct")).toBe("-81.5");
  });

  it("ret_30d_pct 為 null 或 equity 為 null → 屬性不存在", () => {
    const a = render(<EquityCard equity={equity(null)} />);
    expect(a.container.querySelector("[data-ret-30d-pct]")).toBeNull();
    const b = render(<EquityCard equity={null} />);
    expect(b.container.querySelector("[data-ret-30d-pct]")).toBeNull();
  });
});

describe("PnlCard — 隱藏最大回撤", () => {
  it("文字不含數字與回撤標籤，值在 data-max-drawdown-pct", () => {
    const { container } = render(<PnlCard pnl={pnl("-86.42")} />);
    expect(container.textContent).not.toContain("86.42");
    expect(container.textContent).not.toContain(COPY.dashboard.pnl.maxDrawdown);
    expect(container.querySelector("[data-max-drawdown-pct]")?.getAttribute("data-max-drawdown-pct")).toBe("-86.42");
  });

  it("max_drawdown_pct 為 null 或 pnl 為 null → 屬性不存在", () => {
    const a = render(<PnlCard pnl={pnl(null)} />);
    expect(a.container.querySelector("[data-max-drawdown-pct]")).toBeNull();
    const b = render(<PnlCard pnl={null} />);
    expect(b.container.querySelector("[data-max-drawdown-pct]")).toBeNull();
  });
});
