"use client";
import { useEffect, useRef, useState } from "react";
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

export function StepDeposit({ status, refetchStatus, autoVerify = false, onVerified }: {
  status: OnboardStatus;
  refetchStatus: () => void;
  /**
   * ⭐ T10（2026-09-02）：父層判定客戶載入時已 READY、但本地進度沒有
   * 「step2 已 verify」旗標（多半是重新整理／換頁跳過了這顆按鈕）→ 傳
   * `autoVerify=true`，本元件掛載即自動補打一次 `postVerify()`（冪等），
   * 失敗與成功都沿用下面既有的 submit 錯誤 UI，不另建一套。
   */
  autoVerify?: boolean;
  /** verify 成功（`state === "READY"`）時呼叫——不論是自動或手動點擊觸發。 */
  onVerified?: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorList, setErrorList] = useState<string[] | null>(null);
  // ⭐（2026-09-02，opus 審查 S2）guard 只讓 `autoVerify` 觸發一次：StrictMode
  // 的 dev 期雙呼叫（mount→cleanup→remount，同一個 fiber，ref 跨兩次都存活）
  // 或父層意外重掛載，都不該把 `POST /api/onboard/verify` 多打一次——雖然後端
  // 冪等，多打一次仍是白白浪費一次 HL 讀取。ref 而非 state：這是「做過了嗎」
  // 的旁路旗標，改它不該觸發重繪。
  const autoVerifyFired = useRef(false);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    setErrorList(null);
    try {
      const r = await postVerify();
      if (r.state === "READY") {
        setSubmitted(true);
        onVerified?.();
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

  // ⭐ T10：`autoVerify` 只在掛載當下判斷一次是否需要補打——不重複輪詢、不在
  // 失敗後自動重試（失敗留給客戶用下面既有的按鈕手動重試，同一顆按鈕、同一套
  // 錯誤 UI）。effect 依賴刻意只有 `autoVerify`：這是「掛載時要不要做一次」的
  // 判斷，不是要跟著 `status` 每次輪詢重跑。
  useEffect(() => {
    if (autoVerify && !autoVerifyFired.current) {
      autoVerifyFired.current = true;
      void handleSubmit();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoVerify]);

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
