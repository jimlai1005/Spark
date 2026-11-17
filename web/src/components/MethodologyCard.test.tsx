/**
 * `MethodologyCard` 測試（plan strategy-return-hl-roi Task 2）：
 * 績效定義三句（ROI／逐段報酬／取樣年化）為靜態文案，不依賴 `periods_per_year`／
 * `return_basis`；`initial_deposit_usd` 以「投入本金」句型渲染（ZH／EN 對稱）。
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY_EN, COPY_ZH } from "@/lib/copy";
import type { PublicStrategyMetrics, PublicStrategyMethodology } from "@/lib/publicApi";
import { MethodologyCard } from "./MethodologyCard";

const METHODOLOGY: PublicStrategyMethodology = {
  start_date: "2026-01-01",
  end_date: "2026-03-01",
  initial_deposit_usd: "336535.12",
  start_equity_usd: "8800",
  end_equity_usd: "3838588",
  sample_count: 9,
  annualization_days: 365,
  risk_free_rate: "0",
  basis: "combined",
  updated_at: 0,
};

const METRICS = {
  sharpe: "1.32",
  sharpe_insufficient: false,
  sharpe_se: "0.5",
  sharpe_se_insufficient: false,
  sample_count: 9,
} as unknown as PublicStrategyMetrics;

describe("MethodologyCard", () => {
  for (const [name, COPY] of [["zh", COPY_ZH], ["en", COPY_EN]] as const) {
    it(`${name}：三句績效定義＋投入本金句型渲染（無 periods_per_year／return_basis 也成立）`, () => {
      const m = COPY.strategyDetail.methodology;
      const { container } = render(
        <MethodologyCard methodology={METHODOLOGY} metrics={METRICS} copy={m} />,
      );
      const text = container.textContent ?? "";
      expect(text).toContain(m.roiNote);
      expect(text).toContain(m.segmentReturnNote);
      expect(text).toContain(m.samplingNote);
      expect(text).toContain(`${m.depositPrefix}336,535${m.depositSuffix}`);
      expect(text).toContain(`9${m.daysSuffix}2026-01-01 → 2026-03-01${m.rangeSuffix}`);
    });
  }

  it("perf 不可用（全 null）時仍顯示三句定義", () => {
    const m = COPY_ZH.strategyDetail.methodology;
    const empty: PublicStrategyMethodology = {
      ...METHODOLOGY,
      start_date: null, end_date: null, initial_deposit_usd: null,
      start_equity_usd: null, end_equity_usd: null, sample_count: null,
    };
    const noMetrics = { ...METRICS, sharpe_insufficient: true } as PublicStrategyMetrics;
    const { container } = render(<MethodologyCard methodology={empty} metrics={noMetrics} copy={m} />);
    expect(container.textContent).toContain(m.roiNote);
    expect(container.textContent).toContain(m.samplingNote);
  });
});
