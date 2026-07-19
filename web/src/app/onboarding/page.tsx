"use client";
import Link from "next/link";
import { useAccount } from "wagmi";
import { Boundary } from "@/components/Boundary";
import { ReconnectGate } from "@/components/wizard/ReconnectGate";
import { StepDeposit } from "@/components/wizard/StepDeposit";
import { StepRisk } from "@/components/wizard/StepRisk";
import { StepSign } from "@/components/wizard/StepSign";
import type { SpotStranded } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { fmtAmount, shortAddr } from "@/lib/format";
import { useMe, useOnboardingStatus } from "@/lib/hooks";
import { deriveStep, getRiskConfirmed, setRiskConfirmed, threadPercent } from "@/lib/wizard";
import { useCallback, useReducer } from "react";

export default function OnboardingPage() {
  const { isConnected, address: walletAddress } = useAccount();
  const me = useMe();
  const loggedIn = !!me.data;
  const status = useOnboardingStatus({ enabled: loggedIn, pollMs: 5000 });
  const [, bump] = useReducer((x: number) => x + 1, 0); // risk 勾選後重算 deriveStep
  // Minor（opus Finding 5）：穩定 refetchStatus 參考，避免輪詢 re-render 讓
  // StepSign 的 ensure-agent effect 重跑而冗餘 POST /api/onboard/agent。
  const refetch = status.refetch;
  const refetchStatus = useCallback(() => void refetch(), [refetch]);

  if (me.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }
  if (!loggedIn) {
    return (
      <main className="page">
        <div className="narrow">
          <p>{COPY.common.notLoggedIn}</p>
          <Link className="btn btn-primary" href="/">{COPY.common.backToLogin}</Link>
        </div>
      </main>
    );
  }

  const address = me.data!.address;
  const s = status.data ?? null;
  // 設計定案 17：deriveStep 不吃錢包連線狀態——session 是身分權威，
  // 錢包鎖住/斷連不回退步驟；簽署所需連線由下方重連閘處理。
  const step = deriveStep({
    loggedIn,
    riskConfirmed: getRiskConfirmed(address),
    status: s,
  });
  // step 3/4 需要錢包在場（簽署/確保 chainId）；未連即重連閘（Finding 1）。
  const walletReady = isConnected && !!walletAddress;
  const pct = threadPercent(step, s);
  const c = COPY.wizard;

  return (
    <main className="page">
      <div className="onboard-boundary-wrap">
        <Boundary
          walletTitle={COPY.login.walletPanelTitle}
          walletItems={[{ dt: COPY.login.addrLabel, dd: shortAddr(address), mono: true }]}
          engineTitle={COPY.login.enginePanelTitle}
          engineItems={[{ dt: COPY.login.strategyLabel, dd: COPY.login.strategyValue }]}
          threadPct={pct}
          pillText={pct >= 100 ? COPY.login.pillAuthorized : COPY.login.pillUnauthorized}
          pillActive={pct >= 100}
        />
      </div>

      {/*
        ⭐ 錢卡在 spot 的提示放在精靈**外層**、步驟之上：它是「為什麼第 4 步的資金
        一直偵測不到」的答案，而第 4 步在錢包未連線時會被重連閘取代——放進步驟裡，
        最需要看到它的人反而看不到。`null`（沒有卡住的錢**或**查詢失敗）→ 整塊不畫。
      */}
      {s?.spot_stranded != null && <SpotStrandedNotice info={s.spot_stranded} />}

      <div className="wizard-layout">
        <nav aria-label="開通步驟">
          <ol className="wizard-steps">
            {c.stepNames.map((name, i) => {
              const n = i + 1;
              return (
                <li key={name} className={n === step ? "is-current" : n < step ? "is-done" : ""}>
                  <button type="button" className="step-btn" disabled={n > step}
                    aria-current={n === step ? "step" : undefined}>
                    <span className="step-num">{String(n).padStart(2, "0")}</span>
                    <span className="step-name">{name}</span>
                    <span className="step-check">✓</span>
                  </button>
                </li>
              );
            })}
          </ol>
        </nav>

        <div className="wizard-panel">
          {/* step 1（連接錢包）在本頁結構上不可達：未登入已被上方 guard 導回登入頁，
              已登入者 deriveStep ≥ 2（設計定案 17）。側欄的 01 恆顯示為 is-done。 */}
          {step === 2 && (
            <StepRisk onConfirm={() => { setRiskConfirmed(address); bump(); }} />
          )}
          {(step === 3 || step === 4) && !walletReady && <ReconnectGate />}
          {step === 3 && s && walletReady && (
            <StepSign status={s} loginAddress={address} refetchStatus={refetchStatus} />
          )}
          {step === 4 && s && walletReady && (
            <StepDeposit status={s} refetchStatus={refetchStatus} />
          )}
          {(step === 3 || step === 4) && !s && walletReady && (
            <p className="hint">{COPY.common.loading}</p>
          )}
        </div>
      </div>
    </main>
  );
}

/**
 * 「你有資金停在 spot 錢包」提示。
 *
 * ⚠️⚠️ **本元件永遠不會有一顆「幫我劃轉」按鈕**，這不是還沒做，是做不到：
 * spot → perp 是 user-signed action，需要客戶的**主鑰**才簽得動，而我方結構上
 * 不持有主鑰（非託管不變量）。畫一顆我們兌現不了的按鈕，比不畫還糟——客戶會停在
 * 這裡等它生效。能提供的只有「說明 ＋ 外部連結」，連結把人送到他自己的錢包介面，
 * 由他自己簽。
 *
 * ⭐ 金額與門檻取自後端（單一來源）；後端 `note` 原文刻意**不**直接渲染：它帶有
 * 給非 HTML 消費端看的 Markdown 強調符號（`**spot**`），照搬會在畫面上留下裸露的
 * 星號。此處改用等義的前端文案，數字仍然只有後端這一個來源。
 */
function SpotStrandedNotice({ info }: { info: SpotStranded }) {
  const c = COPY.wizard;
  return (
    <section className="spot-stranded" role="status" aria-label={c.spotStrandedTitle}>
      <p className="spot-stranded-title">{c.spotStrandedTitle}</p>
      <p>{c.spotStrandedBody}</p>
      <dl className="spot-stranded-facts">
        <div>
          <dt>{c.spotStrandedAmountLabel}</dt>
          <dd className="mono" title={info.usdc}>{fmtAmount(info.usdc)} USDC</dd>
        </div>
        <div>
          <dt>{c.spotStrandedThresholdLabel}</dt>
          <dd className="mono" title={info.threshold}>{fmtAmount(info.threshold)} USDC</dd>
        </div>
      </dl>
      <p className="hint">{c.spotStrandedManualNote}</p>
      {/* 外部連結（非按鈕）：動作發生在客戶自己的錢包介面，不在我們這裡。 */}
      <a className="btn btn-ghost" href={c.spotStrandedLinkHref}
         target="_blank" rel="noopener noreferrer">
        {c.spotStrandedLink}
      </a>
    </section>
  );
}
