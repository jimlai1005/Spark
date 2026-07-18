"use client";
import Link from "next/link";
import { useAccount } from "wagmi";
import { Boundary } from "@/components/Boundary";
import { ReconnectGate } from "@/components/wizard/ReconnectGate";
import { StepDeposit } from "@/components/wizard/StepDeposit";
import { StepRisk } from "@/components/wizard/StepRisk";
import { StepSign } from "@/components/wizard/StepSign";
import { COPY } from "@/lib/copy";
import { shortAddr } from "@/lib/format";
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
