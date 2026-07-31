# 槓桿同步盲區修復＋breach 二次確認 — 施工任務書（2026-07-31 第三批）

償還兩筆 open item（`2026-07-22-open-items.md`）：2026-07-25 首航的槓桿同步盲區、
2026-07-31 的單輪幻影回撤窗口。TDD、全離線、內部 Decimal。
基線：`84bde94`，`uv run pytest` 2253 passed。

實測依據（2026-07-31 主網唯讀 probe）：`activeAssetData(user, coin)` 對**空手幣**
也回 `{"leverage": {"type": "cross"|"isolated", "value": int}}`（實測 DOGE 空手回
10x cross）——這是修法成立的前提。

## Wave 1 — adapter 讀方法（機械）

檔案：`src/spark/exchange/base.py`、`hyperliquid.py`、`fakes.py`、
`tests/test_exchange_read_types.py`／`tests/test_hyperliquid_reads.py`。

1. `base.py` reads 區（get_ledger_flows 之後）：
   `get_active_asset_leverage(self, address: str, coin: str) -> tuple[int, bool]`
   ——回 (槓桿值, is_cross)。docstring：走 `activeAssetData`，空手幣也查得到
   （回帳戶對該幣的現行設定）；解析失敗 raise ValueError（呼叫端負責降級與告警）。
2. `hyperliquid.py`：`self._info.post("/info", {"type": "activeAssetData",
   "user": address, "coin": coin})` → `(int(d["leverage"]["value"]),
   d["leverage"]["type"] == "cross")`；缺鍵 → ValueError（訊息含 coin）。
3. `fakes.py`：ctor 槽 `active_asset_leverage=None`（dict `{coin: (lev, is_cross)}`；
   查無 coin → raise ValueError 模擬壞回應）；`calls` 記錄照慣例。

測試：型別＋正反例（cross/isolated/缺鍵 ValueError）＋fake 注入與 calls 記錄。

## Wave 2 — 槓桿同步接線（修盲區本體）

檔案：`src/spark/copytrade/orders.py`、`loop.py`、
`tests/test_copy_orders_reconcile.py`、`tests/test_copy_loop.py`。

1. `sync_open_orders` 加 kwarg `leverage_fallback: Callable[[str],
   tuple[int, bool]] | None = None`，傳進 `_set_entry_leverage`。
2. `_set_entry_leverage`：`leverage_by_coin.get(coin)` 查無時——
   - fallback 為 None → 維持現行靜默跳過（相容路徑）。
   - fallback 非 None → 呼叫之；成功 → 以回傳值走既有 `ex.update_leverage` 路徑
     （executor 的同值快取與 dry 模式天然生效）；**拋例外 → notifier.warn
     （dedup key 含 coin）＋跳過該 coin**——把首航的「靜默跳過」升級成「查詢後設定，
     查不到大聲」。docstring（orders.py:397-400）與 loop.py:285-286 的自承註解同步改寫。
3. `loop.py`：`leverage_by_coin` 建構處旁建 closure
   `lambda c: adapter.get_active_asset_leverage(leader, c)` 傳入 sync_open_orders。
   持倉 map 優先、fallback 只補缺——**不做**持倉幣的雙源交叉驗證（省 N 次查詢；
   誠實註明於註解）。
4. 測試更新＋新增：
   - 既有「unknown coin 是 no-op」改為「fallback=None 時 no-op」；
   - **首航回歸測試**：leader 空手＋純掛單（map 空）＋fallback 注入 (25, True) →
     `update_leverage("ETH", 25, True)` 被呼叫、掛單照常送出；
   - fallback 拋例外 → warn＋該 coin 不設槓桿、其餘 coin 照常；
   - loop 整合：FakeAdapter `active_asset_leverage` 注入 → run_cycle 全管線驗證；
   - 每 coin 每輪至多查一次（`seen` 去重既有機制＋calls 計數斷言）。

## Wave 3 — breach 二次確認（幻影窗口）

檔案：`src/spark/copytrade/loop.py`、`tests/test_copy_loop.py`。

1. 插入點：`status = evaluate(...)` 之後、`dd_breach` critical **之前**
   （false alarm 也要攔；`trip` 的 lock-first ARM 在更後面，安全）。
2. 邏輯：
   ```
   if status.breached and settings.follower_flow_correction_enabled:
       apply_follower_flows(...)          # 重呼同一實作：步驟 1.5 之後落地的流量
                                          # 在標記之後，重呼看得到；exactly-once 由
                                          # 標記天然保證；取數失敗＝函式內吞掉告警
                                          # → 基準未變 → 重判仍破線 → 照常 trip（fail-closed）
       ev = perp_equity_view(...); lifetime = update_lifetime_peak(...)
       cov = sample_coverage(root)
       status2 = evaluate(...)
       if not status2.breached:
           notifier.warn("dd_breach_averted", "破線由出入金造成，基準已二次校正，
                          本輪不觸發熔斷", dedup...)
           → 不進 breach 分支，續行本輪
       else: status = status2  # 用校正後的判定進既有路徑（數字較準）
   ```
3. 已接受的副作用（註解明寫）：二次確認多 append 一筆 equity 樣本＋多一次
   get_account_value——只在 breach 事件發生時，頻率可忽略；樣本值是低點，
   不影響 wick-guard 的高點邏輯。
4. 測試（沿 test_copy_loop.py:440-540 那組的 fixture 形狀）：
   - **幻影攔截**：步驟 1.5 時 ledger 無事件、AV 已扣款（出金 300、peak 1000、
     current 700）→ 一次 evaluate 破線；二次確認時 FakeAdapter 回吐該筆出金
     （需讓 fake 支援兩段回覆：第一次呼叫回空、第二次回流量——用 side-effect list）
     → 樣本/peak 校正 → status2 未破 → **trip 不被呼叫**、收到 averted warn、
     cycle 正常完成（後續步驟照跑）。
   - **真虧損放行**：二次確認無新流量 → 仍破線 → trip 照常（含 dd_breach critical）。
   - **取數失敗 fail-closed**：二次確認的 get_ledger_flows 拋例外 → trip 照常。
   - 逃生閥 false → 無二次確認（get_ledger_flows 不被多呼叫）。
   - 成本熔斷路徑不受影響（既有測試零修改）。

## Wave 4 — 文件收尾

`docs/superpowers/plans/2026-07-22-open-items.md`：銷帳兩筆（槓桿同步盲區、
幻影回撤窗口），格式照 2026-07-31 節既有銷帳樣式。RUNBOOK 無新 env、無新程序，
僅 §5.6（若有槓桿注意事項段落）視情況補一句「空手 leader 的槓桿由 activeAssetData
自動同步（2026-07-31 第三批）」。

## 收尾（主對話）

opus fresh 審查（重點：fallback 失敗語意、二次確認的 fail-closed 方向、
標記互動的 exactly-once、既有 breach 測試零弱化）→ 親驗 → 修復 → 親跑全套 →
部署 prod（rsync 流程、無新 env）→ 服務健康 → git（ff main、tag
`breach-guard-20260731`）→ pandora 文件。
