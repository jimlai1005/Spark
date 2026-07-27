/**
 * lib/customLeader.ts — 自訂 leader（任意 wallet address 輸入）的准入預覽編排。
 *
 * 對應 2026-07-27 spec：用戶在 /leaders 輸入任意位址 → 即時格式驗證 → 後端
 * /api/leaders/preview 執行准入檢查（格式／自跟／operator kill-switch）＋精選優先權
 * → 回鏈上預覽（帳戶權益、持倉數、exists）。通過預覽後走**既有**的換 leader 簽章
 * 流程（leaderSelectFlow.ts），本模組不碰簽章。
 *
 * ⚠️ 鏈上活動（exists）自 2026-07-27 裁決後**不是閘門**：查無活動照樣放行、
 * exists=false 誠實回報，顯示層畫警示但不擋（leader 尚未進場時可先完成配置）。
 *
 * 依賴全注入（同 leaderSelectFlow 慣例）：UI 薄殼提供 api 的實作，本模組可離線測試。
 *
 * 失敗分類（工程原則 2）：
 * - `rejected`＝**semantic** 拒絕（後端准入分類碼，或本地格式驗證）——不重試，
 *   顯示對應文案讓使用者自己修正輸入。
 * - `failed`＝transport 或未知錯誤——本模組**不自動重試**：查詢雖是唯讀冪等，
 *   但重按「查詢」對使用者零成本，交回按鈕天然去重即可，錯誤原樣上呈由 UI 決定文案。
 * - `address-mismatch`＝伺服器回聲的位址 ≠ 送出的位址——預覽卡上的權益與持倉
 *   會被拿來決定「要不要跟這個人」，畫成別人的資料是最會騙過本人的錯（工程原則 1：
 *   顯示的資料必須與使用者輸入的位址同源）。fail closed，不裁量。
 *
 * 格式驗證刻意用 `isAddress(…, { strict: false })`（不驗 EIP-55 checksum）：
 * 後端准入的格式門檻是「0x＋40 hex，小寫正規化」（publicapi/app.py
 * `_admit_custom_leader`），前端若加上 checksum 檢查就比後端嚴——同一個位址
 * 一邊擋一邊收，正是混源比較的無聲誤判（工程原則 1）。兩邊同一把尺。
 */
import { isAddress } from "viem";
import { ApiError, type LeaderPreviewResp } from "./api";

/**
 * 後端准入的拒絕分類碼（app.py `_admission_reject`；改一邊要改兩邊）。
 * ⚠️ `not_found` 自 2026-07-27 裁決後**移除**：鏈上無活動不再拒絕，改為放行並回
 * exists=false（顯示層據此畫警示但不擋，見 page.tsx CustomLeaderSection）。
 */
export const CUSTOM_LEADER_REJECT_REASONS = [
  "invalid_format", "self_follow", "leader_disabled",
] as const;
export type CustomLeaderRejectReason = (typeof CUSTOM_LEADER_REJECT_REASONS)[number];

function isKnownReason(v: string | undefined): v is CustomLeaderRejectReason {
  return (CUSTOM_LEADER_REJECT_REASONS as readonly string[]).includes(v ?? "");
}

export type CustomLeaderInputCheck =
  /** `address` 已小寫正規化——後續一切（查詢、簽章錨定）都用這一份。 */
  | { ok: true; address: string }
  /** `empty`：輸入為空（顯示層不畫錯誤訊息）或非空但格式錯（畫錯誤訊息）。 */
  | { ok: false; empty: boolean };

/** 即時格式驗證＋小寫正規化。純函式、零 IO——輸入框每次變更都可以呼叫。 */
export function validateCustomLeaderInput(raw: string): CustomLeaderInputCheck {
  const trimmed = raw.trim();
  if (trimmed === "") return { ok: false, empty: true };
  if (!isAddress(trimmed, { strict: false })) return { ok: false, empty: false };
  return { ok: true, address: trimmed.toLowerCase() };
}

export interface CustomLeaderPreviewDeps {
  /** api.getLeaderPreview——收小寫正規化位址。 */
  fetchPreview: (address: string) => Promise<LeaderPreviewResp>;
}

export type CustomLeaderPreviewResult =
  | { ok: true; preview: LeaderPreviewResp }
  /** 准入被拒（semantic）：reason 是機器可判分類碼，detail 是後端的人話補充。 */
  | { ok: false; kind: "rejected"; reason: CustomLeaderRejectReason; detail?: string }
  /** 伺服器回聲位址 ≠ 送出位址：預覽資料不可信，停手（不重試、不裁量）。 */
  | { ok: false; kind: "address-mismatch" }
  /** transport／未知錯誤：原樣上呈，由 UI 決定文案；使用者可重按查詢。 */
  | { ok: false; kind: "failed"; error: unknown };

export async function runCustomLeaderPreview(
  deps: CustomLeaderPreviewDeps,
  rawInput: string,
): Promise<CustomLeaderPreviewResult> {
  // 本地格式閘門：擋得下就不打後端（與後端同一把尺，見檔頭）。
  const check = validateCustomLeaderInput(rawInput);
  if (!check.ok) return { ok: false, kind: "rejected", reason: "invalid_format" };

  let preview: LeaderPreviewResp;
  try {
    preview = await deps.fetchPreview(check.address);
  } catch (error) {
    // 只認識後端契約中的四種分類碼；未知碼不硬塞進已知分類（fail closed）——
    // 硬塞會讓一個新的拒絕原因被顯示成一句驢唇不對馬嘴的舊文案。
    if (error instanceof ApiError && isKnownReason(error.reason)) {
      return { ok: false, kind: "rejected", reason: error.reason, detail: error.detail };
    }
    return { ok: false, kind: "failed", error };
  }

  // 回聲比對（大小寫不敏感——伺服器本就回小寫，這裡防的是位址本身不同）：
  // 預覽卡的權益與持倉必須屬於使用者輸入的那個位址，否則整張卡都是誤導。
  if (preview.address.toLowerCase() !== check.address) {
    return { ok: false, kind: "address-mismatch" };
  }
  return { ok: true, preview };
}
