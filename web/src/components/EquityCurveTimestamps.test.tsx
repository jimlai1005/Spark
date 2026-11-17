/**
 * `EquityCurve` 時間戳切片測試（plan 2026-11-17-perf-followups §1 A2，E1／E6）。
 *
 * 錨例 B2：10 個點、每 10 天一點、末點為 T。
 * - `30D` 切片＝末 4 點（T−30d 含端點）；
 * - `7D` 切片＝1 點 → `7D` 按鈕 disabled；
 * - X 軸首末標籤＝切片首末點的 UTC 日期；
 * - 不給 `timestamps` 時 `30D` 維持舊行為（取最後 30 點），不拋例外。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { COPY_EN, COPY_ZH } from "@/lib/copy";
import { EquityCurve } from "./EquityCurve";

const DAY = 86_400_000;
// T = 2026-06-30T12:00:00Z（刻意取非午夜，驗證「UTC 日期」而非整點）
const T = Date.UTC(2026, 5, 30, 12, 0, 0);
const TS = Array.from({ length: 10 }, (_, i) => T - (9 - i) * 10 * DAY);
const SERIES = Array.from({ length: 10 }, (_, i) => String(1 + i * 0.1));

function utcMmDd(ms: number): string {
  return new Date(ms).toISOString().slice(5, 10);
}

function polylinePointCount(): number {
  const svg = screen.getByRole("img", { name: COPY_ZH.strategyDetail.equity.heading });
  return (svg.querySelector("polyline")?.getAttribute("points") ?? "").split(" ").length;
}

function xLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".equity-curve-xaxis span")).map(
    (s) => s.textContent ?? "",
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("EquityCurve — 時間戳切片（錨例 B2）", () => {
  it("B2：30D 切片＝末 4 點（T−30d 含端點）；7D 切片僅 1 點 → 按鈕 disabled", () => {
    render(<EquityCurve equityIndex={SERIES} timestamps={TS} />);
    // 預設全部：10 點
    expect(polylinePointCount()).toBe(10);

    const btn7 = screen.getByRole("button", { name: /7D/ });
    expect(btn7).toBeDisabled();
    expect(btn7.getAttribute("title")).toBe(COPY_ZH.strategyDetail.equity.periodUnavailable);
    expect(btn7.getAttribute("aria-label")).toContain(COPY_ZH.strategyDetail.equity.periodUnavailable);

    const btn30 = screen.getByRole("button", { name: /30D/ });
    expect(btn30).not.toBeDisabled();
    fireEvent.click(btn30);
    expect(polylinePointCount()).toBe(4);
  });

  it("B2：X 軸首末標籤等於切片首末點的 UTC 日期（全部與 30D 兩個期間）", () => {
    const { container } = render(<EquityCurve equityIndex={SERIES} timestamps={TS} />);
    let labels = xLabels(container);
    expect(labels[0]).toBe(utcMmDd(TS[0]));
    expect(labels[labels.length - 1]).toBe(utcMmDd(TS[9]));

    fireEvent.click(screen.getByRole("button", { name: /30D/ }));
    labels = xLabels(container);
    expect(labels).toEqual([TS[6], TS[7], TS[8], TS[9]].map(utcMmDd));
    expect(labels[0]).toBe(utcMmDd(T - 30 * DAY));
  });

  it("點擊被停用的 7D 不改變曲線（點數仍為全部 10 點）", () => {
    render(<EquityCurve equityIndex={SERIES} timestamps={TS} />);
    fireEvent.click(screen.getByRole("button", { name: /7D/ }));
    expect(polylinePointCount()).toBe(10);
  });

  it("選中 30D 後若資料更新使 30D 不足 2 點 → 退回全部（不畫單點曲線）", () => {
    const { rerender } = render(<EquityCurve equityIndex={SERIES} timestamps={TS} />);
    fireEvent.click(screen.getByRole("button", { name: /30D/ }));
    expect(polylinePointCount()).toBe(4);
    // 新資料：每點相隔 40 天 → 30D 內只剩 1 點
    const sparse = Array.from({ length: 10 }, (_, i) => T - (9 - i) * 40 * DAY);
    rerender(<EquityCurve equityIndex={SERIES} timestamps={sparse} />);
    expect(screen.getByRole("button", { name: /30D/ })).toBeDisabled();
    expect(polylinePointCount()).toBe(10);
    expect(screen.getByRole("button", { name: COPY_ZH.strategyDetail.equity.periodAll }))
      .toHaveAttribute("aria-pressed", "true");
  });

  it("對照線以各點真實日期對齊收盤價（rebase 錨點＝切片首點當日收盤）", async () => {
    // 只在 T−30d、T−10d、T 三天有收盤價；7D 不可用，切到 30D 後 4 點中第 2 點（T−20d）缺格。
    const day = (ms: number) => Math.floor(ms / DAY) * DAY;
    const series = {
      btc: [
        [day(T - 30 * DAY), "100"], [day(T - 10 * DAY), "150"], [day(T), "200"],
      ],
      eth: [], sp500: [], gold: [],
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: true, json: async () => ({ series, updated_at: 1 }),
    } as Response)));
    const { container } = render(<EquityCurve equityIndex={SERIES} timestamps={TS} />);
    fireEvent.click(screen.getByRole("button", { name: /30D/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "BTC" }));
    await vi.waitFor(() => {
      expect(container.querySelectorAll("svg polyline").length).toBe(2);
    });
    const ov = container.querySelectorAll("svg polyline")[1].getAttribute("points") ?? "";
    // 4 個切片位置中 3 個有收盤價（缺格跳過、不插值）
    expect(ov.split(" ").length).toBe(3);
  });
});

describe("EquityCurve — 時間戳缺席／不符 → 舊行為且不拋例外（E1 相容）", () => {
  it("未給 timestamps：30D 維持舊行為（取最後 30 點，這裡共 10 點故全取），按鈕不 disabled", () => {
    render(<EquityCurve equityIndex={SERIES} />);
    const btn7 = screen.getByRole("button", { name: "7D" });
    expect(btn7).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "30D" }));
    expect(polylinePointCount()).toBe(10);
    fireEvent.click(btn7);
    expect(polylinePointCount()).toBe(7);
  });

  it("timestamps = null：同未給", () => {
    render(<EquityCurve equityIndex={SERIES} timestamps={null} />);
    expect(screen.getByRole("button", { name: "7D" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "7D" }));
    expect(polylinePointCount()).toBe(7);
  });

  it("timestamps 長度不符：維持舊行為、不拋例外、x 軸仍用 endDate 往回推", () => {
    const { container } = render(
      <EquityCurve equityIndex={SERIES} timestamps={TS.slice(0, 5)} endDate="2026-06-30" />,
    );
    expect(screen.getByRole("button", { name: "7D" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "7D" }));
    expect(polylinePointCount()).toBe(7);
    expect(xLabels(container).length).toBeGreaterThan(0);
  });

  it("timestamps 含非有限值：視為不可用，維持舊行為、不拋例外", () => {
    const bad = TS.map((t, i) => (i === 3 ? Number.NaN : t));
    render(<EquityCurve equityIndex={SERIES} timestamps={bad} />);
    expect(screen.getByRole("button", { name: "7D" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "30D" }));
    expect(polylinePointCount()).toBe(10);
  });
});

describe("文案 periodUnavailable（ZH／EN 對稱）", () => {
  it("ZH／EN 皆有值且不同", () => {
    expect(COPY_ZH.strategyDetail.equity.periodUnavailable).toBe("此期間取樣點不足");
    expect(COPY_EN.strategyDetail.equity.periodUnavailable).toBe("Not enough samples in this period");
  });

  it("winRateNoteSuffix／sampleSuffix 改為區間報酬樣本（ZH／EN）", () => {
    expect(COPY_ZH.strategyDetail.metrics.winRateNoteSuffix).toBe(" 個區間報酬樣本");
    expect(COPY_ZH.strategyDetail.methodology.sampleSuffix).toBe(" 個區間報酬樣本）。");
    expect(COPY_EN.strategyDetail.metrics.winRateNoteSuffix).toBe(" period return samples");
    expect(COPY_EN.strategyDetail.methodology.sampleSuffix).toBe(" period return samples).");
    // 「涵蓋 N 個取樣點」那句不變
    expect(COPY_ZH.strategyDetail.methodology.daysSuffix).toBe(" 個取樣點（");
  });
});
