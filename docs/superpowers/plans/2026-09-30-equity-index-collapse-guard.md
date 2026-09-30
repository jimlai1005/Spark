# 權益指數歸零後有後續區間 → 該窗回 insufficient（方案 B）

狀態：**✅ 已部署** 2026-09-30 12:42 UTC，commit `411c6b4`，單檔熱修（§5.8i）＋restart filet-api；重啟後 journal enrich 失敗 0 次。reviewer 可部署＋2 Warning：W1 措辭已修；W2（探索資格對 `max_dd_pct is None` 放行，既有 fail-open，本修法使其可達）**使用者 2026-09-30 裁決：暫不改動**，列入 backlog（觸發條件：所選窗口歸零後再入金的地址；修法候選＝回撤 None 列 pending）。

**Goal:** `compute_window_performance` 在權益指數鏈乘到 0 之後若還有後續已入金區間，整窗回 `status="insufficient"`、新 reason
`equity_index_collapsed`，其他窗口照算；不再在 `ratio_returns` 那行拋 `DivisionUndefined`。

## 事實（2026-09-30 正式機重放）

- 探索 publisher 每分鐘對 0x4cae5bed…7c34 enrich 失敗 `InvalidOperation([DivisionUndefined])`，該列永遠 pending/enrich_error；重啟前 24h 1,377 次。
- traceback：`hl_explore.enrich_candidate:953 → trader_stats.window_stats:88 → leader_perf.compute_window_performance:475`
  `ratio_returns = [equity_index[i] / equity_index[i-1] - 1 …]` → 0/0。
- 原因：該地址 allTime `accountValueHistory` 第 9 點為 0（2024-01 帳戶清空後再入金）。某一區間 `r = d_pnl/prev_av == -1`（設計上合法，
  `leader_perf.py:412-418` 註解與 `tests/test_leader_perf.py:322-325` 釘住「r == −1 → ok、TWR = −1」），指數乘到 0；之後每期
  `0 × (1+r)` 仍是 0；到 `:475` 相鄰兩點相除 0/0。**只有「歸零之後還有區間」才會炸**；歸零發生在最後一區間時 `ratio_returns`
  最後一項是 `0/正數 − 1 = −1`，不炸，且 TWR = −1 是正確答案（既有測試）。
- 既有 `_insufficient` reason 值域：flow_dominated_interval／need_at_least_two_samples／no_funded_interval／non_monotonic_timestamps／
  series_misaligned／too_many_skipped_intervals／window_missing。下游 `trader_stats.window_stats` 對非 OK 回 `max_dd_pct=None、
  max_dd_reason=<reason>`；前端統一顯示「回撤不可用」，**不逐碼對映**，故本 plan 不動前端。

## 裁決（使用者選 B）

歸零後仍有已入金區間的窗口，不能用單一條鏈表示，**整窗判 insufficient**（reason `equity_index_collapsed`），不切段重算（A）、不跳過除法（C）。
其他窗口（month／perpMonth 等）不受影響，因為它們的區間不含歸零點。

### Task 1 `@inline`：guard ＋ 測試

**Files:** Modify `src/spark/filet/leader_perf.py`（`compute_window_performance` 的指數迴圈 :396-420）；Test `tests/test_leader_perf.py`、`tests/test_trader_stats.py`。

實作：在迴圈 `equity_index.append(equity_index[-1] * (1 + r))` 之後加

```python
if equity_index[-1] == 0 and i < len(pnl) - 1:
    # 指數歸零（r == -1，合法）但之後還有區間：單一條鏈無法表示「清空後再入金」，
    # 與 flow_dominated_interval 同級判整窗無效（2026-09-30 方案 B；0x4cae…7c34 事故）。
    logger.warning("portfolio %s 窗第 %d 區間權益指數歸零且其後仍有 %d 區間——整窗判 insufficient",
                   period, i, len(pnl) - 1 - i)
    out = _insufficient(period, "equity_index_collapsed", sample_count=len(pnl))
    out["skipped_intervals"] = skipped
    return out
```
- `_insufficient` 的 docstring／模組檔頭 reason 值域清單（若有列舉）補 `equity_index_collapsed`。
- 不動 r == −1 本身的合法性；不動 `ratio_returns` 那行（縱深防禦可加一個 `assert all(x != 0 for x in equity_index[:-1])`？**不要**——保持
  行為單純，靠 guard 保證不可達）。

測試（先紅後綠）：
1. `tests/test_leader_perf.py`：av＝[1000, 0, 500, 550]、pnl 對應使第 1 區間 r == −1、之後再入金 → `status == "insufficient"`、
   `reason == "equity_index_collapsed"`、不拋例外；`skipped_intervals` 照實。
2. 既有 `:322-325`（r == −1 在最後一區間 → ok、TWR −1）維持通過。
3. `tests/test_trader_stats.py`：同型 portfolio → `window_stats` 回 `max_dd_pct=None`、`max_dd_reason="equity_index_collapsed"`、`pnl_usd` 照算。
4. 用正式機重放的真實形狀做一則整合測試：`hl_explore.enrich_candidate` 對「allTime 含歸零再入金」的 portfolio 不拋，
   `windows["allTime"].max_dd_reason == "equity_index_collapsed"`，month 窗正常（builder 從 `tests/test_hl_explore*.py` 既有 fixture 改造）。

驗收：`uv run pytest tests/test_leader_perf.py tests/test_leader_perf_ratios.py tests/test_trader_stats.py -q -k "collapsed or minus_one or -1 or annualized"`
列出新測試；`uv run pytest -q` 全綠；`uv run ruff check src tests`。

## 部署（主線程）

commit → 單檔熱修 `src/spark/filet/leader_perf.py`（前提：正式機 DEPLOYED_VERSION == HEAD~1 且該檔逐位元一致，RUNBOOK §5.8i）→
restart `filet-api`（follower 不動；leaderboard／perf-series timer 每次新進程自動用新碼）→ 驗收：journal 不再出現 0x4cae 的
`enrich 失敗`，探索 publisher 下一輪該列從 pending/enrich_error 變成 eligibility 依其他門檻判定（allTime 回撤 None → 依
`max_dd` 門檻規則落 pending 或 ineligible，由既有邏輯決定，不在本 plan 內裁決）。
