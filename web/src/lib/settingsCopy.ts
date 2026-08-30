/**
 * lib/settingsCopy.ts — M3 round4 Task R4-4：伺服器 zh 散文欄位 → copy.ts 雙語
 * 文案的顯示層對照函式。獨立成檔（而非留在 `app/settings/page.tsx`）是因為
 * Next.js App Router 的 `page.tsx` 只允許少數受限的具名匯出（`generateMetadata`
 * 等），任意具名匯出（例如給測試用）會讓 `next build` 判為不合法的 Page export
 * 而編譯失敗——這幾個純函式因此需要一個非 page 的落點，`StepRiskLimits.tsx`
 * （onboarding wizard）與 `app/settings/page.tsx` 兩處共用同一份，避免走樣。
 *
 * 共同原則：已知的封閉列舉（`RiskParamName` / `MyCapitalStatus` / `MyLeaderStatus`）
 * 一律優先取 copy.ts 的雙語文案，不再直接渲染伺服器散文；查無對應 key（未來新
 * 參數／新狀態尚未補雙語）才 fallback 伺服器原文，不炸畫面。
 */
import type { MyCapitalResp, MyLeaderResp, RiskParamSpec } from "./api";
import type { DeepString } from "./copy";
import type { COPY_ZH } from "./copy";

type Copy = DeepString<typeof COPY_ZH>;

export function paramCopyOf(
  spec: RiskParamSpec, c: Copy["settings"]["risk"],
): { label: string; help: string } {
  const table = c.paramLabels as Record<string, { label: string; help: string } | undefined>;
  return table[spec.name] ?? { label: spec.label, help: spec.help };
}

export function capitalNoteOf(data: MyCapitalResp, c: Copy["settings"]["capital"]): string {
  const table = c.notesByStatus as Record<string, string | undefined>;
  return table[data.status] ?? data.note;
}

export function leaderNoteOf(d: MyLeaderResp, c: Copy["settings"]["leader"]): string {
  const table = c.notesByStatus as Record<string, string | undefined>;
  return table[d.status] ?? d.note;
}
