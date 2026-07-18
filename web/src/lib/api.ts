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
