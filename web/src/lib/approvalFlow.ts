/**
 * lib/approvalFlow.ts — 單筆授權（ApproveAgent 或 ApproveBuilderFee）的編排 ⭐。
 * 固定順序：fetchPayload → 錢包簽（typed data 原文 JSON.stringify，紅線 4）
 *   → recover 預驗（≠ 登入地址即中止，不送 HL——設計定案 3）
 *   → 直送 HL /exchange（紅線 3：簽名不落地、不回後端）。
 * 依賴全注入：UI 薄殼提供 wagmi provider 簽名與 hl.ts 的 recover/submit。
 * 失敗分類（設計定案 11）：
 *   hl-transient → retrySubmit 重送同一簽名（同 nonce，HL 去重，安全）；
 *   hl-semantic / 其他 → UI 重跑整個 flow（重取 payload、fresh nonce、重簽）。
 */
import type { HlSubmitResult, HlTypedData } from "./hl";

export interface ApprovalDeps {
  fetchPayload: () => Promise<HlTypedData>;
  signTypedData: (typedDataJson: string) => Promise<string>;
  recover: (td: HlTypedData, signature: string) => Promise<string>;
  submit: (td: HlTypedData, signature: string) => Promise<HlSubmitResult>;
}

export type ApprovalResult =
  | { ok: true }
  | { ok: false; kind: "payload-failed" }
  | { ok: false; kind: "wallet-rejected" }
  | { ok: false; kind: "signer-mismatch" }
  | { ok: false; kind: "hl-semantic"; message: string }
  | { ok: false; kind: "hl-transient"; message: string; retrySubmit: () => Promise<HlSubmitResult> };

export async function runApprovalFlow(
  deps: ApprovalDeps,
  opts: { expectedSigner: string },
): Promise<ApprovalResult> {
  let td: HlTypedData;
  try {
    td = await deps.fetchPayload();
  } catch {
    return { ok: false, kind: "payload-failed" };
  }

  let signature: string;
  try {
    // ⭐ 紅線 4：後端 typed data 原文 stringify，不重組、不增刪欄位。
    signature = await deps.signTypedData(JSON.stringify(td));
  } catch {
    return { ok: false, kind: "wallet-rejected" };
  }

  // ⭐ 設計定案 3：本地 recover 預驗——錢包切錯帳號簽的唯一攔截點。
  const recovered = (await deps.recover(td, signature)).toLowerCase();
  if (recovered !== opts.expectedSigner.toLowerCase()) {
    return { ok: false, kind: "signer-mismatch" };
  }

  const result = await deps.submit(td, signature);
  if (result.ok) return { ok: true };
  if (result.kind === "transient") {
    return {
      ok: false,
      kind: "hl-transient",
      message: result.message,
      // 同一 payload 同一 nonce 重送安全（HL 以 nonce 去重）；簽名只活在此閉包。
      retrySubmit: () => deps.submit(td, signature),
    };
  }
  return { ok: false, kind: "hl-semantic", message: result.message };
}
