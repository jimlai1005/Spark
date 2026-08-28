"use client";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSignMessage } from "wagmi";
import {
  getMyCapital, getCapitalSettingsMessage, postCapitalSettings,
  getMyRisk, getRiskSettingsMessage, postMyRisk,
  type MyCapitalResp, type MyRiskResp, type RiskPrefs,
} from "@/lib/api";
import { useCopy } from "@/lib/lang";
import { fmtRatioPct, NO_VALUE } from "@/lib/format";
import { recoverPersonalSigner } from "@/lib/sign";
import { runCapitalSettingsFlow, type CapitalFlowFailure } from "@/lib/capitalSettingsFlow";
import { runRiskSettingsFlow, type RiskFlowFailure } from "@/lib/riskSettingsFlow";

const SCALE_MIN = 5;
const SCALE_MAX = 100;
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

function capitalErrorCopy(r: CapitalFlowFailure, c: ReturnType<typeof useCopy>["wizard"]): string {
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
  ddEnabled: boolean;
  ddPct: number;
}

/**
 * StepRiskLimits — onboarding step 3（設計稿 §05：「設定你的風險限制」）。
 *
 * ⭐ Task 10b（主線程裁決 2026-08-28，取代 Task 10 的原判斷）：投入比例**不是**
 * 純 UI 狀態——`allocated_capital`／`capital_utilization` 直接乘進部位大小
 * （`src/spark/copytrade/sizing.py::compute_scale_factor`），引擎套用前自行
 * 重新驗章，是真實綁定機制。若前端只留一顆好看的 slider 不接這條簽章流，
 * 等於告訴使用者「已設限」而實際上什麼都沒送出——比不做這個功能更糟。
 * 送出走 `GET /api/me/capital/message?allocated_capital=0&capital_utilization={x}
 * &use_full_equity=true` → 錢包簽名 → `POST /api/me/capital`（伺服器簽文原樣
 * 簽，不變量 1；`runCapitalSettingsFlow`，同 `runRiskSettingsFlow` 的謹慎度）。
 * `use_full_equity=true + capital_utilization=x` 對應「淨值 x%」——已對照
 * `sizing.resolve_capital`／`compute_scale_factor` 驗證語義相符（`cap =
 * max(my_equity, 0)`，`scale = cap * capital_utilization * weight / leader_equity`，
 * 與「本金 = 淨值、乘上使用率 x」等價）。
 *
 * 槓桿上限：**不是**使用者可簽的值——引擎的 `COPY_MAX_TARGET_LEVERAGE` 是
 * env 靜態值，沒有 per-user 簽章通道（Task 10 的觀察在這點成立）。原本的
 * slider 因此移除，改唯讀資訊列，誠實呈現平台層強制的上限；per-user 可簽槓桿
 * 上限列 backlog。
 *
 * 最大回撤自動停止：唯一維持 opt-in 的欄位（裁決 1）——預設關閉，只有使用者
 * 主動開啟才呼叫 `/api/me/risk/message` → `/api/me/risk`。關閉時**不對 risk
 * API 發出任何請求**（含唯讀的 `getMyRisk`）。
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

  const [scale, setScale] = useState(initial.scale);
  const [ddEnabled, setDdEnabled] = useState(initial.ddEnabled);
  const [ddPct, setDdPct] = useState(initial.ddPct);
  const [signing, setSigning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ⭐ 投入比例是本步的必要簽章流，一律查（不像回撤是 opt-in）：畫面需要
  // effective/pending 兩態才能誠實呈現「目前生效值」與「已提交待套用」。
  const capital = useQuery<MyCapitalResp>({ queryKey: ["me-capital"], queryFn: getMyCapital });

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
    setSigning(true);

    const capitalUtilization = pctToRatio(String(scale));
    const capResult = await runCapitalSettingsFlow(
      {
        fetchMessage: () => getCapitalSettingsMessage("0", capitalUtilization, true),
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postCapitalSettings,
      },
      {
        expectedSigner: me.address, expectedAccountId: me.account_id,
        expectedAllocatedCapital: "0", expectedCapitalUtilization: capitalUtilization,
        expectedUseFullEquity: true,
      },
    );
    if (!capResult.ok) {
      setSigning(false);
      setError(capitalErrorCopy(capResult, c));
      return;
    }

    if (!ddEnabled) {
      setSigning(false);
      onNext({ scale, ddEnabled, ddPct });
      return;
    }
    if (!risk.data) { setSigning(false); return; } // 按鈕在讀取完成前 disabled，理論上到不了這裡
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
    if (r.ok) onNext({ scale, ddEnabled, ddPct: ddClamped });
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
        {capital.data && (
          <div className="inset">
            {capital.data.effective?.capital_utilization != null && (
              <p className="hint mono">
                {c.capitalEffectiveLabel}：{fmtRatioPct(capital.data.effective.capital_utilization)}
              </p>
            )}
            {capital.data.pending && (
              <p className="hint" role="status">{c.capitalPendingLabel}</p>
            )}
            <p className="hint">{capital.data.note}</p>
          </div>
        )}
      </div>

      <div className="risk-field">
        <div className="risk-slider-row">
          <span>{COPY.strategyDetail.panel.leverageLabel}</span>
          <span className="mono risk-value">
            {maxLeverage != null ? `${maxLeverage}x` : NO_VALUE}
          </span>
        </div>
        <p className="hint">
          {c.leverageInfoPrefix}{maxLeverage != null ? `${maxLeverage}x` : NO_VALUE}{c.leverageInfoSuffix}
        </p>
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
