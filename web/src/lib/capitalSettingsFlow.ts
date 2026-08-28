/**
 * lib/capitalSettingsFlow.ts — 資金配置（投入比例／本金模式）簽章編排 ⭐
 * （沿 riskSettingsFlow.ts 的謹慎度；先讀那份檔頭，這裡只寫**不同**的部分。
 * 2026-08-28 主線程裁決新增，Task 10b：`allocated_capital`／`capital_utilization`
 * 直接乘進部位大小，危害與換 leader 同級，須走同一套簽章防線。）
 *
 * 固定順序：fetchMessage（伺服器產生的 canonical 原文）→ **內容預驗（進錢包之前，
 *   不符即中止，零網路請求、不喚起錢包）** → 錢包 personal_sign 原文 → 本地
 *   recover 預驗（≠ 登入地址即中止，**零網路請求**）→ 送出，原文原樣回送。
 *
 * ⭐ 為什麼要驗「內容」而不只是「簽章者」：`expectedSigner` 只證明「簽的人是本人」，
 * 完全不證明「簽的內容是本人要的那件事」——被打穿的 filet-api 只要回一份把
 * `capital_utilization` 拉到 1.0（滿倉）的待簽原文，錢包上顯示的是一段英文，
 * 客戶按了簽，簽章貨真價實，後端重建訊息、recover、驗證全部通過，事後稽核看起來
 * 完全是客戶自己要求把曝險拉滿。所以預驗必須在**進錢包之前**，且同時比對：
 *   1. 伺服器回聲的欄位（`account_id`／`allocated_capital`／`capital_utilization`／
 *      `use_full_equity`）與客戶端期望的逐項一致；
 *   2. 原文本體的 `Account:`／`Allocated Capital:`／`Capital Utilization:` 三行
 *      確實寫著這組值——客戶在錢包裡實際看到、實際同意的東西。
 * 兩者都對上，才談得上「使用者看到的、簽下的、後端執行的」是同一件事（工程原則 1）。
 *
 * 失敗分類（工程原則 2）：本流程**沒有任何自動重試**。送出是非冪等寫入且 nonce
 * 一次性——重送同一筆必然因 nonce 已消耗而失敗，重來只能整條流程重跑（重取原文、
 * 新 nonce、重簽），且必須由使用者按鈕觸發：這個值直接乘進部位大小，不是可以
 * 替使用者自動重試的動作。
 */
import type { CapitalSettingsMessageResp, CapitalSettingsResp } from "./api";

export type CapitalFlowFailure =
  | { ok: false; kind: "message-failed"; error: unknown }
  | { ok: false; kind: "wallet-rejected" }
  | { ok: false; kind: "signer-mismatch" }
  /** 伺服器回傳的內容（欄位或原文）與客戶端期望的不符。非一般錯誤：應回報。 */
  | { ok: false; kind: "content-mismatch" }
  | { ok: false; kind: "submit-failed"; error: unknown };

export type CapitalSettingsFlowResult =
  | { ok: true; resp: CapitalSettingsResp }
  | CapitalFlowFailure;

export interface CapitalSettingsDeps {
  fetchMessage: () => Promise<CapitalSettingsMessageResp>;
  /** 錢包 personal_sign，原文原樣（不加前綴、不重排）。 */
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (payload: CapitalSettingsMessageResp, signature: string) => Promise<CapitalSettingsResp>;
}

const FULL_EQUITY_MESSAGE_VALUE = "full account equity"; // 沿 capital_settings.py 常數，逐字同步

/** 數值欄位同值即相同（伺服器會把 `"0.25"` 正規化成 `"0.2500"`）；兩側都非數字才退回字串比對。 */
function sameNumeric(a: string, b: string): boolean {
  const x = Number(a);
  const y = Number(b);
  if (Number.isFinite(x) && Number.isFinite(y)) return x === y;
  return a === b;
}

/** `<Label>: <value>` 那一行的值（冒號後、去頭尾空白的剩餘字串）。 */
function lineValue(message: string, label: string): string | undefined {
  const line = message.split("\n").find((l) => l.startsWith(`${label}:`));
  return line?.slice(label.length + 1).trim();
}

export async function runCapitalSettingsFlow(
  deps: CapitalSettingsDeps,
  opts: {
    expectedSigner: string;
    expectedAccountId: string;
    expectedAllocatedCapital: string;
    expectedCapitalUtilization: string;
    expectedUseFullEquity: boolean;
  },
): Promise<CapitalSettingsFlowResult> {
  let payload: CapitalSettingsMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  const fieldsMatch =
    payload.account_id === opts.expectedAccountId
    && payload.use_full_equity === opts.expectedUseFullEquity
    && sameNumeric(payload.allocated_capital, opts.expectedAllocatedCapital)
    && sameNumeric(payload.capital_utilization, opts.expectedCapitalUtilization);

  const expectedCapLine = opts.expectedUseFullEquity
    ? FULL_EQUITY_MESSAGE_VALUE
    : `${opts.expectedAllocatedCapital} USDC`;
  const accountLine = lineValue(payload.message, "Account");
  const capLine = lineValue(payload.message, "Allocated Capital");
  const utilLine = lineValue(payload.message, "Capital Utilization");
  const messageMatches =
    accountLine === opts.expectedAccountId
    && capLine === expectedCapLine
    && utilLine !== undefined && sameNumeric(utilLine, opts.expectedCapitalUtilization);

  if (!fieldsMatch || !messageMatches) {
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
