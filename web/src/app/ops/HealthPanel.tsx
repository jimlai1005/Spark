"use client";
/**
 * /ops 系統健康面板（管理端）。
 *
 * ⭐⭐ 本檔只有一條設計原則：**讀不到就說讀不到，未知絕不折疊成健康值**。
 *
 * 為什麼這條比「好看」重要：健康面板的讀者用它決定「要不要現在去看」。一個謊報
 * 健康的格子會讓他**不去看**，而那正是他最該去看的時刻。後端已經把每一格做成三態
 * （true／false／null ＋ `*_known` 兄弟欄），前端最容易犯的錯就是在 JSX 裡直接寫
 * `row.killswitch_tripped ? "已觸發" : "未觸發"`——null 是 falsy，於是一個**確實已經
 * 熔斷、部位已被平掉**的客戶會被畫成「未觸發」。
 *
 * 結構性防線（不靠人記得處理）：三態一律先過 `engineState`／`killswitchState`／
 * `coverageState` 三個純函式轉成字串聯集，再由字串聯集渲染。null 在那三個函式的
 * 第一行就被攔成 "unknown"，JSX 裡拿不到布林值，因此不可能寫出上面那個 bug。
 *
 * ⚠️⚠️ 心跳與「現況」的分野（本面板最重要的一條）：引擎每個 cycle 發布一份健康
 * 心跳，面板據此顯示 leader、資金設定與 kill switch。**過期的心跳不是現況**——
 * 後端在心跳過期時結構性地不回傳 payload（`heartbeat_status="stale"` 時
 * `leader_address`／`capital` 皆為 null），所以本頁即使想顯示也拿不到那些值；
 * 過期時畫面只多出「最後心跳時刻」與年齡兩格，並明確標示過期。
 * 一份 40 分鐘前的「kill switch 未觸發」顯示成現況，正是這個面板最不能犯的錯。
 */
import type {
  OpsHealthFollower,
  OpsHealthResp,
  OpsHealthSummary,
  OpsUnappliedLeaderChange,
} from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { fmtAmount, fmtRatioPct, NO_VALUE } from "@/lib/format";

const h = COPY.ops.health;
const S = h.state;

// ---------- 三態 → 字串聯集（未知在第一行就被攔下） ----------

type EngineState = "alive" | "stale" | "unknown";
type KillswitchState = "tripped" | "armed" | "unknown";
type CoverageState = "covered" | "insufficient" | "unknown";

/**
 * ⭐ 權益樣本存活三態。`null`／`undefined`（後端換了欄位名時的形狀）一律 "unknown"。
 * **不得**移除第一行的守衛：移除後 null 會落進 `alive ? ...` 的 falsy 分支，
 * 把「讀不到」畫成一個確定的狀態——那是本面板最不能犯的錯。
 */
export function engineState(alive: boolean | null | undefined): EngineState {
  if (alive == null) return "unknown";
  return alive ? "alive" : "stale";
}

/**
 * ⭐⭐ kill switch 三態。`killswitch_known=false`（或值為 null）＝**無從確認**，
 * 不是「沒觸發」。移除第一行的守衛，null 會走進 falsy 分支顯示「未觸發」——
 * 等於在客戶的引擎已經熔斷、部位已被平掉的當下，告訴管理員這個客戶一切正常。
 */
export function killswitchState(row: Pick<OpsHealthFollower,
  "killswitch_known" | "killswitch_tripped">): KillswitchState {
  if (!row.killswitch_known || row.killswitch_tripped == null) return "unknown";
  return row.killswitch_tripped ? "tripped" : "armed";
}

/**
 * 回撤保護覆蓋三態。⚠️ `false`（樣本不足）是一個**確定**的答案，與 `null`（讀不到）
 * 刻意分開：前者是「保護尚未生效」，後者是「不知道保護有沒有生效」。
 */
export function coverageState(sufficient: boolean | null | undefined): CoverageState {
  if (sufficient == null) return "unknown";
  return sufficient ? "covered" : "insufficient";
}

/**
 * ⭐⭐ 心跳新鮮度：後端的四個已知代碼各自對應一句人話，**未知代碼原樣顯示**
 * 且視覺上按「非 ok」處理。刻意不寫 `status === "stale" ? ... : 健康`——那種寫法
 * 會讓後端新增的任何一種狀態自動落進健康分支。
 */
function heartbeatText(status: string): { label: string; hint: string; ok: boolean } {
  switch (status) {
    case "ok": return { label: h.heartbeat.ok, hint: h.heartbeat.okHint, ok: true };
    case "stale": return { label: h.heartbeat.stale, hint: h.heartbeat.staleHint, ok: false };
    case "missing": return { label: h.heartbeat.missing, hint: h.heartbeat.missingHint, ok: false };
    case "unreadable":
      return { label: h.heartbeat.unreadable, hint: h.heartbeat.unreadableHint, ok: false };
    default:
      return { label: `${h.heartbeat.unknownPrefix}${status}`, hint: "", ok: false };
  }
}

/** 資料來源代碼 → 人話；未知代碼原樣顯示（同上，不靜默歸進某個既有來源）。 */
function sourceText(basis: string): string {
  const known = h.sources as Record<string, string | undefined>;
  return known[basis] ?? `${h.sourceUnknownPrefix}${basis}`;
}

// ---------- 顯示工具 ----------

/** 秒 → 粗略年齡字串。null／非有限值 → NO_VALUE（**不是 0 秒**，那是最健康的值）。 */
export function fmtAge(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return NO_VALUE;
  const v = Math.max(0, Math.round(s));
  if (v < 90) return `${v} ${h.units.sec}`;
  if (v < 3600) return `${Math.round(v / 60)} ${h.units.min}`;
  if (v < 172800) return `${Math.round(v / 3600)} ${h.units.hour}`;
  return `${Math.round(v / 86400)} ${h.units.day}`;
}

// ---------- 面板 ----------

export function HealthBlock({ data }: { data: OpsHealthResp }) {
  const s = data.summary;
  const rows = data.followers ?? [];
  // 未知類的計數：這些不是「沒有問題」，是「看不到」。任何一項非零都要大聲說。
  const unknownCount =
    s.engine_unknown_count + s.killswitch_unknown_count
    + s.coverage_unknown_count + s.alerts_unknown_count;
  const backlogUnknown =
    s.unapplied_leader_changes == null || s.leader_change_errors.length > 0;

  return (
    <>
      {/* ⭐ 順序＝危害順序：已熔斷（客戶已停止跟單）> 心跳過期 > 讀不到。 */}
      {s.killswitch_tripped_count > 0 && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-title">{h.trippedTitle}</p>
          <p className="ops-alert-body">{h.trippedBody}</p>
        </div>
      )}
      {s.heartbeat_stale_count > 0 && (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{h.staleTitle}</p>
          <p className="ops-alert-body">{h.staleBody}</p>
        </div>
      )}
      {(unknownCount > 0 || backlogUnknown) && (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{h.unknownTitle}</p>
          <p className="ops-alert-body">{h.unknownBody}</p>
        </div>
      )}

      <div className="panel">
        <p className="hint">{h.note}</p>
        <HealthStats s={s} />
        <p className="hint mono ops-window">{h.checkedAtLabel}: {data.checked_at}</p>
        <p className="hint mono ops-window">
          {h.staleAfterLabel}: {data.engine_stale_after_s}
        </p>
      </div>

      {/* ⭐ 來源與極限都放在表格**之前**：寫成頁尾小字等於讓人先相信那些格子，
          再（也許）讀到它們的極限。 */}
      <div className="panel ops-notice">
        <p className="ops-notice-title">{h.sourceTitle}</p>
        <p className="hint">{h.sourceBody}</p>
        <p className="ops-notice-title">{h.basisTitle}</p>
        <p className="hint">{h.basisBody}</p>
        <p className="hint mono">{h.basisLabel}: <BasisLabel rows={rows} /></p>
      </div>

      {data.manifest_errors.length > 0 && (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{h.manifestErrors}</p>
          <ul className="ops-error-list">
            {data.manifest_errors.map((m, i) => <li key={i} className="mono">{m}</li>)}
          </ul>
        </div>
      )}

      {rows.length === 0 ? (
        <p>{h.empty}</p>
      ) : (
        <>
          <div className="panel">
            <table className="admin-table ops-table">
              <thead>
                <tr>
                  <th scope="col">{h.cols.account}</th>
                  <th scope="col">{h.cols.heartbeat}</th>
                  <th scope="col">{h.cols.lastBeat}</th>
                  <th scope="col">{h.cols.engine}</th>
                  <th scope="col">{h.cols.coverage}</th>
                  <th scope="col">{h.cols.killswitch}</th>
                  <th scope="col">{h.cols.alerts}</th>
                  <th scope="col">{h.cols.source}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => <HealthRow key={row.account_id} row={row} />)}
              </tbody>
            </table>
          </div>
          <EngineStateTable rows={rows} />
        </>
      )}

      <BacklogBlock summary={s} entries={data.unapplied_leader_changes ?? []} />
    </>
  );
}

/**
 * 取樣判定基準。⭐ 刻意不寫死「equity 樣本」：後端換掉基準時本頁會原樣顯示那個新
 * 代碼並標明本頁尚無說明——一句**已經不成立**的說明，比沒有說明更容易誤導。
 */
function BasisLabel({ rows }: { rows: OpsHealthFollower[] }) {
  const bases = Array.from(new Set(rows.map((r) => r.liveness_basis).filter(Boolean)));
  if (bases.length === 0) return <>{NO_VALUE}</>;
  return (
    <>
      {bases.map((b) => (
        <span key={b} className="ops-basis">
          {b === "equity_sample" ? h.basisEquitySample : `${h.basisUnknownPrefix}${b}`}
        </span>
      ))}
    </>
  );
}

function HealthStats({ s }: { s: OpsHealthSummary }) {
  return (
    <dl className="ops-stats">
      <HStat label={h.stats.followers} value={String(s.followers)} />
      {/* ⭐ 心跳三態各自一格，不合併：stale 要立刻查，missing 多半是部署待辦。 */}
      <HStat label={h.stats.heartbeatOk} value={String(s.heartbeat_ok_count)} />
      <HStat label={h.stats.heartbeatStale} value={String(s.heartbeat_stale_count)}
             bad={s.heartbeat_stale_count > 0} />
      <HStat label={h.stats.heartbeatMissing} value={String(s.heartbeat_missing_count)}
             bad={s.heartbeat_missing_count > 0} />
      <HStat label={h.stats.engineAlive} value={String(s.engine_alive_count)} />
      <HStat label={h.stats.engineStale} value={String(s.engine_stale_count)}
             bad={s.engine_stale_count > 0} />
      <HStat label={h.stats.engineUnknown} value={String(s.engine_unknown_count)}
             bad={s.engine_unknown_count > 0} />
      <HStat label={h.stats.killswitchTripped} value={String(s.killswitch_tripped_count)}
             bad={s.killswitch_tripped_count > 0} />
      <HStat label={h.stats.killswitchUnknown} value={String(s.killswitch_unknown_count)}
             bad={s.killswitch_unknown_count > 0} />
      <HStat label={h.stats.coverageInsufficient}
             value={String(s.coverage_insufficient_count)} />
      <HStat label={h.stats.coverageUnknown} value={String(s.coverage_unknown_count)}
             bad={s.coverage_unknown_count > 0} />
      {/* ⭐ 告警合計與「讀不到的份數」成對顯示：合計 3 而有 5 個客戶讀不到，
          那個 3 幾乎沒有意義——拆開任一個，另一個就會被誤讀。 */}
      <HStat label={h.stats.alertsTotal} value={String(s.alerts_total)} />
      <HStat label={h.stats.alertsUnknown} value={String(s.alerts_unknown_count)}
             bad={s.alerts_unknown_count > 0} />
      {/* ⭐ 積壓：null 一律顯示「未知」，**絕不**退化成 0（0＝沒有積壓，正好相反）。 */}
      <HStat
        label={h.stats.backlog}
        value={s.unapplied_leader_changes == null
          ? S.unknown
          : String(s.unapplied_leader_changes)}
        bad={s.unapplied_leader_changes == null || s.unapplied_leader_changes > 0}
      />
    </dl>
  );
}

function HStat({ label, value, bad = false }: {
  label: string; value: string; bad?: boolean;
}) {
  return (
    <div className="ops-stat">
      <dt>{label}</dt>
      <dd className={`mono${bad ? " is-bad" : ""}`}>{value}</dd>
    </div>
  );
}

/**
 * 單一客戶的健康列。
 * ⭐ kill switch 已觸發的列在**視覺上**明顯（整列紅底 ＋ 實心紅 chip），不只是文字：
 * 那一列代表「這個客戶已經停止跟單」，掃過一整頁時它必須自己跳出來。
 */
function HealthRow({ row }: { row: OpsHealthFollower }) {
  const hb = heartbeatText(row.heartbeat_status);
  const eng = engineState(row.engine_alive);
  const ks = killswitchState(row);
  const cov = coverageState(row.sample_coverage_sufficient);

  return (
    <>
      <tr className={ks === "tripped" ? "ops-row-tripped" : undefined}>
        <td className="mono">{row.account_id}</td>
        <td>
          <Chip tone={hb.ok ? "ok" : "warn"} label={hb.label} title={hb.hint} />
        </td>
        {/* ⭐ 心跳時刻與年齡一起給，且左欄的 chip 已經說清楚它是不是過期的。
            只給「幾分鐘前」會讀起來像一個仍在更新的值；只給時刻則要讀者自己心算。 */}
        <td className="mono">
          {row.heartbeat_at == null ? (
            <span className="ops-unknown" title={hb.hint}>{NO_VALUE}</span>
          ) : (
            <>
              {row.heartbeat_at}
              <span className="hint"> （{fmtAge(row.heartbeat_age_s)}{h.ageSuffix}）</span>
            </>
          )}
        </td>
        <td>
          <Chip
            tone={eng === "alive" ? "ok" : eng === "stale" ? "warn" : "unknown"}
            label={eng === "alive" ? S.alive : eng === "stale" ? S.stale : S.engineUnknown}
          />
          {row.last_sample_age_s != null && (
            <span className="hint"> {fmtAge(row.last_sample_age_s)}{h.ageSuffix}</span>
          )}
        </td>
        <td>
          <Chip
            tone={cov === "covered" ? "ok" : cov === "insufficient" ? "warn" : "unknown"}
            label={cov === "covered" ? S.covered
              : cov === "insufficient" ? S.insufficient : S.coverageUnknown}
            title={cov === "insufficient" ? h.coverageInsufficientHint : undefined}
          />
          {row.sample_count != null && (
            <span className="hint mono"> {row.sample_count}</span>
          )}
        </td>
        <td>
          <Chip
            tone={ks === "tripped" ? "bad" : ks === "armed" ? "ok" : "unknown"}
            label={ks === "tripped" ? S.tripped : ks === "armed" ? S.armed : S.killswitchUnknown}
          />
        </td>
        <td className="mono">
          {/* ⭐ 告警數讀不到 → 「未知」，**不是 0**。0 是面板上最令人安心的數字，
              在告警檔權限壞掉的當下顯示 0，等於告訴操作者一切正常。 */}
          {row.alerts == null
            ? <span className="ops-unknown">{S.unknown}</span>
            : row.alerts}
        </td>
        <td className="hint">{sourceText(row.basis)}</td>
      </tr>
      {row.error && (
        <tr className="ops-row-failed">
          <td colSpan={8} className="ops-row-error">
            <span className="ops-row-error-label">{h.rowError}</span>
            <span className="mono">{row.error}</span>
          </td>
        </tr>
      )}
    </>
  );
}

function Chip({ tone, label, title }: {
  tone: "ok" | "warn" | "bad" | "unknown"; label: string; title?: string;
}) {
  return <span className={`ops-chip ops-chip-${tone}`} title={title}>{label}</span>;
}

/**
 * ⭐⭐ 引擎現況（leader 與資金設定）。這些值**只可能來自心跳**——狀態根沒有可讀
 * 投影，而後端在心跳過期時結構上不回傳它們。所以這張表的每一格在心跳非 ok 時
 * 一律「未知」：一份幾十分鐘前的資金設定被讀成「現在生效的設定」，會讓管理員
 * 依一個已經不成立的曝險倍數做判斷。
 */
function EngineStateTable({ rows }: { rows: OpsHealthFollower[] }) {
  const cols = h.engineStateCols;
  return (
    <div className="panel ops-drift">
      <h3 className="ops-drift-title">{h.engineStateTitle}</h3>
      <p className="hint">{h.engineStateNote}</p>
      <table className="admin-table ops-table">
        <thead>
          <tr>
            <th scope="col">{cols.account}</th>
            <th scope="col">{cols.leader}</th>
            <th scope="col">{cols.leaderSource}</th>
            <th scope="col">{cols.allocated}</th>
            <th scope="col">{cols.utilization}</th>
            <th scope="col">{cols.fullEquity}</th>
            <th scope="col">{cols.capitalSource}</th>
            <th scope="col">{cols.lastCycle}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => <EngineStateRow key={row.account_id} row={row} />)}
        </tbody>
      </table>
    </div>
  );
}

function EngineStateRow({ row }: { row: OpsHealthFollower }) {
  const hb = heartbeatText(row.heartbeat_status);
  // 心跳非 ok ⇒ 後端不回傳這些值。整列標未知並說明原因，不留白也不猜。
  if (!hb.ok) {
    return (
      <tr>
        <td className="mono">{row.account_id}</td>
        <td colSpan={7}>
          <span className="ops-unknown" title={hb.hint}>{S.unknown}</span>
          <span className="hint"> {hb.label}</span>
        </td>
      </tr>
    );
  }
  const cap = row.capital;
  return (
    <tr>
      <td className="mono">{row.account_id}</td>
      <td className="mono" title={row.leader_address ?? undefined}>
        {row.leader_address ?? NO_VALUE}
      </td>
      <td className="mono">{row.leader_source ?? NO_VALUE}{row.leader_kind === "vault" ? " · vault" : ""}</td>
      <td className="mono">{fmtAmount(cap?.allocated_capital)}</td>
      <td className="mono">{fmtRatioPct(cap?.capital_utilization)}</td>
      <td className="mono">
        {cap?.use_full_equity == null ? NO_VALUE : cap.use_full_equity ? h.yes : h.no}
      </td>
      <td className="mono">{cap?.source ?? NO_VALUE}</td>
      <td className="mono" title={row.last_cycle?.detail ?? undefined}>
        {row.last_cycle?.result ?? NO_VALUE}
      </td>
    </tr>
  );
}

/** 換 leader 積壓。⭐ 查不下去 → 明說「無從得知」＋原因，**不顯示 0**。 */
function BacklogBlock({ summary, entries }: {
  summary: OpsHealthSummary; entries: OpsUnappliedLeaderChange[];
}) {
  const unknown =
    summary.unapplied_leader_changes == null || summary.leader_change_errors.length > 0;
  return (
    <div className="panel ops-drift">
      <h3 className="ops-drift-title">
        {h.backlogTitle}
        {!unknown && <span className="ops-drift-count">{entries.length}</span>}
      </h3>
      <p className="hint">{h.backlogNote}</p>
      {unknown ? (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{h.backlogUnknownTitle}</p>
          <p className="ops-alert-body">{h.backlogUnknownBody}</p>
          <ul className="ops-error-list">
            {summary.leader_change_errors.map((e, i) => <li key={i} className="mono">{e}</li>)}
          </ul>
        </div>
      ) : entries.length === 0 ? (
        <p className="hint">{h.backlogEmpty}</p>
      ) : (
        <table className="admin-table ops-table">
          <thead>
            <tr>
              <th scope="col">{h.backlogCols.account}</th>
              <th scope="col">{h.backlogCols.nonce}</th>
              <th scope="col">{h.backlogCols.age}</th>
              <th scope="col">{h.backlogCols.reason}</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, i) => (
              <tr key={`${e.account_id}-${e.nonce ?? i}`}>
                <td className="mono">{e.account_id}</td>
                <td className="mono">{e.nonce ?? NO_VALUE}</td>
                <td className="mono">{fmtAge(e.age_s)}</td>
                <td>{reasonText(e.reason)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** 機器可讀碼 → 人話。未知碼**原樣顯示**（新增了一種原因而前端還沒補文案時，
 *  顯示代碼雖然醜，但至少看得到「有這一項」；靜默略過會讓一列憑空消失）。 */
function reasonText(reason: string): string {
  const known = h.reasons as Record<string, string | undefined>;
  return known[reason] ?? `${h.reasonUnknownPrefix}${reason}`;
}
