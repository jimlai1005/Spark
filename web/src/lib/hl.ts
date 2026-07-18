/**
 * lib/hl.ts — Hyperliquid /exchange 直送層（安全關鍵 ⭐）。
 *
 * 職責（publicapi 計畫「移交前端計畫」清單全數落於此檔）：
 * 1. v 正規化：錢包回的 v ∈ {0,1,27,28} → {27,28}（research §4 風險 5）。
 * 2. /exchange payload 組裝：{action, nonce, signature{r,s,v}, vaultAddress:null,
 *    expiresAfter:null}（research §3）。action ≡ typed_data.message 原文（設計定案 2）。
 * 3. 簽後立即直送 HL（紅線 3：簽名不落地、不回後端；本模組沒有任何後端呼叫）。
 * 4. recover 預驗：submit 前由呼叫端（approvalFlow）用 recoverSigner 比對登入地址。
 *
 * HL base URL 由 typed_data.message.hyperliquidChain 推導——單一來源，
 * 前端不另設網路設定（工程原則 1）。HL 呼叫不帶 credentials（紅線 5）。
 */
import { recoverTypedDataAddress } from "viem";

export interface HlTypedData {
  domain: {
    name: string;
    version: string;
    chainId: number;
    verifyingContract: string;
  };
  types: Record<string, { name: string; type: string }[]>;
  primaryType: string;
  message: Record<string, unknown> & {
    type: string;
    hyperliquidChain: "Mainnet" | "Testnet";
    signatureChainId: string;
    nonce: number;
  };
}

export interface RsvSignature {
  r: `0x${string}`;
  s: `0x${string}`;
  v: 27 | 28;
}

export interface ExchangePayload {
  action: HlTypedData["message"];
  nonce: number;
  signature: RsvSignature;
  vaultAddress: null;
  expiresAfter: null;
}

export type HlSubmitResult =
  | { ok: true }
  | { ok: false; kind: "semantic" | "transient"; message: string };

export function normalizeV(v: number): 27 | 28 {
  if (v === 0 || v === 27) return 27;
  if (v === 1 || v === 28) return 28;
  throw new Error(`無法正規化的 v 值: ${v}（只接受 0/1/27/28）`);
}

export function splitSignature(hex: string): RsvSignature {
  if (!/^0x[0-9a-fA-F]{130}$/.test(hex)) {
    throw new Error("簽名必須是 0x + 130 hex（65 bytes：r32 + s32 + v1）");
  }
  return {
    r: `0x${hex.slice(2, 66)}` as `0x${string}`,
    s: `0x${hex.slice(66, 130)}` as `0x${string}`,
    v: normalizeV(parseInt(hex.slice(130, 132), 16)),
  };
}

const HL_API_URLS = {
  Mainnet: "https://api.hyperliquid.xyz",
  Testnet: "https://api.hyperliquid-testnet.xyz",
} as const;

export function hlBaseUrl(chain: "Mainnet" | "Testnet"): string {
  const url = HL_API_URLS[chain];
  if (!url) throw new Error(`未知的 hyperliquidChain: ${String(chain)}`);
  return url;
}

export function buildExchangePayload(
  typedData: HlTypedData,
  signatureHex: string,
): ExchangePayload {
  const nonce = typedData.message.nonce;
  if (typeof nonce !== "number" || !Number.isSafeInteger(nonce) || nonce <= 0) {
    throw new Error(`typed data 的 nonce 不合法: ${String(nonce)}`);
  }
  // nonce 三位一體（research §3）：message.nonce == action.nonce == 頂層 nonce。
  // action 就是 message 原文——不增刪、不改型別（紅線 4）。
  return {
    action: typedData.message,
    nonce,
    signature: splitSignature(signatureHex),
    vaultAddress: null,
    expiresAfter: null,
  };
}

export async function submitToHl(
  typedData: HlTypedData,
  signatureHex: string,
  fetchFn: typeof fetch = fetch,
): Promise<HlSubmitResult> {
  const url = `${hlBaseUrl(typedData.message.hyperliquidChain)}/exchange`;
  const payload = buildExchangePayload(typedData, signatureHex);
  let res: Response;
  try {
    // 不帶 credentials：HL 是跨域公開端點，cookie 絕不外送（紅線 5）。
    res = await fetchFn(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    return { ok: false, kind: "transient", message: "網路錯誤，尚未送達 Hyperliquid" };
  }
  if (!res.ok) {
    // 5xx = transient（可重送同一簽名：HL 以 nonce 去重）；4xx = semantic。
    return {
      ok: false,
      kind: res.status >= 500 ? "transient" : "semantic",
      message: `Hyperliquid 回應 HTTP ${res.status}`,
    };
  }
  const body: unknown = await res.json().catch(() => null);
  if (
    typeof body === "object" && body !== null &&
    (body as { status?: unknown }).status === "ok"
  ) {
    return { ok: true };
  }
  const resp = (body as { response?: unknown } | null)?.response;
  return {
    ok: false,
    kind: "semantic",
    message: typeof resp === "string" ? resp : JSON.stringify(body),
  };
}

/** recover 預驗（設計定案 3）：回小寫地址，呼叫端與登入地址（小寫）比對。
 *  註：viem 接受 types 內含 EIP712Domain（自行處理）；若未來 viem 版本嚴格化而報錯，
 *  在此函式內剝除 types.EIP712Domain 再傳入即可（domain 雜湊由 domain 物件推導，行為不變）。 */
export async function recoverSigner(
  typedData: HlTypedData,
  signatureHex: string,
): Promise<string> {
  const addr = await recoverTypedDataAddress({
    domain: typedData.domain,
    types: typedData.types,
    primaryType: typedData.primaryType,
    message: typedData.message,
    signature: signatureHex as `0x${string}`,
  } as Parameters<typeof recoverTypedDataAddress>[0]);
  return addr.toLowerCase();
}
