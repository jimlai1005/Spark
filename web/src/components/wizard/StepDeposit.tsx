"use client";
import { useState } from "react";
import { postVerify, type OnboardStatus } from "@/lib/api";
import { COPY } from "@/lib/copy";

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
      {submitted ? (
        <p>{c.submitted}</p>
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
