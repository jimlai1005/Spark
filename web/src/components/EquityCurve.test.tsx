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
});
