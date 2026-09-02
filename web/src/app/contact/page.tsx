"use client";
/**
 * `/contact` — 聯絡表單（設計稿 R3-01～R3-08，2026-09-02 Task 7）。
 * POST /api/public/contact（無需登入）；後端落工單 FLT-YYMM-NNNN、寄信到站主、推 TG。
 * 頁面上**沒有任何信箱字串**（R3-01）。錢包地址登入時自動帶入、可「清除」（R3-06）。
 * 狀態（R3-07）：送出中 disabled；5xx／網路 → 紅字「送出失敗，請稍後再試」保留內容；
 * 4xx → 後端 detail 原樣；成功 → 同位置卡片取代表單，不跳頁。
 * honeypot 欄位 `website`：視覺隱藏＋tabIndex=-1，真人不會填；後端見非空即靜默接受。
 * 窄版（R3-08）：grid-template-areas 讓「送出前請確認」移到表單下方（見 globals.css）。
 */
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { useCopy } from "@/lib/lang";
import { ApiError, postContact, type ContactTopic } from "@/lib/api";
import { useMe } from "@/lib/hooks";
import { shortAddr } from "@/lib/format";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
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
  const [walletCleared, setWalletCleared] = useState(false);
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState("");
  const [sentEmail, setSentEmail] = useState("");
  const [copied, setCopied] = useState(false);

  // `wallet === ""` 是載入競態的守門：/api/me 回來之前使用者若已手動輸入地址，不得被自動帶入蓋掉。
  const autofilled = !!meAddress && !walletCleared && wallet === "";

  function clearWallet() {
    setWalletCleared(true);
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
    if (v) { setError(v); return; }
    setError(null);
    setPhase("sending");
    try {
      const resp = await postContact({
        topic,
        email: email.trim(),
        wallet: autofilled ? meAddress : wallet.trim(),
        message: message.trim(),
        page_url: typeof window === "undefined" ? "" : window.location.href,
        user_agent: typeof navigator === "undefined" ? "" : navigator.userAgent,
        website,
      });
      setTicket(resp.ticket);
      setSentEmail(email.trim());
      setPhase("sent");
    } catch (e) {
      setPhase("idle");
      if (e instanceof ApiError && (e.kind === "client" || e.kind === "auth") && e.detail) {
        setError(e.detail);                 // 4xx：後端固定字串（422／429）
      } else {
        setError(c.errSendFailed);          // 5xx／網路（R3-07）
      }
    }
  }

  async function copyTicket() {
    try {
      await navigator.clipboard.writeText(ticket);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <main className="page contact-page">
      <div className="contact-grid">
        <header className="contact-head">
          <p className="eyebrow">{c.eyebrow}</p>
          <h1 className="contact-title">{c.heading}</h1>
          <p className="contact-sub">{c.sub}</p>
        </header>

        <aside className="contact-aside">
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
        </aside>

        <section className="card contact-card">
          {phase === "sent" ? (
            <div className="contact-success" role="status">
              <div className="contact-success-check" aria-hidden>✓</div>
              <h2 className="contact-success-title">{c.successTitle}</h2>
              <p className="contact-success-body">{c.successBody.replace("{email}", sentEmail)}</p>
              <div className="inset contact-ticket-box">
                <span className="contact-ticket-label">{c.ticketLabel}</span>
                <span className="contact-ticket mono">{ticket}</span>
                <button type="button" className="btn btn-secondary contact-copy-btn" onClick={copyTicket}>
                  {copied ? c.copied : c.copy}
                </button>
              </div>
              <Link href="/" className="contact-back-link">{c.backHome}</Link>
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
                  {autofilled && (
                    <span className="contact-wallet-tools">
                      <span className="contact-wallet-badge">{c.walletConnected}</span>
                      <button type="button" className="contact-link-btn" onClick={clearWallet}>
                        {c.walletClear}
                      </button>
                    </span>
                  )}
                </div>
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

              {error && <p className="addr-input-error" role="alert">{error}</p>}

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
