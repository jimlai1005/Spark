/**
 * lib/capitalSettingsFlow.ts — 資金設定授權的編排 ⭐（leaderSelectFlow 的姊妹檔）。
 *
 * 固定順序：fetchMessage（伺服器產生的 canonical 原文）→ **設定值預驗（≠ 使用者所調
 *   即中止，零網路請求、不喚起錢包）** → 錢包 personal_sign 原文
 *   → recover 預驗（≠ 登入地址即中止，**零網路請求**）→ 送出，原文原樣回送。
 * 依賴全注入：UI 薄殼提供 wagmi 的簽名函式與 api/sign 的實作——本模組可離線測試。
 *
 * 為什麼原文必須來自伺服器且原樣回送：後端驗簽時是**重建**訊息再 recover
 * （filet/capital_settings.py 刻意不看客戶送來的 message），所以客戶端組出的字串必須
 * 與伺服器逐位元組相同。少一個小數位、多一個換行，症狀都是「我本人簽的卻一直被拒」，
 * 而兩邊看起來都完全正常。整包 payload 一路帶到底＝結構上不可能組出不同的字串
 * （工程原則 1：被比較的兩個值同源、同處計算）。
 *
 * ⭐ 為什麼還要設定值預驗（expected）：expectedSigner 只證明「簽的人是本人」，
 * 完全不證明「簽的內容是本人要的那件事」。這兩個值**直接乘進部位大小**
 * （sizing.compute_scale_factor），被打穿的 filet-api 只要在回原文時把使用比例改成
 * 1.0000，使用者在錢包裡看到的只是一串字，按下簽名就是一份**貨真價實**的超額曝險
 * 授權——引擎重建訊息、recover、邊界檢查全部通過（1.0 本來就在允許區間內），
 * 事後稽核看起來完全是客戶自己要求的。也就是說：沒有這道預驗，被打穿的 API 能
 * **無中生有一次曝險放大**，而不只是丟掉一筆記錄。與換 leader 那條完全同構。
 * 這一層有效的前提是信任域不同——前端 bundle 由 filet-dashboard 服務、檔案唯讀，
 * 打穿 filet-api 的攻擊者改不到它（與引擎二次驗章同構的獨立防線）。
 *
 * 檢查兩件事而非一件：`allocated_capital`／`capital_utilization` 欄位（後端據以重建
 * 訊息、也是引擎實際會套用的值）與 `message` 本體（使用者在錢包裡實際看到、實際同意
 * 的東西）都必須是同一組數字，缺一邊就有縫。
 *
 * 失敗分類（工程原則 2）：本流程**沒有任何自動重試**。送出是非冪等寫入（寫一筆
 * 設定記錄）且 nonce 一次性——重送同一筆必然因 nonce 已消耗而失敗，重來只能整條
 * 流程重跑（重取原文、新 nonce、重簽），且必須由使用者按鈕觸發：這是曝險變更，
 * 不是可以替使用者自動重試的動作。
 */
import type { CapitalSettingsMessageResp, CapitalSettingsResp } from "./api";
import { capitalMessageLines, type CapitalValues } from "./capitalValues";

export interface CapitalSettingsDeps {
  /** 取伺服器 canonical 原文；參數是使用者所調的 canonical 值。 */
  fetchMessage: () => Promise<CapitalSettingsMessageResp>;
  /** 錢包 personal_sign，原文原樣（不加前綴、不重排）。 */
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (
    payload: CapitalSettingsMessageResp,
    signature: string,
  ) => Promise<CapitalSettingsResp>;
}

export type CapitalSettingsFlowResult =
  | { ok: true; resp: CapitalSettingsResp }
  | { ok: false; kind: "message-failed"; error: unknown }
  | { ok: false; kind: "wallet-rejected" }
  | { ok: false; kind: "signer-mismatch" }
  /** 伺服器回傳的待簽設定值 ≠ 使用者所調。非一般錯誤：不得重試，應回報。 */
  | { ok: false; kind: "values-mismatch" }
  | { ok: false; kind: "submit-failed"; error: unknown };

/**
 * `expected` 必須是**已 canonical 化**的值（lib/capitalValues.ts），與伺服器回傳的
 * canonical 字串同一個基準——否則 `1000` vs `1000.00` 會被判成不符，把安全檢查變成
 * 一個每次都擋人的 bug（工程原則 1）。
 */
export async function runCapitalSettingsFlow(
  deps: CapitalSettingsDeps,
  opts: { expectedSigner: string; expected: CapitalValues },
): Promise<CapitalSettingsFlowResult> {
  let payload: CapitalSettingsMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  // ⭐ 設定值預驗：必須在**進錢包之前**。簽完再驗沒有意義——簽章一旦產生就已經是
  // 一份把使用比例拉滿的有效授權，攻擊者只要拿得到它就能自己送出（且它會通過後端
  // 全部驗證，包括邊界檢查：1.0 本來就合法）。這條路徑上零網路請求、不喚起錢包：
  // 不符就是不符，連問後端都不問（fail closed，沿 signer-mismatch 的嚴格度）。
  // 兩邊都比對：
  //   1. 兩個數值欄位——後端據以**重建**訊息驗簽、引擎據以套用，這是真正生效的值；
  //   2. message 本體的對應兩行——使用者在錢包裡實際看到、實際同意的那串字。
  // 只驗欄位，訊息本體可以寫著別的數字；只驗訊息，欄位可以是別的數字。兩邊同時
  // 對上，才談得上「使用者看到的、簽下的、引擎執行的」是同一組數字。
  if (
    payload.allocated_capital !== opts.expected.allocated_capital ||
    payload.capital_utilization !== opts.expected.capital_utilization ||
    !hasEveryLine(payload.message, capitalMessageLines(opts.expected))
  ) {
    return { ok: false, kind: "values-mismatch" };
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

/**
 * 原文是否**逐行**包含這些行。用整行相等而非 `includes`：`"11000.00"` 含有
 * `"1000.00"`，子字串比對會讓「本金放大十倍」照樣過關（見 capitalValues.ts 的說明）。
 */
function hasEveryLine(message: string, lines: string[]): boolean {
  const actual = message.split("\n").map((l) => l.trim());
  return lines.every((line) => actual.includes(line));
}
