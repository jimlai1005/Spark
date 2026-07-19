"use client";
/**
 * /billing — 訂閱管理（需登入；Header 刻意不放 tab，與 /admin、/ops 同慣例，
 * 入口是 Header 的訂閱 chip）。
 *
 * ⭐ 取消訂閱與更新付款方式一律交給 Stripe Customer Portal，我們不自建：
 * 自建等於在本地複製一份 Stripe 的狀態機，而 webhook 是本地 billing 表的唯一寫入者，
 * 前端多開一條寫入路徑就會產生第二個真相來源（工程原則 1）。
 *
 * 三種「沒有訂閱」的畫面必須分得開：
 * - 501（billing 未啟用）→「即將開放」：功能還沒上線，不是使用者的狀態
 * - status=none / portal 409（無訂閱記錄）→「尚無訂閱」＋前往方案頁
 * - 401 → 未登入
 */
import Link from "next/link";
import { useState } from "react";
import { ApiError, postBillingPortal, type BillingStatusValue } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { useBillingStatus, useMe } from "@/lib/hooks";

const c = COPY.billing;

export default function BillingPage() {
  const me = useMe();
  const loggedIn = !!me.data;
  const status = useBillingStatus({ enabled: loggedIn });
  const [error, setError] = useState<string | null>(null);
  const [noRecord, setNoRecord] = useState(false);
  const [pending, setPending] = useState(false);

  async function handleManage() {
    setError(null);
    setNoRecord(false);
    setPending(true);
    try {
      const { url } = await postBillingPortal();
      window.location.href = url;
    } catch (e) {
      // 409 = 後端查無 stripe customer：這不是失敗，是「還沒訂閱過」的正常狀態，
      // 導向方案頁而不是丟一個錯誤訊息給使用者。
      if (e instanceof ApiError && e.status === 409) setNoRecord(true);
      else setError(portalErrorText(e));
      setPending(false);
    }
  }

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
  if (status.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }

  // billing 未啟用（501）與其他載入失敗要分開：前者是「還沒開放」，後者才是錯誤。
  if (status.error) {
    const notEnabled = status.error instanceof ApiError && status.error.status === 501;
    return (
      <main className="page">
        <p className="eyebrow">{c.manageEyebrow}</p>
        <h1>{c.manageTitle}</h1>
        <p className="plan-notice">{notEnabled ? c.disabledNote : c.errors.loadFailed}</p>
      </main>
    );
  }

  const s: BillingStatusValue = status.data?.status ?? "none";
  const hasSubscription = s !== "none";

  return (
    <main className="page">
      <p className="eyebrow">{c.manageEyebrow}</p>
      <h1>{c.manageTitle}</h1>

      <div className="panel billing-panel">
        <dl className="billing-stats">
          <div className="billing-stat">
            <dt>{c.currentPlanLabel}</dt>
            <dd>{status.data?.active ? c.planNameActive : c.planNameFree}</dd>
          </div>
          <div className="billing-stat">
            <dt>{c.statusLabel}</dt>
            <dd>
              <span className={`chip ${s === "active" ? "chip-up" : "chip-neutral"}`}>
                {c.status[s]}
              </span>
            </dd>
          </div>
        </dl>

        {s === "past_due" && <p className="plan-notice">{c.pastDueNote}</p>}
        {error && <p className="plan-error" role="alert">{error}</p>}

        {hasSubscription && !noRecord ? (
          <>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleManage}
              disabled={pending}
            >
              {pending ? c.managing : c.manage}
            </button>
            <p className="hint billing-manage-note">{c.manageNote}</p>
          </>
        ) : (
          <NoSubscription />
        )}
      </div>
    </main>
  );
}

function portalErrorText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 501) return c.errors.notEnabled;
    if (e.kind === "auth") return COPY.common.notLoggedIn;
  }
  return c.errors.portalFailed;
}

function NoSubscription() {
  return (
    <div className="billing-empty">
      <p className="billing-empty-title">{c.noSubscription}</p>
      <p className="hint">{c.noSubscriptionNote}</p>
      <Link className="btn btn-primary" href="/pricing">{c.goPricing}</Link>
    </div>
  );
}
