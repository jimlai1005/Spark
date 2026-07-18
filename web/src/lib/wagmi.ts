/**
 * wagmi 設定：closed alpha 只接 injected connector（MetaMask）。
 * 不引入 WalletConnect（M3）。chains 宣告只為 connector 型別需要；
 * app 內不用任何會發 RPC 的 hook（useBalance 等）——鏈上事實一律出自後端
 * status（設計定案 10，工程原則 1 同源）。
 */
import { createConfig, http } from "wagmi";
import { arbitrum, mainnet } from "wagmi/chains";
import { injected } from "wagmi/connectors";

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
