"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { Boundary } from "@/components/Boundary";
import { COPY } from "@/lib/copy";
import { shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { loginWithSiwe } from "@/lib/siwe";

type Phase = "idle" | "connecting" | "signing";

export default function LoginPage() {
  const router = useRouter();
  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const me = useMe();
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  const c = COPY.login;
  const loggedIn = !!me.data;

  async function handleConnect() {
    setError(null);
    try {
      let addr = address;
      let cid = chainId;
      if (!isConnected) {
        const injected = connectors[0];
        if (!injected) {
          setError(c.noWallet);
          return;
        }
        setPhase("connecting");
        const result = await connectAsync({ connector: injected });
        addr = result.accounts[0];
        cid = result.chainId;
      }
      if (!addr || !cid) {
        setError(c.noWallet);
        return;
      }
      setPhase("signing");
      await loginWithSiwe({
        address: addr,
        chainId: cid,
        signMessage: (message) => signMessageAsync({ message }),
      });
      router.push("/onboarding");
    } catch (err) {
      const e = err as { name?: string; code?: number; message?: string } | undefined;
      const isRejected =
        e?.name === "UserRejectedRequestError" ||
        e?.code === 4001 ||
        /reject|denied|cancel/i.test(String(e?.message ?? ""));
      setError(isRejected ? c.rejected : c.loginFailed);
    } finally {
      setPhase("idle");
    }
  }

  return (
    <main className="page">
      <section className="narrow login-inner">
        <h1 className="wordmark">{COPY.common.appName}</h1>
        <p className="subtitle">{c.subtitle}</p>

        <div className="login-boundary-wrap">
          <Boundary
            walletTitle={c.walletPanelTitle}
            walletItems={[
              { dt: c.addrLabel, dd: address ? shortAddr(address) : c.notConnected, mono: true },
              { dt: c.balanceLabel, dd: "—", mono: true },
            ]}
            engineTitle={c.enginePanelTitle}
            engineItems={[
              { dt: c.strategyLabel, dd: c.strategyValue },
              { dt: c.engineStateLabel, dd: c.engineStateIdle, mono: true },
            ]}
            threadPct={0}
            pillText={c.pillUnauthorized}
            pillActive={false}
          />
        </div>

        <div className="cta-row">
          {loggedIn ? (
            <button type="button" className="btn btn-primary btn-block"
              onClick={() => router.push("/onboarding")}>
              {COPY.wizard.stepNames[0]} ✓ — {COPY.common.next}
            </button>
          ) : (
            <button type="button" className="btn btn-primary btn-block"
              onClick={handleConnect} disabled={phase !== "idle"}>
              {phase === "connecting" ? c.connecting : phase === "signing" ? c.signingIn : c.connect}
            </button>
          )}
        </div>
        <p className="hint">{c.signInNote}</p>
        {error && (
          <div className="sign-error">
            <p>{error}</p>
          </div>
        )}
        <p className="footnote">{c.footnote}</p>
        <p className="footnote">{COPY.common.nonCustodial}</p>
      </section>
    </main>
  );
}
