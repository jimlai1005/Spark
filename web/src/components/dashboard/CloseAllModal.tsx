"use client";
import { useState } from "react";
import { useSignMessage } from "wagmi";
import { getCloseAllMessage, postCloseAll, type DashboardPosition } from "@/lib/api";
import { runCloseAllFlow, type CloseAllFlowFailure } from "@/lib/closeAllFlow";
import { useCopy } from "@/lib/lang";
import { recoverPersonalSigner } from "@/lib/sign";

function errCopy(r: CloseAllFlowFailure, e: ReturnType<typeof useCopy>["wizard"]["errors"]): string {
  switch (r.kind) {
    case "wallet-rejected": return e.walletRejected;
    case "signer-mismatch": return e.signerMismatch;
    case "content-mismatch": return e.contentMismatch;
    case "message-failed": return e.payloadFailed;
    case "submit-failed": return e.submitFailed;
  }
}

/**
 * 「平倉並撤銷」二次確認 modal（Task 15 kill switch 第二級）。
 *
 * 列出將平倉部位＋不可逆警語＋必勾的理解確認框，全部滿足才可簽署送出。
 * 簽署流程走 `runCloseAllFlow`（`lib/closeAllFlow.ts`）：伺服器產生原文 →
 * 內容預驗 → 錢包簽名 → recover 預驗 → 送出，全程不自組待簽字串（不變量 1）。
 * 成功後呼叫 `onSubmitted()`，交由呼叫端（dashboard 頁）觸發輪詢，本元件
 * 不負責後續狀態轉換。
 */
export function CloseAllModal({
  me, positions, onClose, onSubmitted,
}: {
  me: { address: string; account_id: string };
  positions: DashboardPosition[] | null;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.dashboard.status.closeAllModal;
  const errs = COPY.wizard.errors;
  const { signMessageAsync } = useSignMessage();
  const [ack, setAck] = useState(false);
  const [signing, setSigning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleConfirm() {
    setError(null);
    setSigning(true);
    const r = await runCloseAllFlow(
      {
        fetchMessage: getCloseAllMessage,
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postCloseAll,
      },
      { expectedSigner: me.address, expectedAccountId: me.account_id },
    );
    setSigning(false);
    if (r.ok) {
      onSubmitted();
    } else {
      setError(errCopy(r, errs));
    }
  }

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={c.title}>
      <div className="modal-card card">
        <h3>{c.title}</h3>
        <p className="hint">{c.warning}</p>
        <div className="inset">
          <p className="dash-card-label">{c.positionsHeading}</p>
          {positions && positions.length > 0 ? (
            <ul>
              {positions.map((p) => (
                <li key={p.symbol} className="mono">
                  {p.symbol} · {p.side} · {p.value}
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">{c.noPositions}</p>
          )}
        </div>
        <label className="risk-toggle">
          <input type="checkbox" checked={ack} disabled={signing}
            onChange={(e) => { setError(null); setAck(e.target.checked); }} />
          <span>{c.ackLabel}</span>
        </label>
        {signing && <p className="hint">{c.signingNote}</p>}
        {error && <div className="sign-error" role="alert"><p>{error}</p></div>}
        <div className="step-actions">
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={signing}>
            {c.cancelBtn}
          </button>
          <button type="button" className="dash-btn-close"
            onClick={() => void handleConfirm()} disabled={!ack || signing}>
            {c.confirmBtn}
          </button>
        </div>
      </div>
    </div>
  );
}
