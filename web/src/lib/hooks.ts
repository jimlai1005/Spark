/**
 * lib/hooks.ts — 資料 hooks。
 * useMe：session 身分（401 → null，不當錯誤）。
 * useOnboardingStatus：鏈上進度輪詢（wizard 5s / performance 30s，設計定案 16）。
 * useBillingStatus：訂閱狀態（Header chip 與 /billing 共用同一把快取）。
 */
"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getAdminPending,
  getBillingStatus,
  getMe,
  getStatus,
  type BillingStatusResp,
  type Me,
  type OnboardStatus,
} from "./api";

export function useMe() {
  return useQuery<Me | null>({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await getMe();
      } catch (e) {
        if (e instanceof ApiError && e.kind === "auth") return null;
        throw e;
      }
    },
  });
}

export function useOnboardingStatus(opts: { enabled: boolean; pollMs: number | false }) {
  const queryClient = useQueryClient();
  // 401 stale（opus Minor 1）：使用者停留頁面期間 session 過期時，輪詢會開始拋
  // ApiError(kind="auth")——讓 ["me"] 快取失效，useMe 重抓回未登入態，頁面 guard
  // 自然導回登入視圖；不讓輪詢錯誤靜默堆積。
  return useQuery<OnboardStatus>({
    queryKey: ["onboard-status"],
    queryFn: async () => {
      try {
        return await getStatus();
      } catch (e) {
        if (e instanceof ApiError && e.kind === "auth") {
          queryClient.invalidateQueries({ queryKey: ["me"] });
        }
        throw e;
      }
    },
    enabled: opts.enabled,
    refetchInterval: opts.pollMs,
  });
}

/**
 * 訂閱狀態。⭐ 刻意**不吞任何錯誤**（與 useMe 的 401→null 相反）：
 * Header 與 /billing 共用同一個 queryKey，若在此把 501（billing 未啟用）壓成 null，
 * /billing 就再也分不出「未啟用」與「無訂閱」——兩者的正確畫面不同。錯誤原樣上呈，
 * 由各消費端自行判讀：Header 只認 data（沒 data 就整組不顯示），/billing 讀 error.status。
 * enabled 綁登入：未登入不打這支（避免必然的 401 噪音）。
 */
export function useBillingStatus(opts: { enabled: boolean }) {
  return useQuery<BillingStatusResp, ApiError>({
    queryKey: ["billing-status"],
    queryFn: getBillingStatus,
    enabled: opts.enabled,
  });
}

/**
 * 這個 session 是不是管理員——⭐ **答案由後端給，前端不判斷**（紅線：不得用前端判斷
 * 取代後端授權）。
 *
 * 做法是**探測**而非推論：打一支真正掛著 `_require_admin` 的端點（/api/admin/pending，
 * app.py:1097），成功才回 true。非管理員拿到 403 → false；未登入拿到 401 → false。
 * 也就是說「是不是 admin」這個判斷自始至終只在後端做過一次，前端只是把後端的答案
 * 反映到導覽列上。刻意**不**改用「比對地址清單」或「Me 加一個 is_admin 旗標」之類的
 * 前端推論——那會讓前端持有一份可能與後端不同步的第二事實。
 *
 * 這個回傳值只准用來決定「要不要顯示連結」。它**不是**存取控制：/ops 與 /admin 的
 * 每一支端點都各自掛著後端 admin 閘，使用者手打網址仍會吃 403 並看到「此頁僅限管理員」，
 * 兩頁的 403 分支都已有測試釘住。
 *
 * queryKey 與 /admin 頁共用 → 進到 /admin 時零額外請求（react-query 快取去重）。
 * enabled 綁登入：未登入不打（避免必然的 401 噪音，與 useBillingStatus 同慣例）。
 */
export function useIsAdmin(opts: { enabled: boolean }): boolean {
  const q = useQuery({
    queryKey: ["admin-pending"],
    queryFn: getAdminPending,
    enabled: opts.enabled,
    staleTime: 60_000,
  });
  // 只認成功。載入中／403／401／任何錯誤一律 false——不確定時不顯示（fail closed）。
  return !!q.data;
}
