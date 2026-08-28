"use client";
import { useEffect, useRef, useState } from "react";
import { useAccount, useConnectorClient } from "wagmi";
import {
  ApiError, createAgent, getApproveAgentPayload, getApproveBuilderFeePayload,
  type OnboardStatus,
} from "@/lib/api";
import { runApprovalFlow, type ApprovalResult } from "@/lib/approvalFlow";
import { useCopy } from "@/lib/lang";
import { shortAddr } from "@/lib/format";
import { recoverSigner, submitToHl, type HlTypedData } from "@/lib/hl";

type CardPhase =
  | { t: "idle" }
  | { t: "awaiting" }
  | { t: "submitted" }
  | { t: "error"; message: string; retrySubmit?: () => Promise<unknown> };

interface StepSignProps {
  status: OnboardStatus;
  loginAddress: string;      // session 地址（recover 預驗基準）
  refetchStatus: () => void;
}

export function StepSign({ status, loginAddress, refetchStatus }: StepSignProps) {
  const COPY = useCopy();
  const c = COPY.wizard;
  const { address, chainId } = useAccount();
  const { data: client } = useConnectorClient();
  const [agentError, setAgentError] = useState<string | null>(null);
  const ensuring = useRef(false);

  // 進入本步自動確保 agent 存在（設計定案 15）；409（已有 agent）視為成功。
  useEffect(() => {
    if (status.agent_generated || ensuring.current) return;
    ensuring.current = true;
    createAgent()
      .then(() => refetchStatus())
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 409) refetchStatus();
        else setAgentError(c.errors.agentUnavailable);
      })
      .finally(() => { ensuring.current = false; });
  }, [status.agent_generated, refetchStatus, c.errors.agentUnavailable]);

  function signRaw(typedDataJson: string): Promise<string> {
    if (!client || !address) return Promise.reject(new Error("wallet not ready"));
    // 設計定案 1：raw eth_signTypedData_v4，typed data 原文——不經 viem 高階 API 重組。
    return client.request({
      method: "eth_signTypedData_v4",
      params: [address, typedDataJson],
    } as never) as Promise<string>;
  }

  function makeCard(kind: "agent" | "fee") {
    const fetchPayload = async (): Promise<HlTypedData> => {
      // !chainId 在 UI 上不可達：兩張卡的簽署鈕都以 chainId 缺失為 disabled 條件
      // （見下方 SignCard 的 disabled prop）。此處仍防禦性擋一層，走既有
      // payload-failed 分類（opus 終審 Minor 3：對應的 chainMissing 文案已刪除，
      // 因為它從未在這條路徑被觸發過——沒有專屬 ApprovalResult kind 可映射）。
      if (!chainId) throw new Error("no chainId");
      const r = kind === "agent"
        ? await getApproveAgentPayload(chainId)
        : await getApproveBuilderFeePayload(chainId);
      return r.typed_data;
    };
    return fetchPayload;
  }

  return (
    <div className="step-card">
      {/*
        ⭐ Task 10：本元件的標題（原「03・簽署授權」）改由外層的 StepConnect 統一
        呈現一次（設計稿 §05 每個步驟面板只有一個 eyebrow+標題）——StepSign 現在
        巢狀顯示在 step 2「連接與授權」之下，簽署邏輯本身完全不變。
      */}
      {status.agent_address ? (
        <p className="hint">
          {c.agentLabel}：<span className="mono">{shortAddr(status.agent_address)}</span>
        </p>
      ) : (
        <p className="hint">{c.agentPreparing}</p>
      )}
      {agentError && (
        <div className="sign-error"><p>{agentError}</p></div>
      )}
      <SignCard
        name={c.agentCardName} desc={c.agentCardDesc}
        confirmed={status.agent_approved}
        disabled={!status.agent_address || !chainId}
        run={() =>
          runApprovalFlow(
            { fetchPayload: makeCard("agent"), signTypedData: signRaw,
              recover: recoverSigner, submit: submitToHl },
            { expectedSigner: loginAddress },
          )
        }
      />
      <SignCard
        name={c.feeCardName} desc={c.feeCardDesc}
        confirmed={status.builder_fee_approved}
        disabled={!chainId}
        run={() =>
          runApprovalFlow(
            { fetchPayload: makeCard("fee"), signTypedData: signRaw,
              recover: recoverSigner, submit: submitToHl },
            { expectedSigner: loginAddress },
          )
        }
      />
      <p className="hint">{COPY.common.nonCustodial}</p>
    </div>
  );
}

function SignCard(p: {
  name: string;
  desc: string;
  confirmed: boolean;   // 鏈上事實（status 輪詢）——最終真相
  disabled: boolean;
  run: () => Promise<ApprovalResult>;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;
  const [phase, setPhase] = useState<CardPhase>({ t: "idle" });

  const errorCopy = (r: Extract<ApprovalResult, { ok: false }>): string => {
    const e = COPY.wizard.errors;
    switch (r.kind) {
      case "payload-failed": return e.payloadFailed;
      case "wallet-rejected": return e.walletRejected;
      case "signer-mismatch": return e.signerMismatch;
      case "hl-transient": return e.hlTransient;
      case "hl-semantic": return e.hlSemantic;
    }
  };

  async function handleSign() {
    setPhase({ t: "awaiting" });
    const r = await p.run();
    if (r.ok) setPhase({ t: "submitted" });
    else if (r.kind === "hl-transient") {
      setPhase({
        t: "error", message: errorCopy(r),
        retrySubmit: async () => {
          const rr = await r.retrySubmit();
          setPhase((rr as { ok: boolean }).ok ? { t: "submitted" } : { t: "idle" });
        },
      });
    } else setPhase({ t: "error", message: errorCopy(r) });
  }

  const stateText = p.confirmed ? c.stateConfirmed
    : phase.t === "awaiting" ? c.stateAwaitingWallet
    : phase.t === "submitted" ? c.stateSubmitted
    : phase.t === "error" ? c.stateRejected
    : c.stateUnsigned;

  return (
    <div className={`sign-card${p.confirmed ? " is-signed" : ""}`}>
      <div className="sign-card-head">
        <span className="sign-name">{p.name}</span>
        <span className="sign-check">✓</span>
        <span className="sign-state">{stateText}</span>
      </div>
      <p className="sign-desc">{p.desc}</p>
      {!p.confirmed && phase.t !== "submitted" && (
        <button type="button" className="btn btn-secondary"
          disabled={p.disabled || phase.t === "awaiting"} onClick={handleSign}>
          {c.signWithWallet}
        </button>
      )}
      {phase.t === "error" && (
        <div className="sign-error">
          <p>{phase.message}</p>
          <button type="button" className="btn btn-ghost"
            onClick={() => (phase.retrySubmit ? phase.retrySubmit() : handleSign())}>
            {COPY.common.retry}
          </button>
        </div>
      )}
    </div>
  );
}
