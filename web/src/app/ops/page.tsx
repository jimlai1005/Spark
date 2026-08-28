"use client";
/**
 * /ops — 營運儀表板（管理端；Header 刻意不放連結，與 /admin 同慣例）。
 * 授權是後端結構性職責（app.py 的 _require_admin）；本頁只負責在 403 時把
 * 「僅限管理員」講清楚，不做任何前端授權判斷。
 *
 * ⭐ 顯示原則：所有金額由後端以字串（Decimal 無損）送來，前端只在最後一刻 parse
 * 成顯示字串，不做任何金額算術——門檻比較（over_threshold）一律沿用後端結論，
 * 前端重算等於製造第二個真相來源（工程原則 1）。
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  ApiError,
  getOpsCustomers,
  getOpsHealth,
  getOpsRevenue,
  getOpsSubscriptions,
  getOpsTradeQuality,
  type OpsCustomerRow,
  type OpsCustomersResp,
  type OpsHealthResp,
  type OpsRevenueResp,
  type OpsSubscriptionEntry,
  type OpsSubscriptionsResp,
  type OpsTradeQualityResp,
} from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { fmtAmount, fmtRatioPct, NO_VALUE, shortAddr } from "@/lib/format";
import { HealthBlock } from "./HealthPanel";
import { TradeQualityBlock } from "./TradeQualityPanel";

const DAY_OPTIONS = [1, 7, 30] as const;
/**
 * 客戶表的時間窗模式。⭐ 預設 `accrued`：那是唯一與收入對帳同基準（可相減）的模式。
 * 1/7/30 天是自由檢視窗，與對帳窗必定錯開——切過去時畫面必須改口說「不可相減」。
 */
type RangeMode = "accrued" | (typeof DAY_OPTIONS)[number];
/** 與後端 /api/ops/revenue 的 threshold_pct 預設同值（比例，非百分比）。 */
const THRESHOLD_PCT = 0.01;

const c = COPY.ops;

export default function OpsPage() {
  const [mode, setMode] = useState<RangeMode>("accrued");
  const revenue = useQuery<OpsRevenueResp>({
    queryKey: ["ops-revenue"],
    queryFn: () => getOpsRevenue(THRESHOLD_PCT),
  });
  const customers = useQuery<OpsCustomersResp>({
    queryKey: ["ops-customers", mode],
    queryFn: () =>
      getOpsCustomers(mode === "accrued" ? { window: "accrued" } : { days: mode }),
  });
  const subscriptions = useQuery<OpsSubscriptionsResp>({
    queryKey: ["ops-subscriptions"],
    queryFn: getOpsSubscriptions,
  });
  /**
   * ⭐ 成交品質沿用**同一個** `mode`：三張表要能並排讀，窗口就得由同一個控制項決定。
   * 讓品質面板自己記一個窗口，等於在同一頁上放兩個不同的比較基準，而畫面上看不出
   * 它們不同——那正是那個 Critical bug 的形狀（工程原則 1）。
   */
  const tradeQuality = useQuery<OpsTradeQualityResp>({
    queryKey: ["ops-trade-quality", mode],
    queryFn: () =>
      getOpsTradeQuality(mode === "accrued" ? { window: "accrued" } : { days: mode }),
  });
  /** 系統健康沒有窗口參數：它是「此刻」的檢查，不與上面三張表共用比較窗口。 */
  const health = useQuery<OpsHealthResp>({
    queryKey: ["ops-health"],
    queryFn: getOpsHealth,
  });

  const errors = [revenue.error, customers.error, subscriptions.error,
                  tradeQuality.error, health.error];
  if (errors.some((e) => e instanceof ApiError && e.status === 403)) {
    return <main className="page"><p>{c.forbidden}</p></main>;
  }
  if (errors.some((e) => e instanceof ApiError && e.kind === "auth")) {
    return <main className="page"><p>{COPY.common.notLoggedIn}</p></main>;
  }
  if (revenue.isLoading && customers.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>

      {/* ⭐ 共用窗口標頭：顯示一次，明說下方兩張表同基準。放在兩張表之前而非各自表內，
          是為了讓「它們相同」變成一個可以被人肉核對的單一事實，而不是要讀者自己去
          比對兩處印出來的區間（那正是那個 Critical bug 得以潛伏一整天的原因）。 */}
      {mode === "accrued" && (
        <WindowBanner
          revenue={revenue.data}
          customers={customers.data}
          tradeQuality={tradeQuality.data}
        />
      )}

      <section aria-label="收入對帳">
        <h2 className="ops-section-title">{c.revenue.title}</h2>
        {revenue.error ? (
          <p className="ops-query-error">{errText(revenue.error)}</p>
        ) : revenue.data ? (
          <RevenueBlock data={revenue.data} />
        ) : (
          <p className="hint">{COPY.common.loading}</p>
        )}
      </section>

      {/* billing 未啟用（501）→ 整段不渲染。沿 /billing 與 Header 的既有慣例：
          「功能還沒開放」不是錯誤，也不該在營運頁留一塊永遠紅的區塊。 */}
      {!(subscriptions.error instanceof ApiError && subscriptions.error.status === 501) && (
        <section aria-label="訂閱對帳">
          <h2 className="ops-section-title">{c.subscriptions.title}</h2>
          <p className="hint">{c.subscriptions.note}</p>
          {subscriptions.error ? (
            <p className="ops-query-error">{errText(subscriptions.error)}</p>
          ) : subscriptions.data ? (
            <SubscriptionsBlock data={subscriptions.data} />
          ) : (
            <p className="hint">{COPY.common.loading}</p>
          )}
        </section>
      )}

      <section aria-label="每客戶損益">
        <h2 className="ops-section-title">{c.customers.title}</h2>
        {/* 文案隨模式切換：accrued＝可相減；days＝自由檢視窗、不可相減。
            同一句話不能同時對兩種模式成立，寫死任何一句都會在另一種模式下說謊。 */}
        <p className="hint">
          {mode === "accrued" ? c.customers.note : c.customers.daysNote}
        </p>
        <RangeTabs mode={mode} onSelect={setMode} />
        {customers.error ? (
          <p className="ops-query-error">{errText(customers.error)}</p>
        ) : customers.data ? (
          <CustomersBlock data={customers.data} />
        ) : (
          <p className="hint">{COPY.common.loading}</p>
        )}
      </section>

      {/* 成交品質：與上面兩張表同一個 mode（同一個比較窗口）。載入失敗只降級本區塊。 */}
      <section aria-label="成交品質">
        <h2 className="ops-section-title">{c.tradeQuality.title}</h2>
        <p className="hint">{c.tradeQuality.note}</p>
        <p className="hint">
          {mode === "accrued"
            ? c.tradeQuality.sameWindowNote
            : c.tradeQuality.daysWindowNote}
        </p>
        {tradeQuality.error ? (
          <p className="ops-query-error">
            {c.tradeQuality.loadFailed} {errText(tradeQuality.error)}
          </p>
        ) : tradeQuality.data ? (
          <TradeQualityBlock data={tradeQuality.data} />
        ) : (
          <p className="hint">{COPY.common.loading}</p>
        )}
      </section>

      {/* 系統健康：無窗口參數（「此刻」的檢查）。同樣各自降級。 */}
      <section aria-label="系統健康">
        <h2 className="ops-section-title">{c.health.title}</h2>
        {health.error ? (
          <p className="ops-query-error">
            {c.health.loadFailed} {errText(health.error)}
          </p>
        ) : health.data ? (
          <HealthBlock data={health.data} />
        ) : (
          <p className="hint">{COPY.common.loading}</p>
        )}
      </section>
    </main>
  );
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ---------- 共用比較窗口 ----------
/**
 * 從任一 ops 回應取出窗口兩端；算不出窗口的分支（歷史不足／basis_unknown）回 null。
 * ⭐ 只讀後端給的 window_start/window_end，不從 day／start／end 推——推導出來的窗口
 * 看起來一樣可信，卻可能與另一張表不同源，那正是這次 Critical 的成因（工程原則 1）。
 */
function windowOf(
  d: OpsRevenueResp | OpsCustomersResp | OpsTradeQualityResp | undefined,
): [string, string] | null {
  if (!d || !("window_start" in d)) return null;
  const { window_start: s, window_end: e } = d;
  return s != null && e != null ? [s, e] : null;
}

/**
 * 兩張表的共用窗口標頭 ＋ 不一致守衛。
 *
 * 後端已讓 `/api/ops/customers?window=accrued` 與 `/api/ops/revenue` 共用同一個窗口
 * 推導函式，但前端**不盲信**：真的收到兩組不同的窗口時以 role="alert" 大聲說「不可相減」。
 * 這是防止「健康帳戶被誤判 199 倍差異」那個 Critical 再度靜默潛伏的可視化防線——
 * 靜默錯位算得出數字，只是全錯（工程原則 3：安全關鍵的失敗必須大聲）。
 *
 * 任一邊算不出窗口 → 不渲染：各自的區塊已經寫明原因，這裡再猜一個窗口只會製造假確定感。
 */
function WindowBanner({ revenue, customers, tradeQuality }: {
  revenue: OpsRevenueResp | undefined;
  customers: OpsCustomersResp | undefined;
  tradeQuality: OpsTradeQualityResp | undefined;
}) {
  const w = c.window;
  const rw = windowOf(revenue);
  const cw = windowOf(customers);
  // 成交品質**可選**地納入本標頭：它算不出窗口時（basis_unknown／載入失敗）本標頭
  // 照舊只描述另外兩張表，由品質區塊自己說明為什麼它沒有數字。硬把一個不存在的
  // 窗口湊進來，只會讓標頭宣稱一件它並不知道的事。
  const tw = windowOf(tradeQuality);
  if (!rw || !cw) return null;

  const pairMismatch = rw[0] !== cw[0] || rw[1] !== cw[1];
  const tqMismatch = tw != null && (tw[0] !== rw[0] || tw[1] !== rw[1]);
  if (pairMismatch || tqMismatch) {
    return (
      <div className="ops-alert" role="alert">
        <p className="ops-alert-title">
          {pairMismatch ? w.mismatchTitle : w.tqMismatchTitle}
        </p>
        <p className="ops-alert-body">
          {pairMismatch ? w.mismatchBody : w.tqMismatchBody}
        </p>
        {/* 各組窗口並排列出：警告要能直接告訴人「差在哪」，不然還得自己去翻幾張表 */}
        <p className="hint mono ops-window">{w.revenueLabel}: {rw[0]} → {rw[1]}</p>
        <p className="hint mono ops-window">{w.customersLabel}: {cw[0]} → {cw[1]}</p>
        {tw && (
          <p className="hint mono ops-window">{w.tradeQualityLabel}: {tw[0]} → {tw[1]}</p>
        )}
      </div>
    );
  }
  return (
    <div className="panel ops-window-banner">
      <p className="ops-window-banner-title">{w.sharedTitle}</p>
      <p className="hint mono ops-window">{w.label}: {rw[0]} → {rw[1]}</p>
      <p className="hint">{w.sharedNote}</p>
      {/* 品質面板也在同一個窗口上時，把它併進**同一個**標頭：另外印一次窗口，
          等於又製造一個要人肉比對的地方，而那正是這個標頭當初要消滅的東西。 */}
      {tw && <p className="hint">{w.tradeQualityShared}</p>}
    </div>
  );
}

// ---------- 收入對帳 ----------
function RevenueBlock({ data }: { data: OpsRevenueResp }) {
  const r = c.revenue;
  // ⭐ 判別欄位先擋：歷史不足時後端不給數值欄，這裡也絕不退化成顯示 0
  //（把整段累積量當單日增量會造出假差額——顯示 0 同樣是假訊號的一種）。
  if (data.insufficient_accrued_history) {
    return (
      <div className="panel ops-notice">
        <p className="ops-notice-title">{r.insufficient}</p>
        <p className="hint">{r.insufficientNote}</p>
        <p className="hint mono">history_points: {data.history_points}</p>
      </div>
    );
  }
  // ⭐ 第二個判別欄位：窗口對不齊（快照缺 captured_at 或時刻非嚴格遞增）。
  // 這裡連 day／prev_day 都不顯示——任何看得像區間的東西都會被讀成「已對帳的區間」，
  // 而窗口正是這個分支唯一不知道的東西。只給後端寫好的原因說明。
  if (data.basis_unknown) {
    return (
      <div className="panel ops-notice">
        <p className="ops-notice-title">{r.basisUnknown}</p>
        <p className="hint">{data.note}</p>
        <p className="hint">{r.basisUnknownNote}</p>
      </div>
    );
  }

  const pctUnknown = data.discrepancy_pct == null;
  return (
    <>
      {data.over_threshold && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-title">{r.alertTitle}</p>
          <p className="ops-alert-body">{r.alertBody}</p>
        </div>
      )}
      <div className="panel">
        <p className="hint">{r.note}</p>
        {/* ⭐ 對帳三欄固定 4 位小數且附完整原值（title）：應收與實收若各自捨到 2 位，
            小於 0.01 的真實差額會在畫面上消失——那正是本頁要抓的訊號。 */}
        <dl className="ops-stats">
          <Stat label={r.attributed} value={fmtAmount(data.attributed, 4)} raw={data.attributed} />
          <Stat label={r.accruedDelta} value={fmtAmount(data.accrued_delta, 4)} raw={data.accrued_delta} />
          <Stat
            label={r.discrepancy}
            value={fmtAmount(data.discrepancy, 4)}
            raw={data.discrepancy}
            tone={data.over_threshold ? "bad" : "neutral"}
          />
          <Stat
            label={r.discrepancyPct}
            value={fmtRatioPct(data.discrepancy_pct, 2)}
            tone={data.over_threshold ? "bad" : "neutral"}
          />
          <Stat label={r.threshold} value={fmtRatioPct(data.threshold_pct, 2)} />
          <Stat label={r.rowsCounted} value={String(data.rows)} />
        </dl>
        {pctUnknown && <p className="hint">{r.pctUnavailable}</p>}
        {!data.over_threshold && <p className="hint">{r.ok}</p>}
        {/* ⭐ 實際比較窗口（快照時刻，非日曆日）必須攤在畫面上：窗口與 fills 取樣區間
            錯開曾是 Critical bug，且是靜默的——數字照樣算得出來，只是錯的。把區間顯示
            出來，同一類錯位下次由人眼一秒發現（可視化防線，不靠再加一條檢查清單）。 */}
        <p className="hint mono ops-window">
          {r.window}: {data.window_start} → {data.window_end}（{data.prev_day} → {data.day}）
        </p>
      </div>
    </>
  );
}

function Stat({ label, value, raw, tone = "neutral" }: {
  label: string; value: string; raw?: string | null; tone?: "neutral" | "bad";
}) {
  return (
    <div className="ops-stat">
      <dt>{label}</dt>
      <dd className={`mono${tone === "bad" ? " is-bad" : ""}`} title={raw ?? undefined}>{value}</dd>
    </div>
  );
}

// ---------- 訂閱對帳 ----------
/**
 * 本地 billing 表 vs Stripe。⭐ 清單順序＝危害順序（付了錢沒權益 > 漏財 > 狀態不符 >
 * 孤兒），不是後端回傳順序也不是字母序——admin 從上往下看就是從最該處理的看起。
 * 計數與清單長度皆由後端給，前端不重算（重算等於製造第二個真相來源，工程原則 1）。
 */
function SubscriptionsBlock({ data }: { data: OpsSubscriptionsResp }) {
  const s = c.subscriptions;
  const lists = [
    { key: "stripe_active_local_not", meta: s.lists.stripeActiveLocalNot, rows: data.stripe_active_local_not },
    { key: "local_active_stripe_not", meta: s.lists.localActiveStripeNot, rows: data.local_active_stripe_not },
    { key: "status_mismatch", meta: s.lists.statusMismatch, rows: data.status_mismatch },
    { key: "orphan_stripe", meta: s.lists.orphanStripe, rows: data.orphan_stripe },
  ];
  return (
    <>
      {/* ⭐ 截斷警告在最上方且用 role="alert"：樣本不完整時整個區塊的結論都不可信，
          尤其「Stripe 查無」可能是假漂移。放在清單下方等於讓人先看到結論才看到警告。 */}
      {data.truncated && (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{s.truncatedTitle}</p>
          <p className="ops-alert-body">{s.truncatedBody}</p>
        </div>
      )}
      <div className="panel">
        <dl className="ops-stats">
          <Stat label={s.counts.drift} value={String(data.drift_count)}
                tone={data.drift_count > 0 ? "bad" : "neutral"} />
          <Stat label={s.counts.inSync} value={String(data.in_sync_count)} />
          <Stat label={s.counts.local} value={String(data.local_count)} />
          <Stat label={s.counts.stripe} value={String(data.stripe_count)} />
          <Stat label={s.counts.superseded} value={String(data.superseded_count)} />
        </dl>
        {data.superseded_count > 0 && <p className="hint">{s.supersededNote}</p>}
        {data.drift_count === 0 && <p className="hint">{s.clean}</p>}
        <p className="hint">{s.detectOnly}</p>
      </div>
      {lists.map(({ key, meta, rows }) => (
        <DriftList key={key} title={meta.title} desc={meta.desc} rows={rows} />
      ))}
    </>
  );
}

/** 單一漂移清單。空清單也保留標題與「無」——標題本身就是「這一類已檢查過」的證據。 */
function DriftList({ title, desc, rows }: {
  title: string; desc: string; rows: OpsSubscriptionEntry[];
}) {
  const cols = c.subscriptions.cols;
  return (
    <div className="panel ops-drift">
      <h3 className="ops-drift-title">
        {title}
        <span className="ops-drift-count">{rows.length}</span>
      </h3>
      <p className="hint">{desc}</p>
      {rows.length === 0 ? (
        <p className="hint">{c.subscriptions.empty}</p>
      ) : (
        <table className="admin-table ops-table">
          <thead>
            <tr>
              <th scope="col">{cols.account}</th>
              <th scope="col">{cols.local}</th>
              <th scope="col">{cols.stripe}</th>
              <th scope="col">{cols.raw}</th>
              <th scope="col">{cols.subId}</th>
              <th scope="col">{cols.matchedBy}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e, i) => (
              <tr key={`${e.stripe_subscription_id ?? e.account_id ?? "row"}-${i}`}>
                <td className="mono">{e.account_id ?? NO_VALUE}</td>
                <td className="mono">{e.local_status ?? NO_VALUE}</td>
                <td className="mono">{e.stripe_status ?? NO_VALUE}</td>
                <td className="mono">{e.stripe_status_raw ?? NO_VALUE}</td>
                <td className="mono">{e.stripe_subscription_id ?? NO_VALUE}</td>
                <td className="mono">{e.matched_by ?? NO_VALUE}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------- 每客戶損益 ----------
/** ⭐ 「對帳窗口」排第一且為預設：同基準是常用路徑，不同基準才是需要刻意選擇的例外。 */
function RangeTabs({ mode, onSelect }: {
  mode: RangeMode; onSelect: (m: RangeMode) => void;
}) {
  const r = c.customers.ranges;
  const labels: Record<number, string> = { 1: r.d1, 7: r.d7, 30: r.d30 };
  const options: Array<{ value: RangeMode; label: string }> = [
    { value: "accrued", label: r.accrued },
    ...DAY_OPTIONS.map((d) => ({ value: d as RangeMode, label: labels[d] })),
  ];
  return (
    <div className="ops-range" role="group" aria-label="統計期間">
      <span className="hint">{c.customers.rangeLabel}</span>
      {options.map(({ value, label }) => (
        <button
          key={String(value)}
          type="button"
          className={`btn btn-secondary ops-range-btn${value === mode ? " is-active" : ""}`}
          aria-pressed={value === mode}
          onClick={() => onSelect(value)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function CustomersBlock({ data }: { data: OpsCustomersResp }) {
  const cols = c.customers.cols;
  // ⭐ 判別欄位先擋：窗口對不齊時整塊不出現任何數字（沿 RevenueBlock 的 basis_unknown
  // 分支，同樣的嚴格度）。後端在這個分支不給 customers——顯示空表會被讀成「今天沒客戶
  // 成交」，那是個看起來已對帳的假結論。連 manifest_errors 都不列（同 revenue 的作法）。
  if (data.window === "accrued" && data.basis_unknown) {
    return (
      <div className="panel ops-notice">
        <p className="ops-notice-title">{c.customers.basisUnknown}</p>
        <p className="hint">{data.note}</p>
        <p className="hint">{c.customers.basisUnknownNote}</p>
      </div>
    );
  }
  const rows = data.customers ?? [];
  const manifestErrors = data.manifest_errors ?? [];
  return (
    <>
      {manifestErrors.length > 0 && (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{c.customers.manifestErrors}</p>
          <ul className="ops-error-list">
            {manifestErrors.map((m, i) => <li key={i} className="mono">{m}</li>)}
          </ul>
        </div>
      )}
      {rows.length === 0 ? (
        <p>{c.customers.empty}</p>
      ) : (
        <div className="panel">
          <table className="admin-table ops-table">
            <thead>
              <tr>
                <th scope="col">{cols.account}</th>
                <th scope="col">{cols.address}</th>
                <th scope="col">{cols.fills}</th>
                <th scope="col">{cols.notional}</th>
                <th scope="col">{cols.builderFee}</th>
                <th scope="col">{cols.takerShare}</th>
                <th scope="col">{cols.accountValue}</th>
                <th scope="col">{cols.subscription}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => <CustomerRow key={row.account_id} row={row} />)}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/**
 * 跨客戶隔離的前端鏡射：某列 error 非空時標示該列，但**照樣渲染**它已取得的欄位，
 * 其他列完全不受影響（後端 ops.customer_pnl 已做隔離，前端不得把它退化成整頁失敗）。
 */
function CustomerRow({ row }: { row: OpsCustomerRow }) {
  const failed = !!row.error;
  return (
    <>
      <tr className={failed ? "ops-row-failed" : undefined}>
        <td className="mono">{row.account_id}</td>
        <td className="mono" title={row.user_address}>{shortAddr(row.user_address)}</td>
        <td className="mono">{row.fills ?? NO_VALUE}</td>
        <td className="mono" title={row.notional}>{fmtAmount(row.notional)}</td>
        <td className="mono" title={row.builder_fee}>{fmtAmount(row.builder_fee)}</td>
        <td className="mono">{fmtRatioPct(row.taker_share)}</td>
        <td className="mono" title={row.account_value ?? undefined}>{fmtAmount(row.account_value)}</td>
        <td className="mono">{row.subscription}</td>
      </tr>
      {failed && (
        <tr className="ops-row-failed">
          <td colSpan={8} className="ops-row-error">
            <span className="ops-row-error-label">{c.customers.rowError}</span>
            <span className="mono">{row.error}</span>
            <span className="hint"> {c.customers.rowErrorHint}</span>
          </td>
        </tr>
      )}
    </>
  );
}
