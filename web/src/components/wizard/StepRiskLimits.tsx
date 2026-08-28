"use client";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSignMessage } from "wagmi";
import {
  getMyRisk, getRiskSettingsMessage, postMyRisk,
  type MyRiskResp, type RiskPrefs,
} from "@/lib/api";
import { useCopy } from "@/lib/lang";
import { NO_VALUE } from "@/lib/format";
import { recoverPersonalSigner } from "@/lib/sign";
import { runRiskSettingsFlow, type RiskFlowFailure } from "@/lib/riskSettingsFlow";

const SCALE_MIN = 5;
const SCALE_MAX = 100;
const LEV_MIN = 1;
// 後端 spec 讀到之前的保守 fallback（僅用於畫面刻度，門檻仍以 spec 為準才會真正送出）。
const DD_MIN_FALLBACK = 5;
const DD_MAX_FALLBACK = 50;

/**
 * 比例 ↔ 百分比顯示換算——**與 `web/src/app/leaders/page.tsx` 的
 * `ratioToPct`／`pctToRatio` 同一個公式**（0.1% 刻度，對齊後端
 * `risk_prefs._RATIO_STEP`）。本檔刻意各自持有一份而非匯入該頁的私有函式：
 * leaders/page.tsx 不在 Task 10 的檔案範圍內，不應為了共用兩個純函式去動它；
 * 兩份公式若日後分岔，各自的測試會分別抓到。
 */
function ratioToPct(v: string): string {
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n * 1000) / 10) : "";
}
function pctToRatio(pct: string): string {
  const n = Number(pct);
  if (!Number.isFinite(n)) return pct;
  return String(Number((Math.round(n * 10) / 1000).toFixed(3)));
}

function riskErrorCopy(r: RiskFlowFailure, c: ReturnType<typeof useCopy>["wizard"]): string {
  const e = c.errors;
  switch (r.kind) {
    case "wallet-rejected": return e.walletRejected;
    case "signer-mismatch": return e.signerMismatch;
    case "content-mismatch": return e.contentMismatch;
    case "message-failed": return e.payloadFailed;
    case "submit-failed": return e.submitFailed;
  }
}

export interface StepRiskLimitsValues {
  scale: number;
  lev: number;
  ddEnabled: boolean;
  ddPct: number;
}

/**
 * StepRiskLimits — onboarding step 3（設計稿 §05：「設定你的風險限制」）。
 *
 * 投入比例／槓桿上限：**純本地 UI 狀態**，不接任何簽章端點——與策略詳情頁
 * （Task 9）右欄的同名 slider 同一個定位：純預覽／預填數字，跟單規模由引擎依
 * 既有邏輯核算，這裡不新增一個「客戶簽章授權曝險倍數」的端點（`api.ts` 檔頭
 * 明列的四支簽章端點之外，本 task 檔案範圍也不含後端）。
 *
 * 最大回撤自動停止：唯一在本步驟走**既有**簽章流程的欄位（裁決 1）——
 * 預設關閉；只有使用者主動開啟才呼叫 `/api/me/risk/message` → `/api/me/risk`
 * （`runRiskSettingsFlow`，與 `web/src/app/leaders/page.tsx` 的
 * `RiskControlsSection` 共用同一個 lib，未複製簽章邏輯本身）。關閉時
 * **不對 risk API 發出任何請求**（含唯讀的 `getMyRisk`——只在使用者開啟開關時
 * 才查真實門檻，紅線 5 語義：連「順便查一下現況」都不做，關閉就是完全不碰）。
 */
export function StepRiskLimits({ me, maxLeverage, initial, onBack, onNext }: {
  me: { address: string; account_id: string };
  maxLeverage: number | null;
  initial: StepRiskLimitsValues;
  onBack: () => void;
  onNext: (values: StepRiskLimitsValues) => void;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;
  const { signMessageAsync } = useSignMessage();

  const maxLev = maxLeverage != null && maxLeverage >= LEV_MIN ? maxLeverage : 3;
  const [scale, setScale] = useState(initial.scale);
  const [lev, setLev] = useState(Math.min(initial.lev, maxLev));
  const [ddEnabled, setDdEnabled] = useState(initial.ddEnabled);
  const [ddPct, setDdPct] = useState(initial.ddPct);
  const [signing, setSigning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ⭐ 只在使用者開啟回撤開關後才查（`enabled: ddEnabled`）——關閉時對 risk API
  // 零請求，包含這支唯讀查詢。
  const risk = useQuery<MyRiskResp>({
    queryKey: ["me-risk"], queryFn: getMyRisk, enabled: ddEnabled,
  });

  const spec = risk.data?.specs.find((s) => s.name === "max_drawdown_pct") ?? null;
  const ddMin = spec?.min != null ? Number(ratioToPct(spec.min)) : DD_MIN_FALLBACK;
  const ddMax = spec?.max != null ? Number(ratioToPct(spec.max)) : DD_MAX_FALLBACK;
  const ddClamped = Math.min(Math.max(ddPct, ddMin), ddMax);

  async function handleNext() {
    setError(null);
    if (!ddEnabled) {
      onNext({ scale, lev, ddEnabled, ddPct });
      return;
    }
    if (!risk.data) return; // 按鈕在讀取完成前 disabled，理論上到不了這裡
    setSigning(true);
    const target: RiskPrefs = {
      ...risk.data.prefs, enabled: true, max_drawdown_pct: pctToRatio(String(ddClamped)),
    };
    const r = await runRiskSettingsFlow(
      {
        fetchMessage: () => getRiskSettingsMessage(target),
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postMyRisk,
      },
      { expectedSigner: me.address, expectedAccountId: me.account_id, expectedPrefs: target },
    );
    setSigning(false);
    if (r.ok) onNext({ scale, lev, ddEnabled, ddPct: ddClamped });
    else setError(riskErrorCopy(r, c));
  }

  return (
    <div className="step-card">
      <p className="eyebrow">03・{c.stepNames[2]}</p>
      <h2>{c.step3Title}</h2>
      <p className="hint">{c.step3Body}</p>

      <div className="risk-field">
        <div className="risk-slider-row">
          <label htmlFor="onboard-scale-slider">{COPY.strategyDetail.panel.scaleLabel}</label>
          <span className="mono risk-value">{scale}%</span>
        </div>
        <input id="onboard-scale-slider" type="range" className="risk-slider"
          min={SCALE_MIN} max={SCALE_MAX} step={1} value={scale}
          onChange={(e) => setScale(Number(e.target.value))} />
      </div>

      <div className="risk-field">
        <div className="risk-slider-row">
          <label htmlFor="onboard-lev-slider">{COPY.strategyDetail.panel.leverageLabel}</label>
          <span className="mono risk-value">{lev}x</span>
        </div>
        <input id="onboard-lev-slider" type="range" className="risk-slider"
          min={LEV_MIN} max={maxLev} step={0.5} value={lev}
          onChange={(e) => setLev(Number(e.target.value))} />
      </div>

      <p className="hint">{c.fundsWarning}</p>

      <div className="risk-field">
        <label className="risk-toggle">
          <input type="checkbox" checked={ddEnabled}
            onChange={(e) => { setError(null); setDdEnabled(e.target.checked); }} />
          <span>{COPY.strategyDetail.panel.ddEnableLabel}</span>
        </label>
        <div className="risk-slider-row">
          <span>{COPY.strategyDetail.panel.ddLabel}</span>
          <span className="mono risk-value">{ddEnabled ? `-${ddClamped}%` : NO_VALUE}</span>
        </div>
        <input type="range" className="risk-slider" min={ddMin} max={ddMax} step={1}
          value={ddClamped} disabled={!ddEnabled}
          onChange={(e) => setDdPct(Number(e.target.value))}
          aria-label={COPY.strategyDetail.panel.ddLabel} />
        {!ddEnabled && <p className="hint risk-toggle-help">{COPY.strategyDetail.panel.ddDisabledNote}</p>}
        {ddEnabled && risk.isLoading && <p className="hint">{COPY.common.loading}</p>}
      </div>

      {error && <div className="sign-error" role="alert"><p>{error}</p></div>}

      <div className="step-actions">
        <button type="button" className="btn btn-secondary" onClick={onBack} disabled={signing}>
          {c.backButton}
        </button>
        <button type="button" className="btn btn-primary" onClick={() => void handleNext()}
          disabled={signing || (ddEnabled && (risk.isLoading || !risk.data))}>
          {signing ? c.ddSaving : c.step3NextButton}
        </button>
      </div>
    </div>
  );
}
