"use client";
/**
 * /capital — 投入本金與使用比例（含錢包簽章授權流程）。
 *
 * ⭐ 落點理由：既有資訊架構是「頁面路徑對應後端網域」（/leaders ↔ /api/leaders、
 * /billing ↔ /api/billing），本頁對應 /api/me/capital。與 /leaders 分開而不是併進去，
 * 是因為兩者是**兩個不同的決定**：/leaders 決定「跟誰」，本頁決定「押多大」。後端也
 * 刻意把它們分成兩個不可碰撞的簽章域（filet/capital_settings.py 檔頭），前端把兩個
 * 授權動作擺在同一顆按鈕旁邊，只會讓使用者更難分辨自己正在授權哪一件事。
 *
 * ⭐ 本頁的核心需求是**誠實揭露**，不是版面：
 * 1. 這兩個值直接乘進部位大小——「調高使用比例＝提高曝險與清算風險」貼著滑桿，
 *    不是頁尾小字。
 * 2. 兩個欄位用客戶看得懂的話解釋，並講明**兩者相乘**才是部位規模的基準。
 * 3. 目前生效值由 `/api/me/capital` 查得（CurrentCapitalPanel）——滑桿的位置**不是**
 *    現況，本頁不拿它冒充現況；查不到時說「暫時讀不到」，不退化成「你沒有設定」。
 *    <!-- 2026-07-27: 端點已上線，原註解「後端沒有此端點」已過時，改寫。 -->
 * 4. 拖動滑桿**不簽章**：調好按一次「套用」才進確認與簽署，避免無謂的錢包彈窗。
 * 5. 確認步驟寫明「下一個 cycle 生效」「不會立即再平衡」「提高曝險」三件事。
 * 6. 超界值與過多小數位**阻擋並明說**，前端不四捨五入、不夾取（後端也不夾取，
 *    見 validate_capital_bounds）——靜默修正等於改掉使用者要簽署的數字。
 *
 * 授權流程沿 leaderSelectFlow 的謹慎度（見 lib/capitalSettingsFlow.ts）：伺服器產生
 * 原文 → **設定值預驗（不符則不喚起錢包、零網路請求）** → 錢包 personal_sign →
 * 本地 recover 比對登入地址（不符零網路請求）→ 原文原樣回送。
 *
 * 可用性閘門：一律由後端 `/api/billing/plans` 的 `shipped` 旗標驅動，前端不硬編、
 * 也不自行放行；拿不到方案目錄時 **fail closed**（不確定就不給送出）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSignMessage } from "wagmi";
import {
  ApiError,
  getBillingPlans,
  getCapitalSettingsMessage,
  getMyCapital,
  postCapitalSettings,
  type BillingPlansResp,
  type CapitalSettingsResp,
  type MyCapitalResp,
} from "@/lib/api";
import {
  runCapitalSettingsFlow,
  type CapitalSettingsFlowResult,
} from "@/lib/capitalSettingsFlow";
import {
  MAX_UTILIZATION_PCT,
  MIN_UTILIZATION_PCT,
  canonicalAllocatedCapital,
  canonicalUtilizationPct,
  type CapitalValues,
} from "@/lib/capitalValues";
import { ActivationNotice } from "@/components/ActivationNotice";
import { COPY } from "@/lib/copy";
import { NO_VALUE, fmtAmount, fmtRatioPct } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { recoverPersonalSigner } from "@/lib/sign";

const c = COPY.capital;

/** 與後端 billing.py 的 `_F_RATIO_SLIDER` 同源（改一邊要改兩邊）。 */
const RATIO_SLIDER_KEY = "plans.feature.ratioSlider";

/**
 * 滑桿的預設起始位置。⭐ 這是一個**控制項的起點**，不是「目前生效值」。
 * 取一個保守值而不是 100%：預設值會被一部分使用者直接送出，預設把曝險拉滿不可接受。
 * 本頁有獨立的 CurrentCapitalPanel 元件顯示實際生效值（若可讀得到）。
 */
const DEFAULT_UTILIZATION_PCT = 20;

/**
 * 付費功能閘門。⭐ `shipped` 是**功能層級**的事實（同一個功能在各方案的 shipped
 * 必為同值），所以只要任一方案宣告已推出就是已推出——這裡刻意不看 `included`：
 * 「這個客戶的方案含不含」是後端的授權判斷，前端插手等於自建第二套權益邏輯。
 * 拿不到目錄（載入中／失敗）→ `unknown` 且**不開放送出**：不確定就不放行。
 * （形狀與 /leaders 的 gateOf 相同，鍵不同；兩頁刻意各持一份，避免其中一頁改鍵時
 * 悄悄改到另一頁的閘門。）
 */
type Gate = { open: true } | { open: false; reason: "unshipped" | "unknown" };

function gateOf(plans: BillingPlansResp | undefined): Gate {
  if (!plans) return { open: false, reason: "unknown" };
  const shipped = plans.plans.some((p) =>
    p.features.some((f) => f.text_key === RATIO_SLIDER_KEY && f.shipped),
  );
  return shipped ? { open: true } : { open: false, reason: "unshipped" };
}

type Phase =
  | { t: "idle" }
  /** ⭐ 待授權的值在開確認框時就**凍結**進 phase：之後流程比對的、送出的、
   *  畫面上顯示的都是同一份物件，使用者在對話框開著時改表單也影響不到它。 */
  | { t: "confirming"; expected: CapitalValues }
  | { t: "running" }
  | { t: "done"; resp: CapitalSettingsResp }
  /** `dismissOnly`：此錯誤要求使用者停手回報，不得提供「重新操作」按鈕。 */
  | { t: "error"; message: string; detail?: string; dismissOnly?: boolean };

export default function CapitalPage() {
  const queryClient = useQueryClient();
  const me = useMe();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<Phase>({ t: "idle" });
  const [allocatedInput, setAllocatedInput] = useState("");
  const [utilizationPct, setUtilizationPct] = useState(DEFAULT_UTILIZATION_PCT);
  /** 本次登入期間在本頁送出過的最後一組值。**不是**引擎生效值（見 c.currentSessionNote）。 */
  const [lastSubmitted, setLastSubmitted] = useState<CapitalValues | null>(null);

  const loggedIn = !!me.data;
  // 方案目錄是公開端點，與 /pricing、/leaders 共用同一把快取（同 queryKey）。
  const plans = useQuery<BillingPlansResp>({
    queryKey: ["billing-plans"],
    queryFn: getBillingPlans,
  });
  const gate = gateOf(plans.data);

  // ⭐ 使用者所調的值 → canonical 字串。失敗一律阻擋（不夾取、不四捨五入），
  // 理由與後端 validate_capital_bounds 相同：夾取會讓流程順利跑完，代價是客戶簽了
  // A、系統執行了 B，而且沒有人會知道。
  const parsedAllocated = canonicalAllocatedCapital(allocatedInput);
  const canonicalUtil = canonicalUtilizationPct(utilizationPct);
  const expected: CapitalValues | null =
    parsedAllocated.ok && canonicalUtil !== null
      ? {
          allocated_capital: parsedAllocated.value,
          capital_utilization: canonicalUtil,
        }
      : null;
  // 輸入還空著時不算「錯誤」——一進頁面就紅字是在罵還沒動手的人。
  const inputError =
    !parsedAllocated.ok && parsedAllocated.reason !== "empty"
      ? c.invalid[parsedAllocated.reason]
      : canonicalUtil === null
        ? c.invalid.utilization
        : null;
  const canApply = gate.open && loggedIn && expected !== null && phase.t !== "running";

  async function runFlow(values: CapitalValues) {
    // 防禦性重擋一層：閘門關著或值不合法時連流程都不啟動（按鈕已 disabled）。
    if (!gate.open || !me.data) return;
    setPhase({ t: "running" });
    const r = await runCapitalSettingsFlow(
      {
        fetchMessage: () =>
          getCapitalSettingsMessage(values.allocated_capital, values.capital_utilization),
        // 原文原樣進錢包：不加工、不重組（後端會用自己重建的版本驗簽）。
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postCapitalSettings,
      },
      // ⭐ expected＝使用者實際調的那組值。沒有這個，被打穿的 API 可以回一份把使用
      // 比例拉滿的原文，而流程的其他每一關（recover、後端重建驗簽、邊界檢查）都會
      // 照樣放行——1.0 本來就在允許區間內。
      { expectedSigner: me.data.address, expected: values },
    );
    if (r.ok) {
      setLastSubmitted(values);
      // ⭐ 授權成功 → 重抓「目前生效的資金設定」：後端此刻已有一筆 pending，而本頁
      // 上方若還顯示「沒有待生效的變更」，同一頁就同時說了兩件互相矛盾的事。
      void queryClient.invalidateQueries({ queryKey: ["my-capital"] });
      setPhase({ t: "done", resp: r.resp });
    } else {
      setPhase({ t: "error", ...errorCopy(r) });
    }
  }

  if (me.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }
  if (!loggedIn) {
    return <main className="page"><p>{COPY.common.notLoggedIn}</p></main>;
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>
      <p className="hint">{c.subtitle}</p>

      {/* 未活化就先說——否則使用者會調完、簽完，才撞上後端的「查無 follower」。 */}
      <ActivationNotice />

      {/* ⭐ 誠信要求 2：先讓人看懂這兩個數字是什麼，再讓他調。 */}
      <div className="panel capital-glossary">
        <p className="capital-section-title">{c.glossaryTitle}</p>
        <dl className="capital-terms">
          <dt>{c.glossaryAllocatedTerm}</dt>
          <dd>{c.glossaryAllocatedBody}</dd>
          <dt>{c.glossaryUtilizationTerm}</dt>
          <dd>{c.glossaryUtilizationBody}</dd>
        </dl>
        <p className="capital-product-title">{c.glossaryProductTitle}</p>
        <p className="hint">{c.glossaryProductBody}</p>
      </div>

      {/* ⭐ 誠信要求 3：你目前生效的資金設定（獨立元件、獨立失敗邊界）。 */}
      <CurrentCapitalPanel />

      {lastSubmitted && (
        <div className="panel capital-session">
          <p className="capital-section-title">{c.currentSessionLabel}</p>
          <p className="mono">{describeValues(lastSubmitted)}</p>
          <p className="hint">{c.currentSessionNote}</p>
        </div>
      )}

      {!gate.open && (
        <div className="panel plan-notice capital-gate">
          <p className="capital-gate-title">
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

      <section className="panel capital-form">
        <p className="capital-section-title">{c.formTitle}</p>

        <label className="capital-field" htmlFor="allocated-capital">
          <span className="capital-field-label">{c.allocatedLabel}</span>
          <input
            id="allocated-capital"
            className="capital-input mono"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            placeholder={c.allocatedPlaceholder}
            value={allocatedInput}
            // ⭐ 原樣存進 state：不 trim、不補零、不四捨五入。使用者打了什麼就留著什麼，
            // 錯的就報錯給他自己改——輸入框裡的數字被程式改掉，是最容易騙過本人的一種修改。
            onChange={(e) => setAllocatedInput(e.target.value)}
          />
          <span className="hint">{c.allocatedHint}</span>
        </label>

        <label className="capital-field" htmlFor="capital-utilization">
          <span className="capital-field-label">
            {c.utilizationLabel}
            <span className="capital-util-value mono">{utilizationPct}%</span>
          </span>
          <input
            id="capital-utilization"
            className="capital-slider"
            type="range"
            min={MIN_UTILIZATION_PCT}
            max={MAX_UTILIZATION_PCT}
            step={1}
            value={utilizationPct}
            onChange={(e) => setUtilizationPct(Number(e.target.value))}
          />
          <span className="hint">{c.utilizationHint}</span>
          <span className="hint">{c.utilizationDefaultNote}</span>
        </label>

        {/* ⭐ 誠信要求 1：風險揭露與滑桿同一個容器，不得被推到頁尾。 */}
        <div className="capital-risk">
          <p className="capital-risk-title">{c.riskTitle}</p>
          <p className="capital-risk-body">{c.riskBody}</p>
        </div>

        <div className="capital-basis">
          <span className="capital-basis-label">{c.basisLabel}</span>
          <span className="capital-basis-value mono">{basisDisplay(expected)}</span>
          <span className="hint">{c.basisNote}</span>
        </div>

        {inputError && <p className="capital-input-error" role="alert">{inputError}</p>}

        <p className="hint">{c.applyNote}</p>
        <button
          type="button"
          className="btn btn-primary btn-block"
          disabled={!canApply}
          onClick={() => expected && setPhase({ t: "confirming", expected })}
        >
          {gate.open ? c.apply : c.gateBadge}
        </button>
      </section>

      {phase.t === "confirming" && (
        <ConfirmDialog
          expected={phase.expected}
          previous={lastSubmitted}
          onCancel={() => setPhase({ t: "idle" })}
          onConfirm={() => runFlow(phase.expected)}
        />
      )}
      {phase.t === "running" && <p className="hint" role="status">{c.signing}</p>}
      <p className="hint">{COPY.common.nonCustodial}</p>
    </main>
  );
}

/** 一組設定值的人類可讀敘述（金額千分位、比例百分比）。 */
function describeValues(v: CapitalValues): string {
  return `${fmtAmount(v.allocated_capital)} USDC × ${fmtRatioPct(v.capital_utilization, 0)}`;
}

/**
 * 資金基準＝本金 × 比例。⭐ 只用於**顯示**（讓「兩者相乘」這句話有個具體數字），
 * 實際下單量由引擎以 Decimal 計算——所以文案明講這是估算值，且說明實際名目部位
 * 可能大於它（槓桿）。值不合法時不畫數字（不畫 0：0 是「有算出來且為零」）。
 */
function basisDisplay(expected: CapitalValues | null): string {
  if (!expected) return NO_VALUE;
  const basis = Number(expected.allocated_capital) * Number(expected.capital_utilization);
  if (!Number.isFinite(basis)) return NO_VALUE;
  return `${basis.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} USDC`;
}

/**
 * 失敗文案。⭐ 每一條都必須讓使用者知道**現在的狀態**——「沒有被變更」要講出來：
 * 這兩個值決定曝險倍數，使用者最需要知道的是「我剛才那下到底有沒有發生」。
 * 後端 detail 原樣附在第二行（供回報問題用），不做字串比對推測原因。
 */
function errorCopy(r: Extract<CapitalSettingsFlowResult, { ok: false }>):
  { message: string; detail?: string; dismissOnly?: boolean } {
  const e = c.errors;
  if (r.kind === "wallet-rejected") return { message: e.walletRejected };
  if (r.kind === "signer-mismatch") return { message: e.signerMismatch };
  // ⭐ 伺服器要你簽的數字 ≠ 你調的數字：唯一一種「請停手並回報」的失敗，
  // 因此連按鈕都不給「重新操作」（見 copy.ts 的 valuesMismatch）。
  if (r.kind === "values-mismatch") return { message: e.valuesMismatch, dismissOnly: true };
  const err = r.error;
  const detail = err instanceof ApiError ? err.detail : undefined;
  if (err instanceof ApiError) {
    if (err.kind === "auth") return { message: COPY.common.notLoggedIn, detail };
    if (err.status === 403) return { message: e.forbidden, detail };
    // 400 在這兩支端點上的主要語意是超界／格式（見 app.py 的 CAPITAL_SETTINGS_DETAIL）。
    // 後端 detail 原樣附上，前端不改寫它的判斷。
    if (err.status === 400) return { message: e.outOfRange, detail };
  }
  return {
    message: r.kind === "message-failed" ? e.messageFailed : e.submitFailed,
    detail,
  };
}

/** 成功。⭐ 生效時機與後果一律顯示**後端原文**（單一來源，不在前端另寫一份）。 */
function DoneNotice({ resp }: { resp: CapitalSettingsResp }) {
  return (
    <div className="panel capital-done" role="status">
      <p className="capital-done-title">{c.doneTitle}</p>
      <p className="hint">{resp.effective_note}</p>
      <p className="hint">{resp.consequences}</p>
      <p className="hint mono">
        {describeValues({
          allocated_capital: resp.allocated_capital,
          capital_utilization: resp.capital_utilization,
        })}
      </p>
    </div>
  );
}

/**
 * ⭐ 誠信要求 5：確認對話框。前後對照、生效時機（下一個 cycle）、不會立即再平衡、
 * 提高曝險的風險——四件事都必須在**按下確認之前**看得到。這是使用者唯一一次能反悔
 * 的地方，之後就是簽名與一份對曝險倍數的真實授權。
 * 刻意不用 window.confirm：它塞不下這些內容，也無法被測試釘住。
 *
 * 「目前生效值」在後端提供查詢端點之前一律顯示為未知——這裡不拿本頁的預設值或
 * 上一次送出的值冒充現況（上一次送出的值另外標示，且明說它不等於生效值）。
 */
function ConfirmDialog({ expected, previous, onCancel, onConfirm }: {
  expected: CapitalValues;
  previous: CapitalValues | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="leader-dialog-backdrop">
      <div className="panel leader-dialog" role="dialog" aria-modal="true"
           aria-label={c.confirmTitle}>
        <p className="leader-dialog-title">{c.confirmTitle}</p>

        <p className="capital-section-title">{c.confirmDiffTitle}</p>
        <dl className="capital-diff">
          <dt>{c.confirmFromLabel}</dt>
          <dd className="mono">{c.confirmFromUnknown}</dd>
          {previous && (
            <>
              <dt>{c.currentSessionLabel}</dt>
              <dd className="mono">{describeValues(previous)}</dd>
            </>
          )}
          <dt>{c.confirmToLabel}</dt>
          <dd className="mono capital-diff-new">{describeValues(expected)}</dd>
        </dl>
        <p className="hint mono">
          {`${c.confirmExactLabel}: allocated_capital=${expected.allocated_capital}, `
            + `capital_utilization=${expected.capital_utilization}`}
        </p>
        <p className="hint">{c.confirmExactNote}</p>

        <p className="leader-dialog-body">{c.confirmTiming}</p>
        <p className="leader-dialog-body">{c.confirmNoRebalance}</p>
        <p className="capital-risk-title">{c.riskTitle}</p>
        <p className="leader-dialog-body">{c.riskBody}</p>
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

/**
 * ⭐ 「你目前生效的資金設定」（`/api/me/capital`；spec User Story 24）。
 *
 * 為什麼這一區存在：本頁的核心決定是「押多大」，而「現在押的多大」是這個決定的
 * 左半邊。沒有它，客戶是在不知道現況的情況下被要求簽署變更——尤其調低比例以為
 * 已經降了曝險時，要看得到自己現在實際生效的值。
 *
 * ⚠️ 失敗方向不對稱，三種狀態各有各的畫法：
 * - 查詢失敗（503／網路）→ 說「暫時讀不到」，**絕不**畫成「你沒有設定」（讀不到 ≠ 沒有）。
 * - `pending` 存在時要**單獨一個區塊**標示「已提交、尚未生效」，**絕不能**看起來像已生效
 *   （調低比例的客戶會以為曝險已降下來而在錯誤的安全感下加碼）。
 * - 狀態內文一律用後端 `note` 原文（單一來源），本頁只提供標題與欄位標籤。
 */
function CurrentCapitalPanel() {
  // 本元件只在登入分支被渲染（頁面上方的 guard 已擋掉未登入），故不另掛 enabled。
  // ⭐ query 刻意留在元件內而不是提到頁面 body：提出去之後它會在 `!loggedIn` guard
  // 之前就送出，未登入的訪客一進頁面就會打一次 /api/me/capital 收 401。
  const query = useQuery<MyCapitalResp>({ queryKey: ["my-capital"], queryFn: getMyCapital });
  const cur = c.current;

  if (query.isLoading) {
    return (
      <div className="panel ops-notice capital-current">
        <p className="ops-notice-title">{cur.title}</p>
        <p className="hint" role="status">{cur.loading}</p>
      </div>
    );
  }

  if (query.error || !query.data) {
    return (
      <div className="panel ops-notice capital-current">
        <p className="ops-notice-title">{cur.failedTitle}</p>
        <p className="hint">{cur.failedNote}</p>
      </div>
    );
  }

  const d = query.data;
  const none = d.effective === null ? noneCapitalTitleOf(d.status) : null;
  const fullEquity = d.effective?.use_full_equity === true;

  return (
    <div className="panel ops-notice capital-current">
      <p className="ops-notice-title">{cur.title}</p>
      {d.effective !== null ? (
        <div className="capital-effective-values">
          <p className="capital-section-title">{cur.effectiveLabel}</p>
          <dl className="capital-current-dl">
            <dt>{cur.allocatedLabel}</dt>
            <dd className={fullEquity ? undefined : "mono"}>
              {allocatedDisplay(d.effective.allocated_capital, d.effective.use_full_equity)}
            </dd>
            <dt>{cur.utilizationLabel}</dt>
            <dd className="mono">{fmtRatioPct(d.effective.capital_utilization, 2)}</dd>
            <dt>{cur.sourceLabel}</dt>
            {/* ⭐ 二值 union 但不用二元 else：後端加第三個 source 時，else 會把它
                靜默顯示成「系統預設」——與 F3 同一種錯（陌生值被畫成熟悉值）。 */}
            <dd>{sourceDisplay(d.effective.source)}</dd>
            {/* ⭐ 整列照樣顯示：隱藏會讓「從未變更」與「這次沒讀到這個欄位」看起來一樣。 */}
            <dt>{cur.changedAtLabel}</dt>
            <dd className={d.effective.changed_at === null ? undefined : "mono"}>
              {d.effective.changed_at ?? cur.changedAtNull}
            </dd>
            <dt>{cur.asOfLabel}</dt>
            {/* ⚠️ as_of 可為 null（心跳沒有時點）。空白會讓人以為這是即時查到的值。 */}
            <dd className="mono">{d.effective.as_of ?? NO_VALUE}</dd>
          </dl>
          {fullEquity && <p className="hint">{cur.fullEquityNote}</p>}
        </div>
      ) : (
        <p className="capital-current-none">{none?.title}</p>
      )}
      {none?.unknown === true && (
        <p className="hint mono">{cur.statusLabel}: {d.status}</p>
      )}
      <p className="hint">{d.note}</p>
      {d.pending !== null && <PendingCapitalChange change={d.pending} />}
    </div>
  );
}

/**
 * 本金那一格的顯示值。⭐ `use_full_equity === true` 時**不得**印金額：後端的硬不變量
 * 是這個旗標為真 ⇒ `allocated_capital` 恆為 0（copytrade/config.py 的 `__post_init__`），
 * 而那個 0 的意思是「不准有值」，不是「本金為零」。原樣印成「0.00 USDC」，配上本頁
 * 術語表教的「本金 × 比例 = 部位基準」，客戶會算出曝險為零——實際上引擎拿整個帳戶
 * 權益在下單。且 `use_full_equity=true` 是 COPY_ALLOCATED_CAPITAL 未設時的**預設**，
 * 所以每個 `source="env_default"` 的帳號都走這條路徑。
 */
function allocatedDisplay(allocated: string | null, useFullEquity: boolean | null): string {
  if (useFullEquity === true) return c.current.fullEquityValue;
  const amount = fmtAmount(allocated);
  // 讀不到金額時只給 NO_VALUE：接一個 "— USDC" 反而像是一個有單位的真實值。
  return amount === NO_VALUE ? NO_VALUE : `${amount} USDC`;
}

/** 來源；後端加了新的 source 值時原樣顯示（不猜它是哪一種）。 */
function sourceDisplay(source: string): string {
  const cur = c.current;
  const labels: Record<string, string | undefined> = {
    customer_signed: cur.sourceCustomer,
    env_default: cur.sourceEnvDefault,
  };
  return labels[source] ?? source;
}

/**
 * 沒有 effective 值時的標題。
 * ⭐ `unknown` 是後端**明訂**的一態（已活化但心跳缺席／過期），與「前端不認識的狀態碼」
 * 分開兩句話：合併之後，畫面會在「你目前生效的資金設定」底下寫「未知的狀態碼」，
 * 而面板裡唯一的數字是 pending 那組——客戶會把待生效值讀成現況。
 * `unknown: true` 只給真正陌生的狀態碼（那一行會把原始字串攤出來）。
 */
function noneCapitalTitleOf(status: string): { title: string; unknown: boolean } {
  const cur = c.current;
  const titles: Record<string, string | undefined> = {
    unknown: cur.unknownTitle,
    not_activated: cur.notActivatedTitle,
    indeterminate: cur.indeterminateTitle,
  };
  const title = titles[status];
  return title === undefined
    ? { title: `${cur.unknownStatus}：${status}`, unknown: true }
    : { title, unknown: false };
}

/**
 * 已簽署但尚未生效的資金設定變更。⭐ 與「目前生效」畫在同一區但**分開的區塊**：
 * 兩者是不同時點的事實（現在生效的 vs 下一個 cycle 會換到的），混在一起會讓客戶以為
 * 調整已經發生了——而調低比例時最常犯的錯誤就是以為曝險已經降下來而在錯誤的安全感下加碼。
 */
function PendingCapitalChange({ change }: { change: MyCapitalResp["pending"] }) {
  if (!change) return null;
  const cur = c.current;
  const fullEquity = change.use_full_equity === true;
  return (
    <div className="capital-effective-values capital-pending">
      <p className="capital-section-title">{pendingTitleOf(change.state)}</p>
      <dl className="capital-current-dl">
        <dt>{cur.allocatedLabel}</dt>
        <dd className={fullEquity ? undefined : "mono"}>
          {allocatedDisplay(change.allocated_capital, change.use_full_equity)}
        </dd>
        <dt>{cur.utilizationLabel}</dt>
        <dd className="mono">{fmtRatioPct(change.capital_utilization, 2)}</dd>
        <dt>{cur.pendingStateLabel}</dt>
        <dd>
          {change.state === "not_yet_applied"
            ? cur.pendingStateNotYetApplied
            : change.state === "unconfirmed"
              ? cur.pendingStateUnconfirmed
              : change.state}
        </dd>
        {change.submitted_at !== null && (
          <>
            <dt>{cur.pendingSubmittedAtLabel}</dt>
            <dd className="mono">{change.submitted_at}</dd>
          </>
        )}
        <dt>{cur.pendingEffectiveWhenLabel}</dt>
        <dd className="mono">{change.effective_when}</dd>
      </dl>
      {fullEquity && <p className="hint">{cur.fullEquityNote}</p>}
      <p className="hint">{change.note}</p>
    </div>
  );
}

/**
 * pending 區塊的標題。⭐ 依 `state` 分兩句，因為後端刻意把兩態分開且註明「處置完全
 * 不同」：`not_yet_applied` ＝生效值已知且與提交值不同 ⇒ **確定**還沒套用；
 * `unconfirmed` ＝生效值不可知（心跳缺席／過期）⇒ **無從得知**套用了沒。
 * 兩者共用「尚未生效」的話，會在心跳過期＋一筆往上調的變更時，用畫面上最大的那句話
 * 告訴客戶「還沒生效」——而它可能已經生效了。第三種 state 走保底，不併進上面任一句。
 */
function pendingTitleOf(state: string): string {
  const cur = c.current;
  const titles: Record<string, string | undefined> = {
    not_yet_applied: cur.pendingTitleNotYetApplied,
    unconfirmed: cur.pendingTitleUnconfirmed,
  };
  return titles[state] ?? cur.pendingTitleFallback;
}
