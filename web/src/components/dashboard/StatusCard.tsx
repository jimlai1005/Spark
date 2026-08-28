"use client";
import Link from "next/link";
import type { DashboardGuard, DashboardStatus } from "@/lib/api";
import { NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

/**
 * Kill switch 兩顆按鈕（暫停跟單／平倉並撤銷授權）的 UI 與 handler 接口本 task
 * 就緒，但實際生效（寫 pause 旗標／簽章 close-all）在 Task 15。旗標關閉時本區塊
 * 完全不渲染——不是 disabled 灰階，是不存在（Task 14 規格：以 feature flag 隱藏）。
 */
export const KILL_SWITCH_ENABLED = false;

const WARN_RATIO = 0.8;

/** `now/max`（drawdown 兩者皆負值時符號相消，仍是正確的「距上限比例」）。
 * 任一值缺席或分母為 0 → null（無法判斷，不上色、bar 畫 0%）。 */
export function guardRatio(now: string | null, max: string | null): number | null {
  if (now == null || max == null) return null;
  const n = Number(now);
  const m = Number(max);
  if (!Number.isFinite(n) || !Number.isFinite(m) || m === 0) return null;
  return n / m;
}

function GuardRow({ label, guard }: { label: string; guard: DashboardGuard }) {
  const ratio = guardRatio(guard.now, guard.max);
  const pct = ratio == null ? 0 : Math.max(0, Math.min(1, ratio)) * 100;
  const warn = ratio != null && ratio >= WARN_RATIO;
  return (
    <div>
      <div className="dash-guard-row-head">
        <span style={{ color: "var(--text-dim)" }}>{label}</span>
        <span className="mono">
          {guard.now ?? NO_VALUE} <span style={{ color: "var(--text-dim)" }}>/ {guard.max ?? NO_VALUE}</span>
        </span>
      </div>
      <div className="dash-guard-bar">
        <div
          className="dash-guard-bar-fill"
          style={{ width: `${pct}%`, background: warn ? "var(--warn)" : "var(--primary)" }}
        />
      </div>
    </div>
  );
}

export function StatusCard({ status }: { status: DashboardStatus | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.status;

  const state = status?.state ?? "inactive";
  const stateLabel = {
    following: c.stateFollowing,
    paused: c.statePaused,
    halted: c.stateHalted,
    inactive: c.stateInactive,
  }[state];

  const subtitleParts: string[] = [];
  if (status?.following_days != null) {
    subtitleParts.push(`${c.followingDaysPrefix}${status.following_days}${c.followingDaysSuffix}`);
  }
  subtitleParts.push(status?.signal_source_ok === true ? c.signalOk : c.signalUnknown);

  const guards = status?.guards ?? {
    scale: { now: null, max: null },
    leverage: { now: null, max: null },
    drawdown: { now: null, max: null, enabled: null },
  };

  return (
    <div className="card dash-card" style={{ borderColor: "rgba(70, 214, 179, 0.24)" }}>
      <div className="dash-status-head">
        <div>
          <div className="dash-card-label">{c.label}</div>
          <div className="dash-status-title">
            <span className="dash-status-dot" data-state={state} aria-hidden="true" />
            <span>{status?.strategy_name ?? c.strategyFallback} · {stateLabel}</span>
          </div>
          <div className="dash-status-sub">{subtitleParts.join(" · ")}</div>
        </div>
        {KILL_SWITCH_ENABLED && (
          <div className="dash-status-actions">
            <button type="button" className="dash-btn-pause" onClick={() => {}}>
              {c.pauseBtn}
            </button>
            <button type="button" className="dash-btn-close" onClick={() => {}}>
              {c.closeAllBtn}
            </button>
          </div>
        )}
      </div>
      <div className="dash-divider" />
      <div className="dash-guards-heading">{c.guardsHeading}</div>
      <div className="dash-guards">
        <GuardRow label={c.guardScale} guard={guards.scale} />
        <GuardRow label={c.guardLeverage} guard={guards.leverage} />
        {guards.drawdown.enabled === true ? (
          <GuardRow label={c.guardDrawdown} guard={guards.drawdown} />
        ) : (
          <div>
            <div className="dash-guard-row-head">
              <span style={{ color: "var(--text-dim)" }}>{c.guardDrawdown}</span>
            </div>
            <div className="dash-guard-disabled">
              <Link href="/settings">{c.drawdownDisabled}</Link>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
