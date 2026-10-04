# Dashboard 淨 PnL：移除 builder fee 重複扣除

日期：2026-10-04。基底 commit：`b9733c0`。範圍**只有**這一件；儀表板 30D／回撤口徑、走勢線標示、探索表跑版皆不在本 plan（前者已由 `2026-10-04-dashboard-hide-twr-and-explore-subgrid.md` 處理並 commit，本 plan 不碰那份 plan 動到的 `EquityCard.tsx`／`PnlCard.tsx`／`globals.css`）。

## 根因

`src/spark/publicapi/app.py::_dashboard_pnl_and_return`（1261–1318 行）：

```python
cum_pnl = perf["cum_pnl"]            # HL portfolio perpMonth pnlHistory 末值 − 首值
fees_paid = summary.builder_fee      # 同窗口 fills 的 builderFee 加總
net = cum_pnl - fees_paid            # ← 1306 行，重複扣除
denom = abs(net + fees_paid)         # 1307 行，== |cum_pnl|
fee_share = fees_paid / denom * 100
```

HL `pnlHistory` 是帳戶淨值扣掉出入金後的變化量，手續費（含 builder fee）本來就已經從淨值扣掉，因此 `cum_pnl` 已是淨費後損益。再減一次 `fees_paid` 把 builder fee 算了兩次。

獨立對帳證據（follower 0x438b…ce9，2026-10-04 主線程親跑 HL info API）：
- `userNonFundingLedgerUpdates`：deposit 15,727.117 + 47,100.816 + 55.387 ＝ **62,883.32**（另一筆 `send` 是 `sourceDex: spot` → perp 的同戶內轉，不是外流）。
- `clearinghouseState.marginSummary.accountValue` ＝ 56,279.12（抓取當下）。
- 56,279.12 − 62,883.32 ＝ **−6,604.20**；HL `pnlHistory` 同窗 `cum_pnl` ＝ **−6,604.18**。兩者來源獨立，相差 0.02 是取樣時刻差。
- 儀表板顯示 −7,473.16 ＝ −6,604.18 − 869（builder fee），多扣了一次。
- repo 自己的文案早已這樣定義：`web/src/lib/copy.ts:1902` `pnlSourceNote: "損益含未實現損益、資金費率與手續費，已排除出入金（HL pnlHistory）"`。

## 裁決（2026-10-04 使用者確認）

| 欄位 | 現行 | 改後 |
|---|---|---|
| `pnl.net` | `cum_pnl − fees_paid` | **`cum_pnl`**（HL 損益本身，已含全部手續費） |
| `pnl.fees_paid` | 同窗 builder fee | 不變（仍回傳，供對照） |
| `pnl.fee_share_of_pnl_pct` | `fees_paid / \|cum_pnl\|`（分母是淨費後損益，比例虛高） | **`fees_paid / \|cum_pnl + fees_paid\|`**（分母＝扣 builder fee 前損益）；分母 0 → `None` |
| 標題 ZH | 淨 PnL（已扣 builder fee） | **淨 PnL（已含手續費）** |
| 標題 EN | Net PnL (after builder fee) | **Net PnL (fees included)** |

`fee_share_of_pnl_pct` 目前沒有任何前端元件渲染（只出現在 `web/src/lib/api.ts` 型別與測試 fixture），本次只修後端公式、不新增 UI。

明確不動：
- `app.py:1180 _pnl_share_pct`（ops／billing 用；基礎是 fills `closedPnl` 而非 `pnlHistory`，其 W5 TODO「closedPnl 是否已淨手續費」是另一個未對帳假設，本次證據不涵蓋）。
- `EquityCard.tsx`／`PnlCard.tsx`／`globals.css`／`leader_perf.py`。
- `docs/superpowers/specs/*.html` 設計稿裡的舊標題字樣（歷史文件）。

## Tasks

### Task 1 @inline（builder）— 後端公式 + 測試

檔案：`src/spark/publicapi/app.py`（只動 `_dashboard_pnl_and_return`）、`tests/test_me_dashboard.py`、`tests/test_dashboard_pnl_realized.py`（只改一行註解）。

1. **先寫失敗測試**（TDD；fixture helper `portfolio_rows`／`fill`／`clearinghouse`／`_logged_in` 沿用 `tests/test_me_dashboard.py` 既有的）：
   - 新增 `test_net_pnl_is_hl_pnl_not_minus_builder_fee`：portfolio `[(0, "1000", "0"), (10, "990", "-10")]`、fills 一筆 `builder_fee="2"` → `body["pnl"]["net"] == "-10"`（不是 `-12`）、`body["pnl"]["fees_paid"] == "2"`、`body["pnl"]["fee_share_of_pnl_pct"] == "25.00"`（2 ÷ |−10 + 2| ＝ 25%）。docstring 寫明 2026-10-04 事故：follower 0x438b…ce9 顯示 −7,473 實為 −6,604。
   - 既有 `test_fee_share_null_when_cum_pnl_is_zero`（234 行）改名 `test_fee_share_null_when_gross_pnl_is_zero`，fixture 改成 portfolio `[(0, "1000", "0"), (10, "998.8", "-1.2")]`、fill `builder_fee="1.2"` → `net == "-1.2"`、`fee_share_of_pnl_pct is None`（分母 |−1.2 + 1.2| ＝ 0）。docstring 一併改寫。
   - 跑 `uv run pytest tests/test_me_dashboard.py -q`：新測試紅、舊測試（改寫後）紅、其餘綠。回報要附這一步的輸出末尾。
2. 改 `_dashboard_pnl_and_return`：1306 行 `net = cum_pnl`；1307 行 `denom = abs(cum_pnl + fees_paid)`。docstring 第二段（⭐ `net`／`fees_paid`／`fee_share_of_pnl_pct` 三者同窗口……`net = cum_pnl − fees_paid`）改寫為：pnlHistory 已含手續費故 `net = cum_pnl`；`fee_share` 分母是扣 builder fee 前損益 `|cum_pnl + fees_paid|`；附一行事故註記（2026-10-04，follower 0x438b…ce9 顯示 −7,473 實為 −6,604，ledger 入金對帳證實 pnlHistory 已淨費）。
3. `tests/test_dashboard_pnl_realized.py:42` 的註解「net 仍照舊算法（同窗口 cum_pnl − fees），不受影響」改成「net ＝ 同窗口 cum_pnl（2026-10-04 起不再減 fees），不受影響」；斷言不變。
4. `tests/test_me_dashboard.py` 檔頭 docstring 第 6 行「`fee_share_of_pnl_pct` 分母（|net+fees_paid|）」改成「分母（|cum_pnl+fees_paid|，扣 builder fee 前損益）」。

驗收（builder 回報須附每條的指令輸出末尾）：
- `uv run pytest tests/test_me_dashboard.py tests/test_dashboard_pnl_realized.py -q` 全綠。
- `uv run ruff check src tests` 無錯。
- `grep -c 'cum_pnl - fees_paid' src/spark/publicapi/app.py` 輸出 `0`。
- `git status --short` 只列出上述三個檔案。

### Task 2 @sdd（impl-worker）— 文案

檔案：`web/src/lib/copy.ts`。
- 1213 行 `label: "淨 PnL（已扣 builder fee）",` → `label: "淨 PnL（已含手續費）",`
- 2932 行 `label: "Net PnL (after builder fee)",` → `label: "Net PnL (fees included)",`
- 其他 key 不動。

驗收：
- `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npx vitest run src/lib/copy.test.ts src/app/dashboard src/components/dashboard` 全綠。
- `grep -c -e '已扣 builder fee' -e 'after builder fee' web/src/lib/copy.ts` 輸出 `0`。

### Task 3 — 審核（reviewer）

輸入：`git diff`、本 plan、Task 1／2 測試輸出。重點：(1) `net`／`fee_share` 公式與裁決表一致；(2) 沒有動到 plan 外檔案；(3) 分母 0 路徑仍為 `None`；(4) 新測試的錨例數字能手算對上。

## 部署注意

後端單檔（`app.py`）＋前端文案一行×2；是否部署、何時部署由使用者決定。operator 可見變化：所有 follower 儀表板的淨 PnL 會**上調**等於該窗 builder fee 的金額；`fee_share_of_pnl_pct` 數值變小（分母變大）。

## 狀態

- [x] Task 1（builder；主線程親跑 27 passed、ruff 乾淨、舊公式 grep 0）
- [x] Task 2（impl-worker；主線程親跑 vitest 90 passed、舊文案 grep 0）
- [x] Task 3（reviewer PASS，零 finding；全套 `uv run pytest` 3508 passed）
- [ ] commit／部署：待使用者決定
