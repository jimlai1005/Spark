"use client";
import { useState } from "react";
import { ApiError, postVerify, type OnboardStatus } from "@/lib/api";
import { useCopy } from "@/lib/lang";
import { fmtAmount } from "@/lib/format";

/**
 * ⭐ 2026-08-29 裁決 6：完成綁定失敗不再用單句籠統紅字，改逐條列出未滿足條件
 * （由 `OnboardStatus` 三個旗標 client 端推導，不重造一份平行的伺服器判斷）。
 * 三個旗標全 true（client 端看起來已滿足）但伺服器仍拒 → 沒有可列的條件，
 * 呼叫端改顯示伺服器 `detail` 原文（或通用 fallback）。
 */
function deriveUnmetReasons(
  s: Pick<OnboardStatus, "agent_approved" | "builder_fee_approved" | "funded">,
  c: ReturnType<typeof useCopy>["wizard"],
): string[] {
  const reasons: string[] = [];
  if (!s.agent_approved) reasons.push(c.errors.verifyAgentPending);
  if (!s.builder_fee_approved) reasons.push(c.errors.verifyBuilderFeePending);
  if (!s.funded) reasons.push(c.errors.verifyNotFunded);
  return reasons;
}

export function StepDeposit({ status, refetchStatus }: {
  status: OnboardStatus;
  refetchStatus: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorList, setErrorList] = useState<string[] | null>(null);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    setErrorList(null);
    try {
      const r = await postVerify();
      if (r.state === "READY") {
        setSubmitted(true);
      } else {
        const reasons = deriveUnmetReasons(r, c);
        if (reasons.length > 0) setErrorList(reasons);
        else setError(c.errors.verifyIncomplete);
      }
      refetchStatus();
    } catch (err) {
      // 請求本身失敗（網路／伺服器拒絕）：沒有新的伺服器旗標可用，退而用送出前
      // 已知的 `status` 推導；三旗標全 true 卻仍被拒 → 顯示伺服器 detail 原文。
      const reasons = deriveUnmetReasons(status, c);
      if (reasons.length > 0) {
        setErrorList(reasons);
      } else {
        setError(err instanceof ApiError && err.detail ? err.detail : c.errors.verifyIncomplete);
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="step-card">
      {/* ⭐ Task 10：獨立步驟標題（原「04・入金檢查」）移除，改為 step 2 內的
          子區塊小標——外層 StepConnect 已統一呈現本步驟唯一的 eyebrow+標題。 */}
      <p className="eyebrow">{c.step2DepositSubheading}</p>
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
        // ⭐ Task 10：不再導出到 /leaders——leader（＝所選策略）已在 step 1 決定，
        // 父層 onboarding/page.tsx 會依伺服器 state 自動把使用者帶到 step 3，
        // 這裡只需要一句「已完成、準備繼續」，不放任何會讓人離開精靈的連結。
        <p role="status">{c.submitted}</p>
      ) : (
        <div className="step-actions">
          <button type="button" className="btn btn-primary"
            disabled={!status.funded || submitting} onClick={handleSubmit}>
            {c.submitReview}
          </button>
        </div>
      )}
      {errorList && errorList.length > 0 && (
        <div className="sign-error">
          <ul>
            {errorList.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
      {error && <div className="sign-error"><p>{error}</p></div>}
    </div>
  );
}
