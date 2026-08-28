import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FeeCalculator } from "./FeeCalculator";

describe("FeeCalculator", () => {
  it("錨例：預設 $10,000 → 建倉/平倉各 USD 2.00，合計 USD 4.00", () => {
    render(<FeeCalculator />);
    expect(screen.getByText("USD 10,000")).toBeInTheDocument();
    expect(screen.getAllByText("USD 2.00")).toHaveLength(2);
    expect(screen.getByText("USD 4.00")).toBeInTheDocument();
  });

  it("錨例：拉到 $100,000 → 合計 USD 40.00", () => {
    render(<FeeCalculator />);
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "100000" } });
    expect(screen.getByText("USD 100,000")).toBeInTheDocument();
    expect(screen.getAllByText("USD 20.00")).toHaveLength(2);
    expect(screen.getByText("USD 40.00")).toBeInTheDocument();
  });

  it("slider 邊界：min $1,000 / max $100,000", () => {
    render(<FeeCalculator />);
    const slider = screen.getByRole("slider") as HTMLInputElement;
    expect(slider.min).toBe("1000");
    expect(slider.max).toBe("100000");
    expect(slider.step).toBe("1000");
  });
});
