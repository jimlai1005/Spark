"use client";
import { WalletPicker } from "@/components/WalletPicker";
import { useCopy } from "@/lib/lang";
import { useWalletLogin } from "@/lib/useWalletLogin";

/**
 * ReconnectGate — onboarding 中途錢包斷線（上鎖）時的重連卡。server session 仍有效，
 * 所以只連錢包不重簽（`connectOnly`）。多錢包並存時與登入 CTA 同一套選單
 * （2026-10-24，EIP-6963；流程見 lib/useWalletLogin）。
 */
export function ReconnectGate() {
  const COPY = useCopy();
  const c = COPY.wizard;
  const login = useWalletLogin({ connectOnly: true });
  return (
    <div className="step-card">
      <h2>{c.reconnectTitle}</h2>
      <p className="hint">{c.reconnectHint}</p>
      <div className="step-actions">
        <button type="button" className="btn btn-primary"
          disabled={login.phase !== "idle"}
          onClick={() => void login.start()}>
          {c.reconnectButton}
        </button>
      </div>
      {login.error && <p className="hint sign-error">{login.error}</p>}
      {login.pickerOpen && (
        <WalletPicker
          candidates={login.candidates}
          busy={login.phase !== "idle"}
          onPick={(conn) => void login.choose(conn)}
          onCancel={login.cancelPicker}
        />
      )}
    </div>
  );
}
