import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import { CapabilityMatrix } from "./CapabilityMatrix";

describe("CapabilityMatrix", () => {
  it("渲染可以/不能各四條與單方可撤銷段", () => {
    render(<CapabilityMatrix />);
    for (const item of COPY.auth.can) expect(screen.getByText(item)).toBeInTheDocument();
    for (const item of COPY.auth.cannot) expect(screen.getByText(item)).toBeInTheDocument();
    expect(screen.getByText(COPY.auth.revocable)).toBeInTheDocument();
  });

  it("id prop 掛在 section 上，供 header 錨點跳轉", () => {
    const { container } = render(<CapabilityMatrix id="security" />);
    expect(container.querySelector("#security")).not.toBeNull();
  });
});
