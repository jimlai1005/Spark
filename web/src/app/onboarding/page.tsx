"use client";
/**
 * `/onboarding?strategy={slug}` — 統一 onboarding 四步精靈（Task 10，設計稿 §05）。
 *
 * 取代舊版分岔的兩個空白頁籤：一條路線、四個 step、可中斷續作（NOTE 11）。
 * 未登入或缺 `strategy` 查詢參數一律 redirect `/strategies`（NOTE 10）——不再有
 * 空白頁與返回按鈕。`strategy` 接受兩種形式：精選白名單 slug（`core`）與
 * `advanced:0x…`（Task 11 的 `/advanced` 頁產生，顯示為「進階模式（無背書）」）。
 *
 * 四步映射（詳見各 wizard/* 元件檔頭）：
 *   1 選擇策略 —— 進頁即完成態，持續顯示在 step 2-4 之上（`StepSelectStrategy`）。
 *   2 連接與授權 —— 既有 `StepSign`＋`StepDeposit`，完成條件＝伺服器
 *     `OnboardStatus.state === "READY"`（`StepConnect`）。
 *   3 風險限制 —— 投入比例／槓桿上限純本地調整；最大回撤自動停止 opt-in，
 *     開啟才走既有 `/api/me/risk` 簽章流程（`StepRiskLimits`，裁決 1）。
 *   4 費用與風險確認 —— FeeCalculator＋NOTE 12 三條 checkbox；送出時走既有
 *     `postLeaderSelect` 簽章流程完成「選定策略＝leader」（`StepConfirm`，
 *     見該檔檔頭關於此步為何補上 leader 選定的說明）。
 *
 * 斷點續作：wizard 進度（目前步、滑桿數值、step3 是否已 confirm）存
 * `localStorage.filet_onboarding`，**不存任何簽章內容**——已簽章的事實一律以
 * `/api/onboard/status` 為準（`lib/wizard.ts`）。
 */
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { useAccount } from "wagmi";
import { ReconnectGate } from "@/components/wizard/ReconnectGate";
import { StepConfirm } from "@/components/wizard/StepConfirm";
import { StepConnect } from "@/components/wizard/StepConnect";
import { StepRiskLimits, type StepRiskLimitsValues } from "@/components/wizard/StepRiskLimits";
import { StepSelectStrategy } from "@/components/wizard/StepSelectStrategy";
import type { SpotStranded } from "@/lib/api";
import { useCopy } from "@/lib/lang";
import { fmtAmount } from "@/lib/format";
import { useMe, useOnboardingStatus } from "@/lib/hooks";
import { getPublicStrategy, type PublicStrategyDetail } from "@/lib/publicApi";
import { clearWizardProgress, deriveStep, loadWizardProgress, saveWizardProgress } from "@/lib/wizard";

const ADVANCED_PREFIX = "advanced:";
const SCALE_DEFAULT = 25;
const DD_DEFAULT = 20;

/** `useSearchParams()` 在 build 期 prerender 需要 Suspense 邊界（Next.js
 * missing-suspense-with-csr-bailout），故 default export 只包一層 Suspense，
 * 頁面本體在 OnboardingInner。fallback 留空：本頁全 client 資料，無首繪內容可給。 */
export default function OnboardingPage() {
  return (
    <Suspense fallback={null}>
      <OnboardingInner />
    </Suspense>
  );
}

function OnboardingInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const strategyParam = searchParams.get("strategy") ?? "";
  const { isConnected, address: walletAddress } = useAccount();
  const me = useMe();
  const loggedIn = !!me.data;
  const status = useOnboardingStatus({ enabled: loggedIn, pollMs: 5000 });
  const COPY = useCopy();
  const c = COPY.wizard;
  const refetch = status.refetch;
  const refetchStatus = useCallback(() => void refetch(), [refetch]);

  const isAdvanced = strategyParam.startsWith(ADVANCED_PREFIX);
  const advancedAddress = isAdvanced ? strategyParam.slice(ADVANCED_PREFIX.length) : null;

  const [detail, setDetail] = useState<PublicStrategyDetail | null | undefined>(undefined);
  useEffect(() => {
    if (isAdvanced || !strategyParam) {
      setDetail(undefined);
      return;
    }
    let cancelled = false;
    setDetail(undefined);
    getPublicStrategy(strategyParam).then((r) => {
      if (!cancelled) setDetail(r);
    });
    return () => {
      cancelled = true;
    };
  }, [strategyParam, isAdvanced]);

  // NOTE 10：未登入或缺 strategy 參數一律 redirect /strategies（不在 render 期間
  // 呼叫 router.push，避免 React 警告；guard 用 effect）。
  useEffect(() => {
    if (me.isLoading) return;
    if (!me.data || !strategyParam) router.push("/strategies");
  }, [me.isLoading, me.data, strategyParam, router]);

  const leaderAddress = isAdvanced ? (advancedAddress || null) : (detail?.leader_address ?? null);
  const maxLeverage = !isAdvanced && detail?.max_leverage ? Number(detail.max_leverage) : null;

  const [scale, setScale] = useState(SCALE_DEFAULT);
  const [ddEnabled, setDdEnabled] = useState(false);
  const [ddPct, setDdPct] = useState(DD_DEFAULT);
  const [step3Confirmed, setStep3Confirmed] = useState(false);

  // 斷點續作：me/strategy 就緒後讀一次 localStorage；找不到相符進度就沿用查詢
  // 參數／預設值（URL 帶來的 scale/dd 只在「第一次進來、還沒有本地進度」時採用；
  // `lev` 查詢參數已移除——槓桿改唯讀資訊列，不再是使用者可調的值，Task 10b）。
  useEffect(() => {
    if (!me.data || !strategyParam) return;
    const saved = loadWizardProgress(me.data.address, strategyParam);
    if (saved) {
      setScale(saved.scale);
      setDdEnabled(saved.ddEnabled);
      setDdPct(saved.ddPct);
      setStep3Confirmed(saved.step3Confirmed);
      return;
    }
    const qScale = Number(searchParams.get("scale"));
    const qDd = searchParams.get("dd");
    if (Number.isFinite(qScale) && qScale > 0) setScale(qScale);
    if (qDd != null) {
      const n = Number(qDd);
      if (Number.isFinite(n) && n > 0) {
        setDdEnabled(true);
        setDdPct(n);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me.data?.address, strategyParam]);

  if (me.isLoading || !me.data || !strategyParam) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }

  const address = me.data.address;
  const s = status.data ?? null;
  const step = deriveStep({ status: s, step3Confirmed });
  const walletReady = isConnected && !!walletAddress;

  function persist(next: Partial<StepRiskLimitsValues & { step3Confirmed: boolean }>) {
    saveWizardProgress({
      address, strategy: strategyParam, scale, ddEnabled, ddPct, step3Confirmed,
      ...next,
    });
  }

  function handleStep3Next(values: StepRiskLimitsValues) {
    setScale(values.scale);
    setDdEnabled(values.ddEnabled);
    setDdPct(values.ddPct);
    setStep3Confirmed(true);
    persist({ ...values, step3Confirmed: true });
  }

  function handleDone() {
    clearWizardProgress();
    router.push("/dashboard");
  }

  const perpValue = s ? Number(s.perp_account_value) : NaN;
  const estimatedNotional = Number.isFinite(perpValue) && perpValue > 0
    ? perpValue * (scale / 100)
    : undefined;

  return (
    <main className="page">
      <StepSelectStrategy isAdvanced={isAdvanced} advancedAddress={advancedAddress} detail={detail} />

      {s?.spot_stranded != null && <SpotStrandedNotice info={s.spot_stranded} />}

      <nav aria-label="開通步驟">
        <ol className="wizard-steps">
          {c.stepNames.map((name, i) => {
            const n = i + 1;
            const done = n === 1 || n < step;
            return (
              <li key={name} className={n === step ? "is-current" : done ? "is-done" : ""}>
                <button type="button" className="step-btn" disabled={n > step}
                  aria-current={n === step ? "step" : undefined}>
                  <span className="step-num">{String(n).padStart(2, "0")}</span>
                  <span className="step-name">{name}</span>
                  <span className="step-check">✓</span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      <div className="wizard-panel">
        {step === 2 && !walletReady && <ReconnectGate />}
        {step === 2 && walletReady && s && (
          <StepConnect status={s} loginAddress={address} refetchStatus={refetchStatus} />
        )}
        {step === 2 && walletReady && !s && <p className="hint">{COPY.common.loading}</p>}

        {step === 3 && !walletReady && <ReconnectGate />}
        {step === 3 && walletReady && (
          <StepRiskLimits
            me={me.data}
            maxLeverage={maxLeverage}
            initial={{ scale, ddEnabled, ddPct }}
            onBack={() => router.push("/strategies")}
            onNext={handleStep3Next}
          />
        )}

        {step === 4 && !walletReady && <ReconnectGate />}
        {step === 4 && walletReady && (
          <StepConfirm
            me={me.data}
            leaderAddress={leaderAddress}
            estimatedNotional={estimatedNotional ?? scale * 100}
            onDone={handleDone}
          />
        )}
      </div>
    </main>
  );
}

/**
 * 「你有資金停在 spot 錢包」提示——沿舊版 onboarding/page.tsx 原樣保留（Task 10
 * 只換精靈的殼與步驟編排，這塊與哪一步無關，維持在步驟區塊之上顯示）。
 *
 * ⚠️⚠️ 本元件永遠不會有一顆「幫我劃轉」按鈕：spot → perp 是 user-signed action，
 * 需要客戶的**主鑰**才簽得動，我方結構上不持有主鑰（非託管不變量）。
 */
function SpotStrandedNotice({ info }: { info: SpotStranded }) {
  const COPY = useCopy();
  const c = COPY.wizard;
  return (
    <section className="spot-stranded" role="status" aria-label={c.spotStrandedTitle}>
      <p className="spot-stranded-title">{c.spotStrandedTitle}</p>
      <p>{c.spotStrandedBody}</p>
      <dl className="spot-stranded-facts">
        <div>
          <dt>{c.spotStrandedAmountLabel}</dt>
          <dd className="mono" title={info.usdc}>{fmtAmount(info.usdc)} USDC</dd>
        </div>
        <div>
          <dt>{c.spotStrandedThresholdLabel}</dt>
          <dd className="mono" title={info.threshold}>{fmtAmount(info.threshold)} USDC</dd>
        </div>
      </dl>
      <p className="hint">{c.spotStrandedManualNote}</p>
      <a className="btn btn-ghost" href={c.spotStrandedLinkHref}
         target="_blank" rel="noopener noreferrer">
        {c.spotStrandedLink}
      </a>
    </section>
  );
}
