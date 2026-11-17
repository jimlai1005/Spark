/**
 * `EquityCurve` 對照線對齊與 X 軸標籤補強（plan 2026-11-17-perf-followups §6 第 3 點，
 * Task A2）：
 * - 收盤價查找「當日或之前最近一筆」，回溯上限 4 個日曆日（B3、邊界）；
 * - rebase 錨點＝切片內第一個查得到收盤價的點，錨點之前為 null（B4）；
 * - 切片首末相距 > 300 天 → x 軸標籤 `YYYY-MM`，否則 `MM-DD`（B5）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  alignClosesOnOrBefore, EquityCurve, rebaseFromFirstAvailable, xFractionsFromTs, xLabelsFromTs,
} from "./EquityCurve";

const DAY = 86_400_000;
const ms = (iso: string) => Date.parse(`${iso}T00:00:00Z`);

const SAT_DATES = ["2026-01-03", "2026-01-10", "2026-01-17"];   // 皆為週六

afterEach(() => vi.unstubAllGlobals());

describe("收盤價查找：當日或之前最近一筆", () => {
  it("B3：取樣點全在週六、標的只有週五收盤 → 取到 100／110／121，rebase 後 1／1.1／1.21", () => {
    const series: [number, string][] = [
      [ms("2026-01-02"), "100"], [ms("2026-01-09"), "110"], [ms("2026-01-16"), "121"],
    ];
    const closes = alignClosesOnOrBefore(series, SAT_DATES);
    expect(closes).toEqual([100, 110, 121]);
    const rebased = rebaseFromFirstAvailable([1, 1, 1], closes as (number | null)[]);
    expect(rebased[0]).toBe(1);
    expect(rebased[1]).toBeCloseTo(1.1, 12);
    expect(rebased[2]).toBeCloseTo(1.21, 12);
  });

  it("B4：標的最早收盤價晚於第一點 → 第一點 null，錨點是第二點：null、1、1.1", () => {
    const series: [number, string][] = [[ms("2026-01-09"), "110"], [ms("2026-01-16"), "121"]];
    const closes = alignClosesOnOrBefore(series, SAT_DATES);
    expect(closes).toEqual([null, 110, 121]);
    const rebased = rebaseFromFirstAvailable([1, 1, 1], closes as (number | null)[]);
    expect(rebased[0]).toBeNull();
    expect(rebased[1]).toBe(1);
    expect(rebased[2]).toBeCloseTo(1.1, 12);
  });

  it("回溯上限邊界：相距恰 4 個日曆日取得到、5 個日曆日為 null", () => {
    // 取樣日 2026-01-10；收盤價在 01-06（相距 4 日）／01-05（相距 5 日）
    expect(alignClosesOnOrBefore([[ms("2026-01-06"), "50"]], ["2026-01-10"])).toEqual([50]);
    expect(alignClosesOnOrBefore([[ms("2026-01-05"), "50"]], ["2026-01-10"])).toEqual([null]);
    // 當日有值優先於之前的值
    expect(alignClosesOnOrBefore(
      [[ms("2026-01-09"), "40"], [ms("2026-01-10"), "60"]], ["2026-01-10"])).toEqual([60]);
    // 取「最近一筆」而非最早一筆
    expect(alignClosesOnOrBefore(
      [[ms("2026-01-07"), "40"], [ms("2026-01-09"), "45"]], ["2026-01-10"])).toEqual([45]);
  });

  it("序列為 null（上游失敗）→ null；日期為 null → 該點 null；全部查不到 → 全 null", () => {
    expect(alignClosesOnOrBefore(null, SAT_DATES)).toBeNull();
    expect(alignClosesOnOrBefore([[ms("2026-01-02"), "100"]], [null])).toEqual([null]);
    expect(rebaseFromFirstAvailable([1, 1], [null, null])).toEqual([null, null]);
    expect(rebaseFromFirstAvailable([1, 1], [0, 5])[0]).toBeNull();   // 零不能當錨點
  });
});

describe("EquityCurve 元件：週末取樣點的對照線不消失", () => {
  it("取樣點皆為週六、BTC 只有週五收盤 → 對照線仍畫出 3 點", async () => {
    const ts = SAT_DATES.map((d) => ms(d) + 12 * 3600_000);
    const series = {
      btc: [[ms("2026-01-02"), "100"], [ms("2026-01-09"), "110"], [ms("2026-01-16"), "121"]],
      eth: [], sp500: [], gold: [],
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, json: async () => ({ series, updated_at: 1 }),
    } as Response)));
    const { container } = render(<EquityCurve equityIndex={["1", "1.1", "1.2"]} timestamps={ts} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "BTC" }));
    await waitFor(() => expect(container.querySelectorAll("svg polyline").length).toBe(2));
    const pts = container.querySelectorAll("svg polyline")[1].getAttribute("points") ?? "";
    expect(pts.split(" ").length).toBe(3);
  });
});

describe("B5：X 軸標籤格式依跨度切換", () => {
  function labels(container: HTMLElement): string[] {
    return Array.from(container.querySelectorAll(".equity-curve-xaxis span")).map(
      (s) => s.textContent ?? "");
  }
  const T0 = Date.UTC(2025, 2, 5, 12);   // 2025-03-05T12:00Z

  it("首末相距 400 天 → 首末標籤為 YYYY-MM", () => {
    const ts = Array.from({ length: 5 }, (_, i) => T0 + i * 100 * DAY);
    const { container } = render(
      <EquityCurve equityIndex={["1", "1.1", "1.2", "1.3", "1.4"]} timestamps={ts} />);
    const l = labels(container);
    expect(l[0]).toBe("2025-03");
    expect(l[l.length - 1]).toBe("2026-04");
    for (const x of l) expect(x).toMatch(/^\d{4}-\d{2}$/);
  });

  it("首末相距 60 天 → MM-DD", () => {
    const ts = Array.from({ length: 5 }, (_, i) => T0 + i * 15 * DAY);
    const { container } = render(
      <EquityCurve equityIndex={["1", "1.1", "1.2", "1.3", "1.4"]} timestamps={ts} />);
    const l = labels(container);
    expect(l[0]).toBe("03-05");
    expect(l[l.length - 1]).toBe("05-04");
    for (const x of l) expect(x).toMatch(/^\d{2}-\d{2}$/);
  });

  it("恰 300 天 → 仍是 MM-DD（門檻為嚴格大於）", () => {
    const ts = [T0, T0 + 300 * DAY];
    const { container } = render(<EquityCurve equityIndex={["1", "1.1"]} timestamps={ts} />);
    expect(labels(container)[0]).toMatch(/^\d{2}-\d{2}$/);
  });
});

describe("B6：rebase 乘數＝錨點當天的主曲線值（Task F W1）", () => {
  it("B6：主曲線 [100,120,150]、收盤價對齊後 [null,110,121] → 對照線 [null,120,132]", () => {
    // 主曲線首點 100 ≠ 錨點（第 2 點）值 120：乘數若誤用切片首點會得到 [null,100,110]。
    const out = rebaseFromFirstAvailable([100, 120, 150], [null, 110, 121]);
    expect(out[0]).toBeNull();
    expect(out[1]).toBeCloseTo(120, 10);
    expect(out[2]).toBeCloseTo(132, 10);
  });

  it("B6 元件層：全期 > 切片首點無收盤價時，對照線在錨點那天與主曲線重合（y 相同）", async () => {
    // 3 點、主曲線 100／120／150（index 1／1.2／1.5，initialDepositUsd=100）；
    // 標的只有第 2、3 點當日收盤價 → 對照線第 1 點缺、第 2 點與主曲線同高。
    const ts = ["2026-03-02", "2026-03-03", "2026-03-04"].map((d) => ms(d) + 12 * 3600_000);
    const series = {
      btc: [[ms("2026-03-03"), "110"], [ms("2026-03-04"), "121"]], eth: [], sp500: [], gold: [],
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, json: async () => ({ series, updated_at: 1 }),
    } as Response)));
    const { container } = render(
      <EquityCurve equityIndex={["1", "1.2", "1.5"]} timestamps={ts} initialDepositUsd="100" />);
    fireEvent.click(screen.getByRole("checkbox", { name: "BTC" }));
    await waitFor(() => expect(container.querySelectorAll("svg polyline").length).toBe(2));
    const [main, ov] = Array.from(container.querySelectorAll("svg polyline"))
      .map((e) => (e.getAttribute("points") ?? "").split(" ").map((p) => p.split(",").map(Number)));
    expect(ov.length).toBe(2);
    // 對照線第 1 點（錨點）＝主曲線第 2 點：x、y 皆相同
    expect(ov[0][0]).toBeCloseTo(main[1][0], 2);
    expect(ov[0][1]).toBeCloseTo(main[1][1], 2);
  });
});

describe("B7：x 位置依時間比例（Task F）", () => {
  it("B7：時間戳第 0、10、30 天 → x 比例 0、1/3、1", () => {
    const f = xFractionsFromTs([0, 10 * DAY, 30 * DAY]);
    expect(f[0]).toBe(0);
    expect(f[1]).toBeCloseTo(1 / 3, 12);
    expect(f[2]).toBe(1);
  });

  it("B7 元件層：主曲線 polyline 的 x＝比例×900（0、300、900）", () => {
    const ts = [0, 10 * DAY, 30 * DAY];
    const { container } = render(<EquityCurve equityIndex={["1", "1.1", "1.2"]} timestamps={ts} />);
    const xs = (container.querySelector("svg polyline")?.getAttribute("points") ?? "")
      .split(" ").map((p) => Number(p.split(",")[0]));
    expect(xs).toEqual([0, 300, 900]);
  });

  it("主曲線與對照線共用同一組 x：對照線與主曲線在同一取樣點 x 相同", async () => {
    const ts = [0, 10 * DAY, 30 * DAY].map((t) => ms("2026-03-01") + t);
    const series = {
      btc: [[ms("2026-03-01"), "100"], [ms("2026-03-11"), "110"], [ms("2026-03-31"), "130"]],
      eth: [], sp500: [], gold: [],
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, json: async () => ({ series, updated_at: 1 }),
    } as Response)));
    const { container } = render(<EquityCurve equityIndex={["1", "1.1", "1.2"]} timestamps={ts} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "BTC" }));
    await waitFor(() => expect(container.querySelectorAll("svg polyline").length).toBe(2));
    const [main, ov] = Array.from(container.querySelectorAll("svg polyline"))
      .map((e) => (e.getAttribute("points") ?? "").split(" ").map((p) => Number(p.split(",")[0])));
    expect(ov).toEqual(main);
    expect(main).toEqual([0, 300, 900]);
  });

  it("ts_last == ts_first／單點不除以零（無 NaN、無例外）", () => {
    expect(xFractionsFromTs([5 * DAY, 5 * DAY])).toEqual([0, 1]);
    expect(xFractionsFromTs([5 * DAY])).toEqual([0]);
    expect(xFractionsFromTs([])).toEqual([]);
    const { container } = render(
      <EquityCurve equityIndex={["1", "1.1", "1.2"]} timestamps={[7 * DAY, 7 * DAY, 7 * DAY]} />);
    const pts = container.querySelector("svg polyline")?.getAttribute("points") ?? "";
    expect(pts).not.toContain("NaN");
    expect(pts.split(" ").length).toBe(3);
  });

  it("無 timestamps 的舊路徑維持等距（x＝0、450、900）", () => {
    const { container } = render(<EquityCurve equityIndex={["1", "1.1", "1.2"]} />);
    const xs = (container.querySelector("svg polyline")?.getAttribute("points") ?? "")
      .split(" ").map((p) => Number(p.split(",")[0]));
    expect(xs).toEqual([0, 450, 900]);
  });
});

describe("B8：X 軸刻度按時間等距（Task F2）", () => {
  const labelsOf = (container: HTMLElement) =>
    Array.from(container.querySelectorAll(".equity-curve-xaxis span")).map((x) => x.textContent);

  it("B8：時間戳 01-01、01-11、01-31（UTC 00:00），刻度數 3 → 標籤 01-01、01-16、01-31", () => {
    const ts = ["2026-01-01", "2026-01-11", "2026-01-31"].map(ms);
    expect(xLabelsFromTs(ts).map((l) => l.label)).toEqual(["01-01", "01-16", "01-31"]);
    // 元件層：中間標籤是時間中點 01-16，不是最近資料點 01-11
    const { container } = render(<EquityCurve equityIndex={["1", "1.1", "1.2"]} timestamps={ts} />);
    expect(labelsOf(container)).toEqual(["01-01", "01-16", "01-31"]);
  });

  it("跨 > 300 天時刻度標籤為 YYYY-MM，且仍按時間等距（中間標籤是時間中點）", () => {
    // 2025-01-01 → 2026-03-17（441 天）；3 點，中點約 2025-08-09
    const ts = ["2025-01-01", "2025-01-02", "2026-03-17"].map(ms);
    expect(xLabelsFromTs(ts).map((l) => l.label)).toEqual(["2025-01", "2025-08", "2026-03"]);
  });

  it("k == 1（單點）或 ts_last == ts_first 只顯示首點日期，不除以零", () => {
    expect(xLabelsFromTs([ms("2026-01-05")]).map((l) => l.label)).toEqual(["01-05"]);
    expect(xLabelsFromTs([ms("2026-01-05"), ms("2026-01-05"), ms("2026-01-05")])
      .map((l) => l.label)).toEqual(["01-05"]);
    expect(xLabelsFromTs([])).toEqual([]);
    const { container } = render(
      <EquityCurve equityIndex={["1", "1.1", "1.2"]}
        timestamps={[ms("2026-01-05"), ms("2026-01-05"), ms("2026-01-05")]} />);
    expect(labelsOf(container)).toEqual(["01-05"]);
  });

  it("點數 > 7 時刻度數仍為 7，且首末為首末點日期", () => {
    const ts = Array.from({ length: 20 }, (_, i) => ms("2026-01-01") + i * 3 * DAY);
    const l = xLabelsFromTs(ts);
    expect(l.length).toBe(7);
    expect(l[0].label).toBe("01-01");
    expect(l[6].label).toBe("02-27");   // 01-01 + 57 天
  });
});
