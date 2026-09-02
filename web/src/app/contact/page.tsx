"use client";
/**
 * `/contact` — 聯絡表單（2026-09-02）。POST /api/public/contact（無需登入），後端寄信到
 * 站主信箱、人工回覆。未登入可直接開啟，不掛登入 guard（同 /terms /privacy /risk）。
 * honeypot 欄位 `website`：視覺隱藏＋tabIndex=-1，真人不會填；後端見非空即靜默接受。
 */
import { useState, type FormEvent } from "react";
import { useCopy } from "@/lib/lang";
import { ApiError, postContact } from "@/lib/api";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
type Phase = "idle" | "sending" | "sent";

export default function ContactPage() {
  const c = useCopy().contact;
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  function validate(): string | null {
    const n = name.trim(), e = email.trim(), m = message.trim();
    if (!n || n.length > 80) return c.errNameRequired;
    if (!e || e.length > 254 || !EMAIL_RE.test(e)) return c.errEmailInvalid;
    if (m.length < 10 || m.length > 2000) return c.errMessageLength;
    return null;
  }

  async function onSubmit(ev: FormEvent) {
    ev.preventDefault();
    const v = validate();
    if (v) { setError(v); return; }
    setError(null);
    setPhase("sending");
    try {
      await postContact({ name: name.trim(), email: email.trim(), message: message.trim(), website });
      setPhase("sent");
    } catch (e) {
      setPhase("idle");
      if (e instanceof ApiError) setError(e.kind === "network" ? c.errNetwork : (e.detail ?? c.errGeneric));
      else setError(c.errGeneric);
    }
  }

  function reset() {
    setName(""); setEmail(""); setMessage(""); setWebsite(""); setError(null); setPhase("idle");
  }

  return (
    <main className="page contact-page">
      <header className="contact-head">
        <h1>{c.heading}</h1>
        <p className="section-sub">{c.sub}</p>
        <p className="hint">
          {c.fallbackPrefix}
          <a href={`mailto:${c.fallbackEmail}`}>{c.fallbackEmail}</a>
        </p>
      </header>

      {phase === "sent" ? (
        <section className="card contact-card contact-success" role="status">
          <h2>{c.successTitle}</h2>
          <p>{c.successBody}</p>
          <button type="button" className="btn btn-secondary" onClick={reset}>{c.sendAnother}</button>
        </section>
      ) : (
        <form className="card contact-card" onSubmit={onSubmit} noValidate>
          <label className="addr-field" htmlFor="contact-name">
            <span className="addr-field-label">{c.nameLabel}</span>
            <input id="contact-name" className="addr-input" type="text" value={name}
              maxLength={80} autoComplete="name" placeholder={c.namePlaceholder}
              onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="addr-field" htmlFor="contact-email">
            <span className="addr-field-label">{c.emailLabel}</span>
            <input id="contact-email" className="addr-input" type="email" value={email}
              maxLength={254} autoComplete="email" placeholder={c.emailPlaceholder}
              onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label className="addr-field" htmlFor="contact-message">
            <span className="addr-field-label">{c.messageLabel}</span>
            <textarea id="contact-message" className="addr-input contact-textarea" value={message}
              maxLength={2000} rows={8} placeholder={c.messagePlaceholder}
              onChange={(e) => setMessage(e.target.value)} />
          </label>
          <input className="visually-hidden" tabIndex={-1} autoComplete="off" aria-hidden="true"
            name="website" value={website} onChange={(e) => setWebsite(e.target.value)} />
          {error && <p className="addr-input-error" role="alert">{error}</p>}
          <div className="contact-actions">
            <button type="submit" className="btn btn-primary" disabled={phase === "sending"}>
              {phase === "sending" ? c.sending : c.send}
            </button>
          </div>
        </form>
      )}
    </main>
  );
}
