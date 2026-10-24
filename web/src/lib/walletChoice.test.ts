import { describe, expect, it } from "vitest";
import { classifyLoginError, pickWalletCandidates, type WalletConnectorLike } from "./walletChoice";

const injected: WalletConnectorLike = { id: "injected", name: "Injected" };
const metamask: WalletConnectorLike = { id: "io.metamask", name: "MetaMask", icon: "data:image/svg+xml;base64,AAA" };
const rabby: WalletConnectorLike = { id: "io.rabby", name: "Rabby Wallet", icon: "data:image/svg+xml;base64,BBB" };

describe("pickWalletCandidates", () => {
  it("清單為空 → none", () => {
    expect(pickWalletCandidates([])).toEqual({ kind: "none", candidates: [] });
  });
  it("只有舊式 injected（沒有 EIP-6963 宣告）→ single，直接用 injected", () => {
    expect(pickWalletCandidates([injected])).toEqual({ kind: "single", candidates: [injected] });
  });
  it("發現一個擴充套件錢包 → single，且排除 injected 後備（避免同一錢包出現兩次）", () => {
    expect(pickWalletCandidates([injected, metamask])).toEqual({ kind: "single", candidates: [metamask] });
  });
  it("發現兩個以上 → multiple，順序照清單", () => {
    expect(pickWalletCandidates([injected, metamask, rabby])).toEqual({
      kind: "multiple",
      candidates: [metamask, rabby],
    });
  });
});

describe("classifyLoginError", () => {
  it("viem UserRejectedRequestError → rejected", () => {
    expect(classifyLoginError({ name: "UserRejectedRequestError", message: "User rejected the request." })).toBe("rejected");
  });
  it("EIP-1193 code 4001 → rejected", () => {
    expect(classifyLoginError({ code: 4001, message: "x" })).toBe("rejected");
  });
  it("訊息含 denied/cancel → rejected", () => {
    expect(classifyLoginError(new Error("MetaMask Tx Signature: User denied message signature."))).toBe("rejected");
    expect(classifyLoginError(new Error("cancelled by user"))).toBe("rejected");
  });
  it("wagmi ProviderNotFoundError → noWallet", () => {
    expect(classifyLoginError({ name: "ProviderNotFoundError", message: "Provider not found." })).toBe("noWallet");
  });
  it("其他 → failed；undefined 也不炸", () => {
    expect(classifyLoginError(new Error("boom"))).toBe("failed");
    expect(classifyLoginError(undefined)).toBe("failed");
  });
});
