# Research: ApproveAgent / ApproveBuilderFee 外部簽名（瀏覽器錢包簽、伺服器提交）

日期：2026-07-17　狀態：**可行（GO）**　SDK：hyperliquid-python-sdk 0.24.0
（本地路徑 `.venv/lib/python3.14/site-packages/hyperliquid/`）

## 結論

**可行。** 兩個 action 都是 user-signed action（EIP-712 typed data，domain
`HyperliquidSignTransaction`）。SDK 把「建 typed data」與「簽名」拆在不同函式：
`user_signed_payload()`（signing.py:217-237）**不接觸任何私鑰**即可建出完整 typed data；
瀏覽器用 `eth_signTypedData_v4` 簽出的 r/s/v 與 SDK `eth_account` 簽名同為 EIP-712 標準語意，
伺服器可用 SDK 的 `recover_user_from_user_signed_action()`（signing.py:467-472）預驗，
再以 `{action, nonce, signature}` POST `/exchange` 提交。已用本機腳本實測
「無私鑰建 typed data → 獨立 key 簽名 → SDK recover 回同一地址」round-trip 通過
（ApproveAgent 與 ApproveBuilderFee 皆通過，見 §7 證據）。

最大風險：**signatureChainId 必須等於使用者錢包當下的 active chain**（MetaMask 強制
domain.chainId == active chain），所以伺服器建 typed data 前要先向前端要 chainId，
不能照 SDK 硬編 `0x66eee`。次要風險：approveAgent 是 rotation 語意（非冪等）、
nonce 窗口 (T-2d, T+1d) 內簽名可重放直到被使用。

---

## 子問題逐條

### 1. 是不是 user-signed action？typed data 能否無私鑰建出？

**是，且能。** 證據：

- `Exchange.approve_agent()`（exchange.py:635-657）組 action 後呼叫 `sign_agent()`；
  `Exchange.approve_builder_fee()`（exchange.py:659-664）呼叫 `sign_approve_builder_fee()`。
- `sign_agent`（signing.py:412-424）與 `sign_approve_builder_fee`（signing.py:427-439）
  都委給 `sign_user_signed_action`（signing.py:247-253），它做三件事：
  1. `action["signatureChainId"] = "0x66eee"`（可改，見子問題 4）
  2. `action["hyperliquidChain"] = "Mainnet" if is_mainnet else "Testnet"`
  3. `user_signed_payload(...)` 建 typed data → `sign_inner(wallet, data)` 簽名。
- **`user_signed_payload(primary_type, payload_types, action)`（signing.py:217-237）
  的參數裡沒有 wallet/私鑰**——這就是我們要的「只建不簽」函式，直接可重用。

#### Typed data 完整結構（抄自 SDK 原始碼，可直接寫 code）

**Domain**（signing.py:219-225）：

| 欄位 | 值 |
|---|---|
| name | `"HyperliquidSignTransaction"` |
| version | `"1"` |
| chainId | `int(action["signatureChainId"], 16)` ——由 action 的 signatureChainId 決定 |
| verifyingContract | `"0x0000000000000000000000000000000000000000"` |

**EIP712Domain types**（signing.py:228-233）：name(string), version(string),
chainId(uint256), verifyingContract(address)。

**ApproveAgent**（signing.py:416-421；primaryType `"HyperliquidTransaction:ApproveAgent"`）：

| 欄位 | type | 說明 |
|---|---|---|
| hyperliquidChain | string | `"Mainnet"` / `"Testnet"` |
| agentAddress | address | 伺服器生成的 agent key 對應地址（exchange.py:636-642：SDK 用 `secrets.token_hex(32)` 生 key） |
| agentName | string | 可空字串；HL 文件：可附 `valid_until {timestamp}` 後綴設過期（最長 180 天） |
| nonce | uint64 | unix 毫秒 timestamp（exchange.py:638,644） |

**ApproveBuilderFee**（signing.py:431-436；primaryType
`"HyperliquidTransaction:ApproveBuilderFee"`）：

| 欄位 | type | 說明 |
|---|---|---|
| hyperliquidChain | string | 同上 |
| maxFeeRate | string | 例 `"0.1%"`（百分比字串，exchange.py:662） |
| builder | address | builder 地址 |
| nonce | uint64 | unix 毫秒 timestamp |

message = 上述欄位 + `type` + `signatureChainId`（signing.py:236 直接把整個 action dict
當 message；EIP-712 encoding 只取 types 裡宣告的欄位，多餘欄位不進 hash——實測通過，見 §7）。

### 2. 建 typed data 與簽名是否分離？

**分離。** `user_signed_payload()`（建，無 key）與 `sign_inner()`（簽，signing.py:452-455）
是兩個獨立函式。唯一小坑：`sign_user_signed_action` 會**就地 mutate** action
（塞 signatureChainId/hyperliquidChain，signing.py:250-251）——我們自建流程時，
這兩個欄位由伺服器在建 typed data 前先塞進 action dict 即可，不需要那個 mutation。
不必手工實作 EIP-712 encoding，零手工 hash 風險。

### 3. 簽好的 r/s/v 怎麼組進 POST payload？

`Exchange._post_action()`（exchange.py:101-110）：

```
POST {base_url}/exchange
{
  "action":       <上面的 action dict，含 type/hyperliquidChain/signatureChainId/欄位/nonce>,
  "nonce":        <必須等於 action["nonce"]>,
  "signature":    {"r": "0x…", "s": "0x…", "v": 27|28},
  "vaultAddress": null,
  "expiresAfter": null   // user-signed action 不支援 expiresAfter（exchange.py:134-136 註解）
}
```

- signature 格式：`{"r": to_hex(r), "s": to_hex(s), "v": int}`（signing.py:455）。
- **nonce 三位一體**：typed data message 的 nonce、action 的 nonce、payload 頂層 nonce
  是同一個毫秒 timestamp（exchange.py:638,644,654）。
- nonce 窗口（HL 官方文件 nonces-and-api-wallets）：須落在 **(T-2 天, T+1 天)**，
  且大於該 signer 已用的 100 個最高 nonce 中最小者。user-signed action 的 nonce
  記在**使用者主地址**名下。窗口很寬——伺服器建 typed data 時取 now_ms 當 nonce，
  使用者幾分鐘後才簽完全沒問題。
- SDK 細節（exchange.py:647-648）：`approve_agent` 未給 name 時，簽名 message 含
  `agentName: ""` 但 POST 的 action **刪掉** agentName 欄位。我們一律給名字（M1 慣例
  `spark.onboarding` 用 agent_name），可避開這個特例。

### 4. MetaMask `eth_signTypedData_v4` 與 eth_account 簽名 bit-compatible 嗎？

**相容。** `sign_inner` 用 `eth_account.messages.encode_typed_data`（signing.py:8,453），
即標準 EIP-712 v4 語意；本 typed data 只有平面欄位（無陣列/巢狀 struct），無已知
編碼歧異。HL 官方文件（exchange-endpoint）明說 user-signed action「compatible with
MetaMask's eth_signTypedData_v4」。MetaMask 回傳 65-byte 簽名 hex，前端拆
r=前32B、s=次32B、v=末 byte（27/28），與 SDK 的 `{r,s,v}` 格式一致（v 若拿到 0/1 要 +27）。

**已知坑（最重要的一個）**：MetaMask 強制 **domain.chainId 必須等於錢包當下 active
chain**，否則丟 "Provided chainId must match the active chainId"（MetaMask
extension issue #11296、#18276）。而 SDK 硬編 `signatureChainId = "0x66eee"`
（Arbitrum Sepolia，signing.py:250）——瀏覽器錢包多半沒加這條鏈。解法：SDK 註解
（signing.py:248-249）明說「signatureChainId is the chain used by the wallet to sign
**and can be any chain**；hyperliquidChain 才決定環境與防重放」，HL 官方文件範例也用
`0xa4b1`（Arbitrum One）。所以：**前端把錢包當下 chainId 傳給伺服器，伺服器把它
（hex 字串）塞進 action.signatureChainId 再建 typed data**；domain.chainId 由它推導
（signing.py:218），HL 後端 recover 時同樣從 action 取，永遠自洽。
反面證據：Chainstack 文件宣稱「必須用 0x66eee、絕不可用 42161」——與 SDK 原始碼註解
及 HL 官方文件的 `0xa4b1` 範例直接矛盾，且實測 `0xa4b1` 的 recover round-trip 通過
（§7）；判定 Chainstack 說法有誤（它描述的是 SDK 預設值，不是協議要求）。惟此點
建議在 testnet E2E 時用真 MetaMask 簽一次做最終確認（風險低但代價小）。

### 5. Testnet vs Mainnet 差異

| | Testnet | Mainnet |
|---|---|---|
| action.hyperliquidChain | `"Testnet"` | `"Mainnet"`（signing.py:251；跨環境重放防護） |
| API base URL | `https://api.hyperliquid-testnet.xyz` | `https://api.hyperliquid.xyz`（constants.py:1-2） |
| domain | 不變（chainId 只跟 signatureChainId 走，與環境無關） | 同左 |

### 6. 最小實作面（伺服器端）

全部可站在 SDK 現成函式上，自建 code 極少：

```python
# spark/onboarding_external.py（建議新檔）
from hyperliquid.utils.signing import user_signed_payload, recover_user_from_user_signed_action

APPROVE_AGENT_SIGN_TYPES = [...]        # 抄 signing.py:416-421 的 4 欄
APPROVE_BUILDER_FEE_SIGN_TYPES = [...] # 抄 signing.py:431-436 的 4 欄

def build_approve_agent_typed_data(
    *, agent_address: str, agent_name: str, wallet_chain_id: int,
    is_mainnet: bool, nonce_ms: int | None = None,
) -> tuple[dict, dict]:
    """回 (typed_data 給前端 eth_signTypedData_v4, action 存伺服器待提交)。無私鑰。"""
    action = {
        "type": "approveAgent",
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "signatureChainId": hex(wallet_chain_id),
        "agentAddress": agent_address,
        "agentName": agent_name,
        "nonce": nonce_ms or now_ms(),
    }
    return user_signed_payload("HyperliquidTransaction:ApproveAgent",
                               APPROVE_AGENT_SIGN_TYPES, action), action

def build_approve_builder_fee_typed_data(...)  # 同型，欄位換 maxFeeRate/builder

def submit_signed_action(action: dict, signature_hex: str, *, base_url: str,
                         expected_user: str) -> dict:
    """拆 65B 簽名成 r/s/v（v<27 則 +27）→ recover_user_from_user_signed_action
    預驗 == expected_user（防前端拿錯帳號簽）→ POST /exchange
    {"action": action, "nonce": action["nonce"], "signature": sig,
     "vaultAddress": None, "expiresAfter": None}。"""
```

另外：agent key 由伺服器生成（`secrets.token_hex(32)`，仿 exchange.py:636-637），
key 留伺服器、只把 address 放進 typed data——非託管不變量不破壞（agent 不能
withdraw/transfer，且協議拒絕 agent 代簽 approve 類 action，見
src/spark/exchange/hyperliquid.py:249 註解）。提交仍應走 spark 的 resilience boundary
（src/spark/resilience.py）分類：非冪等寫入（approveAgent 是 rotation）、semantic
失敗不重試、transient 失敗以「重新 recover + 重送同一 payload」處理（同 payload
同 nonce 重送是安全的——HL 以 nonce 去重，重複提交會被當 duplicate 拒絕而非重複生效）。

## 7. 執行證據（本機實測，無網路、無真 key）

腳本：無私鑰呼叫 `user_signed_payload()` 建 ApproveAgent/ApproveBuilderFee typed data
（signatureChainId=`0xa4b1`）→ `eth_account` 隨機新 key 模擬瀏覽器錢包簽名 →
`recover_user_from_user_signed_action()` 還原地址。輸出：

```
domain: {'name': 'HyperliquidSignTransaction', 'version': '1', 'chainId': 42161,
         'verifyingContract': '0x0000000000000000000000000000000000000000'}
v = 28
recovered ok: True 0x84D680354F0733D978a497aB93Dc61B1Af129e1a
builder-fee recovered ok: True
```

（附帶驗證兩點：domain.chainId 確實由 signatureChainId 推導；message 含 `type` 等
非 types 宣告欄位不影響 hash——recover 在 action 含 `type` 欄位下仍成功。）

## 8. 風險清單（按嚴重度）

1. **chainId 不匹配**（高，設計期就要吃下）：MetaMask 拒簽 domain.chainId ≠ active chain
   的請求 → signatureChainId 必須動態取自前端錢包，API 設計要有這個參數。
2. **approveAgent rotation 語意**（高，M1 已知）：同名/unnamed agent 會被新 approve
   踢掉並 prune nonce state；HL 文件警告 prune 後舊簽名可重放，**每次 onboard 都生成
   全新 agent key，絕不重用 agent address**（src/spark/onboarding.py:40 已有此紅線）。
3. **簽名在 nonce 窗口內可重放**（中）：簽好未提交的 approve 在 (T-2d, T+1d) 內任何人
   拿到都能提交。緩解：伺服器拿到簽名立即提交、不落地存簽名；action 本身內容
   （agentAddress/builder/maxFeeRate）是我們指定的，重放最多是重複執行同一授權。
4. **maxFeeRate 字串格式**（低）：必須是含 `%` 的字串（如 `"0.1%"`），且日後下單的
   f 不得超過此上限（src/spark/config.py:30 已有不變量）。
5. **v 值正規化**（低）：部分錢包/庫回 v=0/1，提交前正規化為 27/28。
6. **SDK 版本漂移**（低）：本研究釘在 0.24.0 的行號；升級 SDK 時 re-verify
   `user_signed_payload`/sign types 未變（建議加一個 pin 測試斷言 sign types 常數）。

## 來源

- SDK 原始碼（最高優先）：`.venv/lib/python3.14/site-packages/hyperliquid/utils/signing.py`
  :217-237, 247-253, 412-439, 452-455, 467-472；`hyperliquid/exchange.py`:101-110,
  134-138, 635-664；`hyperliquid/utils/constants.py`:1-2。
- HL 官方文件：exchange-endpoint（approveAgent/approveBuilderFee payload、
  eth_signTypedData_v4 相容、signatureChainId 例 0xa4b1）、nonces-and-api-wallets
  （nonce 窗口 (T-2d,T+1d)、100-nonce set、agent prune/重放警告）：
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets
- MetaMask chainId 強制檢查：https://github.com/MetaMask/metamask-extension/issues/11296
  https://github.com/MetaMask/metamask-extension/issues/18276
- 反面來源（判定有誤）：https://docs.chainstack.com/docs/hyperliquid-signing-overview
  （宣稱必須 0x66eee；與 SDK 註解、HL 官方範例、實測矛盾）。
- spark 現況：src/spark/exchange/hyperliquid.py:249-257、src/spark/onboarding.py:40-73。
