"use client";
/**
 * /pricing — 方案選擇頁（**公開**：/api/billing/plans 不需 session，未登入照樣看得到）。
 *
 * ⭐ 誠實揭露是本頁的核心需求，不是樣式細節：
 * 後端每個功能有 included 與 shipped **兩個軸**。included 且 shipped=false 代表
 * 「這個方案含此功能，但功能還沒推出」——本頁必須把它標成「開發中」，且視覺上
 * 不得與可用功能相同（頁面測試釘住此條）。把兩軸合併成一個勾勾等於賣不存在的功能。
 *
 * 價格：price_display 為 null 時顯示「價格待定」——絕不退化成 0（0 會被讀成免費）。
 * 可購買性：一律沿用後端的 purchasable（= 有 price_id 且 billing 已啟用），
 * 前端不自行推導（第二個真相來源會與後端漂移，工程原則 1）。
 */
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import {
  ApiError,
  getBillingPlans,
  postBillingCheckout,
  type BillingFeature,
  type BillingPlan,
  type BillingPlansResp,
} from "@/lib/api";
import { COPY, copyForKey } from "@/lib/copy";
import { useMe } from "@/lib/hooks";

/** 與後端 billing.py plan_catalog 的方案 id 同源（免費層無 price_id，永遠不可購買）。 */
const FREE_PLAN_ID = "free";

const c = COPY.billing;

export default function PricingPage() {
  const me = useMe();
  const plans = useQuery<BillingPlansResp>({
    queryKey: ["billing-plans"],
    queryFn: getBillingPlans,
  });
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  // 未登入不是錯誤：本頁公開，只是訂閱按鈕改成「請先登入」。
  const loggedIn = !!me.data;

  async function handleSubscribe() {
    setError(null);
    setPending(true);
    try {
      const { checkout_url } = await postBillingCheckout();
      // 非冪等寫入：成功就離站，失敗一律交回使用者重按，前端不自動重試。
      window.location.href = checkout_url;
    } catch (e) {
      setError(checkoutErrorText(e));
      setPending(false);
    }
  }

  if (plans.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }
  if (plans.error || !plans.data) {
    return <main className="page"><p>{c.errors.loadFailed}</p></main>;
  }

  const { billing_enabled, plans: list } = plans.data;
  const anyUnshipped = list.some((p) => p.features.some((f) => f.included && !f.shipped));

  return (
    <main className="page">
      <p className="eyebrow">{c.pricingEyebrow}</p>
      <h1>{c.pricingTitle}</h1>
      <p className="hint">{c.pricingSubtitle}</p>

      {!billing_enabled && <p className="plan-notice">{c.disabledNote}</p>}
      {error && <p className="plan-error" role="alert">{error}</p>}

      <div className="plan-grid">
        {list.map((plan) => (
          <PlanCard
            key={plan.id}
            plan={plan}
            loggedIn={loggedIn}
            pending={pending}
            onSubscribe={handleSubscribe}
          />
        ))}
      </div>

      {/* 未推出功能的整體說明：卡片上的徽章之外再講一次，避免只掃價格的人漏掉。 */}
      {anyUnshipped && <p className="hint plan-unshipped-note">{c.unshippedNote}</p>}
    </main>
  );
}

function checkoutErrorText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409) return c.errors.alreadyActive;
    if (e.status === 501) return c.errors.notEnabled;
    if (e.kind === "auth") return COPY.common.notLoggedIn;
  }
  return c.errors.checkoutFailed;
}

function PlanCard({ plan, loggedIn, pending, onSubscribe }: {
  plan: BillingPlan;
  loggedIn: boolean;
  pending: boolean;
  onSubscribe: () => void;
}) {
  const isFree = plan.id === FREE_PLAN_ID;
  // 價格：null → 免費方案顯示「免費」，付費方案顯示「價格待定」（不是 0）。
  const price = plan.price_display ?? (isFree ? c.priceFree : c.priceTbd);
  const priceUnknown = !isFree && plan.price_display == null;

  return (
    <section className={`panel plan-card${plan.purchasable ? " is-purchasable" : ""}`}>
      <h2 className="plan-name">{copyForKey(plan.name_key)}</h2>
      <p className="plan-price">
        {price}
        {plan.price_display != null && <span className="plan-price-unit">{c.perMonth}</span>}
      </p>
      {priceUnknown && <p className="hint">{c.priceTbdNote}</p>}

      <ul className="plan-features">
        {plan.features.map((f) => <FeatureRow key={f.text_key} feature={f} />)}
      </ul>

      <PlanAction
        plan={plan}
        isFree={isFree}
        loggedIn={loggedIn}
        pending={pending}
        onSubscribe={onSubscribe}
      />
    </section>
  );
}

/**
 * 三種狀態必須看得出差別：
 * - 含且已推出 → 勾、正常字色
 * - 含但**未推出** → 不給勾（用 middot 標記）＋「開發中」徽章＋弱化字色
 * - 未含 → 叉、灰階
 */
function FeatureRow({ feature }: { feature: BillingFeature }) {
  const text = copyForKey(feature.text_key);
  if (!feature.included) {
    return (
      <li className="plan-feature is-excluded">
        <span className="plan-mark" aria-label={c.excludedAria}>✕</span>
        <span>{text}</span>
      </li>
    );
  }
  if (!feature.shipped) {
    return (
      <li className="plan-feature is-unshipped">
        <span className="plan-mark" aria-hidden="true">·</span>
        <span>{text}</span>
        <span className="plan-badge">{c.unshippedBadge}</span>
      </li>
    );
  }
  return (
    <li className="plan-feature">
      <span className="plan-mark is-ok" aria-label={c.includedAria}>✓</span>
      <span>{text}</span>
    </li>
  );
}

function PlanAction({ plan, isFree, loggedIn, pending, onSubscribe }: {
  plan: BillingPlan;
  isFree: boolean;
  loggedIn: boolean;
  pending: boolean;
  onSubscribe: () => void;
}) {
  // 免費層沒有可按的動作——這裡刻意不顯示停用的「即將開放」按鈕：
  // 免費層現在就能用，把它畫成「即將開放」是反向的誤導。
  if (isFree) {
    return <p className="hint plan-action-note">{c.freePlanAction}</p>;
  }
  if (!plan.purchasable) {
    return (
      <button type="button" className="btn btn-secondary btn-block" disabled>
        {c.comingSoon}
      </button>
    );
  }
  if (!loggedIn) {
    return (
      <Link className="btn btn-primary btn-block" href="/">
        {c.loginFirst}
      </Link>
    );
  }
  return (
    <button
      type="button"
      className="btn btn-primary btn-block"
      onClick={onSubscribe}
      disabled={pending}
    >
      {pending ? c.subscribing : c.subscribe}
    </button>
  );
}
