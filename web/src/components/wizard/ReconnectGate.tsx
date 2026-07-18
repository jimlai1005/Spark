"use client";
import { useConnect } from "wagmi";
import { COPY } from "@/lib/copy";

export function ReconnectGate() {
  const c = COPY.wizard;
  const { connect, connectors, isPending } = useConnect();
  const injected = connectors[0];
  return (
    <div className="step-card">
      <h2>{c.reconnectTitle}</h2>
      <p className="hint">{c.reconnectHint}</p>
      <div className="step-actions">
        <button type="button" className="btn btn-primary"
          disabled={!injected || isPending}
          onClick={() => injected && connect({ connector: injected })}>
          {c.reconnectButton}
        </button>
      </div>
      {!injected && <p className="hint">{COPY.login.noWallet}</p>}
    </div>
  );
}
