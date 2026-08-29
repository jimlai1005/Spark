import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { EquityCurve } from "./EquityCurve";

describe("EquityCurve", () => {
  it("有序列 → 畫出 svg polyline，預設週期為全部", () => {
    const series = Array.from({ length: 40 }, (_, i) => String(1 + i * 0.01));
    render(<EquityCurve equityIndex={series} />);
    const svg = screen.getByRole("img", { name: COPY.strategyDetail.equity.heading });
    const polyline = svg.querySelector("polyline");
    expect(polyline).not.toBeNull();
    expect(polyline?.getAttribute("points")?.split(" ").length).toBe(40);
  });

  it("切到 30D → polyline 點數裁切為最後 30 點", () => {
    const series = Array.from({ length: 72 }, (_, i) => String(1 + i * 0.01));
    render(<EquityCurve equityIndex={series} />);
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.equity.period30d }));
    const svg = screen.getByRole("img", { name: COPY.strategyDetail.equity.heading });
    expect(svg.querySelector("polyline")?.getAttribute("points")?.split(" ").length).toBe(30);
  });

  it("切到 7D → 裁切為最後 7 點", () => {
    const series = Array.from({ length: 72 }, (_, i) => String(1 + i * 0.01));
    render(<EquityCurve equityIndex={series} />);
    fireEvent.click(screen.getByRole("button", { name: COPY.strategyDetail.equity.period7d }));
    const svg = screen.getByRole("img", { name: COPY.strategyDetail.equity.heading });
    expect(svg.querySelector("polyline")?.getAttribute("points")?.split(" ").length).toBe(7);
  });

  it("序列不足 2 點 → 顯示空態，不畫 svg", () => {
    render(<EquityCurve equityIndex={["1"]} />);
    expect(screen.getByText(COPY.strategyDetail.equity.empty)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("空序列 → 顯示空態", () => {
    render(<EquityCurve equityIndex={[]} />);
    expect(screen.getByText(COPY.strategyDetail.equity.empty)).toBeInTheDocument();
  });

  it("疊加對照 checkbox 一律 disabled（NOTE 09：無資料源）", () => {
    const series = ["1", "1.01", "1.02"];
    render(<EquityCurve equityIndex={series} />);
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.length).toBe(4);
    for (const b of boxes) expect(b).toBeDisabled();
  });

  it("y 軸（Task 19 修正）：無 initialDepositUsd 時顯示 6 個原始 index 刻度，最大值在最上方", () => {
    const series = ["1", "1.05", "0.98", "1.10"];
    const { container } = render(<EquityCurve equityIndex={series} />);
    const ticks = container.querySelectorAll(".equity-curve-yaxis span");
    expect(ticks.length).toBe(6);
    expect(ticks[0].textContent).toBe((1.10).toFixed(3));
    expect(ticks[5].textContent).toBe((0.98).toFixed(3));
  });

  it("y 軸：有 initialDepositUsd 時換算為美元金額（$ 前綴），與起訖淨值同一換算式", () => {
    const series = ["1", "1.5", "2"];
    const { container } = render(<EquityCurve equityIndex={series} initialDepositUsd="1000" />);
    const ticks = Array.from(container.querySelectorAll(".equity-curve-yaxis span")).map((s) => s.textContent);
    // 首點 1 → $1000；末點 2（首點比值 2） → $2000。
    expect(ticks[0]).toBe("$2,000");
    expect(ticks[ticks.length - 1]).toBe("$1,000");
  });

  it("x 軸（Task 19 修正）：有 start/end date 時顯示日期標籤，末尾對齊 endDate", () => {
    const series = Array.from({ length: 8 }, (_, i) => String(1 + i * 0.01));
    const { container } = render(
      <EquityCurve equityIndex={series} startDate="2026-01-01" endDate="2026-01-08" />,
    );
    const labels = Array.from(container.querySelectorAll(".equity-curve-xaxis span")).map((s) => s.textContent);
    expect(labels.length).toBeGreaterThanOrEqual(5);
    expect(labels.length).toBeLessThanOrEqual(7);
    expect(labels[labels.length - 1]).toBe("01-08");
    expect(labels[0]).toBe("01-01");
  });

  it("x 軸：無日期資訊時退化為相對天數 D1…Dn（依完整序列位置）", () => {
    const series = Array.from({ length: 8 }, (_, i) => String(1 + i * 0.01));
    const { container } = render(<EquityCurve equityIndex={series} />);
    const labels = Array.from(container.querySelectorAll(".equity-curve-xaxis span")).map((s) => s.textContent);
    expect(labels[0]).toBe("D1");
    expect(labels[labels.length - 1]).toBe("D8");
  });

  it("網格橫線：svg 內有 6 條沿設計稿色值的水平線", () => {
    const series = ["1", "1.05", "0.98", "1.10"];
    const { container } = render(<EquityCurve equityIndex={series} />);
    const lines = container.querySelectorAll(".equity-curve-svg line");
    expect(lines.length).toBe(6);
    for (const l of lines) expect(l.getAttribute("stroke")).toBe("#1a1e23");
  });
});
