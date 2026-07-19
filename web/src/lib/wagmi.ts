/**
 * wagmi 設定：closed alpha 只接 injected connector（MetaMask）。
 * 不引入 WalletConnect（M3）。chains 宣告只為 connector 型別需要；
 * app 內不用任何會發 RPC 的 hook（useBalance 等）——鏈上事實一律出自後端
 * status（設計定案 10，工程原則 1 同源）。
 */
// injected 從 wagmi 主入口取（等同 @wagmi/core 的同一個 binding——
// @wagmi/connectors 的 injected 本身就是 `export { injected } from '@wagmi/core'`）。
// 不走 `wagmi/connectors` barrel：該 barrel 會一併載入 baseAccount／walletConnect／
// metaMask 等未使用的 connector，把 @base-org/account、@coinbase/cdp-sdk、
// @walletconnect/* （含各自巢狀的舊版 viem／ws）全部拖進 bundle——
// 既讓 build 因 cdp-sdk 的 optional peer（@x402/*）解析失敗，也把漏洞碼帶進產物。
import { createConfig, http, injected } from "wagmi";
import { arbitrum, mainnet } from "wagmi/chains";

export const wagmiConfig = createConfig({
  chains: [arbitrum, mainnet],
  connectors: [injected()],
  multiInjectedProviderDiscovery: false,
  transports: {
    [arbitrum.id]: http(),
    [mainnet.id]: http(),
  },
});
// 實作提醒：若 `npm run build` 的 SSR prerender 因 storage/hydration 報錯，
// 在 createConfig 加 `ssr: true`（wagmi 官方 SSR 選項），不要改頁面結構繞。
