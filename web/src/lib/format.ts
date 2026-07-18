/** 顯示層工具。位址縮寫照 v1 原型格式：0x5579…B5d（前 6 字元 + 省略號 + 後 3）。 */
export function shortAddr(addr: string): string {
  if (!/^0x[0-9a-fA-F]{40}$/.test(addr)) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-3)}`;
}
