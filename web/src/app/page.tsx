"use client";
/**
 * `/` — 登入頁，同時是全站的產品敘事入口。
 *
 * ⭐ 2026-07-30 大幅減法：SIWE 登入機制與導向行為完全不動（測試契約盡量保留）；
 * 呈現重構為三步旅程——連結錢包並登入、完成兩項授權（跟單授權＋builder code
 * 收款授權）、貼上 leader 地址開始跟單。這三步是「綁定錢包」在本產品裡的權威
 * 定義，文案單一來源見 COPY.login.journey。
 */
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { COPY } from "@/lib/copy";
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
      <section className="login-shell login-inner">
        <p className="eyebrow">{c.eyebrow}</p>
        <h1 className="login-hero">{c.heroTitle}</h1>
        <p className="subtitle">{c.subtitle}</p>

        <ol className="landing-steps">
          {c.journey.map((step, i) => (
            <li key={step.title} className="landing-step">
              <span className="landing-step-num">{i + 1}</span>
              <div>
                <p className="landing-step-title">{step.title}</p>
                <p className="hint">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>

        <div className="login-actions">
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
        </div>
        <div className="login-footnotes">
          <p className="footnote">{c.footnote}</p>
          <p className="footnote">{COPY.common.nonCustodial}</p>
        </div>
      </section>
    </main>
  );
}
