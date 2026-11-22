# leader 權益基準對帳（2026-11-22）

配套 plan：`docs/superpowers/plans/2026-11-22-leader-equity-basis.md`。
全部是 Hyperliquid 主網公開 `info` 端點的唯讀查詢；原始快照在 repo 外 `~/filet-dev/leader-basis/snaps.jsonl`。

## 1. 事故（2026-11-22 13:48–13:51 UTC）

leader `0xfb9c…9760`（`userAbstraction`＝`unifiedAccount`）從空倉開空 BTC：13:48:51 成交 0.015、13:49:43 再 0.005，之後沒有減倉、沒有任何入金（`userNonFundingLedgerUpdates` 三小時內為空）。

| 時間 | leader 倉位 | leader perp `accountValue`（引擎的分母） | `f438b3…` 實際動作 | 隱含倍率 |
|---|---|---|---|---|
| 13:48:01 | 0（掛單 0.015） | ≈ 22（由倍率反推） | 掛限價賣 1.61381 | 107.6 |
| 13:49:03 | −0.015 | ≈ 21（反推） | 市價開空 1.6788（$101,665） | 111.9 |
| 13:50:03 | −0.02 | ≈ 96（反推） | 市價平 1.18745 | 24.6 |
| 13:51:04 | −0.02 | ≈ 120 | 市價平 0.09801 → 剩 −0.39334 | 19.7 |

`f438b3…`：帳戶權益約 $4,744、資金使用率 0.5（客戶簽章值）。三筆成交名目 $179,541，手續費 $116.70（其中 builder fee $35.91），已實現 −$30.90，合計 **−$147.61**。
`f6b3e7…`（權益約 $262、使用率 0.4）：合計 −$6.21。
同期 leader 交易所顯示的帳戶總值約 $708，倉位名目 $1,215（1.7 倍）。

根因：`loop.py:333` 以 `leader_state.account_value`（`clearinghouseState.marginSummary.accountValue`，預設 dex）當 `compute_scale_factor` 的分母。
統一帳戶的資金在現貨帳本，perp `accountValue` 空倉時為 0，有掛單／有倉位時只反映被劃過去的保證金（實測先約 2.5% 名目、後約 10% 名目）。
leader 的 perp `accountValue` 歷史（`portfolio.perpMonth`）：11-18～11-21 01:30 為 $34–54，11-21 05:50 起空倉時為 0。

## 2. 交易所「帳戶總值」的組成（12 個帳戶，四種模式）

`portfolio` 端點 `day` 視窗 `accountValueHistory` 的最後一點（實測時間戳距查詢時刻 ≤ 0.1 秒，四個視窗的最後一點相同）＝交易所顯示的帳戶總值 `T`。

令 `S`＝現貨所有代幣以 mark 計價的總和＋質押 HYPE（`delegatorSummary.delegated`×HYPE mark）＋vault 權益；`A`＝所有 perp dex 的 `accountValue` 總和。

| 模式 | 位址 | `T` | `S − T` | `S + A − T` |
|---|---|---|---|---|
| unifiedAccount | 0xfb9c…（leader） | 708.31 | 0.00 | — |
| unifiedAccount | 0xf97a…（Filet Alpha） | 928,634 | 0.00（補質押 101.06 HYPE 後；未補為 −6,306.38，連四次取樣不變） | — |
| unifiedAccount | 0x999678 | 13,496,648 | 0.00 | +10,096,641 |
| unifiedAccount | 0xfeec88 | 340,841 | 0.00 | +218,912 |
| unifiedAccount | 0xe06650 | 16,643 | 0.00 | +16,524 |
| portfolioMargin | 0xfce053 | 5,074,266 | 0.00 | +808,722 |
| portfolioMargin | 0xb0fbfe | 460,609 | 0.00 | +354,422 |
| portfolioMargin | 0x355ebd | 117,103 | 0.00 | 0.00（無 perp） |
| disabled | 0x320c89 | 31,548,362 | −217,086 | 0.00 |
| disabled | 0x732f71 | 194,400 | −187,872 | −0.10 |
| disabled | 0xe8112d | 272.12 | −271.79 | 0.00 |
| default | 0x659ddd／0xdcda24／0xbfce14 | 53.9M／5.06M／1.69M | 0.00 | 0.00（三個都沒有 perp，無法區分） |

結論：
- **unifiedAccount／portfolioMargin**：`T = S`。perp 的未實現損益已經記在現貨 USDC 的 `total` 裡（leader 三次取樣 `USDC total − upnl` 都是 214.79），perp `accountValue` 不得再加。
- **disabled（傳統模式）**：`T = S + A`。
- `T` 在每一種模式都等於交易所自己的總值，且不需要自己替代幣計價。
- 官方文件（Account abstraction modes）對統一帳戶與 portfolio margin 的說法：「Individual perp dex user states are not meaningful」、現貨餘額端點是「the source of truth for trading account balance across spot and perps」。文件沒有給總權益公式，以上恆等式是實測。

## 3. 探索池的帳戶模式分布（`2026-10-28-explore-300-pool.json` 前 60 個位址）

| 模式 | 個數 |
|---|---|
| unifiedAccount | 39 |
| portfolioMargin | 10 |
| default | 8 |
| disabled | 3 |

現行分母（perp `accountValue`）只對 `disabled` 有意義；60 個裡有 49 個（82%）是它不適用的模式。

## 4. 候選基準在 leader `0xfb9c…` 上的數字（13:57:10 UTC 快照）

現貨 USDC `total` 209.48（`hold` 121.57）、UBTC 0.0081968513（$497.89）；perp `accountValue` 120.58、名目 1,215.66、未實現 −5.31；`T` 707.37。

| 基準 | 值 | leader 名目÷基準 | `f438b3…`（權益 4,704、使用率 0.5）的目標倉位 |
|---|---|---|---|
| 現行：perp `accountValue` | 120.58 | 10.08 倍 | 0.393 BTC（$23.9k） |
| 只算可當保證金的 USDC | 209.48 | 5.80 倍 | 0.225 BTC（$13.6k） |
| 交易所帳戶總值 `T` | 707.37 | 1.72 倍 | 0.0665 BTC（$4.0k） |

## 5. 資料極限

- `default` 模式的三個樣本都沒有 perp，恆等式無法區分它屬於哪一類；`T` 本身與模式無關，不受影響。
- 「`portfolio` 最後一點是即時值」是 14 個帳戶的實測，官方文件未說明；plan 以時間戳新鮮度檢查防守。
- 沒有取樣 vault 位址與子帳戶；部署前的探針要補（plan T3）。
- 統一帳戶下 perp `accountValue`／現貨 `hold` 佔名目的比例（2.5%→10%）成因沒有查，修法不依賴它。
