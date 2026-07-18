/**
 * lib/hooks.test.ts — useOnboardingStatus 401-stale 導回登入（opus 終審 Minor 1）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";

const getStatus = vi.fn();
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getStatus: (...a: unknown[]) => getStatus(...a),
}));

import { useOnboardingStatus } from "./hooks";

function wrap(qc: QueryClient) {
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

describe("useOnboardingStatus", () => {
  it("401（ApiError kind=auth）→ 讓 [\"me\"] 快取失效", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["me"], { address: "0xabc", account_id: "fabc" });
    getStatus.mockRejectedValue(new ApiError("auth", "未登入", 401));

    renderHook(() => useOnboardingStatus({ enabled: true, pollMs: false }), {
      wrapper: wrap(qc),
    });

    await waitFor(() => {
      expect(qc.getQueryState(["me"])?.isInvalidated).toBe(true);
    });
  });

  it("非 auth 錯誤（如 upstream）→ 不動 [\"me\"] 快取", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["me"], { address: "0xabc", account_id: "fabc" });
    getStatus.mockRejectedValue(new ApiError("upstream", "暫時不可用", 503));

    const { result } = renderHook(() => useOnboardingStatus({ enabled: true, pollMs: false }), {
      wrapper: wrap(qc),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(qc.getQueryState(["me"])?.isInvalidated).toBe(false);
  });
});
