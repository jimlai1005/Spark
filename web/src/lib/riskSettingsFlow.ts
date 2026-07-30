/**
 * lib/riskSettingsFlow.ts — 風控設定與「立即恢復跟單」的簽章編排 ⭐
 * （沿 leaderSelectFlow.ts 的形狀與謹慎度；先讀那份檔頭，這裡只寫**不同**的部分）。
 *
 * 固定順序（兩條流程相同）：
 *   fetchMessage（伺服器產生的 canonical 原文）
 *   → **內容預驗（進錢包之前，不符即中止，零網路請求、不喚起錢包）**
 *   → 錢包 personal_sign 原文
 *   → 本地 recover 預驗（≠ 登入地址即中止，**零網路請求**）
 *   → 送出，原文原樣回送。
 * 依賴全注入：UI 薄殼提供 wagmi 的簽名函式與 api/sign 的實作——本模組可離線測試。
 *
 * ⭐ 為什麼設定調整也需要簽章、以及為什麼預驗必須比對「內容」而不只是「簽章者」
 * ----------------------------------------------------------------------------
 * 風控門檻決定「跌到哪裡停手」。能改它的人就能把一道保護整個拿掉，而帳戶裡每一筆
 * 交易看起來都完全正常。`expectedSigner`（recover 預驗）只證明「簽的人是本人」，
 * 完全不證明「簽的內容是本人要的那件事」：被打穿的 filet-api 只要回一份把
 * `enabled` 改成 false（或把門檻拉到上界）的待簽原文，錢包上顯示的是一段英文，
 * 客戶按了簽——簽章貨真價實，後端重建驗簽、引擎二次驗章全部通過，事後稽核看起來
 * 完全是客戶自己要求把保護關掉的。所以預驗必須在**進錢包之前**，且要比對三件事：
 *   1. `account_id` 是我——別人的帳號的設定不該由我的簽章授權；
 *   2. 伺服器回聲的 `prefs` 與我送出的**逐欄位**一致（數值同值即可，見
 *      `samePref`：伺服器會把 `0.20` 正規化成 `0.2`，那是合法的同一個值）；
 *   3. `message` 本體逐項寫著 `prefs` 的那些值——客戶在錢包裡看到、實際同意的
 *      就是這段文字，欄位與原文指向不同設定時，兩者之間就有一道縫。
 * 第 3 步刻意拿**伺服器回聲的 prefs**（不是客戶端草稿）去比對原文：兩者都出自
 * 伺服器的同一次正規化，所以可以逐字比對而不會被格式差異誤擋；而回聲本身已在
 * 第 2 步釘死在客戶的意圖上——鏈接起來就是「客戶要的 ＝ 欄位 ＝ 原文」（工程原則 1）。
 *
 * ⭐ 解鎖流程的域分隔預驗（`runRiskUnlockFlow`）
 * ---------------------------------------------
 * 「立即恢復跟單」的原文**不得**帶任何風控參數設定：若解鎖端點回的其實是一份
 * 「把門檻全部放寬」的設定原文，客戶會在以為自己只是恢復跟單的情況下簽掉保護。
 * 後端的域分隔在版型第一行（filet/risk_settings.py 檔頭），本模組另備一道獨立的
 * 前端防線：原文裡不得出現任何 `<參數名>:` 行。參數名取自後端 `specs`（不是前端
 * 硬編的清單），後端新增參數時這道防線自動跟上。
 *
 * 失敗分類（工程原則 2）：本流程**沒有任何自動重試**。送出是非冪等寫入且 nonce
 * 一次性——重送同一筆必然因 nonce 已消耗而失敗，重來只能整條流程重跑（重取原文、
 * 新 nonce、重簽），且必須由使用者按鈕觸發。
 */
import type {
  RiskPrefs,
  RiskSettingsMessageResp,
  RiskSettingsResp,
  RiskUnlockMessageResp,
  RiskUnlockResp,
} from "./api";

/** 兩條流程共用的失敗分類。`content-mismatch`＝請停手回報，不得重試。 */
export type RiskFlowFailure =
  | { ok: false; kind: "message-failed"; error: unknown }
  | { ok: false; kind: "wallet-rejected" }
  | { ok: false; kind: "signer-mismatch" }
  /** 伺服器回的原文／欄位指向的不是使用者要的那件事。非一般錯誤：應回報。 */
  | { ok: false; kind: "content-mismatch" }
  | { ok: false; kind: "submit-failed"; error: unknown };

export type RiskSettingsFlowResult =
  | { ok: true; resp: RiskSettingsResp }
  | RiskFlowFailure;

export type RiskUnlockFlowResult =
  | { ok: true; resp: RiskUnlockResp }
  | RiskFlowFailure;

export interface RiskSettingsDeps {
  fetchMessage: () => Promise<RiskSettingsMessageResp>;
  /** 錢包 personal_sign，原文原樣（不加前綴、不重排）。 */
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (payload: RiskSettingsMessageResp, signature: string) => Promise<RiskSettingsResp>;
}

export interface RiskUnlockDeps {
  fetchMessage: () => Promise<RiskUnlockMessageResp>;
  signMessage: (message: string) => Promise<string>;
  recover: (message: string, signature: string) => Promise<string>;
  submit: (payload: RiskUnlockMessageResp, signature: string) => Promise<RiskUnlockResp>;
}

/**
 * 單一欄位的比對。布林嚴格相等；數值欄位**同值即相同**——伺服器會把 `0.20`
 * 正規化成 `0.2`（後端 `risk_prefs._as_bounded_decimal`），把那視為不符會讓每一次
 * 儲存都失敗，且看起來像遭到攻擊。兩側都無法解析成有限數字時退回字串嚴格比對。
 */
function samePref(mine: string | boolean, echoed: string | boolean): boolean {
  if (typeof mine === "boolean" || typeof echoed === "boolean") return mine === echoed;
  const a = Number(mine);
  const b = Number(echoed);
  if (Number.isFinite(a) && Number.isFinite(b)) return a === b;
  return mine === echoed;
}

/** 伺服器回聲的偏好是否**逐欄位**等於客戶送出的那一組（欄位集合也必須相同）。 */
function samePrefs(mine: RiskPrefs, echoed: RiskPrefs): boolean {
  const mineKeys = Object.keys(mine).sort();
  const echoedKeys = Object.keys(echoed as unknown as Record<string, unknown>).sort();
  if (mineKeys.join(",") !== echoedKeys.join(",")) return false;
  const m = mine as unknown as Record<string, string | boolean>;
  const e = echoed as unknown as Record<string, string | boolean>;
  return mineKeys.every((k) => samePref(m[k], e[k]));
}

/**
 * `<label>: <value>` 那一行的**值 token**（冒號後的第一個空白分隔詞）。
 * 取 token 而不是整行剩餘字串，是為了容納後端替帶單位的參數附加的單位詞
 * （`cooldown_hours: 12 hours`）——單位是給人看的，值才是被簽下的東西。
 */
function lineValue(message: string, label: string): string | undefined {
  const line = message.split("\n").find((l) => l.startsWith(`${label}:`));
  return line?.slice(label.length + 1).trim().split(/\s+/)[0];
}

/**
 * 原文本體是否逐項寫著這組偏好。⚠️ 比對對象是**伺服器回聲的 prefs**（見檔頭）：
 * 兩者出自伺服器同一次正規化，逐字比對不會被格式差異誤擋。
 */
function messageStatesPrefs(message: string, prefs: RiskPrefs): boolean {
  const p = prefs as unknown as Record<string, string | boolean>;
  for (const [name, v] of Object.entries(p)) {
    if (name === "enabled") continue;
    if (lineValue(message, name) !== (typeof v === "boolean" ? String(v) : v)) return false;
  }
  // 總開關那一行的標籤不是參數名（後端常數 `ENABLED_MESSAGE_LABEL`），所以以**值**
  // 定位：原文裡必須有一行的值恰為我方預期的那一個字，且不得同時出現相反的那個字
  // ——「同時說開又說關」的原文，客戶無從知道系統會執行哪一句。
  const lines = message.split("\n");
  const has = (word: string) =>
    lines.some((l) => new RegExp(`^[^:]+:\\s*${word}\\s*$`).test(l));
  return has(prefs.enabled ? "enabled" : "disabled")
    && !has(prefs.enabled ? "disabled" : "enabled");
}

export async function runRiskSettingsFlow(
  deps: RiskSettingsDeps,
  opts: { expectedSigner: string; expectedAccountId: string; expectedPrefs: RiskPrefs },
): Promise<RiskSettingsFlowResult> {
  let payload: RiskSettingsMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  // ⭐ 內容預驗：必須在**進錢包之前**。簽完再驗沒有意義——簽章一旦產生就已經是
  // 一份「把保護關掉」的有效授權，攻擊者只要拿得到它就能自己送出（且它會通過後端
  // 全部驗證）。這條路徑上零網路請求、不喚起錢包（fail closed）。
  if (
    payload.account_id !== opts.expectedAccountId
    || !samePrefs(opts.expectedPrefs, payload.prefs)
    || !messageStatesPrefs(payload.message, payload.prefs)
  ) {
    return { ok: false, kind: "content-mismatch" };
  }

  return finish(deps, payload, opts.expectedSigner);
}

export async function runRiskUnlockFlow(
  deps: RiskUnlockDeps,
  opts: { expectedSigner: string; expectedAccountId: string; riskParamNames: string[] },
): Promise<RiskUnlockFlowResult> {
  let payload: RiskUnlockMessageResp;
  try {
    payload = await deps.fetchMessage();
  } catch (error) {
    return { ok: false, kind: "message-failed", error };
  }

  // 預驗三件事：帳號是我、原文確實綁在我的帳號上、且這**不是**一份設定原文
  // （見檔頭的域分隔說明）。任何一件不成立都不喚起錢包。
  if (
    payload.account_id !== opts.expectedAccountId
    || !payload.message.includes(payload.account_id)
    || opts.riskParamNames.some((n) => payload.message.includes(`${n}:`))
  ) {
    return { ok: false, kind: "content-mismatch" };
  }

  return finish(deps, payload, opts.expectedSigner);
}

/**
 * 兩條流程的共同尾段：簽名 → 本地 recover 預驗 → 送出。
 *
 * ⭐ 本地 recover 預驗是錢包切錯帳號簽的唯一攔截點。這條路徑上**不得**有任何網路
 * 請求——不符就是不符，連問後端都不問（錯的簽名送出去，最好的情況是被拒，最壞的
 * 情況是被記成另一個人的意圖）。recover 本身拋錯（簽名格式壞）同樣視為不符：
 * 證明不了是本人簽的就不送（fail closed）。
 */
async function finish<P extends { message: string }, R>(
  deps: {
    signMessage: (message: string) => Promise<string>;
    recover: (message: string, signature: string) => Promise<string>;
    submit: (payload: P, signature: string) => Promise<R>;
  },
  payload: P,
  expectedSigner: string,
): Promise<{ ok: true; resp: R } | RiskFlowFailure> {
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
  if (recovered !== expectedSigner.toLowerCase()) {
    return { ok: false, kind: "signer-mismatch" };
  }

  try {
    // 整包 payload 帶進去：原文與各欄位同源，前端沒有機會重組出不一樣的字串。
    const resp = await deps.submit(payload, signature);
    return { ok: true, resp };
  } catch (error) {
    // 不自動重試（非冪等 ＋ nonce 一次性）——錯誤原樣上呈，由 UI 決定怎麼說。
    return { ok: false, kind: "submit-failed", error };
  }
}
