# 主網事故：<$10 殭屍校正（minTradeNtlRejected）與 scale 漂移殘量產生器

日期：2026-07-27 發現、2026-07-28 修復部署。
帳戶：follower `0xbAC652A5Fb611c1BdC3B9D244cc7E0cC03123662`，leader `0xfB9C52f56F03D786AD5D435aa70fe45D80569760`（主網）。
所有數字皆可由 Hyperliquid 公開 `/info` API 重跑驗證（`historicalOrders` / `userFillsByTime` / `clearinghouseState`）。

## 症狀

1. Telegram 每 5 分鐘一則 `ETH 平倉失敗 is_buy=False size=0.0045（第 6 次）`——引擎每輪（60s）送
   同一張單、每輪被拒，耗 HL API 額度。「第 6 次」＝ dedup TTL 300s ÷ interval 60s，精確吻合。
2. 交易所端 `historicalOrders` 實測拒因：**`minTradeNtlRejected`**（$10 最小名目），
   `0.0045 ETH × 1850 ≈ $8.3`。20:33–20:40 連拒 8 輪，之後殘量變 0.0027（$5.0）續拒。
3. 附帶發現：174 筆 `perpMarginRejected`（07-25 起）——leader 網格跑在 ~99% 保證金占用，
   等比鏡像＝零餘裕，任何損耗都撞保證金牆。

## 根因鏈（上游 → 下游）

```
scale = E×u×w / L 每輪重算（loop.py 步驟 4）
  w：波動度權重 [0.3,1.0]，依 leader 當日 |PnL| Z-score —— 實測 10 分鐘內擺盪 12.5%
  L：leader 權益是分母 —— 實測 leader 入金 $46.6→$66.15（+42%）→ scale −30%
        ↓
部位是「舊 scale 的快照」，校正目標用「當前 scale」→ 兩者必然對不齊 → 殘量
        ↓
殘量落在 (size_tolerance 2%, $10 最小名目) 區間 → 每輪送單、每輪被拒、永不收斂
```

兩次事故、同一機制、零參數擬合驗證：

| 事故 | 漂移源 | 模型算出的殘量 | 實測被拒 size |
|---|---|---|---|
| 20:33 起 | w 0.995→0.871 | 0.0361 − 0.0337×(50.2/46.6)×0.871 = **0.0045** | 0.0045 ✓ |
| 23:20 起 | leader 入金後新 scale | 0.0503 − 0.0669×(47.97/66.15) = **0.0018** | 0.0018 ✓ |

交易所規則實測（同日、同 $5 名目、同 reduce-only）：
- 20:41:11 部分減倉 0.0027（部位 0.0361）→ **拒**
- 20:42:10 全平 0.0027（部位恰 0.0027）→ **成交**
⇒ **$10 門檻只對「恰好平掉整個部位」的 reduce-only 豁免**——修法絕不能 gate 全平腿。

程式碼缺口（修復前）：
- `positions.py` 的 min-notional 檢查只 gate 開倉側目標名目；加倉/減倉的 `diff` 兩腿都無下限。
- 名目基準不同源：自家用 mid、交易所用送單價（平倉 = mid×(1−slippage)，差 5%）。
- 拒因被丟棄：`OrderResult.raw` 帶完整錯誤字串，`_try_*` 只讀 `.ok`；
  `_extract_order_error` 已移植、有測試、生產零呼叫端。

## 錢包下限數學（決策依據）

follower 部位名目 = p×E×u×w（L 被約掉，與 leader 資金規模無關；p = leader 部位/其權益）。
引擎最小校正單 = tolerance × 部位名目，須 ≥ $10/(1−slippage)：

```
E ≥ 10 / (tolerance × 0.95 × p × u × w)
```

leader 網格單一格名目實測：BTC ≈ 權益×1.0、ETH ≈ ×1.0、HYPE ≈ ×0.51 ⇒ 最嚴典型態 p≈0.5。
tolerance=0.02 時需 $1,500–2,000；**tolerance=0.08 時降至 ~$600–800**。
（w 長期分布未建模，設計點取 0.5–0.7；此為本文件唯一非實測參數。）

錢包加大的悖論：校正單一旦 >$10 就會**真的成交**——w 的每次 >tolerance 擺動都變成 taker 單。
放寬 tolerance 同時是 churn 的修法，不只是門檻數學。

## 決策（使用者裁決，2026-07-28）

| # | 決策 | 內容 |
|---|---|---|
| 1 | 孤兒成交不修 | 60s 輪詢鏡像 maker 單的固有競態（leader 撤單後鏡像單多活一輪），~$0.06/次，接受 |
| 2 | 錢包 | 先入金至 $100（實測 $99.63 落地），後續再加；live-equity 模式（env 無 ALLOCATED/USE_FULL_EQUITY 覆寫）入金即生效 |
| 3 | env 調整 | `COPY_SIZE_TOLERANCE=0.08`、`COPY_CAPITAL_UTILIZATION=0.9`（後者修 perpMarginRejected：占比問題，加錢不修） |
| 4 | 校正腿 gate **要告警** | gate 省下 HL request，但透過 notifier warn（dedup）保持可見，作為「該不該再加錢」的訊號 |

## 修改內容

`src/spark/copytrade/positions.py`（詳見模組 docstring 偏離清單第 6 點與當日 commit）：
1. **校正腿最小名目 gate**：加倉/減倉的 `diff` 以送單價基準（mid×(1−slippage)，保守低價側）
   過濾，低於 `min_order_notional` → `skipped(reason="min_notional_adjust")` ＋ dedup 告警，不送單。
   flatten 與反轉全平腿**不 gate**（豁免實測），測試釘死。
2. **拒因接線**：`_try_market_open`/`_try_close_reduce_only` 失敗告警附
   `_extract_order_error(order.raw)` 與名目金額。

`deploy/follower.env.example`：補兩個選配參數的文件。

## 部署紀錄（2026-07-27 16:33 UTC ＝ 07-28 00:33 +8）

- fix commit `e39351b`（gate＋拒因接線＋6 測試；全套 1865 passed、ruff clean，
  主對話親跑確認＋fresh-context opus 級審查一輪：1 低嚴重度 finding 已修——
  `_fail_detail` 補認 adapter 自製 `raw={"error":...}` 形狀）。
- 部署程序照 RUNBOOK §3.2：rsync 兩段（uv.lock mtime 2026-07-17 未動 ✓）→
  `uv sync` → `spark import OK` → venv 無 /home 參照 ✓ → root:root 還原 ✓ →
  `DEPLOYED_VERSION commit=3c48e35`。
- env 落地：`COPY_SIZE_TOLERANCE=0.08`、`COPY_CAPITAL_UTILIZATION=0.9`（各恰 1 筆）。
- `systemctl start filet-follower@fbac…3662` → active/running。
- 重啟前快照：follower 權益 $99.63、持 ETH 0.075／BTC 0.00073／HYPE 0.43（停機期間
  殘留掛單被動成交所致）、19 筆舊掛單。預期首輪：HYPE 全平（leader 無此倉，豁免腿）、
  ETH/BTC 向新 scale（≈0.9×99.6/65.9×w≈1.36w）校正、19 筆掛單重掛新 size。
- **重啟後實測（00:34 +8，交易所端）**：首輪三筆 fill 與預期逐項一致——BTC +0.00067、
  ETH +0.0168、HYPE 0.43 reduce-only 全平（豁免腿實戰通過）；部位比精確收斂
  （理論 E×u/L=1.374，實測 BTC 1.373／ETH 1.372）；54 筆訂單事件**零拒單**
  （修復前每分鐘 1 筆 minTradeNtlRejected）；掛單重掛為 21 筆鏡像 leader 21 筆；
  journal 無 WARNING。

## $100 錢包下的預期行為與後續觸發條件

- $100 × 0.9 × w，tolerance 0.08：ETH/BTC 一格倉（名目 ~$90w）的最小校正 ≈ $7.2w×0.95——
  **w<1 時仍會觸發 gate 告警**；HYPE 一格（~$45w）幾乎必觸發。這是預期內行為：
  告警照發（dedup 5 分鐘/coin）但**不再送單耗 API**。
- 告警頻繁出現 ⇒ 依上式把 E 拉向 $600–800（tolerance 0.08 下的安心區）。
- 遺留觀察項（未修）：`COPY_MAX_DRAWDOWN_PCT=0.99`（回撤保護實質關閉，現況沿用）；
  引擎無單一進程鎖；`get_positions` 對缺欄位回應 fail-open（`.get("assetPositions", [])`）；
  costbreaker 不算手續費（其 docstring 宣稱資料不完整與事實不符，`UserFill.fee/.crossed` 齊備）。
