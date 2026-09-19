"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSignMessage } from "wagmi";
import { FeeCalculator } from "@/components/FeeCalculator";
import {
  getLeaderSelectMessage,
  getMyRisk,
  getReferralMessage,
  getReferralStatus,
  postLeaderSelect,
  submitReferralOptin,
  type MyRiskResp,
  type ReferralStatusResp,
} from "@/lib/api";
import { useCopy } from "@/lib/lang";
import { runLeaderSelectFlow, type LeaderSelectFlowResult } from "@/lib/leaderSelectFlow";
import { runReferralOptinFlow, type ReferralOptinFlowResult } from "@/lib/referralFlow";
import { recoverPersonalSigner } from "@/lib/sign";

function leaderErrorCopy(
  r: Extract<LeaderSelectFlowResult, { ok: false }>,
  c: ReturnType<typeof useCopy>["wizard"],
): string {
  const e = c.errors;
  switch (r.kind) {
    case "wallet-rejected": return e.walletRejected;
    case "signer-mismatch": return e.signerMismatch;
    case "leader-mismatch": return e.contentMismatch;
    case "message-failed": return e.payloadFailed;
    case "submit-failed": return e.submitFailed;
  }
}

function referralErrorCopy(
  r: Extract<ReferralOptinFlowResult, { ok: false }>,
  c: ReturnType<typeof useCopy>["wizard"],
): string {
  const e = c.errors;
  switch (r.kind) {
    case "wallet-rejected": return e.walletRejected;
    case "signer-mismatch": return e.signerMismatch;
    case "content-mismatch": return e.contentMismatch;
    case "message-failed": return e.payloadFailed;
    case "submit-failed": return e.submitFailed;
  }
}

/**
 * 推薦碼（選填）卡片——只在 `enabled && !signed` 時顯示（fail-quiet：`enabled`
 * 為 false 或狀態讀取失敗，整塊不渲染，見 `lib/referralFlow.ts` 檔頭）。不簽署
 * 完全不影響本頁原本的完成按鈕與行為。
 *
 * ⭐ 風控 `specs`（域分隔防線第 4 件事所需的參數名清單）刻意**不**用
 * `useQuery` 掛載時預抓——onboarding step 3（`StepRiskLimits`）有一條既有測試
 * 斷言「回撤開關關閉時，進到 step 4 不得觸發任何 risk API 呼叫」；本卡片與
 * 風控設定是兩個無關的功能，不該讓它的存在本身就讓那條斷言破功。改成只在使用者
 * 真的按下「簽署啟用」那一刻才去要 specs（`getMyRisk`），沒點擊就不會有任何
 * risk API 呼叫。
 */
function ReferralOptinCard({ me }: { me: { address: string; account_id: string } }) {
  const COPY = useCopy();
  const c = COPY.wizard.referralOptin;
  const ce = COPY.wizard;
  const { signMessageAsync } = useSignMessage();
  const queryClient = useQueryClient();
  const status = useQuery<ReferralStatusResp>({ queryKey: ["me-referral"], queryFn: getReferralStatus });
  const [signing, setSigning] = useState(false);
  const [signed, setSigned] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!status.data || !status.data.enabled) return null;
  // `enabled && !signed` 才渲染（見檔頭）：伺服器已記錄簽署過、且本次 render 也
  // 不是「剛剛簽完」的那一刻 → 整塊不渲染。剛簽完（本地 `signed` 翻真）則改顯示
  // 下方的已簽署提示，而不是讓卡片憑空消失。
  if (status.data.signed && !signed) return null;
  if (status.data.code === null) return null; // 防禦：enabled 理論上意味 code 非 null
  const code = status.data.code;

  async function sign() {
    setSigning(true);
    setError(null);
    let risk: MyRiskResp;
    try {
      risk = await getMyRisk();
    } catch {
      setSigning(false);
      setError(ce.errors.payloadFailed);
      return;
    }
    const r = await runReferralOptinFlow(
      {
        fetchMessage: getReferralMessage,
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: submitReferralOptin,
      },
      {
        expectedSigner: me.address,
        expectedAccountId: me.account_id,
        expectedCode: code,
        riskParamNames: risk.specs.map((s) => s.name),
      },
    );
    setSigning(false);
    if (r.ok) {
      setSigned(true);
      void queryClient.invalidateQueries({ queryKey: ["me-referral"] });
    } else {
      setError(referralErrorCopy(r, ce));
    }
  }

  return (
    <div className="step-card referral-optin-card">
      <p className="eyebrow">{c.title}</p>
      <p className="hint">{c.body}</p>
      {error && <div className="sign-error" role="alert"><p>{error}</p></div>}
      {signed ? (
        <p className="hint" role="status">{c.signedNote}</p>
      ) : (
        <div className="step-actions">
          <button type="button" className="btn btn-secondary" disabled={signing} onClick={() => void sign()}>
            {signing ? c.signing : c.signButton}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * StepConfirm — onboarding step 4（設計稿 §05／NOTE 12：「費用與風險確認」）。
 *
 * ⭐⭐ 本步驟的送出動作是整條精靈裡**唯一**會實際「開始跟單」的那一下：
 * step 1 選定的策略＝白名單裡的一個 leader，但 leader 授權（`postLeaderSelect`，
 * `api.ts` 檔頭四支簽章端點之一）在舊版 wizard 裡從未被呼叫過——舊流程做完
 * agent／fee／入金三件事後，把使用者導去 `/leaders` 頁**另外**手動選一次 leader
 * 才會真正開始跟單。新產品把「選策略」提前到 step 1，若不在精靈結束前把這一步
 * 走完，使用者會做完全部開通動作、卻連一個 leader 都沒選定，引擎永遠不會啟動
 * ——這是本 task 在讀 plan 與既有程式碼時發現的落地缺口（plan 原文只提到既有
 * pending/activate 流程，未點名 leader 選定），選擇在此**沿用既有、已完整測試
 * 的 `runLeaderSelectFlow`／`postLeaderSelect`**補上，不新增任何簽章端點。
 * 詳見本次交付回報「自行決策點」。
 */
export function StepConfirm({ me, leaderAddress, estimatedNotional, onDone }: {
  me: { address: string; account_id: string };
  leaderAddress: string | null;
  estimatedNotional: number;
  onDone: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;
  const { signMessageAsync } = useSignMessage();
  const [checks, setChecks] = useState([false, false, false]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allChecked = checks.every(Boolean);
  const labels = [c.step4CheckLoss, c.step4CheckFee, c.step4CheckRevoke];

  async function handleSubmit() {
    if (!leaderAddress) return;
    setSubmitting(true);
    setError(null);
    const r = await runLeaderSelectFlow(
      {
        fetchMessage: () => getLeaderSelectMessage(leaderAddress),
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postLeaderSelect,
      },
      { expectedSigner: me.address, expectedLeader: leaderAddress },
    );
    setSubmitting(false);
    if (r.ok) onDone();
    else setError(leaderErrorCopy(r, c));
  }

  return (
    <>
      <div className="step-card">
        <p className="eyebrow">04・{c.stepNames[3]}</p>
        <h2>{c.step4Title}</h2>
        <p className="hint">{c.step4Body}</p>

        <FeeCalculator initialNotional={estimatedNotional} />

        {labels.map((text, i) => (
          <label key={text} className="check-row">
            <input type="checkbox" checked={checks[i]}
              onChange={(e) => {
                const next = [...checks];
                next[i] = e.target.checked;
                setChecks(next);
              }} />
            <span>{text}</span>
          </label>
        ))}

        {error && <div className="sign-error" role="alert"><p>{error}</p></div>}

        <div className="step-actions">
          <button type="button" className="btn btn-primary"
            disabled={!allChecked || submitting || !leaderAddress}
            onClick={() => void handleSubmit()}>
            {submitting ? c.step4Submitting : c.step4SubmitButton}
          </button>
        </div>
        <p className="hint">{COPY.common.nonCustodial}</p>
      </div>
      <ReferralOptinCard me={me} />
    </>
  );
}
