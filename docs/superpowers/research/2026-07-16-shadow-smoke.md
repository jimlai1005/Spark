# Shadow 模式主網唯讀煙霧實測（Task 16）

**Date:** 2026-07-16
**Branch:** `feat/builder-code-verification`
**任務:** Task 16（spec shadow diff 分類器 + differ CLI + 主網唯讀煙霧實測）
**狀態:** 煙霧實測完成（唯讀，零簽章/零下單）；hl-copytrader 線上 log 尚未取得，
差異分類器本身已用合成資料驗證（見 `tests/test_copy_shadow.py`），實地校準待第 4 節。

---

## 1. 實測方式

零憑證、零風險：不設 `SPARK_ACCOUNT_ID`、不觸碰 Keychain（`--shadow` 結構性走
`exchange=None` 建構，`ActionExecutor(live=False)`，零 adapter 寫入）。

```bash
env | grep -i COPY_TG_BOT_TOKEN   # 確認未設，避免真的推播 Telegram
SPARK_NETWORK=mainnet \
SPARK_USER_ADDR=0x000000000000000000000000000000000000dEaD \
SPARK_BUILDER_ADDR=0x00000000000000000000000000000000000cAfE0 \
COPY_LEADER_ADDRESS=0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1 \
COPY_ALLOCATED_CAPITAL=100 \
uv run python -m scripts.run_copytrade --shadow --once
```

連跑 3 次（同一 UTC 日 `20260716`）。地址說明：

- `SPARK_USER_ADDR`/`SPARK_BUILDER_ADDR` 用佔位地址（非真實錢包，僅需符合格式；
  `Settings`/`CopySettings` 對這兩者無 checksum 驗證，只驗 `leader_address` 的
  `0x` + 42 碼格式）。`--shadow` 模式下這兩個地址只被拿去做**讀**呼叫
  （`get_positions`/`get_account_state`/`get_open_orders`），不會有任何簽章或下單。
- `COPY_ALLOCATED_CAPITAL=100`：佔位地址本身鏈上權益為 0，若不覆蓋這個值，
  `resolve_capital`（`src/spark/copytrade/sizing.py:58-64`）會回傳 0 → `scale=0`
  → 所有 desired size 皆為 0 → 每輪 0 動作，煙霧測試看不到任何有意義的動作形狀。
  設定固定 `allocated_capital=100` 讓 scale 用固定本金公式計算（`resolve_capital`
  設計本就支援「> 0 用固定值，不看實際權益」），使跟單邏輯正常產生非零 desired
  orders——這只影響「意圖動作的大小」，**不影響 live gate**（`ActionExecutor.live`
  仍是 `False`，寫入方法結構上碰不到，見 `executor.py:181` 起的 live gate 分支）。

## 2. 結果（3 輪，皆無例外）

| 輪次 | reconcile | scale | skip_trigger | 例外 |
|---|---|---|---|---|
| 1 | placed=35 cancelled=0 modified=0 matched=0 sync_failed=False | 0.0005650731750238377672835037278 | 0 | 無 |
| 2 | placed=35 cancelled=0 modified=0 matched=0 sync_failed=False | 同上 | 0 | 無 |
| 3 | placed=35 cancelled=0 modified=0 matched=0 sync_failed=False | 同上 | 0 | 無 |

三輪 `CycleReport` 原文（`safety_net` 三輪皆為
`{'opened': [], 'adjusted': [], 'flattened': [], 'skipped': [], 'failed': []}`，
leader 本次無新開倉/減倉需要跟）：

```
CycleReport(reconcile=ReconcileResult(placed=35, cancelled=0, modified=0, matched=0,
sync_failed=False, skipped_small=()), safety_net={...全空...},
scale=Decimal('0.0005650731750238377672835037278'), tripped=False)
```

JSONL 落地：`var/copytrade/shadow/20260716.jsonl`，累計 3×35=105 行，全數
`kind="place"`，覆蓋 4 個 coin（`BNB`/`BTC`/`ETH`/`HYPE`，leader 本輪掛單標的）。
樣本行（同一輪的其中兩筆，數字為 leader 真實掛單經 scale 縮放後的結果，非敏感
資料，僅遮去 `ts` 精確到微秒的部分）：

```json
{"ts": 1784205410.64, "kind": "place", "coin": "BNB",
 "payload": {"is_buy": true, "sz": "0.028", "limit_px": "401.0",
             "reduce_only": false, "tif": "Gtc", "oid": "1", "ok": true}}
{"ts": 1784205410.64, "kind": "place", "coin": "BNB",
 "payload": {"is_buy": true, "sz": "0.028", "limit_px": "426.0",
             "reduce_only": false, "tif": "Gtc", "oid": "2", "ok": true}}
```

驗證 JSONL 可被 `load_action_records` 正確解析（無例外、筆數與 kind 分布相符）：

```
>>> load_action_records(Path("var/copytrade/shadow/20260716.jsonl"))
parsed count: 105
kinds: {'place'}
coins: ['BNB', 'BTC', 'ETH', 'HYPE']
```

## 3. 發現的問題 / 觀察

1. **`--once` 的虛擬簿不跨進程持續**：`VirtualBook()` 在 `scripts/run_copytrade.py:main()`
   內建構（`run_copytrade.py:174`），每次 `uv run python -m scripts.run_copytrade --once`
   都是全新進程、全新 `VirtualBook`——`my_orders` 每輪都從空簿開始，故三輪的
   `matched` 恆為 0、`placed` 恆為 35（沒有第二輪起「已掛單保留」的收斂效果）。
   `VirtualBook` 的跨輪收斂設計（docstring：「第二輪起 desired 未變時 plan 應為
   全 matched、零動作」）只在**同一進程的 `main_loop`** 內成立，因為
   `cycle()` closure 共用同一個 `ex`（`run_copytrade.py:183-188`）。若要在
   `--once` 語境下觀察收斂，需要另寫一支腳本在同一進程內重複呼叫
   `run_cycle`（沿用同一個 `ex`/`state`），而非重跑 CLI 三次——這不是 bug，
   是 `--once` 語意本身如此（單輪同步後退出），本次任務照 spec 用「連跑 3 次」
   驗證的目標本就是「跑通無例外＋觀察每輪獨立形狀」，不是驗證跨輪收斂。
2. **scale=0 陷阱**：用真實佔位地址（零鏈上權益）跑 shadow 若不覆蓋
   `COPY_ALLOCATED_CAPITAL`，會得到全零動作的假陰性結果（「跑通了但什麼都沒發生」
   容易誤讀為「leader 本輪無動作」）。已在第 1 節記錄解法；未來若用真實 dogfood
   錢包（`docs/superpowers/research/2026-07-16-dogfood-runbook.md` §0）跑 shadow，
   錢包本身有真實權益，不需要這個覆蓋。
3. **本次未觀察到 `skip_trigger`**：leader 這 3 輪掛單皆為一般限價單
   （`order_type`/trigger 欄位在 `_build_desired` 產出的 `OrderSpec` 中皆為
   `is_trigger=False`），T12 docstring 提到的 M1 限制（`executor.py:6-14`）
   本次煙霧測試未被觸發。這是**資料點而非結論**——leader 的掛單組成會隨時間
   變化，`is_trigger` 單是否存在取決於 leader 當下的策略；後續每日 shadow
   觀察期（dogfood runbook §2）应持續留意 `skip_trigger` 是否出現，出現時
   對應的 warn 告警與 JSONL 記錄已在 T12/T16 就緒，不需要額外開發。
4. 三輪皆 `sync_failed=False`、`tripped=False`，回撤判定與 killswitch 短路路徑
   本次未觸發（符合預期——這是唯讀煙霧測試，非回撤壓力測試）。

## 4. 待 hl log 取得後的校準步驟

本次煙霧測試**沒有**同時取得 hl-copytrader 線上 log（該服務跑在使用者自己的
主機，需要 `user@IP` SSH 存取，本次任務範圍不含取得該存取憑證）。`shadow_diff.py`
與 `shadow.py` 的分類邏輯已用合成資料獨立驗證（`tests/test_copy_shadow.py` 的
`parse_hl_log_line`/`classify_diff` 測試，格式出處見該檔頂部與
`src/spark/copytrade/shadow.py` 模組 docstring 引用的 `trader.py` 行號）。
實地校準（對應 `docs/superpowers/research/2026-07-16-dogfood-runbook.md` §2）
待使用者提供 hl-copytrader 主機存取後，依序：

1. **取得 hl-copytrader log**：`user@IP` 上執行（IP/user 待使用者提供）：
   ```bash
   ssh <user>@<IP> "tail -n 5000 /path/to/hl-copytrader/logs/copytrader.log" \
       > /tmp/hl-copytrader-$(date -u +%Y%m%d).log
   ```
   或若跑在 systemd／docker，改用 `journalctl -u hl-copytrader --since "1 hour ago"`
   或 `docker logs <container> --since 1h`，格式應與
   `logs/copytrader.log` 的 `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
   一致（`main.py:43-51`）。
2. **時間窗對齊**：hl-copytrader 是每小時 `:55` 跑一次同步（`logs/copytrader.log`
   啟動訊息「每小時 :55 執行掛單同步」），spark 的 `COPY_INTERVAL_S` 預設 60 秒
   跑一次（`config.py`，刻意覆蓋 hl 的 hourly，見 `loop.py` 模組 docstring）——
   兩邊同步頻率不同，比對時應取**同一段時間窗內**兩邊各自最新一輪的動作集合，
   不是逐分鐘 1:1 對齊。
3. **執行 differ**：
   ```bash
   uv run python -m scripts.shadow_diff \
       --spark var/copytrade/shadow/YYYYMMDD.jsonl \
       --hl-log /tmp/hl-copytrader-YYYYMMDD.log \
       --px-tol 0.002 --size-tol 0.05
   ```
   輸出三類計數＋逐項 detail，並落檔
   `var/copytrade/shadow/diff-YYYYMMDD.md`。
4. **判讀**：對照 dogfood runbook §2.3 的驗收門檻——連續 3 個交易日
   `unexplained` 計數為 0（`match`/`explainable` 不限）。`unexplained` 非零時，
   先看 detail 是否為結構差（動作種類/coin/方向對不上，通常是邏輯路徑選擇不同，
   紅燈）還是數值差但比值不一致（通常是資料時間戳沒對齊，先重跑一次確認
   時間窗，再判斷是否為真的參數漂移）。
5. **--px-tol/--size-tol 校準**：預設 0.2%/5% 是本任務的合理起始值（覆蓋一般
   OTC 價格波動與 rounding），若實地比對出現大量「px 差在 0.2%~0.5% 之間」
   的 pending 但比值一致的 explainable 案例，可考慮放寬 `--px-tol` 到 0.3~0.5%——
   **是否放寬由使用者依實地數據裁決**，不由 agent 自行決定（放寬驗收門檻屬於
   `~/.claude/rules/judgment.md` 第 4 節「驗收條件被悄悄放寬」的警訊，需要
   明確依據，不能因為「這樣比較好過」就調)。

---

**引用**：`docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md`、
`docs/superpowers/research/2026-07-16-dogfood-runbook.md` §2、
`src/spark/copytrade/executor.py` M1 已知限制 docstring。
