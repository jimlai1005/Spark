"use client";
/**
 * `/strategies/[slug]` — 策略詳情頁（決策頁／白手套驗證門面，Task 9，設計稿 §04）。
 *
 * ⭐⭐ 錢包連接**第一次**在本頁出現（NOTE 07）。舊首頁的 SIWE 登入流程
 * （`useConnect`/`useSignMessage`/`loginWithSiwe`，見 git history `0eb12df~1:
 * web/src/app/page.tsx`）原封不動搬到這裡的跟單面板 CTA：右欄三個 slider
 * 未連錢包即可調整，只有最後一顆「連接錢包並繼續」需要錢包——這是刻意的
 * 產品順序（先設定完、再簽名，降低跳出）。已登入者點同一顆按鈕直接帶參數
 * 跳轉，不重複走一次連線/簽名。
 *
 * ⭐ M3 round3 Task 7（D5 數字一致性）：CAGR 不再是前端自算——後端
 * `/api/public/strategies/{slug}` 直接供給 `cagr_pct`（`sample_days<30`——
 * 2026-08-30 D15 裁決原 60 降為 30——時該鍵整個不回傳，結構性防呆），
 * 本頁只依「鍵是否存在」決定是否渲染 `CagrCard`，
 * 不再重算年化外推（見 `lib/strategyMetrics.ts` 檔頭，工程原則 1：同一個值
 * 只能有一個計算來源）。「起訖淨值」仍是前端用 `methodology.
 * initial_deposit_usd`（真實入金）與 `equity_index` 首尾比值換算（不是統計
 * 外推，是後端已供給兩個真實原始值的另一種呈現，詳見 `strategyMetrics.ts`）。
 *
 * ⭐ Task 7（R2-P0）指標收斂：8 張指標卡中只有總報酬／策略期間回撤／日勝率／
 * 最佳最差日維持個別小卡；Sharpe／Sortino／年化波動／起訖淨值在
 * `sample_days < sample_threshold` 時摺成一行文字，不逐格判斷（版面以整體
 * 門檻為準）——避免「8 格中 5 格是樣本不足、佔兩屏高度」（design R2 issue）。
 */
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { EquityCurve } from "@/components/EquityCurve";
import { fmtAmount, fmtUpdatedAtUtc, NO_VALUE, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import {
  getPublicStrategy,
  type PublicStrategyDetail,
  type PublicStrategyMethodology,
} from "@/lib/publicApi";
import { loginWithSiwe } from "@/lib/siwe";
import { computeStartEndEquity, metricText } from "@/lib/strategyMetrics";
import type { COPY_ZH, DeepString } from "@/lib/copy";

type CagrCopy = DeepString<typeof COPY_ZH.strategyDetail.cagr>;
type MethodologyCopy = DeepString<typeof COPY_ZH.strategyDetail.methodology>;

type ConnectPhase = "idle" | "connecting" | "signing";

const SCALE_MIN = 5;
const SCALE_MAX = 100;
const SCALE_DEFAULT = 25;
const DD_MIN = 5;
const DD_MAX = 50;
const DD_DEFAULT = 20;

export default function StrategyDetailPage() {
  const params = useParams<{ slug: string }>();
  const slug = params?.slug ?? "";
  const router = useRouter();
  const COPY = useCopy();
  const c = COPY.strategyDetail;

  // undefined＝載入中；null＝404／讀不到（兩者對使用者都是「這裡沒有可看的東西」）。
  const [strategy, setStrategy] = useState<PublicStrategyDetail | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    setStrategy(undefined);
    getPublicStrategy(slug).then((r) => {
      if (!cancelled) setStrategy(r);
    });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  // Task 17（SEO）：動態 slug 頁沒有 SSR metadata（client component 不能
  // export generateMetadata），策略載入後在 client 端補上 `%s｜Filet` 標題；
  // 載入中／404 維持 `strategies/layout.tsx` 給的預設標題，不覆蓋成空字串。
  useEffect(() => {
    if (strategy) document.title = `${strategy.name}｜Filet`;
  }, [strategy]);

  const me = useMe();
  const loggedIn = !!me.data;
  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<ConnectPhase>("idle");
  const [error, setError] = useState<string | null>(null);

  // 兩個 slider（NOTE 07：未連錢包即可調）。槓桿上限 Task 10b 起改唯讀資訊列
  // （無 per-user 簽章通道，見 StepRiskLimits.tsx 檔頭），不再是使用者可調的值。
  const [scalePct, setScalePct] = useState(SCALE_DEFAULT);
  const [ddEnabled, setDdEnabled] = useState(false); // 裁決 1：預設關閉
  const [ddPct, setDdPct] = useState(DD_DEFAULT);

  if (strategy === undefined) {
    return (
      <main className="page">
        <p className="hint">{c.loadingNote}</p>
      </main>
    );
  }
  if (strategy === null) {
    return (
      <main className="page">
        <div className="narrow">
          <h1>{c.notFoundTitle}</h1>
          <p>{c.notFoundBody}</p>
          <Link className="btn btn-primary" href="/strategies">
            {c.backToList}
          </Link>
        </div>
      </main>
    );
  }

  const m = strategy.metrics;
  const explorerHref = `https://app.hyperliquid.xyz/explorer/address/${strategy.leader_address}`;
  // ⭐ D5：`as_of`（perf 快照時間戳，列表／詳情同一份）取代 `methodology.updated_at`
  // （那是每次請求各自的 `now_fn()`，即使算同一份快照也會逐請求前進——正是
  // 「數字不一致」的根因之一）。
  const asOf = fmtUpdatedAtUtc(strategy.as_of ?? 0);
  const startEnd = computeStartEndEquity(strategy.methodology, strategy.equity_index, fmtAmount);
  const sampleInsufficient = strategy.sample_days < strategy.sample_threshold;

  function buildQuery(): string {
    const p = new URLSearchParams();
    p.set("strategy", strategy!.slug);
    p.set("scale", String(scalePct));
    if (ddEnabled) p.set("dd", String(ddPct));
    return p.toString();
  }

  async function handleCta() {
    setError(null);
    if (loggedIn) {
      router.push(`/onboarding?${buildQuery()}`);
      return;
    }
    try {
      let addr = address;
      let cid = chainId;
      if (!isConnected) {
        const injected = connectors[0];
        if (!injected) {
          setError(COPY.login.noWallet);
          return;
        }
        setPhase("connecting");
        const result = await connectAsync({ connector: injected });
        addr = result.accounts[0];
        cid = result.chainId;
      }
      if (!addr || !cid) {
        setError(COPY.login.noWallet);
        return;
      }
      setPhase("signing");
      await loginWithSiwe({
        address: addr,
        chainId: cid,
        signMessage: (message) => signMessageAsync({ message }),
      });
      router.push(`/onboarding?${buildQuery()}`);
    } catch (err) {
      const e = err as { name?: string; code?: number; message?: string } | undefined;
      const isRejected =
        e?.name === "UserRejectedRequestError"
        || e?.code === 4001
        || /reject|denied|cancel/i.test(String(e?.message ?? ""));
      setError(isRejected ? COPY.login.rejected : COPY.login.loginFailed);
    } finally {
      setPhase("idle");
    }
  }

  // ⭐ Task 7（主線程驗收修正）：大字只留總報酬／策略期間回撤／日勝率三張
  // （plan Task 7 第 1 條＋設計稿 R2 P0 原文「大字只留總報酬、最大回撤、日勝率」）。
  // Sharpe／Sortino／年化波動／起訖淨值／最佳最差日只在 `sampleInsufficient`
  // 為 false 時才併入同一個 metric-grid；為 true 時整組摺成下方一行文字
  // （不逐格判斷，見檔頭）。
  const headlineCards = [
    {
      key: "total_return",
      label: c.metrics.totalReturnLabel,
      value: metricText(m.total_return_pct, m.total_return_pct_insufficient, "%"),
      insufficient: m.total_return_pct_insufficient,
      note: c.metrics.totalReturnNote,
    },
    {
      key: "max_drawdown",
      label: c.metrics.maxDrawdownLabel,
      value: metricText(m.max_drawdown_pct, m.max_drawdown_pct_insufficient, "%"),
      insufficient: m.max_drawdown_pct_insufficient,
      note: c.metrics.maxDrawdownNote,
    },
    {
      key: "win_rate",
      label: c.metrics.winRateLabel,
      value: metricText(m.win_rate_pct, m.win_rate_pct_insufficient, "%"),
      insufficient: m.win_rate_pct_insufficient,
      note: `${c.metrics.winRateNotePrefix}${m.sample_count}${c.metrics.winRateNoteSuffix}`,
    },
  ];

  const collapsibleCards = [
    {
      key: "sharpe",
      label: c.metrics.sharpeLabel,
      value: metricText(m.sharpe, m.sharpe_insufficient),
      insufficient: m.sharpe_insufficient,
      note: m.sharpe_se_insufficient || m.sharpe_se == null
        ? "" : `±${m.sharpe_se}${c.metrics.sharpeNoteSuffix}`,
    },
    {
      key: "annualized_vol",
      label: c.metrics.annualizedVolLabel,
      value: metricText(m.annualized_vol_pct, m.annualized_vol_pct_insufficient, "%"),
      insufficient: m.annualized_vol_pct_insufficient,
      note: c.metrics.annualizedVolNote,
    },
    {
      key: "sortino",
      label: c.metrics.sortinoLabel,
      value: metricText(m.sortino, m.sortino_insufficient),
      insufficient: m.sortino_insufficient,
      note: c.metrics.sortinoNote,
    },
    {
      key: "best_worst",
      label: c.metrics.bestWorstLabel,
      value: `${metricText(m.best_day_pct, m.best_day_pct_insufficient)} / `
        + `${metricText(m.worst_day_pct, m.worst_day_pct_insufficient)}`,
      insufficient: m.best_day_pct_insufficient || m.worst_day_pct_insufficient,
      note: c.metrics.bestWorstNote,
    },
    {
      key: "start_end_equity",
      label: c.metrics.startEndEquityLabel,
      value: startEnd ? `${startEnd.start} → ${startEnd.end}` : NO_VALUE,
      insufficient: startEnd === null,
      note: c.metrics.startEndEquityNote,
    },
  ];

  const metricCards = sampleInsufficient ? headlineCards : [...headlineCards, ...collapsibleCards];

  const listable = strategy.listable;

  return (
    <main className="page strategy-detail-page">
      <div className="mono strategy-detail-breadcrumb">
        {c.breadcrumb} / <span>{strategy.name}</span>
      </div>

      <div className="strategy-detail-headrow">
        <div>
          <div className="strategy-detail-title-row">
            <h1>{strategy.name}</h1>
            <span className="pill follow-pill" data-state={strategy.status === "running" ? "following" : "paused"}>
              <span className="follow-pill-dot" aria-hidden="true" />
              {strategy.status === "running" ? c.runningPill : c.pausedPill}
            </span>
          </div>
          <div className="strategy-detail-sub">
            {strategy.tagline ? `${strategy.tagline} · ` : ""}
            {c.leaderPrefix}
            <a className="mono" href={explorerHref} target="_blank" rel="noreferrer">
              {shortAddr(strategy.leader_address)}
              {c.leaderLinkSuffix}
            </a>
          </div>
        </div>
        <div className="mono strategy-detail-asof">
          {c.asOfPrefix}
          {asOf}
          {c.sourceSuffix}
        </div>
      </div>

      <div className="strategy-detail-grid">
        <div className="strategy-detail-left">
          <EquityCurve
            equityIndex={strategy.equity_index}
            initialDepositUsd={strategy.methodology.initial_deposit_usd}
            startDate={strategy.methodology.start_date}
            endDate={strategy.methodology.end_date}
          />

          <div className="metric-grid">
            {metricCards.map((card) => (
              <div className="card metric-card" key={card.key}>
                <div className="metric-card-label">{card.label}</div>
                <div className="mono metric-card-value">
                  {card.insufficient ? c.metrics.insufficientLabel : card.value}
                </div>
                <div className="metric-card-note">{card.insufficient ? "" : card.note}</div>
              </div>
            ))}
          </div>

          {sampleInsufficient && (
            <p className="hint metric-collapsed-note">
              {c.metrics.insufficientGroupLabel}
              {c.metrics.insufficientGroupPrefix}
              {strategy.sample_days}
              {c.metrics.insufficientGroupMid}
              {strategy.sample_threshold}
              {c.metrics.insufficientGroupSuffix}
            </p>
          )}

          {/* ⭐ D5：後端 `sample_days<sample_threshold`（30，2026-08-30 D15 裁決
              原 60 降為 30）時 `cagr_pct` 鍵整個不存在——結構性防呆，前端只需
              判斷「有沒有這個值」，不必自己重算門檻。 */}
          {strategy.cagr_pct != null && (
            <CagrCard cagr={strategy.cagr_pct} sampleDays={strategy.sample_days} copy={c.cagr} />
          )}

          <MethodologyCard methodology={strategy.methodology} metrics={m} copy={c.methodology} />
        </div>

        <div className="card strategy-follow-panel">
          <div className="strategy-follow-panel-heading">{c.panel.heading}</div>

          <div className="strategy-follow-sliders">
            <div className="risk-field">
              <div className="risk-slider-row">
                <label htmlFor="scale-slider">{c.panel.scaleLabel}</label>
                <span className="mono risk-value">{scalePct}%</span>
              </div>
              <input
                id="scale-slider"
                type="range"
                className="risk-slider"
                min={SCALE_MIN}
                max={SCALE_MAX}
                step={1}
                value={scalePct}
                onChange={(e) => setScalePct(Number(e.target.value))}
              />
            </div>

            {/*
              ⭐ Task 10b（主線程裁決 2026-08-28）：槓桿上限改唯讀資訊列——沒有
              per-user 可簽的槓桿上限通道（`COPY_MAX_TARGET_LEVERAGE` 是引擎 env
              靜態值），slider 會讓客戶誤以為自己設定了什麼，故移除；`lev` 查詢
              參數同步移除（見 buildQuery）。
            */}
            <div className="risk-field">
              <div className="risk-slider-row">
                <span>{c.panel.leverageLabel}</span>
                <span className="mono risk-value">
                  {strategy.max_leverage ? `${strategy.max_leverage}x` : NO_VALUE}
                </span>
              </div>
              <p className="hint">
                {COPY.wizard.leverageInfoPrefix}
                {strategy.max_leverage ? `${strategy.max_leverage}x` : NO_VALUE}
                {COPY.wizard.leverageInfoSuffix}
              </p>
            </div>

            <div className="risk-field">
              <label className="risk-toggle">
                <input
                  type="checkbox"
                  checked={ddEnabled}
                  onChange={(e) => setDdEnabled(e.target.checked)}
                />
                <span>{c.panel.ddEnableLabel}</span>
              </label>
              <div className="risk-slider-row">
                <span>{c.panel.ddLabel}</span>
                <span className="mono risk-value">{ddEnabled ? `-${ddPct}%` : NO_VALUE}</span>
              </div>
              <input
                type="range"
                className="risk-slider"
                min={DD_MIN}
                max={DD_MAX}
                step={1}
                value={ddPct}
                disabled={!ddEnabled}
                onChange={(e) => setDdPct(Number(e.target.value))}
                aria-label={c.panel.ddLabel}
              />
              <p className="hint risk-toggle-help">{c.panel.ddDisabledNote}</p>
            </div>
          </div>

          <div className="inset strategy-follow-estimate">
            <div className="strategy-follow-estimate-row">
              <span>{c.panel.estDepositLabel}</span>
              <span className="mono">{c.panel.estDepositValue}</span>
            </div>
            <div className="strategy-follow-estimate-row">
              <span>{c.panel.builderFeeLabel}</span>
              <span className="mono">{c.panel.builderFeeValue}</span>
            </div>
            <div className="strategy-follow-estimate-row">
              <span>{c.panel.estMonthlyLabel}</span>
              <span className="mono">{c.panel.estMonthlyValue}</span>
            </div>
          </div>

          {listable ? (
            <>
              <button
                type="button"
                className="btn btn-primary btn-block"
                disabled={phase !== "idle"}
                onClick={handleCta}
              >
                {phase === "connecting"
                  ? c.panel.ctaConnecting
                  : phase === "signing"
                    ? c.panel.ctaSigning
                    : c.panel.cta}
              </button>
              {error && (
                <div className="sign-error">
                  <p>{error}</p>
                </div>
              )}
              <p className="hint strategy-follow-footnote">{c.panel.footnote}</p>
            </>
          ) : (
            <>
              <button type="button" className="btn btn-block" disabled data-testid="follow-panel-disabled">
                {c.panel.pendingCta}
              </button>
              <p className="hint strategy-follow-footnote">{c.panel.pendingNote}</p>
            </>
          )}
        </div>
      </div>
    </main>
  );
}

/**
 * ⭐ Task 7：呼叫端已用 `strategy.cagr_pct != null` 守門——本元件只在後端明確
 * 給出 CAGR 值時才被渲染（`sample_days<sample_threshold`〔30，2026-08-30 D15
 * 裁決原 60 降為 30〕時整個 `<CagrCard>` 不出現在 DOM，
 * 不再有「樣本不足」灰字佔位）。`cagr` 因此恆為非 null 字串。
 */
function CagrCard({ cagr, sampleDays, copy }: {
  cagr: string;
  sampleDays: number;
  copy: CagrCopy;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="card cagr-card">
      <div className="cagr-card-value-col">
        <div className="metric-card-label">{copy.heading}</div>
        <div className="mono cagr-card-value">{cagr}%</div>
      </div>
      <div className="cagr-card-note-col">
        <button type="button" className="cagr-toggle" onClick={() => setOpen(!open)}>
          {open ? copy.toggleHide : copy.toggleShow}
        </button>
        {open && (
          <p className="cagr-note">
            {copy.notePrefix}
            {sampleDays}
            {copy.noteSuffix}
          </p>
        )}
      </div>
    </div>
  );
}

function MethodologyCard({ methodology, metrics, copy }: {
  methodology: PublicStrategyMethodology;
  metrics: PublicStrategyDetail["metrics"];
  copy: MethodologyCopy;
}) {
  // 首個鏈上快照為 0（錢包晚於序列起點入金）時，「以 $0 起算」是誤導不是揭露 →
  // 整句省略，改由 rangePrefix 開頭（2026-08-29 真資料驗證發現）。
  const depositNum = Number(methodology.initial_deposit_usd);
  const hasDeposit = methodology.initial_deposit_usd != null
    && Number.isFinite(depositNum) && depositNum > 0;
  const hasRange = methodology.start_date != null && methodology.end_date != null
    && methodology.sample_count != null;
  const hasSharpe = !metrics.sharpe_insufficient && metrics.sharpe != null
    && !metrics.sharpe_se_insufficient && metrics.sharpe_se != null;
  const hasData = hasDeposit || hasRange || hasSharpe;

  return (
    <div className="inset methodology-card">
      <div className="methodology-heading">{copy.heading}</div>
      {hasData ? (
        <p className="methodology-body">
          {hasDeposit && (
            <>
              {copy.depositPrefix}
              {fmtAmount(methodology.initial_deposit_usd, 0)}
              {copy.depositSuffix}
            </>
          )}
          {hasRange && (
            <>
              {!hasDeposit && copy.rangePrefix}
              {methodology.sample_count}
              {copy.daysSuffix}
              {methodology.start_date} → {methodology.end_date}
              {copy.rangeSuffix}
              {" "}
            </>
          )}
          {hasSharpe && (
            <>
              {copy.sharpePrefix}
              {metrics.sharpe}
              {copy.sharpeSeInfix}
              {metrics.sharpe_se}
              {copy.sharpeSeSuffix}
              {metrics.sample_count}
              {copy.sampleSuffix}
              {" "}
            </>
          )}
          {copy.conventionPrefix}
          {methodology.annualization_days}
          {copy.conventionMid}
          {methodology.risk_free_rate}
          {copy.conventionSuffix}
        </p>
      ) : (
        <p className="methodology-body">{copy.unavailable}</p>
      )}
    </div>
  );
}
