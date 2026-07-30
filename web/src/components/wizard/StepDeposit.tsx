"use client";
import Link from "next/link";
import { useState } from "react";
import { postVerify, type OnboardStatus } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { fmtAmount } from "@/lib/format";

export function StepDeposit({ status, refetchStatus }: {
  status: OnboardStatus;
  refetchStatus: () => void;
}) {
  const c = COPY.wizard;
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      const r = await postVerify();
      if (r.state === "READY") setSubmitted(true);
      else setError(c.errors.verifyIncomplete);
      refetchStatus();
    } catch {
      setError(c.errors.verifyIncomplete);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="step-card">
      <p className="eyebrow">04・{c.stepNames[3]}</p>
      <h2>{c.step4Title}</h2>
      <div className={`deposit-check${status.funded ? "" : " is-pending"}`}>
        <span className="check-icon">{status.funded ? "✓" : "…"}</span>
        <span>{status.funded ? c.depositDetected : c.depositPending}</span>
      </div>
      {/*
        ⭐ 餘額區塊（2026-07-30）：`funded` 是後端拿 **perp 帳戶淨值** 對門檻算出來的，
        所以這裡顯示的就是那個判定值本身（後端同一次讀取，見 api.ts 的 OnboardStatus）。
        只給一個 ✓／… 的話，被擋下的客戶看不出是「錢還沒到」還是「錢到了但在 spot」
        ——而後者是最常見的原因。數字與門檻並排，客戶自己就能診斷。
      */}
      <dl className="deposit-facts">
        <div>
          <dt>{c.depositPerpLabel}</dt>
          <dd className="mono" title={status.perp_account_value}>
            {fmtAmount(status.perp_account_value)} USDC
          </dd>
        </div>
        <div>
          <dt>{c.depositThresholdLabel}</dt>
          <dd className="mono" title={status.min_deposit}>
            {fmtAmount(status.min_deposit)} USDC
          </dd>
        </div>
        {!status.funded && (
          <div>
            <dt>{c.depositShortfallLabel}</dt>
            <dd className="mono" title={status.deposit_shortfall}>
              {fmtAmount(status.deposit_shortfall)} USDC
            </dd>
          </div>
        )}
      </dl>
      {submitted ? (
        <>
          <p>{c.submitted}</p>
          <div className="step-actions">
            <Link className="btn btn-primary" href="/leaders">{c.goFollow}</Link>
          </div>
        </>
      ) : (
        <div className="step-actions">
          <button type="button" className="btn btn-primary"
            disabled={!status.funded || submitting} onClick={handleSubmit}>
            {c.submitReview}
          </button>
        </div>
      )}
      {error && <div className="sign-error"><p>{error}</p></div>}
    </div>
  );
}
