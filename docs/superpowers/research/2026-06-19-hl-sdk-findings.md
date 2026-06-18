# Hyperliquid SDK + Builder Fills Research Findings

**Date:** 2026-06-19  
**Branch:** `feat/builder-code-verification`  
**Researcher:** Task 0 research spike

---

## 1. SDK Version

```
hyperliquid-python-sdk == 0.24.0
```

Installed ephemeral path (uv cache):
```
/Users/jim/.cache/uv/archive-v0/bHKHCK9RzE-oohxH/lib/python3.9/site-packages/hyperliquid/
```

Python runtime used by uv: 3.9 (system). Target runtime for our project is 3.11 — no compatibility issues expected.

---

## 2. Exchange Write Methods — Exact Signatures

All output below is from a live `inspect.signature()` run against the installed SDK.

### 2.1 `Exchange.approve_builder_fee`

```python
(self, builder: str, max_fee_rate: str) -> Any
```

Source (`exchange.py` lines 659–664):
```python
def approve_builder_fee(self, builder: str, max_fee_rate: str) -> Any:
    timestamp = get_timestamp_ms()
    action = {"maxFeeRate": max_fee_rate, "builder": builder, "nonce": timestamp, "type": "approveBuilderFee"}
    signature = sign_approve_builder_fee(self.wallet, action, self.base_url == MAINNET_API_URL)
    return self._post_action(action, signature, timestamp)
```

- `builder`: checksummed or lowercase hex address string (e.g. `"0x8c967E73E7B15087c42A10D344cFf4c96D877f1D"`)
- `max_fee_rate`: string like `"0.001%"` (i.e. 1 basis point = 0.01%, typical: `"0.001%"` to `"0.1%"`)
- Returns: raw API response dict (typically `{"status": "ok"}`)
- **Must be signed by the main wallet, NOT an agent.**

### 2.2 `Exchange.approve_agent`

```python
(self, name: Optional[str] = None) -> Tuple[Any, str]
```

Source (`exchange.py` lines 635–657):
```python
def approve_agent(self, name: Optional[str] = None) -> Tuple[Any, str]:
    agent_key = "0x" + secrets.token_hex(32)
    account = eth_account.Account.from_key(agent_key)
    timestamp = get_timestamp_ms()
    is_mainnet = self.base_url == MAINNET_API_URL
    action = {
        "type": "approveAgent",
        "agentAddress": account.address,
        "agentName": name or "",
        "nonce": timestamp,
    }
    signature = sign_agent(self.wallet, action, is_mainnet)
    if name is None:
        del action["agentName"]

    return (
        self._post_action(action, signature, timestamp),
        agent_key,
    )
```

- Generates a fresh ephemeral private key internally via `secrets.token_hex(32)`.
- Returns `Tuple[Any, str]` where:
  - `[0]`: raw API response (typically `{"status": "ok"}`)
  - `[1]`: the new agent private key as a `"0x..."` hex string
- Caller must persist `agent_key` to construct an agent-signed `Exchange` later.

### 2.3 `Exchange.order`

```python
(self, name: str, is_buy: bool, sz: float, limit_px: float,
 order_type: hyperliquid.utils.signing.OrderType, reduce_only: bool = False,
 cloid: Optional[hyperliquid.utils.types.Cloid] = None,
 builder: Optional[hyperliquid.utils.types.BuilderInfo] = None) -> Any
```

**`OrderType` definition** (`signing.py` line 18):
```python
Tif = Union[Literal["Alo"], Literal["Ioc"], Literal["Gtc"]]
LimitOrderType = TypedDict("LimitOrderType", {"tif": Tif})
OrderType = TypedDict("OrderType", {"limit": LimitOrderType, "trigger": TriggerOrderType}, total=False)
```

Valid tif values: `"Alo"`, `"Ioc"`, `"Gtc"`.

**IoC limit order** (used for market-equivalent fills):
```python
order_type = {"limit": {"tif": "Ioc"}}
```

**`BuilderInfo` definition** (`types.py` line 185):
```python
# b is the public address of the builder, f is the amount of the fee in tenths of basis points.
# e.g. 10 means 1 basis point
BuilderInfo = TypedDict("BuilderInfo", {"b": str, "f": int})
```

**Full call example** (from `market_open` internals, line 258–260):
```python
exchange.order(
    "ETH", is_buy=True, sz=0.01, limit_px=3500.0,
    order_type={"limit": {"tif": "Ioc"}},
    reduce_only=False,
    builder={"b": "0x8c967e73e7b15087c42a10d344cff4c96d877f1d", "f": 1}
)
```

`bulk_orders` (called internally by `order`) automatically lowercases `builder["b"]`:
```python
if builder:
    builder["b"] = builder["b"].lower()
```

---

## 3. Info Read Methods

### 3.1 Full list of `Info` methods (live output)

```
['all_mids', 'candles_snapshot', 'delegator_history', 'disconnect_websocket',
 'extra_agents', 'frontend_open_orders', 'funding_history', 'historical_orders',
 'l2_snapshot', 'meta', 'meta_and_asset_ctxs', 'name_to_asset', 'open_orders',
 'perp_dexs', 'portfolio', 'post', 'query_order_by_cloid', 'query_order_by_oid',
 'query_perp_deploy_auction_status', 'query_referral_state',
 'query_spot_deploy_auction_status', 'query_sub_accounts',
 'query_user_abstraction_state', 'query_user_dex_abstraction_state',
 'query_user_to_multi_sig_signers', 'set_perp_meta', 'spot_meta',
 'spot_meta_and_asset_ctxs', 'spot_user_state', 'subscribe', 'unsubscribe',
 'user_fees', 'user_fills', 'user_fills_by_time', 'user_funding_history',
 'user_non_funding_ledger_updates', 'user_rate_limit', 'user_role',
 'user_staking_delegations', 'user_staking_rewards', 'user_staking_summary',
 'user_state', 'user_twap_slice_fills', 'user_vault_equities']
```

**No `max_builder_fee`, `query_builder_accrued`, or `builder_state` method exists in the SDK.**

### 3.2 Account value — `info.user_state(address)`

Documented in `info.py` (lines 86–128). Returns structure including:
```
{
  "crossMarginSummary": {
    "accountValue": "1234.56",   ← this is the account value string
    ...
  },
  "assetPositions": [...],
  "withdrawable": "...",
  ...
}
```

Access pattern: `float(info.user_state(addr)["crossMarginSummary"]["accountValue"])`

### 3.3 `max_builder_fee` — Raw POST (not in SDK)

The SDK has **no wrapper method** for this. Must call raw:

```python
info.post("/info", {"type": "maxBuilderFee", "user": user_addr, "builder": builder_addr})
```

**Live test result** (testnet + mainnet, address `0x000...001` / `0x000...002`):
```
HTTP 200
Response: 0      (integer, not a string)
Type: <class 'int'>
```

Returns an `int` in tenths of basis points (0 = no approval, 10 = 1 bp approved).  
The official docs confirm: "maximum fee approved in tenths of a basis point i.e. 1 means 0.001%"

### 3.4 Accrued builder fees — `info.query_referral_state(builder_addr)`

```python
info.query_referral_state(builder_addr)
# → POST /info {"type": "referral", "user": builder_addr}
```

**Live response structure** (testnet):
```json
{
  "referredBy": null,
  "cumVlm": "0.0",
  "unclaimedRewards": "3.42474599",
  "claimedRewards": "0.0",
  "builderRewards": "3.42474599",
  "referrerState": {"stage": "needToTrade", "data": {"required": "10000.0"}},
  "rewardHistory": [],
  "tokenToState": [
    [0, {
      "cumVlm": "0.0",
      "unclaimedRewards": "3.42474599",
      "claimedRewards": "0.0",
      "builderRewards": "3.42474599"
    }]
  ]
}
```

`builderRewards` is the **cumulative accrued builder fee** (float string, denominated in USDC).  
No `builderState`, `builderFeeAccrued`, or `accruedBuilderFee` endpoint exists — other guesses all returned HTTP 422.

---

## 4. `builder_fills` CSV

### 4.1 URL Pattern

```
https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{builder_address}/{YYYYMMDD}.csv.lz4
https://stats-data.hyperliquid.xyz/Testnet/builder_fills/{builder_address}/{YYYYMMDD}.csv.lz4
```

**Critical:** URLs are case-sensitive; `builder_address` must be fully lowercase.  
Files are LZ4-frame compressed (use `lz4.frame.decompress()`).

### 4.2 CSV Column Names — DEFERRED, NOT CONFIRMED

**All download attempts returned HTTP 403 (S3 AccessDenied from CloudFront).**

Addresses tried:
- `0x0de9675e4a05e2d0d6213e3c30b7b77e19e3b26d`
- `0x8c967e73e7b15087c42a10d344cff4c96d877f1d` (SDK example)
- `0x1234567890123456789012345678901234567890` (fake, also 403)

All requests — both Mainnet and Testnet paths, both yesterday and two days ago — returned:
```xml
<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>
```

This is an S3 bucket behind CloudFront that appears to restrict public access. The fake address also returns 403 (not 404), so it is not possible to distinguish "no data" from "forbidden" without valid credentials or the correct builder address with an actual signed request mechanism.

**Hypothesis for 403:** The bucket may require AWS Signature V4 authentication or may only be accessible from a specific origin/referer. The Hyperliquid official frontend likely fetches these files server-side with signed credentials. There is no public documentation of an auth mechanism.

**Testnet path availability:** The Testnet URL pattern (`/Testnet/builder_fills/...`) exists as an endpoint but returns the same 403 — it is **not confirmed to serve data** without auth.

**Expected columns (from SDK `Fill` TypedDict + API docs, not confirmed from actual file):**

The SDK's `Fill` TypedDict (`types.py` lines 132–150) defines:
```python
Fill = TypedDict("Fill", {
    "coin": str,
    "px": str,
    "sz": str,
    "side": Side,     # "A" (ask/sell) or "B" (bid/buy)
    "time": int,
    "startPosition": str,
    "dir": str,
    "closedPnl": str,
    "hash": str,
    "oid": int,
    "crossed": bool,
    "fee": str,
    "tid": int,
    "feeToken": str,
})
```

The `userFills` info endpoint response also includes an optional `builderFee` field (per API docs: "optional and will not be present if 0"). The CSV file likely contains a subset of these fields, with `builderFee` included since the file is builder-specific.

**Action required:** Actual CSV headers must be confirmed on the first real testnet run when we have our own builder address with fills.

---

## 5. Deltas vs Plan

This section explicitly lists every assumption from the plan and whether it holds.

### 5.1 `info.max_builder_fee(user, builder)` returns int

**DELTA — PLAN WRONG ON METHOD NAME.**

There is no `max_builder_fee` method on `Info`. Must call raw:
```python
result = info.post("/info", {"type": "maxBuilderFee", "user": user, "builder": builder})
```
Return value IS an `int` (confirmed: `type <class 'int'>`, value `0` for no approval).  
Plan's assumption about return type (`int`) is correct. Method name is wrong.

### 5.2 `info.query_builder_accrued(builder)` exists

**DELTA — PLAN WRONG. Method does not exist.**

No such method. The real approach:
```python
state = info.query_referral_state(builder_addr)
accrued_usd = float(state["builderRewards"])
```
`builderRewards` is a float string in the referral response. This IS accessible via an SDK method (`query_referral_state`), just not via a purpose-built builder method.

### 5.3 `exchange.order(..., {"limit": {"tif": "Ioc"}}, ..., builder={"b":..., "f":...})`

**CONFIRMED — plan is correct.**

Exact signature matches. `order_type={"limit": {"tif": "Ioc"}}` is valid. `builder={"b": addr, "f": fee_int}` is valid. The `f` value is `int` in tenths of basis points (1 = 0.1 bp = 0.001%).

### 5.4 `exchange.approve_builder_fee(builder, max_fee_rate_str)` signature

**CONFIRMED — plan is correct.**

Exact signature: `(self, builder: str, max_fee_rate: str)`.  
`max_fee_rate` is a string like `"0.001%"` — not a numeric rate.

### 5.5 `exchange.approve_agent()` returns `(result, agent_key)`

**CONFIRMED — plan is correct.**

Returns `Tuple[Any, str]`. `[0]` is the API response, `[1]` is the raw private key string `"0x..."`.  
The key is generated freshly inside the method using `secrets.token_hex(32)`.

### 5.6 CSV columns: `time, coin, side, px, sz, builderFee`

**DEFERRED — NOT CONFIRMED (403 on all download attempts).**

Cannot verify. Based on SDK `Fill` TypedDict and API docs, the columns most likely include `time`, `coin`, `side`, `px`, `sz`, and `builderFee` (optional field). Exact CSV headers and column order unknown until we have a real builder address with fills and working access.

The `builderFee` field is documented as optional in `userFills` API response (absent if 0). In a builder-specific CSV it would presumably always be present.

---

## 6. Additional Findings (Not in Plan)

### 6.1 `builder["b"]` is auto-lowercased

`bulk_orders` (called by `order`) does `builder["b"] = builder["b"].lower()` before signing. Builder address can be passed in any case.

### 6.2 `approve_builder_fee` must use main wallet

The action uses `sign_approve_builder_fee` (not `sign_l1_action`), meaning it requires an EIP-712 user-signed action. It cannot be sent by an agent wallet.

### 6.3 `approve_agent` generates the key internally

You cannot supply your own agent key. The SDK always generates a fresh one via `secrets.token_hex(32)`. To re-use an agent across test runs, persist the returned `agent_key` string and reconstruct with `eth_account.Account.from_key(agent_key)`.

### 6.4 `Exchange.__init__` takes `account_address` parameter

For agent wallets acting on behalf of a main wallet, pass `account_address=main_wallet.address` to `Exchange`. This affects how `user_state` queries are addressed.

### 6.5 `maxBuilderFee` returns `0` for unknown/unapproved pairs

Confirmed: returns integer `0` (not null, not error) when no approval exists. Safe to compare with `>= required_fee`.

### 6.6 S3 builder_fills access

The `stats-data.hyperliquid.xyz` bucket is not publicly readable. Access requires either:
- AWS Signature V4 credentials (not documented publicly), or
- The Hyperliquid frontend serves these via a signed proxy.

For our project, we need to either (a) obtain signed access credentials from Hyperliquid, or (b) rely on the `query_referral_state` endpoint for programmatic fee verification rather than parsing CSV files.

**Testnet path structure exists** but returns the same 403 — cannot confirm whether Testnet data is populated at all.
