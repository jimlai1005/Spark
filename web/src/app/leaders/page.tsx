"use client";
/**
 * /leaders — 選擇跟單 leader（含錢包簽章授權流程）。
 *
 * ⭐ 本頁的核心需求是**誠實揭露**，不是版面：
 * 1. 卡片分成**兩區，視覺上分開**：上區是規模與當下曝險（帳戶淨值、名目部位總額、
 *    未實現損益、持倉數），下區才是績效（報酬率與回撤）。欄位名一律沿用後端原名的
 *    直譯，前端**不換算、不改名**——把兩區混在一起，使用者會把規模讀成績效，
 *    而那個誤讀在畫面上完全看不出來（後端也是基於同一理由把 `performance` 放在
 *    獨立子物件，見 app.py `_LEADER_PERF_WINDOWS` 上方註解）。
 *
 *    ⭐⭐ `performance` 的結構性不變式（2026-07-19 揭露模型改版後）：
 *    **數字與它的警示標記同生共死**。舊模型用「鍵存不存在」承載分級揭露（不足
 *    30 天就沒有 `twr` 鍵），改版後薄資料的數字**照樣回傳**，警示改由一組
 *    `*_insufficient_data` 標記承載（見 filet/leader_perf.py 檔頭）。
 *      - `twr`／`max_drawdown`：恆存在，帶 `*_insufficient_data`（< 30 天為 true）
 *      - `annualized_return`：帶不足標記 ＋ `..._extrapolated_from_days`（實際天數）；
 *        三鍵**一起**缺席的唯一情形是年化在數學上無定義（`1+TWR <= 0`）
 *      - `status="insufficient"` → 連 `cum_pnl` 都沒有
 *    ⚠️ 最危險的失敗方向因此換了邊：不再是「造出後端沒給的數字」，而是
 *    **「把數字送出去而沒有它的警示」**——一個 7 天 +3% 的 leader 年化是 365%，
 *    數字本身完全正常，少掉標記就完全看不出它把雜訊放大了 52 倍。
 *    ⭐ 本頁因此**不在 JSX 裡自己判斷成對與否**：判定全部收斂在 lib/leaderPerf.ts
 *    的 `perfView()`（數字 ∩ 標記，缺一就 fail closed），JSX 只讀 `view.shown` 上
 *    **存在的**鍵，且渲染數字的地方必須在同一個 scope 讀到它的標記——
 *    lib/redline.test.ts 的兩條結構性斷言在擋這兩件事。
 *
 *    與績效數字同時揭露、且**貼著數字**（不是頁尾小字）的三件事：
 *      - `max_drawdown` 由 15 分鐘取樣算出，**系統性低估**真實盤中回撤——貼著 MDD；
 *      - leader 的任何報酬率都是跟單者的**上界不是期望值**——貼著績效區；
 *      - 資料充足度（`covered_days`／`sample_count`）——與數字同時出現：
 *        涵蓋 31 天與涵蓋 2 年的 TWR 在畫面上長得一模一樣，可信度卻天差地遠。
 *
 *    ⭐ 刻意**不提供依績效排序**：排序等同於本頁在替使用者推薦，而這些數字的限制
 *    （上界、低估的 MDD、可能只有 31 天的樣本）還不足以支撐推薦。清單順序沿用
 *    後端給的順序。要加排序的人請先想清楚這一點，並讓它是使用者主動選擇＋附警語。
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
import { useRef, useState } from "react";
import { useSignMessage } from "wagmi";
import {
  ApiError,
  getBillingPlans,
  getLeaderPreview,
  getLeaderSelectMessage,
  getLeaders,
  postLeaderSelect,
  type BillingPlansResp,
  type LeaderEntry,
  type LeaderPreviewResp,
  type LeaderSelectResp,
  type LeadersResp,
} from "@/lib/api";
import { runCustomLeaderPreview, validateCustomLeaderInput } from "@/lib/customLeader";
import { ActivationNotice } from "@/components/ActivationNotice";
import { COPY } from "@/lib/copy";
import { NO_VALUE, fmtAmount, fmtRatioPct, shortAddr } from "@/lib/format";
import {
  MIN_DAYS_FOR_ANNUALIZATION,
  MIN_DAYS_FOR_RETURN,
  daysShortOf,
  fmtDays,
  perfView,
  resolvePerfNotes,
  type LeaderPerfView,
  type PerfNotesResolved,
} from "@/lib/leaderPerf";
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

/**
 * 這次到底會不會畫績效——⭐ **單一判定**，揭露文案與實際渲染共用同一個答案。
 * 兩邊各算一次是工程原則 1 的反例：文案說「下半部是績效」、下半部卻因為缺時點
 * 而什麼都沒畫，兩邊各自都「對」，湊在一起就是假話。
 *
 * fail closed：`performance_available` 不是明確的 true（載入中、舊版後端沒這個
 * 欄位、後端說沒有）→ 一律當作沒有績效。時點條件與規模欄位同源：績效與規模出自
 * 同一份每日快照，沒有時點的報酬率一樣會被當成即時數字讀。
 */
function perfVisible(data: LeadersResp | undefined): boolean {
  if (!data) return false;
  const hasTimestamp = data.stats_day != null || data.stats_as_of != null;
  return data.performance_available === true && hasTimestamp;
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
  // ⭐ 揭露文案必須描述**畫面上實際有什麼**：顯示績效時還說「本頁沒有報酬率、
  // 沒有回撤」，那句話本身就成了頁面上第一個不誠實的元素。判定與實際渲染共用
  // `perfVisible`（同一個答案，不各算一次）。
  const perfShown = perfVisible(leaders.data);

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
        <p className="leader-scope-title">
          {perfShown ? c.statsScopeTitleWithPerf : c.statsScopeTitle}
        </p>
        <p className="hint">{perfShown ? c.statsScopeBodyWithPerf : c.statsScopeBody}</p>
      </div>

      {/* 「目前跟隨中」沒有客戶端資料來源——說明缺口，不猜一個標記。 */}
      <div className="panel ops-notice">
        <p className="ops-notice-title">{c.currentUnknownTitle}</p>
        <p className="hint">{c.currentUnknownNote}</p>
      </div>

      {/* 未活化就先說——否則使用者會一路簽到最後才撞上「查無 follower」。 */}
      <ActivationNotice />

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

      {/* ⭐ 自訂 leader：與精選清單**互相獨立**——快照或清單壞掉不影響自訂輸入。 */}
      <CustomLeaderSection
        gate={gate}
        busy={phase.t === "running"}
        onSelect={(leader) => setPhase({ t: "confirming", leader })}
      />

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
  // ⭐ 績效自己一道閘，但**同樣受時點閘管**（見 perfVisible）：刻意不把兩個
  // available 旗標合併（後端明寫不可合併）——是 `performance_available` ∩ 時點，
  // 不是 ∩ `stats_available`。與上方揭露文案共用同一個判定，不各算一次。
  const perfShown = perfVisible(data);
  // 後端明確說「沒有績效」才畫原因；欄位整個缺席（舊版後端不談績效）則整區不出現，
  // 兩種情況的共通點是**一個數字都不畫**。
  const perfUnavailable = data.performance_available === false;
  // 警語在此一次解析（後端原文優先，空白或缺席才用前端等義文案）——顯示層不再自己
  // 判斷警語有沒有；⭐ 數字缺了不顯示、警語缺了要補上，見 resolvePerfNotes。
  const notes = resolvePerfNotes(data.performance_notes, {
    basis: c.perf.basisFallback,
    upperBound: c.perf.upperBoundFallback,
    maxDrawdown: c.perf.mddFallback,
    sufficiency: c.perf.sufficiencyFallback,
  });
  // 窗的顯示順序以後端清單為準（單一來源）；缺席時退回後端 `_LEADER_PERF_WINDOWS`
  // 的內容（改一邊要改兩邊）。
  const windows = Array.isArray(data.performance_windows)
    ? data.performance_windows
    : DEFAULT_PERF_WINDOWS;

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

      {/* ⭐ 誠信要求：績效不可得 → 說原因，且整段零數字（沿 stats_available 的嚴格度）。 */}
      {perfUnavailable && (
        <div className="panel ops-notice leader-perf-notice">
          <p className="ops-notice-title">{c.perf.unavailableTitle}</p>
          <p className="hint">{c.perf.unavailableBody}</p>
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
              perfShown={perfShown}
              perfWindows={windows}
              perfNotes={notes}
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

function LeaderCard({
  leader, statsShown, perfShown, perfWindows, perfNotes, gate, busy, onSelect,
}: {
  leader: LeaderEntry;
  statsShown: boolean;
  perfShown: boolean;
  perfWindows: string[];
  perfNotes: PerfNotesResolved;
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

      {/* ---- 上區：規模與當下曝險（資產負債切面，不是績效） ---- */}
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

      {/* ---- 下區：績效（報酬率與回撤）。與上區之間有明確分隔，不共用容器。 ---- */}
      {perfShown && (
        <LeaderPerfBlock leader={leader} windows={perfWindows} notes={perfNotes} />
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

/** HL 官方 leaderboard（外部研究入口；spec 的 hybrid UI 決策）。 */
const HL_LEADERBOARD_URL = "https://app.hyperliquid.xyz/leaderboard";

/**
 * 自訂 leader 區塊的預覽狀態機（仿本頁 Phase 的 useState 慣例）。
 * `rejected`＝准入分類碼對應的拒絕（semantic，使用者改輸入）；
 * `failed`＝transport／未知錯誤（可重按查詢；查詢唯讀，重按零成本）。
 */
type CustomPhase =
  | { t: "idle" }
  | { t: "checking" }
  | { t: "previewed"; preview: LeaderPreviewResp }
  | { t: "rejected"; message: string; detail?: string }
  | { t: "failed"; message: string; detail?: string };

/** 預覽 → 既有選擇流程用的 LeaderEntry。⭐ 位址取伺服器回聲（已通過回聲比對）。 */
function customEntryOf(preview: LeaderPreviewResp): LeaderEntry {
  return {
    address: preview.address,
    name: c.custom.entryName,
    description: "",
    account_value: preview.account_value,
    total_ntl_pos: null,
    unrealized_pnl: null,
    position_count: preview.position_count,
  };
}

/**
 * ⭐ 自訂 leader：任意位址輸入（2026-07-27 spec）。
 *
 * 閘門順序（每一道未過就零後續動作）：
 * 1. 本地格式驗證（viem isAddress，strict:false——與後端准入同一把尺，見
 *    lib/customLeader.ts 檔頭）→ 不合法連「查詢」都按不下去；
 * 2. 後端准入預覽（格式／自跟／鏈上存在＋精選優先權）→ 被拒按 reason 分類碼
 *    顯示對應文案，**不對人話字串比對**；
 * 3. 專屬風險聲明 checkbox（純前端閘門，仿 wizard AML attestation）→ 未勾不得送出；
 * 4. 之後進**既有**確認框與簽章流程（ConfirmDialog → runLeaderSelectFlow）——
 *    自訂 leader 的授權安全性與精選完全同一條路，不因來源不同而打折。
 *
 * 誠實揭露：預覽卡明說是「查詢當下的鏈上切面」（confirm 沒貼錯位址用），
 * 無績效快照 → 說「沒有資料」，不畫 0、不畫錯誤（沿本頁缺值不畫 0 的一貫嚴格度）。
 *
 * 輸入一變即整組重置（預覽、錯誤、勾選）＋ in-flight 序號防護：舊位址的預覽卡
 * 留在新位址旁邊，是本區塊最會騙過本人的顯示錯誤（工程原則 1：畫面上的資料
 * 必須與輸入框裡的位址同源）。
 */
function CustomLeaderSection({ gate, busy, onSelect }: {
  gate: Gate;
  busy: boolean;
  onSelect: (leader: LeaderEntry) => void;
}) {
  const cc = c.custom;
  const [input, setInput] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [cPhase, setCPhase] = useState<CustomPhase>({ t: "idle" });
  // in-flight 防護：查詢途中輸入變了，回來的結果屬於舊位址，一律丟棄。
  const seq = useRef(0);

  const check = validateCustomLeaderInput(input);
  const showFormatError = !check.ok && !check.empty;

  function onInputChange(v: string) {
    setInput(v);
    seq.current += 1;
    setAgreed(false);
    setCPhase({ t: "idle" });
  }

  async function runPreview() {
    if (!check.ok) return; // 按鈕已 disabled，這裡是第二道（沿 runFlow 的防禦慣例）
    const mySeq = ++seq.current;
    setCPhase({ t: "checking" });
    const r = await runCustomLeaderPreview({ fetchPreview: getLeaderPreview }, input);
    if (seq.current !== mySeq) return; // 結果屬於舊輸入
    if (r.ok) {
      setAgreed(false); // 每一份新預覽都要重新勾選（聲明綁的是「這個位址」）
      setCPhase({ t: "previewed", preview: r.preview });
    } else if (r.kind === "rejected") {
      setCPhase({ t: "rejected", message: cc.reasons[r.reason], detail: r.detail });
    } else if (r.kind === "address-mismatch") {
      setCPhase({ t: "failed", message: cc.echoMismatch });
    } else {
      const detail = r.error instanceof ApiError ? r.error.detail : undefined;
      setCPhase({ t: "failed", message: cc.previewFailed, detail });
    }
  }

  return (
    <section className="panel leader-custom">
      <p className="leader-custom-title">{cc.title}</p>
      <p className="hint">{cc.subtitle}</p>
      <p className="hint">
        {cc.leaderboardLabel}
        {/* noopener：外部站不得拿到 window.opener（noreferrer 順帶擋 referrer） */}
        <a href={HL_LEADERBOARD_URL} target="_blank" rel="noopener noreferrer">
          {cc.leaderboardLinkText}
        </a>
      </p>

      <label className="capital-field" htmlFor="custom-leader-address">
        <span className="capital-field-label">{cc.inputLabel}</span>
        <input
          id="custom-leader-address"
          className="capital-input mono"
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder={cc.inputPlaceholder}
          value={input}
          // 原樣存 state（沿 /capital：輸入框裡的字被程式改掉，最容易騙過本人）；
          // 小寫正規化只發生在送後端前（lib/customLeader.ts）。
          onChange={(e) => onInputChange(e.target.value)}
        />
        <span className="hint">{cc.inputHint}</span>
      </label>
      {showFormatError && (
        <p className="capital-input-error" role="alert">{cc.formatError}</p>
      )}

      <button
        type="button"
        className="btn btn-ghost"
        disabled={!check.ok || cPhase.t === "checking" || busy}
        onClick={() => void runPreview()}
      >
        {cPhase.t === "checking" ? cc.checking : cc.check}
      </button>
      {cPhase.t === "checking" && <p className="hint" role="status">{cc.checking}</p>}

      {(cPhase.t === "rejected" || cPhase.t === "failed") && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-body">{cPhase.message}</p>
          {cPhase.detail && <p className="hint mono">{cPhase.detail}</p>}
        </div>
      )}

      {cPhase.t === "previewed" && (
        <div className="leader-custom-preview">
          <p className="leader-custom-title">{cc.previewTitle}</p>
          <p className="hint mono leader-addr" title={cPhase.preview.address}>
            {shortAddr(cPhase.preview.address)}
          </p>
          {/* ⭐ 鏈上無活動（exists=false）→ 警示但**不擋**（2026-07-27 裁決）：leader
              尚未進場時客戶可先完成配置，進場後引擎自動開始跟。不是錯誤、不停送出。 */}
          {!cPhase.preview.exists && (
            <div className="ops-alert" role="alert">
              <p className="ops-alert-body">{cc.noActivityWarning}</p>
            </div>
          )}
          {/* ⭐ accepting_new=false（例行下架）→ 警示但**不擋**（2026-07-27 拆旗標）：
              只擋新客的行銷狀態，引擎仍照跟；客戶堅持要跟就放行（使用者裁決）。
              enabled=false 的安全撤銷早在准入層硬擋（leader_disabled），到不了這裡。 */}
          {!cPhase.preview.accepting_new && (
            <div className="ops-alert" role="alert">
              <p className="ops-alert-body">{cc.notAcceptingNewWarning}</p>
            </div>
          )}
          {cPhase.preview.already_listed && (
            <p className="leader-custom-listed">
              <span className="plan-badge">{cc.alreadyListedBadge}</span>
            </p>
          )}
          <p className="hint">{cc.previewNote}</p>
          <dl className="leader-stats">
            <LeaderStat label={cc.previewAccountValue} hint={cc.previewAccountValueHint}
              value={fmtAmount(cPhase.preview.account_value)}
              raw={cPhase.preview.account_value} />
            <LeaderStat label={cc.previewPositionCount} hint={cc.previewPositionCountHint}
              value={String(cPhase.preview.position_count)} />
          </dl>

          {/* ⭐ 無績效快照＝「沒有資料」的狀態區塊——不是錯誤、不是零、不是留白。 */}
          <div className="leader-perf-state leader-custom-noperf">
            <p className="leader-perf-state-title">{cc.noPerfTitle}</p>
            <p className="hint">{cc.noPerfBody}</p>
          </div>

          {cPhase.preview.already_listed && <p className="hint">{cc.alreadyListedNote}</p>}

          {/* ⭐ 專屬風險聲明：純前端閘門（仿 StepRisk），未勾選送出按鈕不開。 */}
          <label className="check-row">
            <input
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
            />
            <span>{cc.attestation}</span>
          </label>

          {/* 上界警語與送出按鈕同容器（沿精選卡片的誠信要求 5）。 */}
          <div className="leader-action">
            <p className="leader-upper-bound">{c.upperBound}</p>
            <button
              type="button"
              className="btn btn-primary btn-block"
              disabled={!agreed || !gate.open || busy}
              onClick={() => onSelect(customEntryOf(cPhase.preview))}
            >
              {gate.open ? cc.select : c.gateBadge}
            </button>
          </div>
        </div>
      )}
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
 * 後端 `_LEADER_PERF_WINDOWS` 的內容（改一邊要改兩邊）。只在回應沒帶
 * `performance_windows` 時使用——正常情況一律以後端那份清單為準。
 */
const DEFAULT_PERF_WINDOWS = ["perpMonth", "perpAllTime"];

/**
 * 卡片下區：績效。⭐ 與上區（規模／曝險）分屬兩個容器、各有標題，不是同一張表格
 * 多幾欄——混在一起，使用者會把「淨值大」讀成「賺得多」。
 *
 * 上界警語放在**本區塊內、所有數字之前**：它適用於這一區的每一個數字，
 * 而不是頁尾的一句免責。
 */
function LeaderPerfBlock({ leader, windows, notes }: {
  leader: LeaderEntry;
  windows: string[];
  notes: PerfNotesResolved;
}) {
  const rows = leader.performance;
  // 該 leader 沒有績效資料 → 這是一個**狀態**，要說出來；不畫空欄位、不畫 0。
  const present = rows ? windows.filter((w) => rows[w] != null) : [];

  return (
    <section className="leader-perf" aria-label={c.perf.sectionTitle}>
      <p className="leader-perf-title">{c.perf.sectionTitle}</p>
      <p className="hint leader-perf-scope">{c.perf.sectionNote}</p>
      {/*
        ⭐ 上界警語。後端 `performance_notes` 有給就用後端原文（單一來源：計算的極限
        與呈現的警語必須出自同一處），沒給才用等義的前端文案——
        警語與數字的規則相反：數字缺了就不顯示，警語缺了要補上。
      */}
      <p className="leader-perf-upper-bound">
        {notes.upperBound}
      </p>

      {present.length === 0 ? (
        <div className="leader-perf-state leader-perf-state-empty">
          <p className="leader-perf-state-title">{c.perf.leaderMissingTitle}</p>
          <p className="hint">{c.perf.leaderMissingBody}</p>
        </div>
      ) : (
        present.map((w) => (
          <PerfWindowBlock key={w} name={w} view={perfView(rows?.[w])} notes={notes} />
        ))
      )}

      <p className="hint leader-perf-basis">{notes.basis}</p>
    </section>
  );
}

/**
 * 一個績效窗。⭐ 本函式**不判斷鍵存不存在**的規則，只讀 `view.shown` 上存在的鍵
 * （判定收斂在 lib/leaderPerf.ts 的 `perfView`）。用 `!== undefined` 而不是
 * `?? 預設值`：缺席就整個欄位不渲染，不留一格「—」——一格「—」看起來像
 * 「有查到而且是空的」，那正是後端用「鍵不存在」避免的誤讀。
 *
 * ⭐ 分級揭露要看得出是**狀態**，不是「這幾格恰好空著」：每個窗都帶一個層級標籤，
 * 且凡是缺一級就有一段對應的說明（缺什麼、為什麼、還差幾天）。
 */
function PerfWindowBlock({ name, view, notes }: {
  name: string;
  view: LeaderPerfView;
  notes: PerfNotesResolved;
}) {
  const s = view.shown;
  const tierLabel = c.perf.tiers[view.tier as keyof typeof c.perf.tiers];
  const gapToWindow = daysShortOf(view.coveredDays, MIN_DAYS_FOR_RETURN);
  const gapToAnnual = daysShortOf(view.coveredDays, MIN_DAYS_FOR_ANNUALIZATION);

  return (
    <div className="leader-perf-window" data-tier={view.tier} data-level={view.level}>
      <p className="leader-perf-window-head">
        <span className="leader-perf-period">{c.perf.periods[name] ?? name}</span>
        <span className="leader-perf-tier" title={c.perf.tierLabel}>{tierLabel}</span>
      </p>

      {/*
        ⭐ 資料充足度與數字同時出現，不是折疊起來的細節：涵蓋 31 天的 TWR 與涵蓋
        兩年的 TWR 在畫面上長得一模一樣，可信度卻天差地遠。
      */}
      <p className="leader-perf-sufficiency">
        <span className="leader-perf-suff-item" title={view.coveredDaysRaw ?? undefined}>
          {c.perf.coveredDaysLabel}:{" "}
          {view.coveredDays === null
            ? NO_VALUE
            : `${fmtDays(view.coveredDays)} ${c.perf.daysUnit}`}
        </span>
        <span className="leader-perf-suff-item">
          {c.perf.sampleCountLabel}:{" "}
          {view.sampleCount === null
            ? NO_VALUE
            : `${view.sampleCount} ${c.perf.pointsUnit}`}
        </span>
      </p>
      <p className="hint leader-perf-suff-note">
        {notes.sufficiency}
      </p>

      {view.level === "none" ? (
        // 「資料不足」畫成一個有內容的狀態區塊——不是一排空欄位。
        <div className="leader-perf-state leader-perf-state-insufficient">
          <p className="leader-perf-state-title">{c.perf.insufficientTitle}</p>
          <p className="hint">{c.perf.insufficientBody}</p>
          {view.reason !== null && (
            <p className="hint mono">{c.perf.reasonLabel}: {view.reason}</p>
          )}
        </div>
      ) : (
        <>
          <dl className="leader-perf-stats">
            {s.cum_pnl !== undefined && (
              <PerfStat name="cum-pnl" label={c.perf.cols.cumPnl} hint={c.perf.colHints.cumPnl}
                value={fmtAmount(s.cum_pnl)} raw={s.cum_pnl} />
            )}
            {/*
              ⭐⭐ 標記與數字在**同一個 JSX 區塊**裡讀出來，不是「上面畫數字、
              下面某處畫警語」：redline.test.ts 的紅線 (b) 就在檢查這件事。
              薄資料的 TWR 現在照樣顯示，這行 marker 是它唯一的警示載體。
            */}
            {s.twr !== undefined && s.twr_insufficient_data !== undefined && (
              <PerfStat name="twr" label={c.perf.cols.twr} hint={c.perf.colHints.twr}
                value={fmtRatioPct(s.twr)} raw={s.twr}
                marker={s.twr_insufficient_data ? thinMarker(view.coveredDays) : undefined} />
            )}
            {s.max_drawdown !== undefined
              && s.max_drawdown_insufficient_data !== undefined && (
              // ⭐ 取樣低估的警語與 MDD 數字在**同一個容器**：分開放，看到數字的人
              // 不一定看得到警語，而「回撤只有 3%」正是最需要那句話的時候。
              <PerfStat name="max-drawdown" label={c.perf.cols.maxDrawdown}
                hint={c.perf.colHints.maxDrawdown}
                value={fmtRatioPct(s.max_drawdown)} raw={s.max_drawdown}
                note={notes.maxDrawdown}
                marker={s.max_drawdown_insufficient_data
                  ? thinMarker(view.coveredDays) : undefined} />
            )}
          </dl>

          {/*
            ⭐⭐ 年化**刻意排在期間報酬之後、另起一個字級較小的次級區塊**。
            理由是誠信而不是版面：期間報酬是**實際發生過的事**，年化是**外推**。
            一個 7 天 +3% 的 leader 年化會顯示成 365%——那個數字的錨定效果極強，
            而它在數學上把 7 天的雜訊放大了 52 倍。並排等大會讓最不可靠的數字
            拿到最大的視覺份量，所以這裡把版面權重與證據強度對齊。
            年化三鍵一起缺席（數學上無定義）時，整區不渲染——**絕不**只留標記。
          */}
          {s.annualized_return !== undefined
            && s.annualized_return_insufficient_data !== undefined
            && s.annualized_return_extrapolated_from_days !== undefined && (
            <dl className="leader-perf-annualized">
              <PerfStat name="annualized-return" label={c.perf.cols.annualizedReturn}
                hint={c.perf.colHints.annualizedReturn}
                value={fmtRatioPct(s.annualized_return)} raw={s.annualized_return}
                note={c.perf.annualizedSecondaryNote}
                marker={fillDays(
                  s.annualized_return_insufficient_data
                    ? c.perf.markerExtrapolatedThinBody
                    : c.perf.markerExtrapolatedBody,
                  s.annualized_return_extrapolated_from_days,
                )} />
            </dl>
          )}
        </>
      )}

      {/* 缺一級就說明缺什麼、為什麼、還差幾天——把空白變成狀態。 */}
      {view.level === "pnl" && (
        <PerfGap title={c.perf.gapWindowTitle} body={c.perf.gapWindowBody} shortfall={gapToWindow} />
      )}
      {view.level === "window" && (
        <PerfGap title={c.perf.gapAnnualizedTitle} body={c.perf.gapAnnualizedBody}
          shortfall={gapToAnnual} />
      )}

      {/* 後端宣告的層級高於實際資料 → 出聲，不靜默降級（工程原則 3）。 */}
      {view.degraded && (
        <p className="leader-perf-degraded" role="status">{c.perf.degradedNote}</p>
      )}
    </div>
  );
}

/** 缺少的那一級：說明 ＋ 距門檻還差幾天（已達門檻卻仍缺 → 只給說明，不編一個天數）。 */
function PerfGap({ title, body, shortfall }: {
  title: string; body: string; shortfall: number | null;
}) {
  return (
    <div className="leader-perf-state leader-perf-state-gap">
      <p className="leader-perf-state-title">{title}</p>
      <p className="hint">{body}</p>
      {shortfall !== null && (
        <p className="leader-perf-shortfall">
          {c.perf.gapShortfallLabel} {fmtDays(shortfall)} {c.perf.daysUnit}
        </p>
      )}
    </div>
  );
}

/**
 * 「僅涵蓋 N 天」的不足標記文字。
 * ⭐ 天數一律取自後端送來的值（單一來源），前端不從別處推算——涵蓋天數與不足判定
 * 出自後端同一次計算，混用兩個來源正是最容易產生無聲誤判的地方。
 */
function thinMarker(coveredDays: number | null): string {
  const body = coveredDays === null
    ? c.perf.markerThinBodyNoDays
    : c.perf.markerThinBody.replace("{days}", fmtDays(coveredDays));
  return `${c.perf.markerThinTitle}：${body}`;
}

/** `{days}` 佔位符 → 後端送來的天數字串（Decimal string，例如 `"6.0000"`）。 */
function fillDays(template: string, daysRaw: string): string {
  return template.replace("{days}", fmtDays(Number(daysRaw)));
}

/**
 * 一個績效數字。`note` 與 `marker` 都與數字**同容器**——分開放，看到數字的人不一定
 * 看得到警語。
 *
 * ⭐ `marker` 與 `note` 刻意分成兩個 prop：`note` 是這個指標**恆常**的極限說明
 * （例如 MDD 的取樣低估），`marker` 是**這一次資料**的不足／外推警示。兩者的成因與
 * 生命週期不同，合成一個欄位會讓「這次資料很薄」被寫成一句看起來像常態免責的話。
 */
function PerfStat({ name, label, hint, value, raw, note, marker }: {
  name: string; label: string; hint: string; value: string; raw: string;
  note?: string; marker?: string;
}) {
  return (
    <div className={`leader-perf-stat leader-perf-stat-${name}`}>
      <dt title={hint}>{label}</dt>
      <dd className="mono" title={raw}>{value}</dd>
      {/* 緊貼數字：role="note" 讓螢幕閱讀器也拿得到這個警示，不只是視覺上的小字。 */}
      {marker && (
        <p className="leader-perf-stat-marker" role="note">{marker}</p>
      )}
      {note && <p className="leader-perf-stat-note">{note}</p>}
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
