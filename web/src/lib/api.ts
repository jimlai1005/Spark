/**
 * lib/api.ts — 後端 Public API 的唯一出口（工程原則 5 的前端鏡射）。
 * 一律同源相對路徑 + credentials:"include"（紅線 5）。
 * 錯誤分類（工程原則 2）：auth(401)/client(4xx)/upstream(502|503)/network。
 * ⭐ 紅線 3：唯一帶簽名的呼叫是 authVerify（SIWE 登入簽名，EIP-191）；
 *   EIP-712 授權簽名走 lib/hl.ts 直送 HL，本模組結構上沒有那條路。
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

export interface OpsCustomersResp {
  days: number;
  start: string;
  end: string;
  customers: OpsCustomerRow[];
  /** 壞掉的 manifest 條目（容錯載入跳過者）。 */
  manifest_errors: string[];
}

/**
 * 收入對帳。`insufficient_accrued_history` 是判別欄位（discriminant）：
 * 為 true 時後端**不給**任何數值欄——型別上就讀不到，避免把「無資料」顯示成 0。
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
      window_start: string;
      window_end: string;
      customers: OpsCustomerRow[];
      manifest_errors: string[];
    };

/** 每客戶損益（days 1..90；超出範圍後端回 400 → ApiError kind=client）。 */
export function getOpsCustomers(days: number): Promise<OpsCustomersResp> {
  const q = new URLSearchParams({ days: String(days) });
  return request<OpsCustomersResp>(`/api/ops/customers?${q.toString()}`);
}

/** 收入對帳（threshold_pct 為比例，非百分比；0.01 = 1%）。 */
export function getOpsRevenue(thresholdPct: number): Promise<OpsRevenueResp> {
  const q = new URLSearchParams({ threshold_pct: String(thresholdPct) });
  return request<OpsRevenueResp>(`/api/ops/revenue?${q.toString()}`);
}
