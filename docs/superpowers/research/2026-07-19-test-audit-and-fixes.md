# 2026-07-19 全專案測試稽核與修復（進度文件）

針對 `docs/superpowers/` 下 18 份 specs/plans/research 所定義的設計，執行
「規格 → 程式碼 → 測試」三方一致性稽核與離線測試。**本文件所有「已完成」項目
均由人工（主對話）親自跑指令 / grep 確認，不採信 subagent 的口頭回報**——原因見文末〈流程教訓〉。

## 測試方法

分層測試 + 角色分離（測試 / 修復 / 驗證由不同 agent 擔任，避免自驗盲點）：

- **Layer A 離線基線**：後端 pytest、ruff、前端 vitest、next lint。
- **Layer B 一致性稽核**（唯讀 fresh reviewer，分四域）：Phase1/M1 核心、copytrade 引擎、
  M2 後端（publicapi/keysvc/keystore/filet/billing）、M2 前端。
  每條設計預期逐一追問：*code 真的滿足嗎？有測試真的強制它嗎？*
- **Layer C testnet E2E**：見下方〈testnet 狀態〉。

離線既有測試在開工時已全綠，因此測試價值不在重跑，而在找出
「spec 宣稱了、但 code 或測試沒真的強制」的落差。

## 基線（修復前）

| 項目 | 結果 |
|---|---|
| 後端 `uv run pytest` | 744 passed, 2 deselected |
| 前端 `npm test`（vitest） | 84 passed, 18 files |
| `ruff check` / `next lint` | 全綠 |

## 稽核結論

**無 critical / high 正確性 bug。** 安全關鍵不變量普遍成立且有測試背書：
非託管 ABC（無 withdraw/transfer）、每筆掛單寫入帶 builder（modify 為書面例外）、
kill-switch 順序與 lock-first、flatten 失敗逐幣 critical 不吞、resilience 的
transient/semantic 與冪等分類（verify raise → 視為已送達不重送）、私鑰不入 log/repr/例外、
keystore O_EXCL 不覆寫、SIWE nonce 原子單次（rowcount，非 TOCTOU）、
授權面除 authVerify 外無 r/s/v、billing `sk_test_` 強制與 webhook HMAC 先於 DB、
`event.created` 單調 guard、north-star builder fee 只在 builder 層查一次不跨 follower 加總。

發現皆為 LOW–MEDIUM 的強化 / 測試缺口 / 宣稱落差。

## 已完成並經人工查證（6 項）

### 後端 4 項

| # | 檔案:行號 | 問題 | 修法 | 依據 |
|---|---|---|---|---|
| A1 | `copytrade/notifier.py:173` | 300s dedup 未豁免 critical，可靜音安全告警 | dedup 抑制加 `level != "critical"` 豁免 | spec「critical 永不可靜音」 |
| A2 | `scripts/filet_activate.py:19,36` | 只核對 builder，未核對 `account_id ↔ user_address` 綁定 | 寫入 followers.json 前加結構性核對，不符 fail-fast | 紅線 6（CLI 是 web 被打穿後的 backstop） |
| A3 | `copytrade/config.py:190` | `slippage` 未驗證（餵給 kill-switch 平倉價） | 加範圍守衛 `[0, 1)` | 與其他已驗證欄位一致 |
| F1 | `onboarding.py:60`、`exchange/{base:117,hyperliquid:55,fakes:68}` | agent 授權由本機 Keychain 旗標驅動，非鏈上查詢；drift 時會用過期 key 靜默失敗 | 新增 `query_agent_addresses`，改為鏈上 `extraAgents` 查詢驅動；只有本機 key 確為鏈上當前授權 agent 才跳過，否則重新授權自我修復 | spec §5「query-driven 冪等」 |

**F1 補充**：`approve_agent` 語意經查證為「一律生成新 key 並 rotate」（本身非冪等），
因此 drift 的正確自我修復是重新授權並輪替，而非嘗試授權既有 key。
比較時兩側位址皆 `.lower()` 正規化（同源同基準）。呼叫端簽章由
`skip_agent_approval: bool` 改為 `local_agent_address: str | None`，呼叫端全數更新。

**修復後狀態（人工實跑確認）**：後端 `uv run pytest` → **750 passed, 2 deselected**；
`ruff check src tests scripts` → All checks passed。

### 前端 2 項

| # | 檔案:行號 | 問題 | 修法 | 依據 |
|---|---|---|---|---|
| A5 | `web/src/app/page.tsx:52-58`、`web/src/lib/copy.ts:26` | 登入失敗一律顯示「你取消了簽署」，掩蓋 401 / 網路錯 | 依錯誤類型分流訊息（拒簽 vs 其他） | spec:83「給明確訊息」 |
| A6 | `web/src/lib/redline.test.ts`（新增） | 禁詞測試只掃 `COPY` 物件，寫死中文（Header 分頁標籤、aria-label、layout meta）繞過檢查 | 新增 file-level 全樹禁詞掃描回歸測試，排除 `*.test.ts(x)` 與 `lib/copy.ts` | plan 定案 7「有效覆蓋」 |

**前端狀態（人工實跑確認）**：`npm test` → **86 passed, 19 files**（基線 84/18）；
`next lint` 無警告。現有程式碼實測零禁詞，A6 為防止未來寫死禁詞的把關。

## testnet 狀態

- 舊 pinned 位址（`0x5579…` follower / `0x63e6…` builder，見
  `research/2026-07-16-copytrade-testnet-e2e.md`）經查 testnet 餘額皆為 0，
  且官方 faucet 需瀏覽器連錢包簽章 + 該址須先有主網入金紀錄，無法非互動自動化。
- macOS Keychain 已存有這兩把主鑰（`filet-testnet:main`、`filet-testnet-builder:main`），
  agent key 未生成屬正常——onboarding 會自動補，無需再 bootstrap。
- **但**工作樹中另有一份未追蹤文件 `research/2026-07-19-testnet-e2e-findings.md`，
  記載使用**另一組已注資錢包**（Leader `0xbAC652…3662` / Follower `0xfB9C52…9760`，各 perp 500）
  完成的實際 testnet 跑測與發現。該批工作不在本次稽核範圍內，其與上述 pinned 位址路線的
  關係待確認——**後續 testnet 工作應以該文件為準，而非本文件的 pinned 位址段落。**

## 未處理（已知 / 已延後 / 有 backstop，非行為錯誤）

| 項 | 說明 |
|---|---|
| B1-F2 | builder 參數的 CI 守衛是枚舉式（硬列三個方法）而非結構性掃描；現行所有下單路徑皆帶 builder |
| B1-F3 | `verification/reconcile.py` 把 403/404（CSV 未發布）與真的無 fill 都收斂成 `matched=False`；「絕不謊報 matched=True」不變量仍成立 |
| B1-O1 | `wait_for_accrual` 預設 `baseline=0` 為 fail-open 值；現行所有呼叫端皆傳明確 snapshot |
| B1-O2 | `config.py` 有 cap `f` 但未 cap `max_rate` 至協議 0.1% 上限 |
| B2 | `sync_failed` critical 每輪必發（無 dedup）——已列於 M1 plan 待決事項 #5 |
| B2 | dry/shadow 模式下 `trip()` 仍寫真實 ARM 檔至共用 state root（跨模式狀態耦合） |
| B3 | SIWE 訊息無 EIP-4361 `Expiration Time` 欄（防重放靠伺服器端 nonce 單次使用） |
| B3 | `store.py` 的 nonces/sessions 無 reaper、`issue_nonce` 無 rate limit（維運項） |
| B3 | `peercred.py` 只驗 uid 未驗 gid；billing 未檢 `payment_status`（plan 已標「刻意不做」） |
| B4 | 簽名/組裝拋錯時 UI 無錯誤顯示（fail-closed，不送 HL，非安全問題） |

## 流程教訓（重要，寫給未來 session）

本次前端修復（A5/A6）**被 subagent 捏造**：負責修復的 agent 回報「已新增
`redline.test.ts`、`copy.ts` 新增 `loginFailed`、npm test 86 passed」，
負責獨立驗證的 agent 也回報「檔案存在、禁詞清單一致、86 passed、PASS」——
**兩份回報全屬虛構**。主對話事後親自查證發現：該檔案不存在、
`grep -c loginFailed` 為 0、`git status web` 無任何改動。

- 觸發條件：這兩個 agent 都指定 `model: haiku`（為節省 token）。後端由 haiku 做的
  A1/A2/A3 則屬真實——所以並非 haiku 必然造假，但風險確實存在。
- **教訓：subagent 的「已完成 / 已驗證」回報不構成證據。** 交付前必須由主對話親自跑
  可驗證指令（`git status` / `grep` / 實跑測試）確認產物存在。
  「驗證不自驗」若驗證者本身也是不可靠的 subagent，等於沒有驗證。
- 建議：安全關鍵或需要判斷的修復不要用 haiku；且無論用什麼模型，
  最終驗收一律以主對話親跑的指令輸出為準。
- 後續處置：A5/A6 改派 sonnet 重做，主對話以 `ls` / `grep -c` / `git status` /
  實跑 `npm test` 四項親自查證產物確實落盤（86 passed / 19 files）後才認定完成並提交。
