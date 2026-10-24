/**
 * lib/walletChoice.ts — 錢包候選判定與登入錯誤分類（純函式，離線可測）。
 *
 * 背景（2026-10-24）：wagmi 開啟 `multiInjectedProviderDiscovery` 後，connector
 * 清單＝ `[injected() 後備, ...EIP-6963 發現到的錢包]`。發現到的 connector `id`
 * 是 rdns（io.metamask／io.rabby…）；後備那個 `id === "injected"`，provider 是
 * `window.ethereum`。規則：
 *   - 有任何發現到的錢包 → 只列發現到的（排除 injected 後備，否則同一錢包會
 *     出現兩次：一次叫 MetaMask、一次叫 Injected）。
 *   - 沒有發現到任何錢包 → 退回 injected 後備（舊式只注入 window.ethereum 的
 *     錢包，以及 e2e 的 mock）。此時**不**在這裡檢查 window.ethereum 是否存在：
 *     交給 connect() 丟 ProviderNotFoundError，由 classifyLoginError 判成 noWallet。
 *     （這也讓既有 vitest 以 `connectors: [{ id: "injected" }]` mock 的測試維持不變。）
 */

export const INJECTED_FALLBACK_ID = "injected";

/** 只取用得到的三個欄位，讓測試不必建完整的 wagmi Connector。 */
export interface WalletConnectorLike {
  id: string;
  name: string;
  icon?: string;
}

export type WalletChoice<C extends WalletConnectorLike> =
  | { kind: "none"; candidates: [] }
  | { kind: "single"; candidates: [C] }
  | { kind: "multiple"; candidates: C[] };

export function pickWalletCandidates<C extends WalletConnectorLike>(connectors: readonly C[]): WalletChoice<C> {
  const discovered = connectors.filter((c) => c.id !== INJECTED_FALLBACK_ID);
  const fallback = connectors.find((c) => c.id === INJECTED_FALLBACK_ID);
  const candidates = discovered.length > 0 ? discovered : fallback ? [fallback] : [];
  if (candidates.length === 0) return { kind: "none", candidates: [] };
  if (candidates.length === 1) return { kind: "single", candidates: [candidates[0]] };
  return { kind: "multiple", candidates };
}

export type LoginErrorKind = "rejected" | "noWallet" | "failed";

/**
 * 錯誤分類。原本五個呼叫點各自複製的 catch 判別搬來這裡；新增 ProviderNotFoundError
 * → noWallet（以前會被歸成 loginFailed，使用者看不出是沒裝錢包）。
 * 不得把 err 內容印到 console（可能含簽章／訊息原文）。
 */
export function classifyLoginError(err: unknown): LoginErrorKind {
  const e = err as { name?: string; code?: number; message?: string } | undefined;
  if (e?.name === "ProviderNotFoundError") return "noWallet";
  const isRejected =
    e?.name === "UserRejectedRequestError"
    || e?.code === 4001
    || /reject|denied|cancel/i.test(String(e?.message ?? ""));
  return isRejected ? "rejected" : "failed";
}
