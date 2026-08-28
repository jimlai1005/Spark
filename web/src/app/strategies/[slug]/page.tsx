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
 * CAGR 與「起訖淨值」不是後端 `/api/public/strategies*` 直接給的欄位（後端
 * `build_metrics` 只公開 7 個比率型指標＋sample_count，`annualized_return`
 * 停在 leader_perf 內部、未經過策略卡的 insufficient 收斂契約）。本頁在既有
 * API 欄位上做兩個**純算術**的客戶端推導（過程見下方函式），而不是新增後端
 * 欄位——Task 9 檔案範圍只有前端三檔＋copy.ts，加後端欄位超出本 task 範圍：
 *   - CAGR：由 `total_return_pct` 與 `live_days`，用 methodology 揭露的 365 日
 *     慣例外推（`(1+r)^(365/live_days) - 1`）。與後端 `annualized_return` 概念
 *     一致，但係數/樣本閘門是本頁自己算的，不宣稱與後端內部欄位逐位元相同。
 *   - 起訖淨值：由 `methodology.initial_deposit_usd`（真實入金，來自鏈上
 *     `accountValueHistory` 首點）與 `equity_index` 首尾比值換算。
 * 任一輸入缺席／在數學上無定義（帳戶歸零）→ 該卡回「樣本不足」，不硬算。
 */
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { EquityCurve } from "@/components/EquityCurve";
import { fmtAmount, NO_VALUE, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import {
  getPublicStrategy,
  type PublicStrategyDetail,
  type PublicStrategyMethodology,
} from "@/lib/publicApi";
import { loginWithSiwe } from "@/lib/siwe";
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

/** insufficient → 佔位符；否則附尾綴（例如 %）。與 StrategyCard 的 metricText 同形狀。 */
function metricText(value: string | null, insufficient: boolean, suffix = ""): string {
  if (insufficient || value == null) return NO_VALUE;
  return `${value}${suffix}`;
}

/**
 * CAGR（年化外推）：由 `total_return_pct`＋`live_days`，用 365 日/年慣例
 * （與 methodology.annualization_days 對齊）算 `(1+r)^(365/live_days) - 1`。
 * 回傳 `null`＝樣本不足或數學上無定義（帳戶歸零，`1+r<=0`），呼叫端一律顯示
 * 「樣本不足」，不強行印出一個沒有意義的數字。
 */
export function computeCagrPct(
  totalReturnPct: string | null,
  insufficient: boolean,
  liveDays: number,
): string | null {
  if (insufficient || totalReturnPct == null || liveDays <= 0) return null;
  const r = Number(totalReturnPct) / 100;
  if (!Number.isFinite(r)) return null;
  const base = 1 + r;
  if (base <= 0) return null;
  const cagr = base ** (365 / liveDays) - 1;
  if (!Number.isFinite(cagr)) return null;
  return (cagr * 100).toFixed(2);
}

/**
 * 起訖淨值（USD）：`methodology.initial_deposit_usd`（真實入金起點）×
 * `equity_index` 首尾比值 → 起點／終點淨值。任一輸入缺席或首點為 0（無法取
 * 比值）→ `null`（樣本不足）。
 */
export function computeStartEndEquity(
  methodology: PublicStrategyMethodology,
  equityIndex: string[],
): { start: string; end: string } | null {
  const depositNum = methodology.initial_deposit_usd == null
    ? null : Number(methodology.initial_deposit_usd);
  if (depositNum == null || !Number.isFinite(depositNum) || equityIndex.length === 0) return null;
  const first = Number(equityIndex[0]);
  const last = Number(equityIndex[equityIndex.length - 1]);
  if (!Number.isFinite(first) || !Number.isFinite(last) || first === 0) return null;
  return {
    start: fmtAmount(String(depositNum), 0),
    end: fmtAmount(String(depositNum * (last / first)), 0),
  };
}

function formatUpdatedAt(epochSeconds: number): string {
  if (!epochSeconds) return NO_VALUE;
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

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
  const asOf = formatUpdatedAt(strategy.methodology.updated_at);

  const cagr = computeCagrPct(m.total_return_pct, m.total_return_pct_insufficient, strategy.live_days);
  const startEnd = computeStartEndEquity(strategy.methodology, strategy.equity_index);

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

  const metricCards = [
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
      key: "sharpe",
      label: c.metrics.sharpeLabel,
      value: metricText(m.sharpe, m.sharpe_insufficient),
      insufficient: m.sharpe_insufficient,
      note: m.sharpe_se_insufficient || m.sharpe_se == null
        ? "" : `±${m.sharpe_se}${c.metrics.sharpeNoteSuffix}`,
    },
    {
      key: "win_rate",
      label: c.metrics.winRateLabel,
      value: metricText(m.win_rate_pct, m.win_rate_pct_insufficient, "%"),
      insufficient: m.win_rate_pct_insufficient,
      note: `${c.metrics.winRateNotePrefix}${m.sample_count}${c.metrics.winRateNoteSuffix}`,
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
          <EquityCurve equityIndex={strategy.equity_index} />

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

          <CagrCard cagr={cagr} liveDays={strategy.live_days} copy={c.cagr} />

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

function CagrCard({ cagr, liveDays, copy }: {
  cagr: string | null;
  liveDays: number;
  copy: CagrCopy;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="card cagr-card">
      <div className="cagr-card-value-col">
        <div className="metric-card-label">{copy.heading}</div>
        <div className="mono cagr-card-value">
          {cagr == null ? copy.insufficientNote : `${cagr}%`}
        </div>
      </div>
      <div className="cagr-card-note-col">
        <button type="button" className="cagr-toggle" onClick={() => setOpen(!open)}>
          {open ? copy.toggleHide : copy.toggleShow}
        </button>
        {open && (
          <p className="cagr-note">
            {copy.notePrefix}
            {liveDays}
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
  const hasDeposit = methodology.initial_deposit_usd != null;
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
