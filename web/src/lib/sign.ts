/**
 * lib/sign.ts — EIP-191 personal_sign 的本地 recover。
 *
 * 用途只有一個：**送出前的預驗**——把簽名 recover 回位址，與目前登入的位址比對，
 * 不符就完全不送出（見 leaderSelectFlow）。錢包切到另一個帳號時，使用者看到的
 * 帳號與實際簽名的帳號會不一致，這是唯一能在送出前攔下來的地方（沿 hl.ts
 * recoverSigner 的既有模式；差別只在那邊是 EIP-712、這邊是 EIP-191）。
 *
 * ⭐ 這**不是**授權判斷（授權一律由後端負責，後端會自己重建原文再 recover）。
 * 它防的是「把一筆註定被拒的簽名送出去」，以及更糟的：把 A 帳號的意圖送成 B 帳號的。
 * 紅線：簽名與原文都不進 log——本模組不做任何輸出。
 */
import { recoverMessageAddress } from "viem";

/** 回小寫地址；呼叫端與登入地址（小寫）比對。簽名格式壞 → viem 拋出，由呼叫端接住。 */
export async function recoverPersonalSigner(
  message: string,
  signature: string,
): Promise<string> {
  const addr = await recoverMessageAddress({
    message,
    signature: signature as `0x${string}`,
  });
  return addr.toLowerCase();
}
