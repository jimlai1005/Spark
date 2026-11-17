/**
 * `/docs#methodology` 績效定義三句（plan strategy-return-hl-roi §1.5）：
 * 與策略詳情頁方法論卡共用 `strategyDetail.methodology` 同一份字串。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import DocsPage from "./page";

describe("DocsPage methodology definitions", () => {
  it("渲染 ROI／逐段報酬／取樣年化三句", () => {
    render(<DocsPage />);
    const m = COPY.strategyDetail.methodology;
    expect(screen.getByText(m.roiNote)).toBeInTheDocument();
    expect(screen.getByText(m.segmentReturnNote)).toBeInTheDocument();
    expect(screen.getByText(m.samplingNote)).toBeInTheDocument();
  });
});
