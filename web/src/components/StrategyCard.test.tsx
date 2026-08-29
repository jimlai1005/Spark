import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import type { PublicStrategy } from "@/lib/publicApi";
import { StrategyCard } from "./StrategyCard";

const BASE: PublicStrategy = {
  slug: "core",
  name: "Filet Core",
  tagline: "多資產動能 · 永續合約",
  featured: false,
  leader_address: "0xfeed000000000000000000000000000000f00d",
  status: "running",
  listable: true,
  live_days: 72,
  follower_count: 14,
  min_notional_usd: "500",
  max_leverage: "3",
  metrics: {
    total_return_pct: "31.42", total_return_pct_insufficient: false,
    max_drawdown_pct: "-2.71", max_drawdown_pct_insufficient: false,
    sharpe: "6.28", sharpe_insufficient: false,
    sharpe_se: "1.59", sharpe_se_insufficient: false,
    win_rate_pct: "58.13", win_rate_pct_insufficient: false,
    annualized_vol_pct: "19.99", annualized_vol_pct_insufficient: false,
    sortino: "12.12", sortino_insufficient: false,
    best_day_pct: "4.44", best_day_pct_insufficient: false,
    worst_day_pct: "-2.71", worst_day_pct_insufficient: false,
    sample_count: 72,
  },
};

describe("StrategyCard", () => {
  it("listable → 渲染指標與可跟單 CTA 連向 /strategies/{slug}", () => {
    render(<StrategyCard strategy={BASE} />);
    expect(screen.getByText("Filet Core")).toBeInTheDocument();
    expect(screen.getByText(/31\.42%/)).toBeInTheDocument();
    expect(screen.getByText(/-2\.71%/)).toBeInTheDocument();
    expect(screen.getByText(/58\.13%/)).toBeInTheDocument();
    const cta = screen.getByRole("link", { name: COPY.home.strategies.cta });
    expect(cta).toHaveAttribute("href", "/strategies/core");
  });

  it("Sharpe 附標準誤（±se）", () => {
    render(<StrategyCard strategy={BASE} />);
    expect(screen.getByText(/±1\.59/)).toBeInTheDocument();
  });

  it("featured → 顯示主推 badge", () => {
    render(<StrategyCard strategy={{ ...BASE, featured: true }} />);
    expect(screen.getByText(COPY.home.strategies.featuredBadge)).toBeInTheDocument();
  });

  it("listable=false → disabled 態：無可跟單連結，顯示暫不開放新跟單＋說明", () => {
    const pending: PublicStrategy = {
      ...BASE,
      listable: false,
      metrics: { ...BASE.metrics, sharpe: null, sharpe_insufficient: true, sharpe_se: null, sharpe_se_insufficient: true },
    };
    render(<StrategyCard strategy={pending} />);
    expect(screen.queryByRole("link", { name: COPY.home.strategies.cta })).not.toBeInTheDocument();
    expect(screen.getAllByText(COPY.home.strategies.pendingBadge).length).toBeGreaterThan(0);
    expect(screen.getByText(COPY.home.strategies.pendingNote)).toBeInTheDocument();
    const disabledCta = screen.getByTestId("strategy-card-disabled");
    expect(disabledCta).toBeDisabled();
  });

  it("指標 insufficient → 顯示「樣本不足」而非數字", () => {
    const thin: PublicStrategy = {
      ...BASE,
      metrics: { ...BASE.metrics, sharpe: null, sharpe_insufficient: true },
    };
    render(<StrategyCard strategy={thin} />);
    expect(screen.getByText(COPY.home.strategies.insufficientLabel)).toBeInTheDocument();
  });

  it("chips：槓桿上限與最低跟單金額", () => {
    render(<StrategyCard strategy={BASE} />);
    expect(screen.getByText(/槓桿 ≤ 3x/)).toBeInTheDocument();
    expect(screen.getByText(/最低跟單 \$500/)).toBeInTheDocument();
  });

  it("summary=true（Task 19 修正）：不渲染 CTA（listable 與 non-listable 皆不出現）", () => {
    render(<StrategyCard strategy={BASE} summary />);
    expect(screen.queryByRole("link", { name: COPY.home.strategies.cta })).not.toBeInTheDocument();
    expect(screen.queryByTestId("strategy-card-disabled")).not.toBeInTheDocument();

    const pending: PublicStrategy = { ...BASE, listable: false };
    render(<StrategyCard strategy={pending} summary />);
    expect(screen.queryByText(COPY.home.strategies.pendingNote)).not.toBeInTheDocument();
    expect(screen.queryByTestId("strategy-card-disabled")).not.toBeInTheDocument();
  });
});
