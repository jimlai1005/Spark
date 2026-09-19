/**
 * lib/referralFlow.ts — 推薦碼 opt-in 的簽章編排（riskSettingsFlow.ts 姊妹檔，2026-09-19）。
 *
 * 沿用同一套「fetchMessage（伺服器 canonical 原文）→ 內容預驗（進錢包之前，零網路
 * 請求）→ 錢包 personal_sign → 本地 recover 預驗 → 送出」骨架，尾段直接複用
 * `riskSettingsFlow.ts` 的 `signRecoverSubmit`（同一份邏輯，不重抄）。
 *
 * ⭐ 內容預驗要比對什麼、為什麼
 * -----------------------------
 * 推薦碼 opt-in 是一次性、不可逆的鏈上動作（`Referrer already set` 之後永遠改不了），
 * 且授權的是「用我已核准的下單 agent 去簽一筆 L1 action」。與風控設定同一種攻擊
 * 形狀：被打穿的 filet-api 可以回一份指向別的帳號、別的推薦碼，或其實是別的動作
 * （例如一份風控設定原文）的待簽內容，客戶在錢包裡看到的只是一段英文。預驗因此要
 * 擋四件事：
 *   1. `account_id` 是我；
 *   2. `payload.code` 與我方預期的推薦碼一致（避免被換成別的碼）；
 *   3. 原文第一行是這個動作專屬的域分隔字面量，且原文確實逐字寫著
 *      `Referral Code: <expectedCode>` 這一行——客戶在錢包裡實際看到、實際同意的
 *      就是這段文字；
 *   4. 原文**不含**任何風控參數名的 `<name>:` 行（沿 `runRiskUnlockFlow` 的域分隔
 *      防線——一份「調整風控門檻」的原文絕不能被兌換成「設定推薦碼」的簽章，
 *      反向亦然）。參數名由呼叫端傳入（取自後端 `specs`，不是前端硬編清單）。
 * 任何一件不成立都不喚起錢包（fail closed）。
 *
 * 失敗分類（工程原則 2）：本流程同樣**沒有任何自動重試**——非冪等寫入 ＋ nonce
 * 一次性，重送必因 nonce 已消耗而失敗，重來只能整條流程重跑，且必須由使用者按鈕
 * 觸發。
 */
import type { ReferralMessageResp, ReferralOptinResp } from "./api";
import { signRecoverSubmit, type RiskFlowFailure } from "./riskSettingsFlow";

/** 待簽原文第一行的域分隔字面量（`filet/referral_optin.py` 的第五個字面量）。 */
export const REFERRAL_OPTIN_FIRST_LINE = "Filet: set Hyperliquid referral code";

export type ReferralOptinFlowResult =
  | { ok: true; resp: ReferralOptinResp }
  | RiskFlowFailure;

export interface ReferralOptinDeps {
  fetchMessage: () => Promise<ReferralMessageResp>;
  /** 錢包 personal_sign，原文原樣（不加前綴、不重排）。 */
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (payload: ReferralMessageResp, signature: string) => Promise<ReferralOptinResp>;
}

export async function runReferralOptinFlow(
  deps: ReferralOptinDeps,
  opts: {
    expectedSigner: string;
    expectedAccountId: string;
    expectedCode: string;
    /** 風控參數名清單（取自後端 `specs`），用於域分隔防線第 4 件事。 */
    riskParamNames: string[];
  },
): Promise<ReferralOptinFlowResult> {
  let payload: ReferralMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  // ⭐ 內容預驗：必須在**進錢包之前**（見檔頭）。這條路徑上零網路請求、不喚起
  // 錢包（fail closed）。
  const lines = payload.message.split("\n");
  if (
    payload.account_id !== opts.expectedAccountId
    || payload.code !== opts.expectedCode
    || lines[0] !== REFERRAL_OPTIN_FIRST_LINE
    || !lines.includes(`Referral Code: ${opts.expectedCode}`)
    || opts.riskParamNames.some((n) => payload.message.includes(`${n}:`))
  ) {
    return { ok: false, kind: "content-mismatch" };
  }

  return signRecoverSubmit(deps, payload, opts.expectedSigner);
}
