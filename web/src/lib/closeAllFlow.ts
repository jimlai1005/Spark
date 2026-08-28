/**
 * lib/closeAllFlow.ts — 「平倉並撤銷」的簽章編排（kill switch 第二級）⭐
 * （沿 `riskSettingsFlow.ts` 的 `runRiskUnlockFlow` 形狀與謹慎度；先讀那份檔頭，
 * 這裡只寫**不同**的部分——本流程沒有 prefs，比解鎖流程還單純）。
 *
 * 固定順序：
 *   fetchMessage（伺服器產生的 canonical 原文）
 *   → **內容預驗（進錢包之前，不符即中止，零網路請求、不喚起錢包）**
 *   → 錢包 personal_sign 原文
 *   → 本地 recover 預驗（≠ 登入地址即中止，**零網路請求**）
 *   → 送出，原文原樣回送。
 *
 * ⭐ 這是客戶**自己終止**跟單關係的動作，且**不可逆**：引擎收尾後不會自動恢復。
 * 預驗必須在進錢包之前比對「這份原文確實綁在我的帳號上」——理由與
 * `runRiskUnlockFlow` 完全相同：被打穿的後端若回一份指向別人帳號（或其實是另一種
 * 動作）的原文，客戶會在以為自己只是平倉並撤銷自己帳號的情況下簽掉別的東西。
 * 域分隔的結構性防線在待簽訊息第一行（見 `filet/close_all.py` 檔頭），本模組的
 * 預驗是前端這一側的縱深防禦。
 *
 * 失敗分類（工程原則 2）：本流程**沒有任何自動重試**——送出是非冪等寫入且
 * nonce 一次性，重送同一筆必然因 nonce 已消耗而失敗；重來只能整條流程重跑
 * （重取原文、新 nonce、重簽），且必須由使用者按鈕觸發（modal 二次確認）。
 */
import type { CloseAllMessageResp, CloseAllResp } from "./api";

/** `content-mismatch`＝請停手回報，不得重試（同 `RiskFlowFailure` 的分類）。 */
export type CloseAllFlowFailure =
  | { ok: false; kind: "message-failed"; error: unknown }
  | { ok: false; kind: "wallet-rejected" }
  | { ok: false; kind: "signer-mismatch" }
  | { ok: false; kind: "content-mismatch" }
  | { ok: false; kind: "submit-failed"; error: unknown };

export type CloseAllFlowResult = { ok: true; resp: CloseAllResp } | CloseAllFlowFailure;

export interface CloseAllDeps {
  fetchMessage: () => Promise<CloseAllMessageResp>;
  /** 錢包 personal_sign，原文原樣（不加前綴、不重排）。 */
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (payload: CloseAllMessageResp, signature: string) => Promise<CloseAllResp>;
}

export async function runCloseAllFlow(
  deps: CloseAllDeps,
  opts: { expectedSigner: string; expectedAccountId: string },
): Promise<CloseAllFlowResult> {
  let payload: CloseAllMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  // ⭐ 內容預驗：必須在**進錢包之前**。簽完再驗沒有意義——簽章一旦產生就已經是
  // 一份「平掉這個帳號全部部位並停止跟單」的有效授權，攻擊者只要拿得到它就能
  // 自己送出（且它會通過後端全部驗證）。這條路徑上零網路請求、不喚起錢包。
  if (
    payload.account_id !== opts.expectedAccountId
    || !payload.message.includes(payload.account_id)
    || !payload.message.startsWith("Filet: close all positions and revoke copy-trading")
  ) {
    return { ok: false, kind: "content-mismatch" };
  }

  let signature: string;
  try {
    signature = await deps.signMessage(payload.message);
  } catch {
    return { ok: false, kind: "wallet-rejected" };
  }

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
    const resp = await deps.submit(payload, signature);
    return { ok: true, resp };
  } catch (error) {
    return { ok: false, kind: "submit-failed", error };
  }
}
