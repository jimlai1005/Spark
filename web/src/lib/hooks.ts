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
