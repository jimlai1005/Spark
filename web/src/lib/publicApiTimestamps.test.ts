/**
 * `getPublicStrategy` 對 `equity_index_ts`／`metrics.return_sample_count` 的處理
 * （plan 2026-11-17-perf-followups §1 A2）：缺席時整個鍵不出現（不補假值）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { getPublicStrategy, returnSampleCount } from "./publicApi";

function stub(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: async () => body } as Response)));
}

afterEach(() => vi.unstubAllGlobals());

describe("getPublicStrategy — equity_index_ts／return_sample_count", () => {
  it("有時間戳與 return_sample_count → 原樣帶出", async () => {
    stub({
      slug: "core", equity_index: ["1", "1.1"], equity_index_ts: [0, 864000000],
      metrics: { sample_count: 2, return_sample_count: 1 },
    });
    const r = await getPublicStrategy("core");
    expect(r?.equity_index_ts).toEqual([0, 864000000]);
    expect(r?.metrics.return_sample_count).toBe(1);
  });

  it("缺席／null／含非數字 → 不出現該鍵", async () => {
    for (const ts of [undefined, null, [0, "x"]]) {
      stub({ slug: "core", equity_index: ["1", "1.1"], equity_index_ts: ts, metrics: { sample_count: 2 } });
      const r = await getPublicStrategy("core");
      expect(r).not.toBeNull();
      expect("equity_index_ts" in (r as object)).toBe(false);
      expect("return_sample_count" in (r as { metrics: object }).metrics).toBe(false);
    }
  });
});

describe("returnSampleCount", () => {
  it("優先用後端值；缺席退回 max(sample_count − 1, 0)", () => {
    expect(returnSampleCount({ sample_count: 38, return_sample_count: 37 })).toBe(37);
    expect(returnSampleCount({ sample_count: 38 })).toBe(37);
    expect(returnSampleCount({ sample_count: 0 })).toBe(0);
  });
});
