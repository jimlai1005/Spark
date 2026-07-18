/**
 * lib/hooks.ts — 資料 hooks。
 * useMe：session 身分（401 → null，不當錯誤）。
 * useOnboardingStatus：鏈上進度輪詢（wizard 5s / performance 30s，設計定案 16）。
 */
"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
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
