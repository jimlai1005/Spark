# spark

Hyperliquid builder-code 基礎設施 + copytrade orchestrator（Filet M1）。Python 3.11 + uv。

## 指令
- 測試（離線）：`uv run pytest`（integration 標記預設跳過）
- 前端測試：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test`（vitest；預設 shell PATH 無 node）
- Lint：`uv run ruff check src tests scripts`
- Testnet 流程：`uv run python -m scripts.run_testnet_flow`（需 SPARK_ACCOUNT_ID/SPARK_USER_ADDR/SPARK_BUILDER_ADDR）

## 紅線（動之前必問使用者）
1. `/Users/jim/projects/hl-copytrader` 上有線上實盤產品：**唯讀**，不寫入、不執行其程式或測試。
2. 不讀取或印出任何 `.env*`；私鑰不得出現在 log/repr/例外訊息（`TxResult.agent_key` 為 repr=False 慣例）。
3. `ExchangeAdapter` 不含 withdraw/transfer（非託管不變量，tests/test_base_types.py 結構性斷言）。
4. 所有會產生掛單的寫入必帶 builder 參數（SDK `modify_order` 無此欄位為已知例外）。
5. copytrade `live_trading` 預設 False；任何主網寫入（下單/開平倉）是人工決策，不得自動開啟。
   例外（2026-07-30 使用者裁決，產品僅內部使用）：auto-activate watcher 對完成綁定、
   **曾通過入金檢查**（寫入 pending 當下驗過鏈上入金，啟用時不重驗）且已簽章選定 leader
   的用戶，自動建 env（`COPY_LIVE_TRADING=true`）並啟動引擎——僅此一條路徑豁免；
   對外開放前必須重審本例外。
   ⚠️ 2026-07-30 追加：新錢包**預設不啟用任何風控**（回撤 kill switch ＋ 成本熔斷），
   由錢包主人在跟單頁自行勾選啟用。此路徑已於同日**升級為客戶簽章記錄**
   （`POST /api/me/risk/message` 取原文 → `POST /api/me/risk` 落簽章記錄 →
   watcher 建 env 與引擎每輪套用各自重新驗章；解除熔斷同形狀走 `/api/me/risk/unlock`）
   ——原本「寫進 pending 條目、無簽章」的路徑已移除。設計見
   `src/spark/filet/risk_settings.py` 檔頭；**對外開放前仍必須重審本例外**。
   既有正在跑的 follower 一律不動（缺 `COPY_RISK_CONTROLS_ENABLED` 時引擎預設
   True＝維持風控）。
6. 測試全離線：autouse socket-ban（tests/conftest.py）；新測試不得連網、不得真發通知。
7. **撤銷 leader 一律跑 `scripts/revoke_leader.py`**（冪等、跨精選白名單＋user registry 兩檔、自我驗收 `is_still_permitted` 為 False）。
   ⚠️ 自 2026-07-27 起「刪除白名單條目」**不再等於** `enabled:false`：位址若也在 `user_leaders.json`，刪精選條目只會讓 registry 那筆遞補上來＝撤銷靜默失效。
   做錯的方向全是 fail-open，詳見 `deploy/RUNBOOK.md` §「安全撤銷一律跑 revoke_leader.py」。

## 慣例
- 內部一律 Decimal；float 只在 adapter↔SDK 邊界（`_round_px`/`_round_size`）。
- 文件流：docs/superpowers/{specs,plans,research}/，檔名 YYYY-MM-DD-<slug>.md。
- Commit 格式：feat:/fix:/test:/docs: 一行敘述（見 git log）。
- 通知一律走 `spark.copytrade.notifier.Notifier` 注入，引擎不 import 具體實作。
- 前端（2026-08-29 改版後）：文案雙語單一來源 `web/src/lib/copy.ts`（COPY_ZH/COPY_EN 結構對稱、元件經 `useCopy()` 取用，禁內嵌中文；法務長文在 `web/src/content/legal.ts` 同紀律）。公開路由 `/ /strategies /strategies/[slug] /explore /traders/[address] /advanced /docs /terms /privacy /risk /status`（/docs 自 2026-08-29 起不在導覽；/leaderboard 自 2026-08-30 起 redirect → /explore）；登入後 `/onboarding /dashboard /settings`。nav：未登入 策略/探索/運作方式/安全性，登入後 Dashboard/策略/探索/設定。對外主機名走 build 期 `NEXT_PUBLIC_SITE_ORIGIN`（RUNBOOK §4.2）。
