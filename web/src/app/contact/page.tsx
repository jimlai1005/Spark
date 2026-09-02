"use client";
/**
 * `/contact` — 聯絡表單（2026-09-02 Task 6，照 Claude Design 雙欄深色設計稿重做）。
 * POST /api/public/contact（無需登入），後端寄信到站主信箱、人工回覆。未登入可直接
 * 開啟，不掛登入 guard（同 /terms /privacy /risk）。**沒有姓名欄**——`topic` 單選 chip
 * 取代之。已登入時錢包地址自動帶入（唯讀＋徽章），可按「改填其他地址」切換成手填。
 * honeypot 欄位 `website`：視覺隱藏＋tabIndex=-1，真人不會填；後端見非空即靜默接受。
 */
import { useState, type FormEvent } from "react";
import { useCopy } from "@/lib/lang";
import { ApiError, postContact, type ContactTopic } from "@/lib/api";
import { useMe } from "@/lib/hooks";
import { shortAddr } from "@/lib/format";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const WALLET_RE = /^0x[0-9a-fA-F]{40}$/;
const TOPICS: ContactTopic[] = ["copytrade", "billing", "security", "partnership", "other"];
type Phase = "idle" | "sending" | "sent";

export default function ContactPage() {
  const c = useCopy().contact;
  const me = useMe();
  const meAddress = me.data?.address ?? "";

  const [topic, setTopic] = useState<ContactTopic>("copytrade");
  const [email, setEmail] = useState("");
  const [wallet, setWallet] = useState("");
  const [walletOverride, setWalletOverride] = useState(false);
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [errorFallback, setErrorFallback] = useState(false);
  const [ticket, setTicket] = useState("");

  // ⭐ `wallet === ""` 是載入競態的守門：/api/me 回來之前使用者若已手動輸入地址，
  //   不得被自動帶入蓋掉（reviewer W1）。
  const autofilled = !!meAddress && !walletOverride && wallet === "";

  function useOtherWallet() {
    setWalletOverride(true);
    setWallet("");
  }

  function validate(): string | null {
    const e = email.trim();
    if (!EMAIL_RE.test(e)) return c.errEmailInvalid;
    const w = autofilled ? meAddress : wallet.trim();
    if (w && !WALLET_RE.test(w)) return c.errWalletInvalid;
    const m = message.trim();
    if (m.length < 10 || m.length > 2000) return c.errMessageLength;
    return null;
  }

  async function onSubmit(ev: FormEvent) {
    ev.preventDefault();
    const v = validate();
    if (v) { setError(v); setErrorFallback(false); return; }
    setError(null);
    setErrorFallback(false);
    setPhase("sending");
    try {
      const resp = await postContact({
        topic,
        email: email.trim(),
        wallet: autofilled ? meAddress : wallet.trim(),
        message: message.trim(),
        website,
      });
      setTicket(resp.ticket);
      setPhase("sent");
    } catch (e) {
      setPhase("idle");
      if (e instanceof ApiError) {
        if (e.kind === "network") {
          setError(c.errNetwork);
          setErrorFallback(false);
        } else if (e.kind === "upstream") {
          setError(e.detail ?? c.errGeneric);
          setErrorFallback(true);
        } else {
          setError(e.detail ?? c.errGeneric);
          setErrorFallback(false);
        }
      } else {
        setError(c.errGeneric);
        setErrorFallback(false);
      }
    }
  }

  function reset() {
    setTopic("copytrade");
    setEmail("");
    setWallet("");
    setWalletOverride(false);
    setMessage("");
    setWebsite("");
    setError(null);
    setErrorFallback(false);
    setPhase("idle");
    setTicket("");
  }

  return (
    <main className="page contact-page">
      <div className="contact-grid">
        <section className="contact-intro">
          <p className="eyebrow">{c.eyebrow}</p>
          <h1 className="contact-title">{c.heading}</h1>
          <p className="contact-sub">{c.sub}</p>
          <div className="card contact-checklist">
            <p className="contact-checklist-title">{c.checklistTitle}</p>
            <ol className="contact-checklist-list">
              <li>
                <span className="contact-check-num mono">01</span>
                <span>{c.check1}</span>
              </li>
              <li>
                <span className="contact-check-num mono">02</span>
                <span>{c.check2}</span>
              </li>
              <li>
                <span className="contact-check-num mono neg">!</span>
                <span>
                  {c.checkWarnPrefix}
                  <strong>{c.checkWarnStrong}</strong>
                  {c.checkWarnSuffix}
                </span>
              </li>
            </ol>
          </div>
          <p className="contact-security-note">
            <span className="contact-dot" aria-hidden />
            {c.securityNote}
          </p>
        </section>

        <section className="card contact-card">
          {phase === "sent" ? (
            <div className="contact-success" role="status">
              <h2>{c.successTitle}</h2>
              <p className="contact-ticket mono">{ticket}</p>
              <p>{c.successBody.replace("{email}", email.trim())}</p>
              <button type="button" className="btn btn-secondary" onClick={reset}>
                {c.sendAnother}
              </button>
            </div>
          ) : (
            <form onSubmit={onSubmit} noValidate className="contact-form">
              <div className="contact-field">
                <div className="contact-label-row">
                  <span className="contact-label">{c.topicLabel}</span>
                </div>
                <div className="contact-chips" role="radiogroup" aria-label={c.topicLabel}>
                  {TOPICS.map((t) => (
                    <button
                      key={t}
                      type="button"
                      role="radio"
                      aria-checked={topic === t}
                      className={"contact-chip" + (topic === t ? " is-active" : "")}
                      onClick={() => setTopic(t)}
                    >
                      {c.topics[t]}
                    </button>
                  ))}
                </div>
              </div>

              <div className="contact-field">
                <div className="contact-label-row">
                  <label className="contact-label" htmlFor="contact-email">
                    {c.emailLabel} <span className="neg">*</span>
                  </label>
                  <span className="contact-hint">{c.emailHint}</span>
                </div>
                <input
                  id="contact-email"
                  className="addr-input"
                  type="email"
                  value={email}
                  maxLength={254}
                  autoComplete="email"
                  placeholder={c.emailPlaceholder}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>

              <div className="contact-field">
                <div className="contact-label-row">
                  <label className="contact-label" htmlFor="contact-wallet">
                    {c.walletLabel}
                  </label>
                  <span className="contact-hint">{c.walletHint}</span>
                </div>
                <div className="contact-wallet-wrap">
                  <input
                    id="contact-wallet"
                    className="addr-input mono"
                    readOnly={autofilled}
                    value={autofilled ? shortAddr(meAddress) : wallet}
                    placeholder={c.walletPlaceholder}
                    onChange={(e) => {
                      if (!autofilled) setWallet(e.target.value);
                    }}
                  />
                  {autofilled && <span className="contact-wallet-badge">{c.walletConnected}</span>}
                </div>
                {autofilled && (
                  <button type="button" className="contact-link-btn" onClick={useOtherWallet}>
                    {c.walletUseOther}
                  </button>
                )}
              </div>

              <div className="contact-field">
                <div className="contact-label-row">
                  <label className="contact-label" htmlFor="contact-message">
                    {c.messageLabel} <span className="neg">*</span>
                  </label>
                  <span className="contact-hint mono">{message.length} / 2000</span>
                </div>
                <textarea
                  id="contact-message"
                  className="addr-input contact-textarea"
                  rows={7}
                  maxLength={2000}
                  value={message}
                  placeholder={c.messagePlaceholder}
                  onChange={(e) => setMessage(e.target.value)}
                />
              </div>

              <input
                className="visually-hidden"
                tabIndex={-1}
                autoComplete="off"
                aria-hidden="true"
                name="website"
                value={website}
                onChange={(e) => setWebsite(e.target.value)}
              />

              {error && (
                <p className="addr-input-error" role="alert">
                  {error}
                  {errorFallback && (
                    <>
                      {" "}
                      {c.fallbackPrefix}
                      <a href={`mailto:${c.fallbackEmail}`}>{c.fallbackEmail}</a>
                    </>
                  )}
                </p>
              )}

              <div className="contact-footer-row">
                <p className="contact-consent">{c.consent}</p>
                <button type="submit" className="btn btn-primary" disabled={phase === "sending"}>
                  {phase === "sending" ? c.sending : c.send}
                </button>
              </div>
            </form>
          )}
        </section>
      </div>
    </main>
  );
}
