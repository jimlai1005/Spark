import { describe, expect, it, vi } from "vitest";
import { getPublicStatus } from "./publicApi";

function mockFetchOnce(impl: () => Response | Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: async () => body,
  } as Response;
}

describe("getPublicStatus", () => {
  it("回傳後端的三態之一（ok）", async () => {
    mockFetchOnce(() => jsonResponse({ status: "ok", components: [{ name: "api", status: "ok" }], updated_at: 123 }));
    const s = await getPublicStatus();
    expect(s).toEqual({ status: "ok", components: [{ name: "api", status: "ok" }], updated_at: 123 });
  });

  it("degraded 原樣回傳", async () => {
    mockFetchOnce(() => jsonResponse({ status: "degraded", components: [], updated_at: 1 }));
    const s = await getPublicStatus();
    expect(s.status).toBe("degraded");
  });

  it("非 200 → 降級為 unknown（不得偽裝成 ok）", async () => {
    mockFetchOnce(() => jsonResponse({ status: "ok" }, false));
    const s = await getPublicStatus();
    expect(s.status).toBe("unknown");
    expect(s.components).toEqual([]);
  });

  it("網路例外 → 降級為 unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    const s = await getPublicStatus();
    expect(s.status).toBe("unknown");
  });

  it("回應格式不是預期三態之一 → 降級為 unknown", async () => {
    mockFetchOnce(() => jsonResponse({ status: "totally-broken" }));
    const s = await getPublicStatus();
    expect(s.status).toBe("unknown");
  });
});
