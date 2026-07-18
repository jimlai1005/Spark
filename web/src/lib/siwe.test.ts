import { describe, expect, it, vi } from "vitest";
import { loginWithSiwe } from "./siwe";

vi.mock("./api", () => ({
  getNonce: vi.fn(async () => ({ nonce: "n-123", message: "SIWE MESSAGE VERBATIM" })),
  authVerify: vi.fn(async () => ({ address: "0xabc", account_id: "fabc" })),
}));

import { authVerify, getNonce } from "./api";

describe("loginWithSiwe", () => {
  it("nonce → 錢包簽 message 原文 → verify（⭐ 訊息一字不改）", async () => {
    const signMessage = vi.fn(async (m: string) => `0xsig-of:${m.length}`);
    const me = await loginWithSiwe({
      address: "0xAbC0000000000000000000000000000000000001",
      chainId: 42161,
      signMessage,
    });
    expect(getNonce).toHaveBeenCalledWith("0xAbC0000000000000000000000000000000000001", 42161);
    // ⭐ 錢包收到的是伺服器 message 原文
    expect(signMessage).toHaveBeenCalledWith("SIWE MESSAGE VERBATIM");
    expect(authVerify).toHaveBeenCalledWith("n-123", "0xsig-of:21");
    expect(me).toEqual({ address: "0xabc", account_id: "fabc" });
  });

  it("使用者在錢包按拒絕 → 錯誤原樣上拋（呼叫端顯示文案），不呼叫 verify", async () => {
    vi.mocked(authVerify).mockClear();
    const signMessage = vi.fn(async () => {
      throw new Error("User rejected the request.");
    });
    await expect(
      loginWithSiwe({ address: "0xAbC0000000000000000000000000000000000001", chainId: 1, signMessage }),
    ).rejects.toThrow(/User rejected/);
    expect(authVerify).not.toHaveBeenCalled();
  });
});
