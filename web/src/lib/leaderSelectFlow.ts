/**
 * lib/leaderSelectFlow.ts — 換 leader 授權的編排 ⭐（沿 approvalFlow.ts 的謹慎度）。
 *
 * 固定順序：fetchMessage（伺服器產生的 canonical 原文）→ **leader 預驗（≠ 使用者所選
 *   即中止，零網路請求、不喚起錢包）** → 錢包 personal_sign 原文
 *   → recover 預驗（≠ 登入地址即中止，**零網路請求**）→ 送出，原文原樣回送。
 * 依賴全注入：UI 薄殼提供 wagmi 的簽名函式與 api/sign 的實作——本模組可離線測試。
 *
 * 為什麼原文必須來自伺服器且原樣回送：後端驗簽時是**重建**訊息再 recover
 * （filet/leader_change.py 刻意不看客戶送來的 message），所以客戶端組出的字串必須
 * 與伺服器逐位元組相同。少一個換行、位址大小寫不同，症狀都是「我本人簽的卻一直
 * 被拒」，而兩邊看起來都完全正常。整包 payload 一路帶到底＝結構上不可能組出不同
 * 的字串（工程原則 1：被比較的兩個值同源、同處計算）。
 *
 * ⭐ 為什麼還要 leader 預驗（expectedLeader）：expectedSigner 只證明「簽的人是本人」，
 * 完全不證明「簽的內容是本人要的那件事」。filet-api 若被打穿，使用者點 Delta、API 回
 * 一份指向 0xEVIL 的原文，錢包只顯示一串 hex，使用者按簽——簽章貨真價實地綁定 0xEVIL，
 * 引擎重建訊息、recover、比對 manifest 全部通過，事後稽核看起來完全是客戶自己要求的。
 * 也就是說：沒有這道預驗，被打穿的 API 能**無中生有一次換手**，而不只是丟掉一筆記錄。
 * 這一層有效的前提是信任域不同——前端 bundle 由 filet-dashboard 服務、檔案唯讀，
 * 打穿 filet-api 的攻擊者改不到它（與引擎二次驗章同構的獨立防線）。
 * 檢查兩件事而非一件：`leader_address` 欄位（後端據以重建訊息）與 `message` 本體
 * （使用者在錢包裡實際看到、實際同意的東西）都必須指向同一個人，缺一邊就有縫。
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
  /** 伺服器回傳的授權對象 ≠ 使用者所選。非一般錯誤：不得重試，應回報。 */
  | { ok: false; kind: "leader-mismatch" }
  | { ok: false; kind: "submit-failed"; error: unknown };

export async function runLeaderSelectFlow(
  deps: LeaderSelectDeps,
  opts: { expectedSigner: string; expectedLeader: string },
): Promise<LeaderSelectFlowResult> {
  let payload: LeaderSelectMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  // ⭐ leader 預驗：必須在**進錢包之前**。簽完再驗沒有意義——簽章一旦產生就已經是
  // 一份對 0xEVIL 的有效授權，攻擊者只要拿得到它就能自己送出（且它會通過後端全部
  // 驗證）。這條路徑上零網路請求、不喚起錢包：不符就是不符，連問後端都不問
  // （fail closed，沿 signer-mismatch 的嚴格度）。
  // 兩邊都比對，且都小寫化（位址大小寫不敏感，checksum 形式不得被誤擋——伺服器版型
  // 本身也把位址正規化成小寫，見 filet/leader_change.py）：
  //   1. leader_address 欄位——後端據以**重建**訊息驗簽，這是真正生效的那個值；
  //   2. message 本體——使用者在錢包裡實際看到、實際同意的那串字。
  // 只驗欄位，訊息本體可以指向別人；只驗訊息，欄位可以指向別人。兩邊同時對上，
  // 才談得上「使用者看到的、簽下的、後端執行的」是同一件事（工程原則 1）。
  const expectedLeader = opts.expectedLeader.toLowerCase();
  if (
    payload.leader_address.toLowerCase() !== expectedLeader ||
    !payload.message.toLowerCase().includes(expectedLeader)
  ) {
    return { ok: false, kind: "leader-mismatch" };
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
