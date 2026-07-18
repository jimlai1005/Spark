import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Boundary } from "./Boundary";

describe("Boundary", () => {
  it("渲染兩側面板、絲線比例與 pill 狀態", () => {
    const { container } = render(
      <Boundary
        walletTitle="你的錢包" engineTitle="Filet 引擎"
        walletItems={[{ dt: "地址", dd: "0x5579…B5d", mono: true }]}
        engineItems={[{ dt: "策略", dd: "網格・多幣" }]}
        threadPct={50} pillText="尚未授權" pillActive={false}
      />,
    );
    expect(screen.getByText("你的錢包")).toBeInTheDocument();
    expect(screen.getByText("0x5579…B5d")).toBeInTheDocument();
    const boundary = container.querySelector(".boundary") as HTMLElement;
    expect(boundary.style.getPropertyValue("--thread-pct")).toBe("50");
    expect(container.querySelector(".boundary-pill")).not.toHaveClass("is-active");
  });
});
