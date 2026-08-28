/**
 * lib/publicApi.ts — `/api/public/*` 的 fetch helpers（無需登入，Task 6 後端）。
 *
 * ⭐ 與 lib/api.ts 的 `request()` 刻意分開：`/api/public/*` 不需要 session cookie，
 * 且後端對這幾支端點承諾**永遠 200**（子來源失敗時整段降級為 null/unknown，見
 * publicapi/app.py 對應端點的檔頭註解），呼叫端因此不需要 lib/api.ts 的
 * ApiError 分類——直接 fetch，任何非預期失敗（網路、非 200、格式異常）一律
 * 折疊成呼叫端能安全顯示的保守值（fail-safe：讀不到就說讀不到，不偽裝成健康）。
 */

export type PublicComponentStatus = "ok" | "degraded" | "unknown";

export interface PublicStatusComponent {
  name: string;
  status: PublicComponentStatus;
}

export interface PublicStatus {
  status: PublicComponentStatus;
  components: PublicStatusComponent[];
  updated_at: number;
}

const UNKNOWN_STATUS: PublicStatus = { status: "unknown", components: [], updated_at: 0 };

function isComponentStatus(v: unknown): v is PublicComponentStatus {
  return v === "ok" || v === "degraded" || v === "unknown";
}

/**
 * 讀取 `/api/public/status`。連線失敗、非 200、或回應格式不是預期的三態之一，
 * 一律降級為 `UNKNOWN_STATUS`——絕不把「讀不到」畫成 `"ok"`（工程原則 3 的
 * 前端鏡射：安全訊號讀不到時，寧可顯示保守值，不得偽裝成健康）。
 */
export async function getPublicStatus(): Promise<PublicStatus> {
  try {
    const res = await fetch("/api/public/status");
    if (!res.ok) return UNKNOWN_STATUS;
    const body = (await res.json()) as Partial<PublicStatus> | null;
    if (body == null || !isComponentStatus(body.status)) return UNKNOWN_STATUS;
    return {
      status: body.status,
      components: Array.isArray(body.components) ? body.components : [],
      updated_at: typeof body.updated_at === "number" ? body.updated_at : 0,
    };
  } catch {
    return UNKNOWN_STATUS;
  }
}
