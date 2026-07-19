/**
 * lib/api.ts — 後端 Public API 的唯一出口（工程原則 5 的前端鏡射）。
 * 一律同源相對路徑 + credentials:"include"（紅線 5）。
 * 錯誤分類（工程原則 2）：auth(401)/client(4xx)/upstream(502|503)/network。
 * ⭐ 紅線 3：帶簽名的後端呼叫只有兩支，兩支都是 EIP-191 personal_sign，且兩支的
 *   **原文都由伺服器產生**（前端不組字串）：
 *     1. authVerify —— SIWE 登入簽名；
 *     2. postLeaderSelect —— 換 leader 授權簽名（原文來自 getLeaderSelectMessage）。
 *   EIP-712 的鏈上授權簽名走 lib/hl.ts 直送 HL，本模組結構上沒有那條路。
 */
import type { HlTypedData } from "./hl";

export type ApiErrorKind = "auth" | "client" | "upstream" | "network";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly detail?: string;

  constructor(kind: ApiErrorKind, message: string, status?: number, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

export interface Me { address: string; account_id: string }
export interface NonceResp { nonce: string; message: string }
export interface AgentResp { agent_address: string; recovered?: boolean }
export interface TypedDataResp { typed_data: HlTypedData }
export interface OnboardStatus {
  address: string;
  account_id: string;
  agent_address: string | null;
  agent_generated: boolean;
  builder_fee_approved: boolean;
  agent_approved: boolean;
  funded: boolean;
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
  const detail = await res
    .json()
    .then((b: { detail?: string }) => b?.detail)
    .catch(() => undefined);
  if (res.status === 401) throw new ApiError("auth", detail ?? "未登入", 401, detail);
  if (res.status === 502 || res.status === 503) {
    throw new ApiError("upstream", detail ?? "上游服務暫時不可用", res.status, detail);
  }
  // 非 502/503 的其他 5xx（如 500）目前歸類 client：後端現況不產生此類回應，
  // 若未來出現需重新評估歸 upstream（並補對應測試），此處僅記錄現況假設。
  throw new ApiError("client", detail ?? `請求失敗（HTTP ${res.status}）`, res.status, detail);
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

export function getApproveAgentPayload(chainId: number): Promise<TypedDataResp> {
  return post<TypedDataResp>("/api/onboard/payload/approve-agent", { chain_id: chainId });
}

export function getApproveBuilderFeePayload(chainId: number): Promise<TypedDataResp> {
  return post<TypedDataResp>("/api/onboard/payload/approve-builder-fee", { chain_id: chainId });
}

// ---------- leaders（leader 目錄與換 leader 授權；對照 publicapi/app.py 三個端點） ----------
/**
 * 目錄的一位 leader。⭐ 統計欄位是 watchlist 每日快照的**資產負債切面**，
 * 不是績效：沒有報酬率、沒有回撤、沒有勝率。欄位名刻意與後端／快照原名一字不差
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
}

/**
 * leader 目錄。⭐ 兩種形狀，判別欄位 `stats_available`：
 * 快照不可用時後端**不給**時間戳、只給 `note`——型別上就讀不到日期，顯示層因此
 * 不可能畫出「沒有時點的數字」（工程原則 1 的變形：連時點都不同源的兩個數字不可比）。
 * 快照可用時 `stats_day`／`stats_as_of` 仍可能為 null（快照缺欄位）——顯示層必須把
 * 「沒有時間戳」視同「不可顯示數字」，一份三天前的切面沒有時點就會被當成即時數字讀。
 */
export type LeadersResp =
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
    };

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

// ---------- billing（M3 計費；對照 src/spark/publicapi/app.py 的四個端點） ----------
/**
 * 方案目錄的功能列。`included` 與 `shipped` 是**兩個獨立的軸**（後端 billing.py
 * PlanFeature 的鏡射）：付費層可以「包含但尚未推出」——顯示層必須據此標「開發中」
 * 而非假裝可用。前端不得把兩者合併成單一布林（合併就抹掉了誠實揭露的資訊）。
 */
export interface BillingFeature {
  /** i18n 鍵（後端不寫死使用者可見文案）；文案在 lib/copy.ts 的 COPY.billing.keys。 */
  text_key: string;
  included: boolean;
  shipped: boolean;
}

export interface BillingPlan {
  id: string;
  name_key: string;
  /** null = 免費方案，或價格尚未設定 → 顯示「價格待定」，絕不退化成 0。 */
  price_display: string | null;
  /** 有 stripe price_id 且 billing 已啟用；免費方案恆 false。 */
  purchasable: boolean;
  features: BillingFeature[];
}

export interface BillingPlansResp {
  billing_enabled: boolean;
  plans: BillingPlan[];
}

/** 後端白名單映射的結果（未知 stripe 狀態一律歸 canceled）；無記錄為 "none"。 */
export type BillingStatusValue = "active" | "past_due" | "canceled" | "none";

export interface BillingStatusResp {
  account_id: string;
  status: BillingStatusValue;
  /** entitlement 查詢結果（僅供顯示；停用跟單是人工政策決策，前端不得自動化）。 */
  active: boolean;
}

/** 方案目錄。⭐ 公開端點：不需 session，未登入的定價頁照樣拿得到。 */
export function getBillingPlans(): Promise<BillingPlansResp> {
  return request<BillingPlansResp>("/api/billing/plans");
}

/** 訂閱狀態（需 session）。billing 未啟用 → 501（kind=client, status=501）。 */
export function getBillingStatus(): Promise<BillingStatusResp> {
  return request<BillingStatusResp>("/api/billing/status");
}

/**
 * 建 Stripe Checkout Session，回 `checkout_url`（⭐ 欄位名不是 `url`——與 portal 不同，
 * 見 app.py billing_checkout）。已有生效訂閱 → 409；billing 未啟用 → 501。
 * 非冪等寫入：呼叫端**不得自動重試**，失敗一律交回使用者重按（人肉重試天然去重）。
 */
export function postBillingCheckout(): Promise<{ checkout_url: string }> {
  return post<{ checkout_url: string }>("/api/billing/checkout");
}

/**
 * 建 Stripe Billing Portal Session，回 `url`。無訂閱記錄 → 409；未啟用 → 501。
 * 同為非冪等寫入，重試政策同上。
 */
export function postBillingPortal(): Promise<{ url: string }> {
  return post<{ url: string }>("/api/billing/portal");
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
