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
6. 測試全離線：autouse socket-ban（tests/conftest.py）；新測試不得連網、不得真發通知。

## 慣例
- 內部一律 Decimal；float 只在 adapter↔SDK 邊界（`_round_px`/`_round_size`）。
- 文件流：docs/superpowers/{specs,plans,research}/，檔名 YYYY-MM-DD-<slug>.md。
- Commit 格式：feat:/fix:/test:/docs: 一行敘述（見 git log）。
- 通知一律走 `spark.copytrade.notifier.Notifier` 注入，引擎不 import 具體實作。
