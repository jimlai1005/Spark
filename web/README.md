# web — Filet 儀表板（Next.js 15 + wagmi）

指令：`npm test`（vitest，離線）、`npm run build`、`npm run lint`。
預設 shell 無 node，先 `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH"`。

## 依賴衛生（改 package.json 或 import 前務必先讀）

`package.json` 是純 JSON、放不進註解，故決策理由寫在這裡。以下兩條都是**已驗證過的
結論**，不是偏好；改動前請先重跑對應的驗證指令，不要「順手」還原。

### 1. `overrides.postcss: ^8.5.10`

`next` 依賴的 postcss <8.5.10 有 XSS advisory（未跳脫的 `</style>` 可脫出 style 區塊），
且它會進生產樹。收斂後 `npm audit --production` 由 10 moderate 降為 8。
驗證：`npm ls postcss` 應只解析出單一 8.5.10+ 版本（實測 8.5.19）。

同一區塊的 `overrides.viem: 2.55.2` 是既有條目（收斂多版本 viem），一併保留。

### 2. `wagmi.ts` 必須從 `wagmi` 主入口 import `injected`

```ts
import { createConfig, http, injected } from "wagmi";   // ✅
import { injected } from "wagmi/connectors";            // ❌ 絕對不要改成這樣
```

兩者拿到的是**同一個 binding**（`@wagmi/connectors` 的 injected 本身就是
`export { injected } from '@wagmi/core'`），所以改成 barrel 版本「看起來也能動」——
這正是它危險的地方。差別在 barrel 會一併載入 `baseAccount` / `walletConnect` /
`metaMask` 等我們沒用到的 connector，把 `@metamask/sdk`（uuid advisory）、
`@base-org/account`、`@coinbase/cdp-sdk`、`@walletconnect/*`（含各自巢狀的舊版
viem／ws）全部拖進 bundle。後果有兩層：

- build 會因 `@coinbase/cdp-sdk` 的 optional peer（`@x402/*`）解析失敗；
- 更糟的是 build **沒**失敗的情況——漏洞碼靜靜進了產物。

`npm audit --production` 目前殘餘的 8 moderate（root：`uuid`、`@metamask/utils`）
全部只透過這條 barrel 路徑可達。它們留在依賴**樹**裡是因為 `@wagmi/connectors` 是
`wagmi` 的 dependency，而 audit 讀的是 package tree、不是實際產出的 bundle。

實測驗證（`npm run build` 後）：

```bash
grep -rl 'MetaMaskSDK' .next/static/chunks/ | wc -l    # → 0
```

即：advisory 進不了產物。若哪天這個計數不是 0，代表有人改回 barrel import，
或引入了別的 connector——那時要先確認 bundle 影響，而不是只看 audit 數字。

> ⚠️ 不要為了把 audit 數字清成 0 而動 `wagmi` 版本或加 `npm audit fix --force`：
> 可運行性優先於數字。真正的防線是上面那條 import 紀律與 grep 驗證。
