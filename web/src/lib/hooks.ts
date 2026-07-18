/**
 * lib/hooks.ts — 資料 hooks。
 * useMe：session 身分（401 → null，不當錯誤）。
 * useOnboardingStatus：鏈上進度輪詢（wizard 5s / performance 30s，設計定案 16）。
 */
"use client";
import { useQuery } from "@tanstack/react-query";
import { ApiError, getMe, getStatus, type Me, type OnboardStatus } from "./api";

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
  // 實作提醒（401 stale）：使用者停留頁面期間 session 過期時，輪詢會開始拋
  // ApiError(kind="auth")——此時應讓 ["me"] 快取失效（invalidateQueries），
  // 頁面 guard 自然呈現「回登入頁」，不要讓輪詢錯誤靜默堆積。
  return useQuery<OnboardStatus>({
    queryKey: ["onboard-status"],
    queryFn: getStatus,
    enabled: opts.enabled,
    refetchInterval: opts.pollMs,
  });
}
