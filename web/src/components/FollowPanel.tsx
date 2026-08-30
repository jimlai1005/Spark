"use client";
/**
 * `FollowPanel` — 跟單設定右欄面板（M3 round4 Task R4-11 項目 1，從
 * `strategies/[slug]/page.tsx` 抽出共用元件，供 `/strategies/[slug]` 與
 * `/traders/[address]` 兩頁共用）。
 *
 * 兩頁的差異全部收在 props，不寫死在元件內：
 * - `heading`：策略頁「跟隨此策略」／交易員頁「跟隨這個地址」（各自的既有
 *   copy key，不合併成一個新 key——沿既有語言測試慣例）。
 * - `advancedNote`：只有交易員頁（進階模式 CTA）會傳，面板頂部多一行無背書
 *   說明（沿用 `COPY.advanced.gate.body` 同一句無背書語義，見呼叫端）。
 * - `leverageDisplay`：策略頁有平台審核過的 `max_leverage`；交易員頁沒有
 *   平台層帽，呼叫端傳 `NO_VALUE`（「—」），本元件不判斷「有沒有帽」，只
 *   負責顯示呼叫端給的字串——不臆造數字（工程原則 1）。
 * - `disabledState`：策略頁 `listable=false`（暫不開放新跟單）渲染一顆
 *   disabled 按鈕＋一行說明；交易員頁 `follow_blocked=true`（已被安全撤銷）
 *   完全不渲染按鈕，只顯示一行提示——兩者的 DOM 形狀本來就不同，用
 *   `kind` 判別式表達，不硬套同一個形狀。
 *
 * 投入比例／回撤 slider 的文案（`copy`）兩頁目前共用同一份
 * `COPY.strategyDetail.panel`（`cta`/`ctaConnecting`/`ctaSigning`/`footnote`
 * 兩頁原文完全相同，`scaleLabel`/`leverageLabel`/`ddLabel`/估算區塊只有策略頁
 * 定義過——交易員頁沿用同一份，不另開一組重複 key）。
 */
import { NO_VALUE } from "@/lib/format";

export const SCALE_MIN = 5;
export const SCALE_MAX = 100;
export const SCALE_DEFAULT = 25;
export const DD_MIN = 5;
export const DD_MAX = 50;
export const DD_DEFAULT = 20;

export type ConnectPhase = "idle" | "connecting" | "signing";

/** 面板文案形狀——兩頁共用 `COPY.strategyDetail.panel`（見檔頭）。 */
export interface FollowPanelCopy {
  scaleLabel: string;
  leverageLabel: string;
  ddLabel: string;
  ddEnableLabel: string;
  ddDisabledNote: string;
  estDepositLabel: string;
  estDepositValue: string;
  builderFeeLabel: string;
  builderFeeValue: string;
  estMonthlyLabel: string;
  estMonthlyValue: string;
  cta: string;
  ctaConnecting: string;
  ctaSigning: string;
  footnote: string;
}

/**
 * 取代 CTA 的 disabled 態——`kind` 判別式，兩頁形狀刻意不同：
 * - `pending`（策略頁 `listable=false`）：disabled 按鈕（`data-testid=
 *   "follow-panel-disabled"`，沿既有測試）＋一行說明。
 * - `blocked`（交易員頁 `follow_blocked=true`）：不渲染任何按鈕，只顯示一行
 *   提示（沿既有 `/traders/[address]` 行為，[W4]）。
 */
export type FollowPanelDisabledState =
  | { kind: "pending"; cta: string; note: string }
  | { kind: "blocked"; note: string };

export interface FollowPanelProps {
  heading: string;
  copy: FollowPanelCopy;
  leverageDisplay: string;
  leverageInfoPrefix: string;
  leverageInfoSuffix: string;
  scalePct: number;
  onScalePctChange: (v: number) => void;
  ddEnabled: boolean;
  onDdEnabledChange: (v: boolean) => void;
  ddPct: number;
  onDdPctChange: (v: number) => void;
  phase: ConnectPhase;
  error: string | null;
  onCta: () => void;
  disabledState?: FollowPanelDisabledState;
  /** 進階模式（交易員頁）專屬：面板頂部一行無背書說明；策略頁不傳。 */
  advancedNote?: string;
}

export function FollowPanel({
  heading, copy, leverageDisplay, leverageInfoPrefix, leverageInfoSuffix,
  scalePct, onScalePctChange, ddEnabled, onDdEnabledChange, ddPct, onDdPctChange,
  phase, error, onCta, disabledState, advancedNote,
}: FollowPanelProps) {
  return (
    <div className="card strategy-follow-panel">
      <div className="strategy-follow-panel-heading">{heading}</div>

      {advancedNote && <p className="hint follow-panel-advanced-note">{advancedNote}</p>}

      <div className="strategy-follow-sliders">
        <div className="risk-field">
          <div className="risk-slider-row">
            <label htmlFor="scale-slider">{copy.scaleLabel}</label>
            <span className="mono risk-value">{scalePct}%</span>
          </div>
          <input
            id="scale-slider"
            type="range"
            className="risk-slider"
            min={SCALE_MIN}
            max={SCALE_MAX}
            step={1}
            value={scalePct}
            onChange={(e) => onScalePctChange(Number(e.target.value))}
          />
        </div>

        {/*
          ⭐ Task 10b（主線程裁決 2026-08-28）：槓桿上限改唯讀資訊列——沒有
          per-user 可簽的槓桿上限通道；交易員頁（R4-11）沒有平台審核過的
          上限，呼叫端傳 `NO_VALUE`（見檔頭），本元件不臆造數字。
        */}
        <div className="risk-field">
          <div className="risk-slider-row">
            <span>{copy.leverageLabel}</span>
            <span className="mono risk-value">{leverageDisplay}</span>
          </div>
          <p className="hint">
            {leverageInfoPrefix}
            {leverageDisplay}
            {leverageInfoSuffix}
          </p>
        </div>

        <div className="risk-field">
          <label className="risk-toggle">
            <input
              type="checkbox"
              checked={ddEnabled}
              onChange={(e) => onDdEnabledChange(e.target.checked)}
            />
            <span>{copy.ddEnableLabel}</span>
          </label>
          <div className="risk-slider-row">
            <span>{copy.ddLabel}</span>
            <span className="mono risk-value">{ddEnabled ? `-${ddPct}%` : NO_VALUE}</span>
          </div>
          <input
            type="range"
            className="risk-slider"
            min={DD_MIN}
            max={DD_MAX}
            step={1}
            value={ddPct}
            disabled={!ddEnabled}
            onChange={(e) => onDdPctChange(Number(e.target.value))}
            aria-label={copy.ddLabel}
          />
          <p className="hint risk-toggle-help">{copy.ddDisabledNote}</p>
        </div>
      </div>

      <div className="inset strategy-follow-estimate">
        <div className="strategy-follow-estimate-row">
          <span>{copy.estDepositLabel}</span>
          <span className="mono">{copy.estDepositValue}</span>
        </div>
        <div className="strategy-follow-estimate-row">
          <span>{copy.builderFeeLabel}</span>
          <span className="mono">{copy.builderFeeValue}</span>
        </div>
        <div className="strategy-follow-estimate-row">
          <span>{copy.estMonthlyLabel}</span>
          <span className="mono">{copy.estMonthlyValue}</span>
        </div>
      </div>

      {disabledState?.kind === "blocked" ? (
        <p className="hint">{disabledState.note}</p>
      ) : disabledState?.kind === "pending" ? (
        <>
          <button type="button" className="btn btn-block" disabled data-testid="follow-panel-disabled">
            {disabledState.cta}
          </button>
          <p className="hint strategy-follow-footnote">{disabledState.note}</p>
        </>
      ) : (
        <>
          <button
            type="button"
            className="btn btn-primary btn-block"
            disabled={phase !== "idle"}
            onClick={onCta}
          >
            {phase === "connecting" ? copy.ctaConnecting : phase === "signing" ? copy.ctaSigning : copy.cta}
          </button>
          {error && (
            <div className="sign-error">
              <p>{error}</p>
            </div>
          )}
          <p className="hint strategy-follow-footnote">{copy.footnote}</p>
        </>
      )}
    </div>
  );
}
