"use client";
import Link from "next/link";
import { useState } from "react";
import type { DashboardGuard, DashboardPosition, DashboardStatus } from "@/lib/api";
import { postPause } from "@/lib/api";
import { NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import { CloseAllModal } from "./CloseAllModal";

/**
 * Kill switch 兩顆按鈕（暫停跟單／平倉並撤銷授權）——Task 15：真的接上
 * `POST /api/me/pause`（暫停/恢復，無需簽章）與簽章的「平倉並撤銷」流程
 * （`CloseAllModal`，`lib/closeAllFlow.ts`）。`state==="halted"` 時兩顆按鈕
 * 讓位給「至 Hyperliquid 官方介面移除 API wallet」指引卡（v1 不代發撤銷交易，
 * 見 plan 0.2）。
 */
export const KILL_SWITCH_ENABLED = true;

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

export function StatusCard({
  status, me, positions, closeAllPending, closeAllFailed, riskControlsEnabled,
  onActionSettled, onCloseAllSubmitted,
}: {
  status: DashboardStatus | null;
  /** 登入身分——kill switch 動作皆需要（暫停/恢復不簽章，平倉並撤銷需要它比對
   * 簽章者）。`null`＝呼叫端尚未確認登入態，兩顆按鈕不渲染。 */
  me: { address: string; account_id: string } | null;
  /** 將列進「平倉並撤銷」確認 modal 的目前持倉（來自同一份 dashboard 回應）。 */
  positions: DashboardPosition[] | null;
  /** 平倉並撤銷已送出、正在等待引擎收尾完成（呼叫端輪詢 dashboard 直到 halted）。 */
  closeAllPending: boolean;
  /** 輪詢逾時或後端判定請求過期（opus 審查 Critical 2c）——停止暗示「即將完成」，
   * 改顯示明確失敗卡＋官方介面自助指引。 */
  closeAllFailed: boolean;
  /** ⭐ M3 round3 Task 6（R2·C 態一）：dashboard 回應頂層的 `risk_controls_enabled`
   * ——恆為明確布林（見 api.ts::DashboardResp 檔頭），取代舊有的
   * `guards.drawdown.enabled === true` 判斷（後者在心跳過期時可能是 `null`，
   * 不如這個欄位確定）。`false` 時回撤護欄列渲染「未啟用 · 前往設定 →」引導，
   * 不是灰色「—」。 */
  riskControlsEnabled: boolean;
  /** 暫停/恢復送出成功後呼叫——讓呼叫端立即重新整理 dashboard 資料，不必等下一次
   * 自然輪詢週期。 */
  onActionSettled: () => void;
  /** 平倉並撤銷簽章送出成功後呼叫——與 `onActionSettled` 分開，因為呼叫端要對它
   * 額外開始輪詢（`closeAllPending`），暫停/恢復不需要這個副作用。 */
  onCloseAllSubmitted: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.dashboard.status;
  const [pauseBusy, setPauseBusy] = useState(false);
  const [pauseError, setPauseError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

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

  async function togglePause() {
    setPauseError(null);
    setPauseBusy(true);
    try {
      await postPause(state === "paused" ? "resume" : "pause");
      onActionSettled();
    } catch {
      setPauseError(c.pauseErrorNote);
    } finally {
      setPauseBusy(false);
    }
  }

  // 只在「有引擎在追蹤這個帳號」時才提供動作（following/paused）——inactive
  // 沒有東西可暫停/撤銷，halted 讓位給下面的完成指引卡。
  const showActions = me != null && (state === "following" || state === "paused");

  return (
    <div className="card dash-card dash-card-status" style={{ borderColor: "rgba(70, 214, 179, 0.24)" }}>
      <div className="dash-status-head">
        <div>
          <div className="dash-card-label">{c.label}</div>
          <div className="dash-status-title">
            <span className="dash-status-dot" data-state={state} aria-hidden="true" />
            {/* ⭐ R2 P1：策略名 nowrap＋badge 獨立元素，不與標題同一文流——舊版把
               名稱與「跟單中」用 " · " 接成同一個文字節點，390px 寬時會斷在
               文字中間（讀起來像 bug）。現在兩者是獨立的 flex item，badge 整體
               換行也不會被切成兩半。 */}
            <span className="dash-status-name">{status?.strategy_name ?? c.strategyFallback}</span>
            <span className="pill dash-status-badge" data-state={state}>{stateLabel}</span>
          </div>
          <div className="dash-status-sub">{subtitleParts.join(" · ")}</div>
        </div>
        {showActions && (
          <div className="dash-status-actions">
            <button type="button" className="dash-btn-pause" onClick={() => void togglePause()}
              disabled={pauseBusy}>
              {state === "paused" ? c.resumeBtn : c.pauseBtn}
            </button>
            <button type="button" className="dash-btn-close" onClick={() => setModalOpen(true)}
              disabled={pauseBusy}>
              {c.closeAllBtn}
            </button>
          </div>
        )}
      </div>
      {pauseError && <p className="dash-status-error">{pauseError}</p>}
      {closeAllFailed && state !== "halted" && (
        <div className="dash-guide-card">
          <h4>{c.closeAllFailed.title}</h4>
          <p className="hint">{c.closeAllFailed.note}</p>
          <a href="https://app.hyperliquid.xyz/API" target="_blank" rel="noreferrer">
            {c.closeAllFailed.linkLabel}
          </a>
        </div>
      )}
      {closeAllPending && !closeAllFailed && state !== "halted" && (
        <div className="dash-progress-card">
          <strong>{c.closeAllProgress.title}</strong>
          <p style={{ margin: "4px 0 0" }}>{c.closeAllProgress.note}</p>
        </div>
      )}
      {state === "halted" && (
        <div className="dash-guide-card">
          <h4>{c.closeAllDone.title}</h4>
          <p className="hint">{c.closeAllDone.note}</p>
          <ol>
            {c.closeAllDone.steps.map((step) => <li key={step}>{step}</li>)}
          </ol>
          <a href="https://app.hyperliquid.xyz/API" target="_blank" rel="noreferrer">
            {c.closeAllDone.linkLabel}
          </a>
        </div>
      )}
      <div className="dash-divider" />
      <div className="dash-guards-heading">{c.guardsHeading}</div>
      <div className="dash-guards">
        <GuardRow label={c.guardScale} guard={guards.scale} />
        <GuardRow label={c.guardLeverage} guard={guards.leverage} />
        {riskControlsEnabled ? (
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
      {modalOpen && me != null && (
        <CloseAllModal
          me={me}
          positions={positions}
          onClose={() => setModalOpen(false)}
          onSubmitted={() => {
            setModalOpen(false);
            onCloseAllSubmitted();
          }}
        />
      )}
    </div>
  );
}
