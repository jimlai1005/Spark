"use client";
import Link from "next/link";
import { Boundary } from "@/components/Boundary";
import { COPY } from "@/lib/copy";
import { shortAddr } from "@/lib/format";
import { useMe, useOnboardingStatus } from "@/lib/hooks";

export default function PerformancePage() {
  const me = useMe();
  const loggedIn = !!me.data;
  const status = useOnboardingStatus({ enabled: loggedIn, pollMs: 30_000 });
  const c = COPY.perf;

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

  const s = status.data;
  const ready = s?.state === "READY";
  const authorized = !!s && s.agent_approved && s.builder_fee_approved;

  return (
    <main className="page">
      <p className="eyebrow">{c.heroLabel}</p>
      <h1>
        {c.title}{" "}
        <span className={`chip ${ready ? "chip-up" : "chip-neutral"}`}>
          {ready ? c.stateReady : c.stateInProgress}
        </span>
      </h1>

      <div className="onboard-boundary-wrap">
        <Boundary
          walletTitle={c.walletPanelTitle}
          walletItems={[
            { dt: c.addrLabel, dd: shortAddr(me.data!.address), mono: true },
            { dt: c.fundedLabel, dd: s?.funded ? c.fundedOk : c.fundedNo },
          ]}
          engineTitle={c.enginePanelTitle}
          engineItems={[
            { dt: c.agentLabel, dd: s?.agent_address ? shortAddr(s.agent_address) : c.agentNone, mono: true },
            { dt: c.approvalsLabel, dd: authorized ? c.approvalsBoth : c.approvalsPartial },
          ]}
          threadPct={authorized ? 100 : 0}
          pillText={authorized ? COPY.login.pillAuthorized : COPY.login.pillUnauthorized}
          pillActive={authorized}
        />
      </div>

      {!ready && (
        <p>
          <Link className="btn btn-secondary" href="/onboarding">{c.goOnboarding}</Link>
        </p>
      )}

      <div className="panel" style={{ maxWidth: 420 }}>
        <p className="eyebrow">{c.feePanelTitle}</p>
        <p className="fee-note">{c.feeRateNote}</p>
        <p className="fee-note">{c.feeUpperNote}</p>
      </div>
      <p className="hint">{c.refreshNote}</p>
    </main>
  );
}
