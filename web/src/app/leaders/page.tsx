"use client";
/**
 * /leaders — 選擇跟單 leader（含錢包簽章授權流程）。
 *
 * ⭐ 本頁的核心需求是**誠實揭露**，不是版面：
 * 1. 本頁**只顯示規模與當下曝險**：帳戶淨值、名目部位總額、未實現損益、持倉數。
 *    欄位名一律沿用後端原名的直譯，前端**不換算、不改名**——本頁上任何看起來像
 *    報酬率的東西都是這裡憑空造出來的。
 *
 *    ⚠️ 這條是從「後端沒有任何績效指標」改寫來的，原說法自 commit a1bf002 起已失效：
 *    `/api/leaders` 現在**確實**回一個 `performance` 子物件（perpMonth／perpAllTime），
 *    只是本頁尚未接上。要接的人請先讀完下面這段——那是接法的硬性規則，不是背景說明。
 *
 *    ⭐⭐ `performance` 的結構性不變式：**該不該顯示，載體是「鍵存不存在」**
 *    （後端分級揭露見 filet/leader_perf.py；publicapi/app.py 的 `_leader_perf_public`
 *    刻意用 `if k in row` 投影而不是 `.get()`，就是為了不把缺席的鍵補成 null）：
 *      - 樣本不足 30 天 → **沒有** `twr`／`max_drawdown` 鍵（tier `pnl_only`）
 *      - 樣本不足 90 天 → **沒有** `annualized_return` 鍵（tier `window_return`）
 *      - `status="insufficient"` → 連 `cum_pnl` 都沒有
 *    **缺鍵的意思是「不該顯示」，不是「顯示為空」。** 用 `?? "—"`／`|| 0` 之類的預設值
 *    接上去，等於把後端刻意不給的東西在前端造出來——後端那道結構性防線就在那一行
 *    退化成「前端記得檢查」。要顯示就走 `disclosure_tier` 分支、或
 *    `"annualized_return" in w` 這種二元判斷，不要走預設值。
 *    lib/redline.test.ts 有一條斷言在擋這個寫法。
 *
 *    接上時必須同時揭露的兩件事：`max_drawdown` 由 15 分鐘取樣算出，**系統性低估**
 *    真實盤中回撤（取樣點之間更深的回撤看不見）；而 leader 的任何報酬率都是跟單者的
 *    **上界不是期望值**（第 3 點那句警語同樣適用於績效數字，不是只適用於規模數字）。
 * 2. 數字取自每日快照。`stats_available=false`（快照不可用）→ 一個數字都不畫；
 *    快照可用但**沒有時點**（day 與 generated_at 皆缺）→ 同樣一個數字都不畫。
 *    沒有時點的數字會被當成即時數字讀，比沒有數字危險（沿 /ops basis_unknown 的嚴格度）。
 * 3. leader 的數字是**上界不是期望值**（延遲、滑價、資金規模差異侵蝕跟單結果）——
 *    這句話貼著選擇按鈕，不是頁尾小字。
 * 4. 換 leader 有真實成本（收斂部位：平舊開新，付實際交易成本）且下一個 cycle 才生效，
 *    確認對話框寫明這兩件事後才進簽章流程。
 *
 * 授權流程沿 approvalFlow 的謹慎度（見 lib/leaderSelectFlow.ts）：伺服器產生原文 →
 * 錢包 personal_sign → **本地 recover 比對登入地址**（不符零網路請求）→ 原文原樣回送。
 *
 * 可用性閘門：一律由後端 `/api/billing/plans` 的 `shipped` 旗標驅動，前端不硬編、
 * 也不自行放行；拿不到方案目錄時 **fail closed**（不確定就不給送出）。
 * 至於「這個客戶有沒有權益」則是後端的授權判斷，前端不做（紅線 4）。
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSignMessage } from "wagmi";
import {
  ApiError,
  getBillingPlans,
  getLeaderSelectMessage,
  getLeaders,
  postLeaderSelect,
  type BillingPlansResp,
  type LeaderEntry,
  type LeaderSelectResp,
  type LeadersResp,
} from "@/lib/api";
import { COPY } from "@/lib/copy";
import { NO_VALUE, fmtAmount, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { runLeaderSelectFlow, type LeaderSelectFlowResult } from "@/lib/leaderSelectFlow";
import { recoverPersonalSigner } from "@/lib/sign";

const c = COPY.leaders;

/** 與後端 billing.py 的 `_F_MULTI_LEADER` 同源（改一邊要改兩邊）。 */
const MULTI_LEADER_KEY = "plans.feature.multiLeader";

/**
 * 付費功能閘門。⭐ `shipped` 是**功能層級**的事實（同一個功能在各方案的 shipped
 * 必為同值），所以只要任一方案宣告已推出就是已推出——這裡刻意不看 `included`：
 * 「這個客戶的方案含不含」是後端的授權判斷，前端插手等於自建第二套權益邏輯。
 * 拿不到目錄（載入中／失敗）→ `unknown` 且**不開放送出**：不確定就不放行。
 */
type Gate = { open: true } | { open: false; reason: "unshipped" | "unknown" };

function gateOf(plans: BillingPlansResp | undefined): Gate {
  if (!plans) return { open: false, reason: "unknown" };
  const shipped = plans.plans.some((p) =>
    p.features.some((f) => f.text_key === MULTI_LEADER_KEY && f.shipped),
  );
  return shipped ? { open: true } : { open: false, reason: "unshipped" };
}

type Phase =
  | { t: "idle" }
  | { t: "confirming"; leader: LeaderEntry }
  | { t: "running"; leader: LeaderEntry }
  | { t: "done"; resp: LeaderSelectResp }
  /** `dismissOnly`：此錯誤要求使用者停手回報，不得提供「重新操作」按鈕。 */
  | { t: "error"; message: string; detail?: string; dismissOnly?: boolean };

export default function LeadersPage() {
  const me = useMe();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<Phase>({ t: "idle" });

  const loggedIn = !!me.data;
  const leaders = useQuery<LeadersResp>({
    queryKey: ["leaders"],
    queryFn: getLeaders,
    enabled: loggedIn,
  });
  // 方案目錄是公開端點，與 /pricing 共用同一把快取（同 queryKey）。
  const plans = useQuery<BillingPlansResp>({
    queryKey: ["billing-plans"],
    queryFn: getBillingPlans,
  });
  const gate = gateOf(plans.data);

  async function runFlow(leader: LeaderEntry) {
    // 防禦性重擋一層：閘門關著時連流程都不啟動（按鈕已 disabled，這裡是第二道）。
    if (!gate.open || !me.data) return;
    setPhase({ t: "running", leader });
    const r = await runLeaderSelectFlow(
      {
        fetchMessage: () => getLeaderSelectMessage(leader.address),
        // 原文原樣進錢包：不加工、不重組（後端會用自己重建的版本驗簽）。
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postLeaderSelect,
      },
      // ⭐ expectedLeader＝使用者實際點的那一位。沒有這個，被打穿的 API 可以回一份
      // 指向別人的原文，而流程的其他每一關（recover、後端重建驗簽）都會照樣放行。
      { expectedSigner: me.data.address, expectedLeader: leader.address },
    );
    if (r.ok) setPhase({ t: "done", resp: r.resp });
    else setPhase({ t: "error", ...errorCopy(r) });
  }

  if (me.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }
  if (!loggedIn) {
    return <main className="page"><p>{COPY.common.notLoggedIn}</p></main>;
  }
  if (leaders.error instanceof ApiError && leaders.error.kind === "auth") {
    return <main className="page"><p>{COPY.common.notLoggedIn}</p></main>;
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>
      <p className="hint">{c.subtitle}</p>

      {/* ⭐ 誠信要求 1／2：在任何數字之前先講清楚這些數字是什麼、不是什麼。 */}
      <div className="panel leader-scope">
        <p className="leader-scope-title">{c.statsScopeTitle}</p>
        <p className="hint">{c.statsScopeBody}</p>
      </div>

      {/* 「目前跟隨中」沒有客戶端資料來源——說明缺口，不猜一個標記。 */}
      <div className="panel ops-notice">
        <p className="ops-notice-title">{c.currentUnknownTitle}</p>
        <p className="hint">{c.currentUnknownNote}</p>
      </div>

      {!gate.open && (
        <div className="panel plan-notice leader-gate">
          <p className="leader-gate-title">
            {c.gateTitle}
            <span className="plan-badge">{c.gateBadge}</span>
          </p>
          <p className="hint">{gate.reason === "unshipped" ? c.gateNote : c.gateUnknown}</p>
        </div>
      )}

      {phase.t === "done" && <DoneNotice resp={phase.resp} />}
      {phase.t === "error" && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-body">{phase.message}</p>
          {phase.detail && <p className="hint mono">{phase.detail}</p>}
          <button type="button" className="btn btn-ghost"
            onClick={() => setPhase({ t: "idle" })}>
            {phase.dismissOnly ? c.errors.dismiss : c.errors.retry}
          </button>
        </div>
      )}

      {leaders.isLoading ? (
        <p className="hint">{COPY.common.loading}</p>
      ) : leaders.error || !leaders.data ? (
        <p>{c.loadFailed}</p>
      ) : (
        <LeaderList
          data={leaders.data}
          gate={gate}
          busy={phase.t === "running"}
          onSelect={(leader) => setPhase({ t: "confirming", leader })}
        />
      )}

      {phase.t === "confirming" && (
        <ConfirmDialog
          leader={phase.leader}
          onCancel={() => setPhase({ t: "idle" })}
          onConfirm={() => runFlow(phase.leader)}
        />
      )}
      {phase.t === "running" && <p className="hint" role="status">{c.signing}</p>}
      <p className="hint">{COPY.common.nonCustodial}</p>
    </main>
  );
}

/**
 * 失敗文案。⭐ 每一條都必須讓使用者知道**現在的狀態**——「沒有被變更」要講出來：
 * 換 leader 有真實成本，使用者最需要知道的是「我剛才那下到底有沒有發生」。
 * 後端 detail 原樣附在第二行（供回報問題用），不做字串比對推測原因。
 */
function errorCopy(r: Extract<LeaderSelectFlowResult, { ok: false }>):
  { message: string; detail?: string; dismissOnly?: boolean } {
  const e = c.errors;
  if (r.kind === "wallet-rejected") return { message: e.walletRejected };
  if (r.kind === "signer-mismatch") return { message: e.signerMismatch };
  // ⭐ 伺服器指定的授權對象 ≠ 使用者所選：唯一一種「請停手並回報」的失敗，
  // 因此連按鈕都不給「重新操作」（見 copy.ts 的 leaderMismatch）。
  if (r.kind === "leader-mismatch") return { message: e.leaderMismatch, dismissOnly: true };
  const err = r.error;
  const detail = err instanceof ApiError ? err.detail : undefined;
  if (err instanceof ApiError) {
    if (err.kind === "auth") return { message: COPY.common.notLoggedIn, detail };
    if (err.status === 403) return { message: e.forbidden, detail };
    // 待簽原文端點的 400 只有一種語意：這位 leader 不可選（見 app.py）。
    if (err.status === 400 && r.kind === "message-failed") {
      return { message: e.notSelectable, detail };
    }
  }
  return {
    message: r.kind === "message-failed" ? e.messageFailed : e.submitFailed,
    detail,
  };
}

/** 成功。⭐ 生效時機與後果一律顯示**後端原文**（單一來源，不在前端另寫一份）。 */
function DoneNotice({ resp }: { resp: LeaderSelectResp }) {
  return (
    <div className="panel leader-done" role="status">
      <p className="leader-done-title">{c.doneTitle}</p>
      <p className="hint">{resp.effective_note}</p>
      <p className="hint">{resp.consequences}</p>
      <p className="hint mono" title={resp.leader_address}>
        {c.confirmLeaderLabel}: {shortAddr(resp.leader_address)}
      </p>
    </div>
  );
}

function LeaderList({ data, gate, busy, onSelect }: {
  data: LeadersResp;
  gate: Gate;
  busy: boolean;
  onSelect: (leader: LeaderEntry) => void;
}) {
  // ⭐ 兩道「不畫數字」的閘門，合成單一布林：快照不可用，或快照可用卻沒有時點。
  // 後者同樣危險——一份三天前的切面沒有時點就會被當成即時數字讀。
  const hasTimestamp = data.stats_day != null || data.stats_as_of != null;
  const statsShown = data.stats_available && hasTimestamp;

  return (
    <>
      {!data.stats_available ? (
        // 後端 note 原樣呈現，且本區塊不出現任何數字（沿 /ops basis_unknown 的嚴格度）。
        <div className="panel ops-notice leader-stats-notice">
          <p className="ops-notice-title">{c.statsUnavailableTitle}</p>
          <p className="hint">{data.note}</p>
          <p className="hint">{c.statsUnavailableNote}</p>
        </div>
      ) : !hasTimestamp ? (
        <div className="panel ops-notice leader-stats-notice">
          <p className="ops-notice-title">{c.statsNoTimestampTitle}</p>
          <p className="hint">{c.statsNoTimestampNote}</p>
        </div>
      ) : (
        <div className="panel leader-stats-meta">
          <p className="hint mono">
            {c.statsDayLabel}: {data.stats_day ?? NO_VALUE}
            {"　"}
            {c.statsAsOfLabel}: {data.stats_as_of ?? NO_VALUE}
          </p>
          <p className="hint">{c.statsStaleNote}</p>
        </div>
      )}

      {data.leaders.length === 0 ? (
        <p>{c.empty}</p>
      ) : (
        <div className="leader-grid">
          {data.leaders.map((leader) => (
            <LeaderCard
              key={leader.address}
              leader={leader}
              statsShown={statsShown}
              gate={gate}
              busy={busy}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </>
  );
}

function LeaderCard({ leader, statsShown, gate, busy, onSelect }: {
  leader: LeaderEntry;
  statsShown: boolean;
  gate: Gate;
  busy: boolean;
  onSelect: (leader: LeaderEntry) => void;
}) {
  // 該 leader 不在快照中 → 全欄 null。此時不畫網格（畫一排「—」看起來像有查到但是零）。
  const hasStats =
    leader.account_value != null || leader.total_ntl_pos != null ||
    leader.unrealized_pnl != null || leader.position_count != null;

  return (
    <section className="panel leader-card">
      <h2 className="leader-name">{leader.name}</h2>
      <p className="hint mono leader-addr" title={leader.address}>{shortAddr(leader.address)}</p>
      <p className="leader-desc">{leader.description}</p>

      {statsShown && (
        hasStats ? (
          <dl className="leader-stats">
            <LeaderStat label={c.cols.accountValue} hint={c.colHints.accountValue}
              value={fmtAmount(leader.account_value)} raw={leader.account_value} />
            <LeaderStat label={c.cols.totalNtlPos} hint={c.colHints.totalNtlPos}
              value={fmtAmount(leader.total_ntl_pos)} raw={leader.total_ntl_pos} />
            <LeaderStat label={c.cols.unrealizedPnl} hint={c.colHints.unrealizedPnl}
              value={fmtAmount(leader.unrealized_pnl)} raw={leader.unrealized_pnl} />
            <LeaderStat label={c.cols.positionCount} hint={c.colHints.positionCount}
              value={leader.position_count == null ? NO_VALUE : String(leader.position_count)} />
          </dl>
        ) : (
          <p className="hint">{c.leaderStatsMissing}</p>
        )
      )}

      {/* ⭐ 誠信要求 5：上界警語與按鈕同一個容器，不得被推到頁尾。 */}
      <div className="leader-action">
        <p className="leader-upper-bound">{c.upperBound}</p>
        <button
          type="button"
          className="btn btn-primary btn-block"
          disabled={!gate.open || busy}
          onClick={() => onSelect(leader)}
        >
          {gate.open ? c.select : c.gateBadge}
        </button>
      </div>
    </section>
  );
}

function LeaderStat({ label, hint, value, raw }: {
  label: string; hint: string; value: string; raw?: string | null;
}) {
  return (
    <div className="leader-stat">
      <dt title={hint}>{label}</dt>
      <dd className="mono" title={raw ?? undefined}>{value}</dd>
    </div>
  );
}

/**
 * ⭐ 誠信要求 6：確認對話框。成本（收斂部位＝平舊開新、付真實交易成本）與生效時機
 * （下一個 cycle，不是立即）都必須在**按下確認之前**看得到——這是使用者唯一一次
 * 能反悔的地方，之後就是簽名與真實成交。
 * 刻意不用 window.confirm：它塞不下這些內容，也無法被測試釘住。
 */
function ConfirmDialog({ leader, onCancel, onConfirm }: {
  leader: LeaderEntry;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="leader-dialog-backdrop">
      <div className="panel leader-dialog" role="dialog" aria-modal="true"
           aria-label={c.confirmTitle}>
        <p className="leader-dialog-title">{c.confirmTitle}</p>
        <p className="hint mono" title={leader.address}>
          {c.confirmLeaderLabel}: {leader.name}（{shortAddr(leader.address)}）
        </p>
        <p className="leader-dialog-body">{c.confirmCost}</p>
        <p className="leader-dialog-body">{c.confirmTiming}</p>
        <p className="leader-upper-bound">{c.upperBound}</p>
        <p className="hint">{c.confirmSignNote}</p>
        <div className="leader-dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            {c.confirmCancel}
          </button>
          <button type="button" className="btn btn-primary" onClick={onConfirm}>
            {c.confirmOk}
          </button>
        </div>
      </div>
    </div>
  );
}
