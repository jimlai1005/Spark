/**
 * SyncCard 空值三態（M3 round3 Task 6，R2·C）——`data_state` 決定整卡呈現：
 * "warming"／"error" 摺為一行，不留一整塊「—」空白卡片；"ok" 維持逐欄渲染，
 * 個別欄位 null 時「—」但絕不顯示 0ms（R2·C 態二核心要求：0 是假數據）。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DashboardSync } from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { SyncCard } from "./SyncCard";

const c = COPY.dashboard.sync;

const BASE: DashboardSync = {
  latency_median_ms: 512, latency_p95_ms: 900, price_diff_bp: "2.3",
  unsynced_positions: 0, scale_deviation_pct: "0.8", missed_signals_24h: 1,
  missed_reason: "insufficient_margin", last_recon_ts: 1724805060,
  data_state: "ok", since_ts: null,
};

describe("SyncCard — data_state=\"ok\"", () => {
  it("逐欄渲染真實數字", () => {
    render(<SyncCard sync={BASE} updatedAt={1724805063} onRetry={vi.fn()} />);
    expect(screen.getByText("512ms")).toBeInTheDocument();
    expect(screen.queryByText(c.warmingLine)).not.toBeInTheDocument();
    expect(screen.queryByText(c.errorLine, { exact: false })).not.toBeInTheDocument();
  });

  it("latency_median_ms 為 null → 顯示「—」，不出現「0ms」字樣（0 是假數據）", () => {
    render(
      <SyncCard
        sync={{ ...BASE, latency_median_ms: null, latency_p95_ms: null }}
        updatedAt={1724805063}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByText(/^0ms$/)).not.toBeInTheDocument();
    expect(screen.queryByText("0ms", { exact: false })).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("SyncCard — data_state=\"warming\"（R2·C 態二）", () => {
  it("整卡摺為一行固定文案，不渲染逐欄指標", () => {
    render(
      <SyncCard sync={{ ...BASE, data_state: "warming" }} updatedAt={1724805063} onRetry={vi.fn()} />,
    );
    expect(screen.getByText(c.warmingLine)).toBeInTheDocument();
    expect(screen.queryByText("512ms")).not.toBeInTheDocument();
    expect(screen.queryByText(c.latencyMedian)).not.toBeInTheDocument();
  });
});

describe("SyncCard — data_state=\"error\"（R2·C 態三）", () => {
  it("摺為一行＋時間戳＋重試鍵，點擊重試呼叫 onRetry", () => {
    const onRetry = vi.fn();
    render(
      <SyncCard sync={{ ...BASE, data_state: "error" }} updatedAt={1724805063} onRetry={onRetry} />,
    );
    expect(screen.getByText(c.errorLine, { exact: false })).toBeInTheDocument();
    // 1724805063 → 2024-08-28T00:11:03Z（時間戳格式 HH:mm UTC，見 SyncCard::formatTs）
    expect(screen.getByText(/\d{2}:\d{2} UTC/)).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", { name: COPY.common.retry });
    retryBtn.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("sync 整塊為 null（_safe_block 吞例外）→ 視同 error 態，不誤顯示成尚無資料", () => {
    render(<SyncCard sync={null} updatedAt={1724805063} onRetry={vi.fn()} />);
    expect(screen.getByText(c.errorLine, { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(c.warmingLine)).not.toBeInTheDocument();
  });
});
