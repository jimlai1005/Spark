/**
 * StatusCard — badge 獨立 DOM（R2 P1：策略名斷行像 bug）＋風險護欄未啟用引導連結
 * （M3 round3 Task 6，R2·C 態一：`risk_controls_enabled===false` 顯示可點連結，
 * 不是灰色「—」）。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DashboardStatus } from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { StatusCard } from "./StatusCard";

const c = COPY.dashboard.status;

function statusWith(overrides: Partial<DashboardStatus>): DashboardStatus {
  return {
    strategy_name: "Filet Alpha", state: "following", following_days: 12,
    signal_source_ok: true,
    guards: {
      scale: { now: "0.2", max: "0.25" },
      leverage: { now: "1.0", max: "3.0" },
      drawdown: { now: null, max: "-0.10", enabled: true },
    },
    ...overrides,
  };
}

function renderCard(status: DashboardStatus | null, riskControlsEnabled: boolean) {
  return render(
    <StatusCard
      status={status}
      me={null}
      positions={null}
      closeAllPending={false}
      closeAllFailed={false}
      riskControlsEnabled={riskControlsEnabled}
      onActionSettled={vi.fn()}
      onCloseAllSubmitted={vi.fn()}
    />,
  );
}

describe("StatusCard — 策略名／badge 獨立 DOM（R2 P1）", () => {
  it("「Filet Alpha」與「跟單中」是兩個獨立元素，不是同一個文字節點", () => {
    renderCard(statusWith({}), true);
    const name = screen.getByText("Filet Alpha");
    const badge = screen.getByText(c.stateFollowing);
    expect(name).not.toBe(badge);
    // 名稱節點自己的文字不含 "· 跟單中"（舊版把兩者用 " · " 接成同一個 span）。
    expect(name.textContent).toBe("Filet Alpha");
    expect(name.textContent).not.toContain(c.stateFollowing);
    expect(badge.className).toContain("dash-status-badge");
  });
});

describe("StatusCard — 風險護欄未啟用引導連結（R2·C 態一）", () => {
  it("riskControlsEnabled=false → 顯示可點連結「未啟用 · 前往設定 →」連 /settings，不是灰色「—」", () => {
    renderCard(statusWith({}), false);
    const link = screen.getByRole("link", { name: c.drawdownDisabled });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("riskControlsEnabled=true → 不顯示未啟用引導連結，改渲染回撤護欄列", () => {
    renderCard(statusWith({}), true);
    expect(screen.queryByRole("link", { name: c.drawdownDisabled })).not.toBeInTheDocument();
    expect(screen.getByText(c.guardDrawdown)).toBeInTheDocument();
  });

  it("即使 guards.drawdown.enabled 為 true，riskControlsEnabled=false 仍顯示未啟用引導（新欄位為權威來源）", () => {
    renderCard(statusWith({
      guards: {
        scale: { now: "0.2", max: "0.25" },
        leverage: { now: "1.0", max: "3.0" },
        drawdown: { now: null, max: "-0.10", enabled: true },
      },
    }), false);
    expect(screen.getByRole("link", { name: c.drawdownDisabled })).toBeInTheDocument();
  });
});
