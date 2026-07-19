/**
 * lib/leaderSelectFlow.ts — 換 leader 授權的編排 ⭐（沿 approvalFlow.ts 的謹慎度）。
 *
 * 固定順序：fetchMessage（伺服器產生的 canonical 原文）→ 錢包 personal_sign 原文
 *   → recover 預驗（≠ 登入地址即中止，**零網路請求**）→ 送出，原文原樣回送。
 * 依賴全注入：UI 薄殼提供 wagmi 的簽名函式與 api/sign 的實作——本模組可離線測試。
 *
 * 為什麼原文必須來自伺服器且原樣回送：後端驗簽時是**重建**訊息再 recover
 * （filet/leader_change.py 刻意不看客戶送來的 message），所以客戶端組出的字串必須
 * 與伺服器逐位元組相同。少一個換行、位址大小寫不同，症狀都是「我本人簽的卻一直
 * 被拒」，而兩邊看起來都完全正常。整包 payload 一路帶到底＝結構上不可能組出不同
 * 的字串（工程原則 1：被比較的兩個值同源、同處計算）。
 *
 * 失敗分類（工程原則 2）：本流程**沒有任何自動重試**。送出是非冪等寫入（寫一筆
 * 變更記錄）且 nonce 一次性——重送同一筆必然因 nonce 已消耗而失敗，重來只能整條
 * 流程重跑（重取原文、新 nonce、重簽），且必須由使用者按鈕觸發：換 leader 有真實
 * 交易成本，不是可以替使用者自動重試的動作。
 */
import type { LeaderSelectMessageResp, LeaderSelectResp } from "./api";

export interface LeaderSelectDeps {
  fetchMessage: () => Promise<LeaderSelectMessageResp>;
  /** 錢包 personal_sign，原文原樣（不加前綴、不重排）。 */
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (payload: LeaderSelectMessageResp, signature: string) => Promise<LeaderSelectResp>;
}

export type LeaderSelectFlowResult =
  | { ok: true; resp: LeaderSelectResp }
  | { ok: false; kind: "message-failed"; error: unknown }
  | { ok: false; kind: "wallet-rejected" }
  | { ok: false; kind: "signer-mismatch" }
  | { ok: false; kind: "submit-failed"; error: unknown };

export async function runLeaderSelectFlow(
  deps: LeaderSelectDeps,
  opts: { expectedSigner: string },
): Promise<LeaderSelectFlowResult> {
  let payload: LeaderSelectMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  let signature: string;
  try {
    signature = await deps.signMessage(payload.message);
  } catch {
    return { ok: false, kind: "wallet-rejected" };
  }

  // ⭐ 本地 recover 預驗：錢包切錯帳號簽的唯一攔截點。這條路徑上**不得**有任何
  // 網路請求——不符就是不符，連問後端都不問（錯的簽名送出去，最好的情況是被拒，
  // 最壞的情況是被記成另一個人的意圖）。recover 本身拋錯（簽名格式壞）同樣視為
  // 不符：證明不了是本人簽的就不送（fail closed）。
  let recovered: string;
  try {
    recovered = (await deps.recover(payload.message, signature)).toLowerCase();
  } catch {
    return { ok: false, kind: "signer-mismatch" };
  }
  if (recovered !== opts.expectedSigner.toLowerCase()) {
    return { ok: false, kind: "signer-mismatch" };
  }

  try {
    // 整包 payload 帶進去：原文與各欄位同源，前端沒有機會重組出不一樣的字串。
    const resp = await deps.submit(payload, signature);
    return { ok: true, resp };
  } catch (error) {
    // 不自動重試（非冪等 ＋ nonce 一次性）——錯誤原樣上呈，由 UI 決定怎麼說、
    // 由使用者決定要不要重跑整條流程。
    return { ok: false, kind: "submit-failed", error };
  }
}
