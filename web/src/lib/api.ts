/**
 * lib/api.ts — 後端 Public API 的唯一出口（工程原則 5 的前端鏡射）。
 * 一律同源相對路徑 + credentials:"include"（紅線 5）。
 * 錯誤分類（工程原則 2）：auth(401)/client(4xx)/upstream(502|503)/network。
 * ⭐ 紅線 3：帶簽名的後端呼叫只有四支，四支都是 EIP-191 personal_sign，且四支的
 *   **原文都由伺服器產生**（前端不組字串）：
 *     1. authVerify —— SIWE 登入簽名；
 *     2. postLeaderSelect —— 換 leader 授權簽名（原文來自 getLeaderSelectMessage）。
 *     3. postMyRisk —— 風控設定授權簽名（原文來自 getRiskSettingsMessage）；
 *     4. postRiskUnlock —— 解除熔斷授權簽名（原文來自 getRiskUnlockMessage）。
 *   後兩支的域分隔（一份「調門檻」的簽章不得被兌換成一次「開熔斷鎖」，反之亦然）
 *   由伺服器版型的第一行在結構上確立，見 src/spark/filet/risk_settings.py 檔頭。
 *   EIP-712 的鏈上授權簽名走 lib/hl.ts 直送 HL，本模組結構上沒有那條路。
 */
import type { HlTypedData } from "./hl";

export type ApiErrorKind = "auth" | "client" | "upstream" | "network";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly detail?: string;
  /**
   * 機器可判的分類碼——僅在後端 detail 為 `{reason, message}` 物件時存在
   * （目前只有 /api/leaders/preview 與自訂 leader 准入相關端點）。
   * ⭐ 呼叫端要分流時看這個欄位，不要對 `detail` 的人話字串做字串比對
   * （文案會改，分類碼不會——分類在邊界完成，工程原則 5）。
   */
  readonly reason?: string;

  constructor(
    kind: ApiErrorKind, message: string, status?: number, detail?: string, reason?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
    this.reason = reason;
  }
}

export interface Me { address: string; account_id: string }
export interface NonceResp { nonce: string; message: string }
export interface AgentResp { agent_address: string; recovered?: boolean }
export interface TypedDataResp { typed_data: HlTypedData }
/**
 * 錢卡在 **spot** 錢包的提示（`/api/onboard/status` 的 `spot_stranded`）。
 *
 * ⚠️⚠️ 本結構**刻意不含**任何劃轉入口、代簽 payload 或「幫我轉」旗標，顯示層也
 * 絕不得長出一顆這種按鈕：spot → perp 是 user-signed action，需要客戶的**主鑰**才簽
 * 得動，而我方結構上不持有主鑰（非託管不變量）。做得到的只有「說明 ＋ 外部連結」。
 *
 * 型別註記（實測後端 `publicapi/app.py::_spot_stranded`）：`usdc`／`threshold` 是
 * Decimal → **string**（落地慣例）；`action_required` 目前恆為
 * `"manual_transfer_spot_to_perp"`——名字本身就在說「只能人工」。
 */
export interface SpotStranded {
  usdc: string;
  threshold: string;
  action_required: string;
  note: string;
}

export interface OnboardStatus {
  address: string;
  account_id: string;
  agent_address: string | null;
  agent_generated: boolean;
  builder_fee_approved: boolean;
  agent_approved: boolean;
  funded: boolean;
  /**
   * ⭐ `funded` 判定所用的 **perp 帳戶淨值**（marginSummary.accountValue，字串以
   * 免精度損失）與門檻，由後端同一次讀取產出——顯示這個數字而不是自己另外查，
   * 是為了讓畫面上的餘額與擋下客戶的那個值結構上不可能不一致（工程原則 1）。
   *
   * ⭐ 兩者**永不為 null**：後端讀不到餘額時整個端點回 502（把「讀不到」吞成 0
   * 會讓它偽裝成「沒錢」），所以前端不必畫「未知餘額」狀態。
   */
  perp_account_value: string;
  min_deposit: string;
  /** 還差多少才達門檻（後端以 Decimal 算，已達標為 `"0"`）。 */
  deposit_shortfall: string;
  /**
   * `null` ＝ 沒有卡住的錢**或**查詢失敗——後端刻意把兩者合成同一個值
   * （`_spot_stranded` 對查詢失敗 fail-silent：對一個已正常跟單的客戶顯示
   * 「你的錢卡在 spot」是純粹的困惑，假警報比沒提示糟）。
   * ⭐ 所以前端對 `null` 的正確反應是**完全不顯示**，不是顯示「無卡住資金」——
   * 後者會把一個「我們也不知道」的狀態畫成一句肯定的斷言。
   */
  spot_stranded: SpotStranded | null;
  state: "READY" | "IN_PROGRESS";
}
export interface PendingEntry {
  account_id: string;
  user_address: string;
  builder_address: string;
  network: string;
  agent_address: string;
  label: string;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      credentials: "include",
      headers: init.body != null ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw new ApiError("network", "無法連線到伺服器，請檢查網路後重試");
  }
  if (res.ok) return (await res.json()) as T;
  const raw = await res
    .json()
    .then((b: { detail?: unknown }) => b?.detail)
    .catch(() => undefined);
  // 後端 detail 兩種形狀：字串（多數端點）或 `{reason, message}` 物件（自訂 leader
  // 准入，reason 為機器可判分類碼）。在邊界拆開，呼叫端拿到的 detail 恆為人話字串。
  const structured =
    raw !== null && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as { reason?: unknown; message?: unknown })
      : undefined;
  const detail =
    typeof raw === "string"
      ? raw
      : structured && typeof structured.message === "string"
        ? structured.message
        : undefined;
  const reason =
    structured && typeof structured.reason === "string" ? structured.reason : undefined;
  if (res.status === 401) throw new ApiError("auth", detail ?? "未登入", 401, detail, reason);
  if (res.status === 502 || res.status === 503) {
    throw new ApiError("upstream", detail ?? "上游服務暫時不可用", res.status, detail, reason);
  }
  // 非 502/503 的其他 5xx（如 500）目前歸類 client：後端現況不產生此類回應，
  // 若未來出現需重新評估歸 upstream（並補對應測試），此處僅記錄現況假設。
  throw new ApiError(
    "client", detail ?? `請求失敗（HTTP ${res.status}）`, res.status, detail, reason);
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// ---------- auth ----------
export function getNonce(address: string, chainId: number): Promise<NonceResp> {
  const q = new URLSearchParams({ address, chain_id: String(chainId) });
  return request<NonceResp>(`/api/auth/nonce?${q.toString()}`);
}

/** 唯一帶簽名的後端呼叫：SIWE 登入簽名（EIP-191）。 */
export function authVerify(nonce: string, signature: string): Promise<Me> {
  return post<Me>("/api/auth/verify", { nonce, signature });
}

export function logout(): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/auth/logout");
}

export function getMe(): Promise<Me> {
  return request<Me>("/api/me");
}

/**
 * 「我目前跟隨的 leader」的四種狀態（後端 `me_leader` 的 `status`，改一邊要改兩邊）。
 * ⭐ 後端刻意**不用 null 讓前端猜**：`leader_address` 為 null 時只有 `status` 分得出
 * 「還沒活化」與「已活化但用引擎預設」——後者**是真的在跟單**，只是跟的對象由部署
 * 決定。把兩者合併成一句「你沒有 leader」，會讓一個正在跟單的客戶以為資金沒在動。
 */
export type MyLeaderStatus =
  /** manifest 明確指定了 leader（`leader_address` 必為非 null）。 */
  | "following"
  /** 已活化但未指定 leader，引擎沿用進程 env 的預設——仍在跟單。 */
  | "engine_default"
  /** 帳號不在 manifest（活化是人工 CLI 動作）。 */
  | "not_activated"
  /** 帳號不在 manifest **且** manifest 有壞條目——壞的那筆可能就是他自己的。 */
  | "indeterminate";

/**
 * 已簽署、尚未反映在 manifest 的換 leader 記錄（後端 `_pending_leader_change`）。
 * ⚠️ 後端只投影這四個欄位——signature 與 message 原文結構上不外流。
 */
export interface MyLeaderPendingChange {
  leader_address: string;
  issued_at: string | null;
  /** 機器可讀語意，目前恆為 `next_engine_cycle`。 */
  effective: string;
  /** 後端寫給人看的原文；顯示層原樣呈現，不在前端另寫一份。 */
  note: string;
}

/**
 * 我目前跟隨的 leader（`/api/me/leader`）。
 *
 * ⭐ `leader_name` 只從精選白名單查得到，查無**不代表位址有問題**——只代表它不在
 * 目前的可選清單裡（自訂 leader、或已下架的精選 leader 都會是 null）。顯示層因此
 * 必須能只靠位址把這一區畫完整，不得把「沒有名稱」當成「沒有 leader」。
 *
 * ⚠️ 錯誤方向不對稱：manifest 讀不到時後端回 **503**（不是「你沒在跟單」）。
 * 顯示層對這個錯誤的正確反應是「暫時讀不到」，把它畫成「未在跟單」會讓一個正在
 * 跟單的客戶以為資金沒在動而不去看它（工程原則 3：讀不到 ≠ 沒有）。
 */
export interface MyLeaderResp {
  account_id: string;
  status: MyLeaderStatus;
  /** `following` 以外的狀態恆為 null（後端 `me_leader` 的結構性不變式）。 */
  leader_address: string | null;
  leader_name: string | null;
  pending_change: MyLeaderPendingChange | null;
  /** 每一種狀態各有一句後端原文；顯示層原樣呈現（單一來源）。 */
  note: string;
}

/**
 * 我目前跟隨的 leader（需 session）。⭐ **沒有任何 account 參數**——account_id 由
 * session 衍生，所以結構上不可能查到別人的（沿後端「別人不能替你 onboard」的慣例）。
 * manifest 讀不到 → 503（kind=upstream）。冪等讀取，重試安全。
 */
export function getMyLeader(): Promise<MyLeaderResp> {
  return request<MyLeaderResp>("/api/me/leader");
}


// ---------- onboarding ----------
export function createAgent(): Promise<AgentResp> {
  return post<AgentResp>("/api/onboard/agent");
}

export function getStatus(): Promise<OnboardStatus> {
  return request<OnboardStatus>("/api/onboard/status");
}

export function postVerify(): Promise<OnboardStatus> {
  return post<OnboardStatus>("/api/onboard/verify");
}

// ---------- 風控偏好（錢包主人自選，2026-07-30；對照 publicapi/app.py /api/me/risk）----------

/** 可調參數的識別碼（後端 `RISK_PARAM_SPECS` 的 `name` 欄，改一邊要改兩邊）。 */
export type RiskParamName =
  | "size_tolerance"
  | "max_drawdown_pct"
  | "max_total_drawdown_pct"
  | "flatten_on_breach"
  | "cooldown_hours";

/**
 * 一個可調參數的規格。⭐ **由後端供給**（`specs`），前端不硬編任何上下界、預設值
 * 或建議值：引擎的合法區間定義在 `src/spark/filet/risk_prefs.py`，前端另存一份的
 * 下場是「畫面允許 0.01、API 收下、引擎拒絕啟動」，而症狀出現在啟用那一刻。
 */
export interface RiskParamSpec {
  name: RiskParamName;
  env: string;
  type: "decimal" | "bool";
  /**
   * ⭐ `tracking` 的參數**不受風控開關影響**，永遠生效——顯示層必須把它畫在啟用
   * checkbox **之外**。藏在 checkbox 底下會讓關掉風控的客戶以為自己也關掉了它。
   * `risk` 的參數只有啟用風控時才生效。
   */
  group: "tracking" | "risk";
  /** 目前只有 `hours`；缺席＝比例（顯示層據此決定要不要做百分比換算）。 */
  unit?: "hours";
  default: string | boolean;
  /**
   * ⭐ 我方的建議值。與 `default` 分成兩欄是刻意的：預設值是「沒表達時系統用什麼」，
   * 建議值是「我們認為你該用什麼」，兩者目前相同但語意不同，顯示層必須顯示這一欄
   * （客戶自行決定門檻的前提是看得到我們的建議）。
   */
  recommended: string | boolean;
  min: string | null;
  max: string | null;
  label: string;
  help: string;
}

export interface RiskPrefs {
  enabled: boolean;
  size_tolerance: string;
  max_drawdown_pct: string;
  max_total_drawdown_pct: string;
  flatten_on_breach: boolean;
  cooldown_hours: string;
}

/** 已簽章提交的那一刻（`null` ＝ 客戶從未提交過任何設定）。 */
export interface RiskSubmittedInfo {
  issued_at: string | null;
}

/**
 * 引擎**當輪實際採用**的風控姿態（心跳投影）。
 * ⚠️ 目前只投影總開關（`controls_enabled`）——逐項門檻不在心跳裡，所以顯示層只能
 * 就這一欄比對「已提交 vs 已生效」，不得宣稱逐項都已生效。
 * `controls_enabled` 為 null ＝ 心跳在但這一欄讀不到（同樣是「我們不知道」）。
 */
export interface RiskAppliedInfo {
  controls_enabled: boolean | null;
  /** 這組值從哪來（已簽署的設定／預設值…），由引擎在同一次求值中標記。 */
  source: string;
  changed_at: string | null;
  /**
   * ⭐ 引擎**實際在執法**的那一組門檻（canonical 字串，與你簽的同源）。
   * `null` ＝引擎版本較舊、心跳還沒帶這一格 ⇒ 顯示層必須說「無法確認」，
   * 不得只比對開關就宣稱逐項都已生效（把上限從 20% 調到 10% 時兩邊開關都是
   * 「開」，只比開關會顯示「已生效」而引擎其實還在用舊門檻）。
   */
  prefs: RiskPrefs | null;
}

/**
 * 熔斷（kill switch）現況。
 * ⚠️ `reason === "leader_revoked"` 是**治理動作**（我方撤下 leader），客戶不得自助
 * 解除——顯示層必須據此**不畫**「立即恢復跟單」按鈕，改顯示說明。
 */
export interface RiskHaltedInfo {
  tripped: boolean;
  reason: string | null;
  tripped_at: string | null;
  /**
   * ⭐ 這次鎖定可否由你自己解除——由**引擎**的同一份判定（`rearm_allowed_for`）
   * 導出，顯示層不得自己比對 `reason` 字串（引擎新增一種不可恢復的原因時，
   * 各自比對的那一邊會給出一顆按了必然失敗的按鈕）。
   * `null` ＝引擎版本較舊、判不出來 ⇒ 同樣不給按鈕。
   */
  resumable: boolean | null;
  /**
   * ⭐ 熔斷當下有部位未平掉／掛單未撤。**不影響** `resumable`（客戶仍可自助恢復，
   * 恢復本身就是收拾殘局的手段），但必須顯示出來——客戶按下那顆按鈕之前有權知道
   * 市場上還留著什麼。`null` ＝引擎版本較舊或讀不到。
   */
  residual_exposure: boolean | null;
  /** 熔斷當下生效的冷靜期（小時）；`null` ＝ 讀不到。 */
  cooldown_hours: string | null;
  /** 依冷靜期推算的自動恢復時刻；`null` ＝ 不會自動恢復（冷靜期 0）或算不出來。 */
  resume_at: string | null;
}

export interface MyRiskResp {
  /** 已「簽章提交」的偏好（不是引擎當下採用的值——那是 `applied`）。 */
  prefs: RiskPrefs;
  specs: RiskParamSpec[];
  defaults: RiskPrefs;
  submitted: RiskSubmittedInfo;
  /**
   * ⭐⭐ `null` ＝引擎心跳過期或缺席＝**我們不知道**。顯示層對 null 的正確反應是
   * 說「暫時無法確認」，**不得**畫成「尚未生效」——後者是一句我們沒有根據的斷言
   * （工程原則 3：讀不到 ≠ 沒有）。
   */
  applied: RiskAppliedInfo | null;
  /**
   * ⭐⭐ 同上：`null` **不是**「沒有熔斷」。把「讀不到」畫成「一切正常」，等於在
   * 客戶的引擎已經停止跟單的當下告訴他一切照常。
   */
  halted: RiskHaltedInfo | null;
  /**
   * 現在恆為 `true`（設定改為簽章提交後，任何時候都能改，引擎下一輪套用）。
   * 型別保留這一欄是為了讓後端未來要收回編輯權時，顯示層有地方接住。
   */
  editable: boolean;
}

export function getMyRisk(): Promise<MyRiskResp> {
  return request<MyRiskResp>("/api/me/risk");
}

/**
 * 風控設定的 canonical 待簽原文 ＋ 一次性 nonce。⭐ `prefs` 是**伺服器正規化後**的
 * 偏好回聲（`0.20` → `0.2`）：客戶端必須拿它與自己送出的值逐欄位比對，再比對原文
 * 本體——伺服器回一份指向別的設定的原文時，必須在**進錢包之前**擋下。
 */
export interface RiskSettingsMessageResp {
  message: string;
  nonce: string;
  issued_at: string;
  account_id: string;
  prefs: RiskPrefs;
}

/** 設定生效的回應。`effective_note` 是後端寫給人看的原文（單一來源）。 */
export interface RiskSettingsResp {
  ok: boolean;
  prefs: RiskPrefs;
  effective_note: string;
}

/** 解除熔斷的 canonical 待簽原文（一次性動作，**沒有** prefs）。 */
export interface RiskUnlockMessageResp {
  message: string;
  nonce: string;
  issued_at: string;
  account_id: string;
}

export interface RiskUnlockResp {
  ok: boolean;
  effective_note: string;
}

/**
 * 取風控設定的待簽原文（需 session）。⭐ 本呼叫**不帶簽名**：它只是請伺服器就這組
 * 偏好產生一份 canonical 原文，值超界 → 400（kind=client）。
 */
export function getRiskSettingsMessage(prefs: RiskPrefs): Promise<RiskSettingsMessageResp> {
  return post<RiskSettingsMessageResp>("/api/me/risk/message", { prefs });
}

/**
 * 送出風控設定授權。⭐ 沿 `postLeaderSelect` 的形狀，刻意收**整包 payload 物件**
 * 而不是散裝欄位：伺服器驗簽時會用 account_id／prefs／nonce／issued_at **重建**
 * 訊息再 recover，客戶端若從別處（例如畫面上的草稿）拼一個欄位進來，就會出現
 * 「簽的是 A、送的是 B」的縫——症狀是「我本人簽的卻一直被拒」，而兩邊看起來都
 * 完全正常。由本函式從同一個 payload 物件取全部欄位＝結構上不可能拼錯（工程原則 1）。
 *
 * 非冪等寫入 ＋ nonce 一次性：**不得自動重試**（重送必因 nonce 已消耗而失敗）。
 */
export function postMyRisk(
  payload: RiskSettingsMessageResp,
  signature: string,
): Promise<RiskSettingsResp> {
  return post<RiskSettingsResp>("/api/me/risk", {
    account_id: payload.account_id,
    prefs: payload.prefs,
    nonce: payload.nonce,
    issued_at: payload.issued_at,
    signature,
    message: payload.message,
  });
}

/** 取「立即恢復跟單」的待簽原文（需 session）。沒有熔斷時後端回 4xx。 */
export function getRiskUnlockMessage(): Promise<RiskUnlockMessageResp> {
  return post<RiskUnlockMessageResp>("/api/me/risk/unlock/message");
}

/**
 * 送出解除熔斷授權。⭐ 與 `postMyRisk` 是**兩個端點、兩份原文、兩種動作**：一份
 * 「調整門檻」的授權絕不能被兌換成一次「把熔斷鎖打開」（反向亦然），域分隔由
 * 伺服器版型的第一行結構性地確立（filet/risk_settings.py 檔頭）。同樣不得自動重試。
 */
export function postRiskUnlock(
  payload: RiskUnlockMessageResp,
  signature: string,
): Promise<RiskUnlockResp> {
  return post<RiskUnlockResp>("/api/me/risk/unlock", {
    account_id: payload.account_id,
    nonce: payload.nonce,
    issued_at: payload.issued_at,
    signature,
    message: payload.message,
  });
}

export function getApproveAgentPayload(chainId: number): Promise<TypedDataResp> {
  return post<TypedDataResp>("/api/onboard/payload/approve-agent", { chain_id: chainId });
}

export function getApproveBuilderFeePayload(chainId: number): Promise<TypedDataResp> {
  return post<TypedDataResp>("/api/onboard/payload/approve-builder-fee", { chain_id: chainId });
}

// ---------- leaders（leader 目錄與換 leader 授權；對照 publicapi/app.py 三個端點） ----------
/**
 * 目錄的一位 leader。⭐ **平鋪的統計欄位**是 watchlist 每日快照的**資產負債切面**，
 * 不是績效——報酬率與回撤一律只在 `performance` 子物件裡（兩者分屬兩層是後端刻意的
 * 設計，見該欄位註解）。欄位名刻意與後端／快照原名一字不差
 * （app.py 的 `_LEADER_STAT_FIELDS`）——在任何一層改名成看起來像績效的東西，
 * 都會讓使用者把「規模」讀成「賺多少」。顯示層同理：叫什麼就顯示什麼。
 *
 * 金額為字串（後端 Decimal 無損序列化，沿 ops 慣例）；該 leader 不在快照中時
 * 各欄為 null——**不是 0**（0 會被讀成「這個 leader 沒有部位」，是有意義且錯誤的訊息）。
 */
export interface LeaderEntry {
  address: string;
  name: string;
  description: string;
  /** 帳戶淨值（規模）。 */
  account_value: string | null;
  /** 名目部位總額（當下曝險）。 */
  total_ntl_pos: string | null;
  /** 未實現損益（當下浮動，非已實現報酬）。 */
  unrealized_pnl: string | null;
  position_count: number | null;
  /**
   * 績效（perp 窗）。⭐ 與上面的**規模**欄位刻意分屬兩層，理由見後端
   * `_LEADER_PERF_WINDOWS` 上方註解：資產負債切面與報酬率混在同一層，遲早有人
   * 把其中一個讀成另一個，而那個誤讀在畫面上完全看不出來。
   *
   * `null`／缺席 = 這位 leader 沒有績效資料——**不是**「績效為 0」。
   */
  performance?: LeaderPerf | null;
}

/**
 * 一位 leader 的績效，依窗切分（後端 `_LEADER_PERF_WINDOWS`：perpMonth／perpAllTime）。
 * 窗本身也可能缺席（該窗算不出來），所以值是 optional。
 */
export type LeaderPerf = { [window: string]: LeaderPerfWindow | undefined };

/**
 * 一個績效窗的原始線上形狀（`_LEADER_PERF_FIELDS` 白名單投影）。
 *
 * ⭐⭐ 揭露模型改版（2026-07-19，後端 `filet/leader_perf.py` 檔頭）：
 * **舊行為**——資料不足時後端**不給鍵**，缺鍵＝不該顯示。
 * **新行為**——資料不足時**照樣給數字**，警示改由一組 `*_insufficient_data`
 * 布林標記承載。所以 `twr`／`max_drawdown`／`annualized_return` 現在幾乎恆存在，
 * 唯一的缺席情形是**年化在數學上無定義**（`1+TWR <= 0`，帳戶被歸零）——此時
 * 三個 `annualized_*` 鍵**一起**缺席（標記絕不單獨存在）。
 *
 * ⚠️ 這個改版把最危險的失敗方向從「憑空造出數字」換成了「**把數字送出去而沒有
 * 它的警示**」：一個 7 天 +3% 的 leader，年化會算成 365%，數字本身完全正常，
 * 少掉標記則畫面上完全看不出它是外推的。因此標記是**強制欄位**：
 * 顯示層拿不到標記就不放行數字（fail closed）。
 *
 * 型別註記（**實測自後端**，不是推測）：後端內部一律 Decimal，
 * `leader_perf.jsonable_performance` 落地時 `str(v) if isinstance(v, Decimal)`，
 * 所以 `covered_days`／`cum_pnl`／`twr`／`max_drawdown`／`annualized_return`／
 * `annualized_return_extrapolated_from_days` 在 JSON 裡是 **string**；
 * `sample_count`／`skipped_intervals`／`first_ts_ms`／`last_ts_ms` 是 **number**；
 * 三個 `*_insufficient_data` 是**原生 bool**（後端刻意不轉字串——`"False"` 這種
 * 字串的真值是 **true**，轉了就是一個致命的無聲反轉）。
 */
export interface LeaderPerfWindow {
  /** 窗名，例如 `perpMonth`。 */
  period: string;
  /** 基準，目前恆為 `perp`。 */
  basis: string;
  /** `ok`／`insufficient`。 */
  status: string;
  /** 資料不足的機器可讀原因碼；`ok` 時為 null。 */
  reason: string | null;
  /**
   * `insufficient`／`pnl_only`／`window_return`／`annualizable`。
   * ⚠️ 改版後這是 `covered_days` 的**純函式**，**不再**決定哪些鍵存在，因此也
   * 不再是「能不能顯示」的授權——顯示與否看數字與標記是否成對到齊。
   */
  disclosure_tier: string;
  /** 樣本點數（number）。 */
  sample_count: number;
  /** 涵蓋天數。Decimal → **string**（例如 `"45.2000"`）；資料不足時為 null。 */
  covered_days: string | null;
  first_ts_ms: number | null;
  last_ts_ms: number | null;
  skipped_intervals: number;
  cum_pnl?: string;
  twr?: string;
  /** `covered_days < 30` → true。⭐ 與 `twr` **同生共死**，缺了就不准顯示 `twr`。 */
  twr_insufficient_data?: boolean;
  max_drawdown?: string;
  /** `covered_days < 30` → true。⭐ 與 `max_drawdown` 同生共死。 */
  max_drawdown_insufficient_data?: boolean;
  /** 缺席＝年化在數學上無定義（此時另兩個 `annualized_*` 也一起缺席）。 */
  annualized_return?: string;
  /** `covered_days < 90` → true。 */
  annualized_return_insufficient_data?: boolean;
  /**
   * 年化實際涵蓋的天數（Decimal → **string**，例如 `"6.0000"`）。後端**無條件**
   * 回傳（不是只在不足時才給）：年化本質上就是外推，前端要能一律寫出「由 N 天外推」。
   */
  annualized_return_extrapolated_from_days?: string;
}

/**
 * 績效的揭露文案，由後端 `leader_perf` 的常數供給（**單一來源**：計算的極限與
 * 呈現的警語必須出自同一處，否則改了公式而文案還停在舊說法）。
 * 全部 optional：舊版後端沒有這些欄位時，顯示層改用自己的等義文案——
 * ⭐ 警語與數字的規則相反：**數字**缺了就不顯示，**警語**缺了要補上。
 * 少一個數字只是少一個數字，少一句警語則是把數字送出去而沒有它的極限說明。
 */
export interface LeaderPerfNotes {
  basis?: string;
  upper_bound?: string;
  max_drawdown?: string;
  sufficiency?: string;
}

/**
 * 目錄回應裡的績效**中繼資訊**（與逐 leader 的 `performance` 分開）。
 * 全部 optional：現行後端一律回傳，但顯示層對缺席**fail closed**
 * （`performance_available` 不是明確的 true 就整區不畫，見 /leaders 顯示層）。
 */
export interface LeadersPerfMeta {
  /**
   * ⭐ 與 `stats_available` 是**兩個獨立**的旗標，不可合併（後端註解明寫）：
   * 快照可能存在（規模欄位有值）卻沒有績效。合併會讓「有規模沒績效」時
   * 把有效資料也一起藏起來。
   */
  performance_available?: boolean;
  performance_basis?: string;
  /** 窗的顯示順序以後端這份清單為準（單一來源）。 */
  performance_windows?: string[];
  performance_notes?: LeaderPerfNotes;
}

/**
 * leader 目錄。⭐ 兩種形狀，判別欄位 `stats_available`：
 * 快照不可用時後端**不給**時間戳、只給 `note`——型別上就讀不到日期，顯示層因此
 * 不可能畫出「沒有時點的數字」（工程原則 1 的變形：連時點都不同源的兩個數字不可比）。
 * 快照可用時 `stats_day`／`stats_as_of` 仍可能為 null（快照缺欄位）——顯示層必須把
 * 「沒有時間戳」視同「不可顯示數字」，一份三天前的切面沒有時點就會被當成即時數字讀。
 */
export type LeadersResp = LeadersPerfMeta & (
  | {
      leaders: LeaderEntry[];
      stats_available: true;
      stats_day: string | null;
      stats_as_of: string | null;
      note: null;
    }
  | {
      leaders: LeaderEntry[];
      stats_available: false;
      stats_day: null;
      stats_as_of: null;
      /** 後端寫好的原因說明；顯示層原樣呈現，且**不得**顯示任何數字（沿 ops basis_unknown）。 */
      note: string;
    }
);

/** 換 leader 的 canonical 待簽原文 ＋ 一次性 nonce（原文由伺服器產生，前端不重組）。 */
export interface LeaderSelectMessageResp {
  message: string;
  nonce: string;
  issued_at: string;
  leader_address: string;
  account_id: string;
}

/** 授權成功的回應。⭐ `effective`＝機器可讀語意，後兩個字串是後端寫給人看的原文。 */
export interface LeaderSelectResp {
  ok: boolean;
  account_id: string;
  leader_address: string;
  effective: string;
  effective_note: string;
  consequences: string;
}

/** leader 目錄（需 session）。白名單載入失敗 → 503（kind=upstream）。 */
export function getLeaders(): Promise<LeadersResp> {
  return request<LeadersResp>("/api/leaders");
}

/**
 * 自訂 leader 的准入預覽（2026-07-27 spec；對照 app.py /api/leaders/preview）。
 * `account_value` 是 Decimal → **string**（落地慣例，不是 number）。
 * `already_listed=true` ⇒ 位址已在精選清單且可選，後續流程視同選擇精選 leader。
 */
export interface LeaderPreviewResp {
  /** 伺服器正規化（小寫）後的位址——後續簽章流程以此為錨。 */
  address: string;
  exists: boolean;
  account_value: string;
  position_count: number;
  already_listed: boolean;
  /**
   * ⭐ 例行下架旗標（2026-07-27）：false ＝ 此 leader `accepting_new=false`
   * （名額調整／準備退場，只擋新客戶——引擎照跟）。**放行**、由顯示層畫警示但
   * 不擋送出。與 `leader_disabled` 的**安全撤銷**（enabled=false，硬拒絕）分兩支。
   */
  accepting_new: boolean;
  /**
   * vault 契約（2026-07-31 跨 wave 契約）：此位址是不是 Hyperliquid vault。
   * **資訊性標示，不是警告**——顯示層加標示與說明，不新增任何步驟、不擋送出。
   * optional：舊後端不回此欄，undefined 一律當 "standard"。
   */
  kind?: "standard" | "vault";
  /**
   * advisory 檢查結果；契約規定僅 kind=="vault" 時非 null，failures 只列 FAIL 項
   * （PASS 不回細節；資訊性 WARN 不算 FAIL）。optional 同上：undefined 當無檢查。
   * 只有 `passed=false` 才由顯示層畫警語——同樣不擋送出（advisory，非閘門）。
   */
  vault_checks?: null | {
    passed: boolean;
    failures: { name: string; detail: string }[];
  };
}

/**
 * 准入預覽（需 session；唯讀、無副作用）。檢查不過 → 4xx，`ApiError.reason` 帶
 * 機器可判分類碼（invalid_format／self_follow／leader_disabled），`detail` 帶後端
 * 的人話說明。分流一律看 reason，不對 detail 做字串比對。
 * ⚠️ 鏈上無活動自 2026-07-27 裁決後**不再是 4xx**：回 200 帶 exists=false（放行），
 * 顯示層據此畫警示但不擋（見 customLeader.ts）。
 */
export function getLeaderPreview(leaderAddress: string): Promise<LeaderPreviewResp> {
  const q = new URLSearchParams({ leader_address: leaderAddress });
  return request<LeaderPreviewResp>(`/api/leaders/preview?${q.toString()}`);
}

/** 取待簽原文（需 session）。leader 不可選 → 400（kind=client）。 */
export function getLeaderSelectMessage(leaderAddress: string): Promise<LeaderSelectMessageResp> {
  const q = new URLSearchParams({ leader_address: leaderAddress });
  return request<LeaderSelectMessageResp>(`/api/leaders/select/message?${q.toString()}`);
}

/**
 * 送出換 leader 授權。⭐ 刻意收**整包 payload 物件**而不是散裝欄位：伺服器驗簽時
 * 會用 account_id／leader_address／nonce／issued_at **重建**訊息再 recover，客戶端
 * 若從別處（例如 session 的 me.account_id）拼一個欄位進來，就會出現「簽的是 A、
 * 送的是 B」的縫——症狀是「我本人簽的卻一直被拒」，而兩邊看起來都完全正常。
 * 由本函式從同一個 payload 物件取全部欄位＝結構上不可能拼錯（工程原則 1）。
 * `message` 原文原樣回送（後端僅稽核留存，一個位元組差異都不該由前端製造）。
 *
 * 非冪等寫入 ＋ nonce 一次性：**不得自動重試**。重送同一筆會因 nonce 已消耗而必然
 * 失敗；要重來必須整條流程重跑（重取原文、新 nonce、重簽），由使用者按鈕觸發。
 */
export function postLeaderSelect(
  payload: LeaderSelectMessageResp,
  signature: string,
): Promise<LeaderSelectResp> {
  return post<LeaderSelectResp>("/api/leaders/select", {
    account_id: payload.account_id,
    leader_address: payload.leader_address,
    nonce: payload.nonce,
    issued_at: payload.issued_at,
    signature,
    message: payload.message,
  });
}

// ---------- admin ----------
export function getAdminPending(): Promise<{ pending: PendingEntry[] }> {
  return request<{ pending: PendingEntry[] }>("/api/admin/pending");
}

// ---------- ops（管理端；跨客戶聚合） ----------
/**
 * ⭐ 金額欄一律 string：後端 ops.jsonable 把 Decimal 序列化成字串（float 會有精度
 * 損失，對帳數字不得走 float）。前端也不在型別層把它們變回 number——只在顯示的
 * 最後一刻 parse（lib/format.ts），算術比較留在後端。
 */
export interface OpsCustomerRow {
  account_id: string;
  user_address: string;
  label: string;
  network: string;
  fills: number;
  notional: string;
  builder_fee: string;
  taker_share: string;
  account_value: string | null;
  subscription: string;
  /** 該列查詢失敗的原文（跨客戶隔離：其他列照樣有資料）。 */
  error: string | null;
}

/**
 * 每客戶損益，**三種**回應形狀，判別欄位為 `window`（＋ accrued 下的 `basis_unknown`）。
 * 沿 OpsRevenueResp 的 discriminated union 慣例：漏處理分支時 TS 編譯期報錯。
 *
 * ⭐ `window: "accrued"` 是本端點**唯一**能與 /api/ops/revenue 並排相減的模式——
 * 兩者共用後端 `ops.accrued_window()`，window_start/window_end 必為同值（結構性同源）。
 * `window: "days"` 是自由檢視窗（now 往回 N 天），與對帳窗必定錯開，兩張表不可相減。
 */
export type OpsCustomersResp =
  | {
      window: "days";
      days: number;
      start: string;
      end: string;
      window_start: string;
      window_end: string;
      customers: OpsCustomerRow[];
      /** 壞掉的 manifest 條目（容錯載入跳過者）。 */
      manifest_errors: string[];
    }
  | {
      window: "accrued";
      basis_unknown: false;
      start: string;
      end: string;
      window_start: string;
      window_end: string;
      customers: OpsCustomerRow[];
      manifest_errors: string[];
    }
  | {
      window: "accrued";
      basis_unknown: true;
      /** ⭐ 窗口界只能來自快照時刻；缺了就沒有正確答案 → 後端回 null，不用日曆日猜。 */
      window_start: null;
      window_end: null;
      /** 後端寫好的原因說明；顯示層原樣呈現，且**不得**顯示任何數字。 */
      note: string;
      /** ⭐ 本分支後端不給 `customers` 也不給 `days`——型別上就讀不到（不會畫成空表）。 */
      manifest_errors: string[];
    };

/**
 * customers 的時間窗參數：`days` 與 `window=accrued` **互斥**（同時給後端回 400）。
 * ⭐ 用 optional-never 讓「同時給」在型別層就不可能——把後端的 400 提前到編譯期，
 * 靜默走錯基準正是本頁要消滅的失敗模式（工程原則 1）。
 */
export type OpsCustomersQuery =
  | { days: number; window?: never }
  | { window: "accrued"; days?: never };

/**
 * 收入對帳，**三種**回應形狀，兩層判別欄位（discriminant）：
 *   1. `insufficient_accrued_history: true` — 歷史不足兩點。
 *   2. `basis_unknown: true` — 歷史有兩點但快照時刻缺漏／非嚴格遞增（回填或時鐘倒退），
 *      窗口無從對齊。
 *   3. 兩者皆 false — 正常對帳結果。
 * ⭐ 前兩種後端**不給**任何數值欄——型別上就讀不到，避免把「無資料」顯示成 0。
 * 這裡刻意用 discriminated union 而非全欄位 optional：漏處理分支時 TS 會在編譯期報錯，
 * 而不是在畫面上渲染出 undefined（結構性擋掉，不靠人記得處理，工程原則 5 的精神）。
 */
export type OpsRevenueResp =
  | {
      insufficient_accrued_history: true;
      history_points: number;
      detail: string;
      manifest_errors: string[];
    }
  | {
      insufficient_accrued_history: false;
      basis_unknown: true;
      /** 恆 False：算不出來 ≠ 有異常，後端刻意不告警。 */
      over_threshold: false;
      day: string;
      prev_day: string;
      /** ⭐ 窗口界只能來自快照時刻，缺了就沒有正確答案 → 後端回 null，不用日期猜。 */
      window_start: null;
      window_end: null;
      /** 後端寫好的原因說明（缺 captured_at ／ 時刻非嚴格遞增），顯示層原樣呈現。 */
      note: string;
      manifest_errors: string[];
    }
  | {
      insufficient_accrued_history: false;
      basis_unknown: false;
      /** 應收／歸屬：Σ 各客戶 builder_fee。 */
      attributed: string;
      /** 實收／北極星：builder 位址累積量的今昨差（查一次，不由 rows 推導）。 */
      accrued_delta: string;
      accrued_now: string;
      accrued_prev: string;
      discrepancy: string;
      /** attributed 為 0 時後端回 null（不得除零）——顯示層須區分 null 與 0。 */
      discrepancy_pct: string | null;
      over_threshold: boolean;
      threshold_pct: string;
      rows: number;
      day: string;
      prev_day: string;
      /**
       * ⭐ 實際比較窗口＝兩個快照時刻（captured_at），**不是**日曆日。顯示層必須把它
       * 秀出來：窗口與 fills 取樣區間錯開曾經是 Critical bug（日報 cron 排在 00:10 時，
       * accrued 增量涵蓋昨天一整天、fills 卻只有今天十幾分鐘，健康帳戶被誤判成漏財）。
       * 把區間攤在畫面上，同一類錯位下次一眼就看得出來。
       */
      window_start: string;
      window_end: string;
      customers: OpsCustomerRow[];
      manifest_errors: string[];
    };

// ---------- ops 成交品質（admin；跨客戶橫切，對照 app.py 的 /api/ops/trade-quality） ----------
/**
 * 單一 follower 的成交品質列。**兩種形狀，判別欄位 `quality_available`**：
 * 自己的 fills 查不到時後端**不給**任何量測欄（連 taker_share 都沒有）——型別上就
 * 讀不到，顯示層不可能把「查詢失敗」畫成一列 0（沿 OpsRevenueResp 的既有慣例）。
 *
 * ⭐ 三個「未知」旗標互相獨立，不得合併：
 * - `te_available=false`＝不知道這個 follower 跟誰（manifest 無 leader_address）→
 *   配對延遲與滑價為 null。**不是 0**：「延遲 0 秒」讀起來是完美的跟單品質。
 * - `skipped_available=false`＝skipped 檔讀不到 → 名目與比例皆 null（同樣不是 0）。
 * - `skipped_available=true` 但 `skipped_small_ratio=null`＝窗口非整個 UTC 日，
 *   分子（日曆日落檔）與分母（依窗口過濾）不同基準，後端刻意不硬算（工程原則 1）。
 *   顯示層必須把這兩種 null 分開說：一個是「讀不到」，一個是「此窗口算不出來」。
 *
 * ⚠️ 數值欄一律 string（後端 jsonable 把 Decimal 無損序列化）；`fills`／`pair_count`
 * 是真正的計數，故為 number。
 */
export type OpsTradeQualityRow =
  | {
      account_id: string;
      label: string;
      network: string;
      quality_available: false;
      te_available: false;
      skipped_available: false;
      /** 本分支必有原文（fills 查詢失敗）；跨客戶隔離，其餘列照常有資料。 */
      error: string;
    }
  | {
      account_id: string;
      label: string;
      network: string;
      quality_available: true;
      fills: number;
      /** taker 佔比：只由我方成交算出，與 leader 無關 → te_available=false 時仍有效。 */
      taker_share: string;
      te_available: boolean;
      pair_count: number | null;
      /** ⭐ 配對延遲**中位數**（秒）：我方成交與 leader 對應成交的時間差。不是「速度」。 */
      median_delay_s: string | null;
      /** taker 滑價中位數（bp）；正值＝成交價對跟單者不利。 */
      taker_slippage_bp_median: string | null;
      skipped_available: boolean;
      skipped_small_notional: string | null;
      skipped_small_ratio: string | null;
      /** te_available=false 時後端附上的原因說明。 */
      te_note?: string;
      /** 比例不可比時後端附上的原因說明（分子分母不同基準）。 */
      skipped_note?: string;
      error: string | null;
    };

/**
 * 跨 follower 彙總。⭐ 只回**最差值**與樣本數，不回「中位數的平均」那種沒有統計
 * 意義卻很像數字的東西。`*_sample` 必須與最差值一起顯示：只給一個最差值會讓
 * 「10 個客戶只有 1 個有資料」與「10 個都有資料」在畫面上長得一模一樣。
 */
export interface OpsTradeQualitySummary {
  followers: number;
  quality_available_count: number;
  te_available_count: number;
  skipped_available_count: number;
  worst_median_delay_s: string | null;
  delay_sample: number;
  worst_taker_slippage_bp_median: string | null;
  slippage_sample: number;
}

/**
 * 成交品質，**兩種**回應形狀，判別欄位 `basis_unknown`（沿 OpsRevenueResp 的慣例）。
 * basis_unknown 分支後端**不給** `followers`／`summary`——型別上就讀不到，
 * 顯示層不可能畫出空表（空表會被讀成「今天成交品質完美」）。
 */
export type OpsTradeQualityResp =
  | {
      window: "accrued";
      basis_unknown: true;
      window_start: null;
      window_end: null;
      /** 後端寫好的原因說明；顯示層原樣呈現，且**不得**顯示任何數字。 */
      note: string;
      manifest_errors: string[];
    }
  | {
      window: "accrued" | "days";
      basis_unknown: false;
      /** 僅 days 窗有此欄（accrued 窗由快照時刻決定，不接受天數）。 */
      days?: number;
      window_start: string;
      window_end: string;
      /** 窗口涵蓋的日曆日（skipped 以日曆日落檔，供人肉核對比例的分母基準）。 */
      skipped_days: string[];
      followers: OpsTradeQualityRow[];
      summary: OpsTradeQualitySummary;
      manifest_errors: string[];
    };

/**
 * 成交品質。時間窗與 /api/ops/customers **同一套規則**（互斥、同一個
 * `ops.accrued_window()` 推導）——三張表要能並排讀，窗口就必須同源。
 */
export function getOpsTradeQuality(query: OpsCustomersQuery): Promise<OpsTradeQualityResp> {
  const q = new URLSearchParams(
    query.window === "accrued" ? { window: "accrued" } : { days: String(query.days) },
  );
  return request<OpsTradeQualityResp>(`/api/ops/trade-quality?${q.toString()}`);
}

// ---------- ops 系統健康（admin；對照 app.py 的 /api/ops/health） ----------
/**
 * 引擎當輪實際採用的資金設定（心跳投影）。⚠️ 兩個數值一律 string：它們直接乘進
 * 部位大小，而 0.1 在 float 裡不是 0.1（沿 CapitalSettingsResp 的既有慣例）。
 */
export interface OpsHealthCapital {
  allocated_capital: string | null;
  capital_utilization: string | null;
  use_full_equity: boolean | null;
  /** 這組值從哪來（已簽署的設定／預設值…），由引擎在同一次求值中標記。 */
  source: string;
  changed_at: string | null;
}

/** 引擎上一個 cycle 的結果（心跳投影）。 */
export interface OpsHealthLastCycle {
  result: string;
  detail: string | null;
}

/**
 * 單一 follower 的健康列。⭐⭐ **本型別的每一個「未知」都必須留在型別上**：
 * 後端刻意讓讀不到的格子回 `null`＋一個 `*_known` 兄弟欄，而不是折疊成看起來
 * 健康的值。顯示層若把 `null` 當 falsy 處理（`killswitch_tripped` 為 null 時顯示
 * 「未觸發」），就等於在客戶的引擎已經熔斷、部位已被平掉的當下告訴管理員一切正常。
 * 謊報健康比沒有面板更危險——所以三態一律經由 page 的 `engineState`／
 * `killswitchState` 純函式轉成明確的字串聯集，不在 JSX 裡直接用布林判斷。
 *
 * ⚠️ `liveness_basis`（誠實標註，後端一併上呈）：這**不是** process 檢查，而是
 * 「引擎最近有沒有寫心跳（equity 樣本，每 cycle 一筆）」的代理。刻意型別為 string
 * 而非字面量聯集：後端換掉判定基準時，顯示層會原樣顯示那個新代碼（看得懂但陌生），
 * 而不是靜默沿用一句已經不成立的說明——過期的說明比沒有說明更危險。
 */
export interface OpsHealthFollower {
  account_id: string;
  label: string;
  network: string;
  liveness_basis: string;
  state_root: string;
  /**
   * ⭐⭐ 引擎心跳的新鮮度。已知值 `ok`／`stale`／`missing`／`unreadable`——後端刻意
   * **不把後三者折疊成一個「未知」**（處置完全不同：missing 多半是剛啟用或子通道
   * 沒建好，stale 是引擎沒跑或寫不進交換目錄，unreadable 是檔案壞了）。
   *
   * 型別刻意為 `string` 而非字面量聯集：後端新增一種狀態時，顯示層會原樣顯示那個
   * 陌生代碼（看得懂但明顯不對勁），而不是靜默落進某個既有分支——落進 "ok" 的
   * 那一版會謊報健康，而那是本面板最不能犯的錯。
   */
  heartbeat_status: string;
  /** ⭐ 最後一次心跳的時刻（引擎寫入時的 UTC ISO）。過期時**只剩這個與年齡**。 */
  heartbeat_at: string | null;
  heartbeat_age_s: number | null;
  heartbeat_stale_after_s: number;
  /**
   * ⭐ 這一列的 kill switch 與覆蓋度**出自哪個來源**：`state_root`（API 直讀，較新鮮）
   * 或 `heartbeat`（引擎發布的摘要），另有 `absent`／`unreadable`。兩個來源並存卻
   * 不標示，讀者無從判斷他看到的值有多舊（工程原則 1 的變形）。同為 string 以容納
   * 後端新增的來源。
   */
  basis: string;
  /** 以下四項**只可能來自心跳**（狀態根沒有可讀投影）；心跳非 ok 時一律 null。 */
  leader_address: string | null;
  leader_source: string | null;
  capital: OpsHealthCapital | null;
  last_cycle: OpsHealthLastCycle | null;
  coverage_known: boolean;
  sample_count: number | null;
  /** 回撤保護是否已生效。false＝**確定**尚未生效（樣本不足），null＝讀不到。 */
  sample_coverage_sufficient: boolean | null;
  /** 最後一次心跳距 `checked_at` 的秒數；null＝完全沒有心跳（不是 0）。 */
  last_sample_age_s: number | null;
  /** ⭐ 三態：true＝心跳新鮮；false＝心跳過期；null＝讀不到（既非健康也非確定的壞）。 */
  engine_alive: boolean | null;
  /** ⭐ 三態：true＝已觸發（該客戶已停止跟單）；false＝未觸發；null＝讀不到。 */
  killswitch_tripped: boolean | null;
  /** false ⇒ `killswitch_tripped` 的 null 是「無從確認」，**不是**「沒觸發」。 */
  killswitch_known: boolean;
  /** 告警筆數；null＝告警檔讀不到（**不是** 0，0 是「確實沒有告警」）。 */
  alerts: number | null;
  error: string | null;
}

/** 「客戶簽了、API 收了、引擎從沒套用」的一筆積壓（reason 為機器可讀碼）。 */
export interface OpsUnappliedLeaderChange {
  account_id: string;
  nonce: string | null;
  age_s: number | null;
  /** not_redeemed／bad_issued_at／ledger_unreadable；未知碼顯示層原樣呈現。 */
  reason: string;
}

/**
 * 健康彙總。⭐ 每個計數都有一個 `*_unknown` 兄弟欄：「0 個引擎沒回應」與
 * 「10 個引擎的狀態都讀不到」在單一計數上長得一模一樣，而後者才是該立刻處理的那個。
 */
export interface OpsHealthSummary {
  followers: number;
  engine_alive_count: number;
  engine_stale_count: number;
  engine_unknown_count: number;
  killswitch_tripped_count: number;
  killswitch_unknown_count: number;
  coverage_insufficient_count: number;
  coverage_unknown_count: number;
  alerts_total: number;
  alerts_unknown_count: number;
  /**
   * ⭐ 心跳三態各自計數，**不合併成一個「心跳有問題」**：`stale`＝引擎沒跑或寫不進
   * 交換目錄（要立刻查），`missing`＝多半剛 activate／子通道沒建（部署待辦）。
   * 合成一個數字會讓前者被後者的常態雜訊蓋掉。
   */
  heartbeat_ok_count: number;
  heartbeat_stale_count: number;
  heartbeat_missing_count: number;
  /** ⭐ null＝這條鏈路查不下去（見 leader_change_errors），**不是** 0（沒有積壓）。 */
  unapplied_leader_changes: number | null;
  leader_change_errors: string[];
}

export interface OpsHealthResp {
  /** 本次檢查的時刻；心跳年齡以此為基準（同源，故可相減出最後心跳時刻）。 */
  checked_at: string;
  /** 心跳超過這麼久沒更新即判為過期（後端 ENGINE_STALE_S）。 */
  engine_stale_after_s: number;
  followers: OpsHealthFollower[];
  unapplied_leader_changes: OpsUnappliedLeaderChange[];
  summary: OpsHealthSummary;
  manifest_errors: string[];
}

/** 系統健康（admin only）。冪等讀取，重試安全。 */
export function getOpsHealth(): Promise<OpsHealthResp> {
  return request<OpsHealthResp>("/api/ops/health");
}


/**
 * 每客戶損益。`{ window: "accrued" }` → 與 /api/ops/revenue 同一比較窗口（可相減）；
 * `{ days }` → 自由檢視窗（1..90，超出範圍後端回 400 → ApiError kind=client）。
 */
export function getOpsCustomers(query: OpsCustomersQuery): Promise<OpsCustomersResp> {
  const q = new URLSearchParams(
    query.window === "accrued" ? { window: "accrued" } : { days: String(query.days) },
  );
  return request<OpsCustomersResp>(`/api/ops/customers?${q.toString()}`);
}

/** 收入對帳（threshold_pct 為比例，非百分比；0.01 = 1%）。 */
export function getOpsRevenue(thresholdPct: number): Promise<OpsRevenueResp> {
  const q = new URLSearchParams({ threshold_pct: String(thresholdPct) });
  return request<OpsRevenueResp>(`/api/ops/revenue?${q.toString()}`);
}


/**
 * 訂閱對帳的一列（本地 billing 表 vs Stripe）。
 * ⭐ 每個欄位都可能是 null：對不到本地 account 時 `account_id`／`local_status` 為 null，
 * 本地有而 Stripe 查無時 `stripe_status`／`stripe_status_raw` 為 null。顯示層一律用
 * NO_VALUE 佔位，不得退化成空字串（分不出「查無」與「空值」）。
 */
export interface OpsSubscriptionEntry {
  account_id: string | null;
  local_status: string | null;
  /** 已用後端 map_stripe_status 正規化到本地值域（同基準比較，工程原則 1）。 */
  stripe_status: string | null;
  /** Stripe 原始 status（trialing／unpaid…）；正規化前的原文，供人工查證用。 */
  stripe_status_raw: string | null;
  stripe_subscription_id: string | null;
  /** 命中方式：精確 id 比對或 metadata fallback；對不到本地 account 時為 null。 */
  matched_by: "subscription_id" | "metadata" | null;
}

/**
 * 訂閱對帳結果。四個漂移清單**互斥不重複計數**，危害程度不同（後端 ops.subscription_drift）：
 * `stripe_active_local_not`（客戶付了錢沒權益）> `local_active_stripe_not`（漏財）>
 * `status_mismatch` > `orphan_stripe`。顯示層的排序必須照這個順序，不得按字母或長度排。
 */
export interface OpsSubscriptionsResp {
  local_active_stripe_not: OpsSubscriptionEntry[];
  stripe_active_local_not: OpsSubscriptionEntry[];
  status_mismatch: OpsSubscriptionEntry[];
  orphan_stripe: OpsSubscriptionEntry[];
  in_sync_count: number;
  drift_count: number;
  local_count: number;
  stripe_count: number;
  /** 被更新訂閱取代的歷史訂閱（回鍋客戶）。**不是漂移**，不計入 drift_count。 */
  superseded_count: number;
  /**
   * ⭐ 達 Stripe 列表上限，樣本不完整。為 true 時「本地有、Stripe 查無」可能全是**假漂移**
   * ——顯示層必須把結論不可信這件事講出來，不得靜默照常顯示（工程原則 3）。
   */
  truncated: boolean;
}

/**
 * 訂閱對帳（admin only）。billing 未啟用 → 501（kind=client, status=501），
 * 消費端據此整段隱藏（沿 /billing 與 Header 的既有慣例）。
 * 冪等讀取，重試安全——但本端點**只偵測不修正**，任何以 Stripe 為準的同步都是人工決策。
 */
export function getOpsSubscriptions(): Promise<OpsSubscriptionsResp> {
  return request<OpsSubscriptionsResp>("/api/ops/subscriptions");
}
