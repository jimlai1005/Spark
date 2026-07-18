/**
 * lib/siwe.ts — SIWE 登入編排（⭐）。
 * 伺服器是 SIWE 訊息的權威（app.py 以 domain/URI 設定重建訊息驗簽）；
 * 前端唯一職責：把 message **原文**交給錢包 personal_sign，簽名交回 verify。
 * signMessage 由呼叫端注入（頁面傳 wagmi 的 signMessageAsync）——本模組可離線測試。
 */
import { authVerify, getNonce, type Me } from "./api";

export async function loginWithSiwe(opts: {
  address: string;
  chainId: number;
  signMessage: (message: string) => Promise<string>;
}): Promise<Me> {
  const { nonce, message } = await getNonce(opts.address, opts.chainId);
  const signature = await opts.signMessage(message); // EIP-191 personal_sign，原文
  return authVerify(nonce, signature);
}
