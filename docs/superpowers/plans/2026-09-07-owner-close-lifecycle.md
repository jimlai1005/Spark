# owner_close 生命週期收尾 Implementation Plan

> **For agentic workers:** 逐 task 由主線程派 `builder`（@inline）執行；每個 task 只讀本檔，不依賴規劃對話。步驟用 `- [ ]` 追蹤。

**Goal:** 用戶簽章「平倉並撤銷」（owner_close）完成後，引擎發一則完成通知就**自行結束**，不再每 15 分鐘重送 kill switch 告警；用戶日後重新選定 leader 即**自動**重新啟用（舊 tripped 檔自動歸檔並在啟動時提示「先前曾平倉撤銷」），不需要 operator 手動刪檔。

**Architecture:** 三個既有元件各加一小段、互相用檔案系統交握：
1. **引擎**（`loop.py`／`killswitch.py`）：ARM 檔 `reason=owner_close, phase=complete` ⇒ 終態 → 發最後一則 critical（有殘留暴險時明列於訊息中，剩餘由用戶自行收尾）→ `main_loop` 正常返回（exit 0；unit 是 `Restart=on-failure`，不會被拉起）。
2. **API**（`leaders_select` 補寫 pending）：帳號已在 manifest 但 `close_all_result` 標記為 `completed` ⇒ 視同「重新跟單」，照新客戶流程重驗 READY（授權＋入金即時查）後寫 pending。dashboard 的 `state` 在心跳過期時也以該標記判 `halted`（引擎已結束，心跳必然過期，不得顯示成「跟單中」）。
3. **auto-activate watcher**：pending 條目的帳號已在 manifest、state 目錄有 owner_close 終態 ARM、且用戶有新簽章 leader ⇒ 把 ARM＋全期高水位＋權益樣本搬進 `var/copytrade/owner_close_archive/<tripped_at>/`、清 `close_all_result` 標記與 `owner_close.json` 條目、`systemctl start`、通知。引擎啟動時看到歸檔目錄非空 → 發一則「先前曾於 … 平倉並撤銷，本次為新一輪跟單」。

**Tech Stack:** Python 3.11 + uv、pytest（離線，autouse socket-ban）、Next.js（vitest）。

**背景事實（builder 不必重查）：**
- ARM 檔：`<state_root>/var/copytrade/killswitch.tripped`（`killswitch.ARM_FILE_RELPATH`），payload 欄位見 `killswitch.trip()`（`src/spark/copytrade/killswitch.py:518-580`）：`tripped_at`（ISO）、`reason`、`phase`、`cancelled`、`orders_not_cancelled`、`closed`、`failures`。`_read_arm_payload()`（:193-212）是唯一解析點，回 `(tripped_at, reason, epoch_s, residual)`。
- `REASON_OWNER_CLOSE = "owner_close"`（:165），結構性不在任何 rearm 清單。
- 全期高水位 `equity.LIFETIME_PEAK_RELPATH = var/copytrade/equity_lifetime_peak.json`、樣本 `equity.SAMPLES_RELPATH = var/copytrade/equity_samples.json`。
- `loop.run_cycle` 的 tripped 短路在 `src/spark/copytrade/loop.py:90-102`；`CycleReport`／`tripped_report()` 在 :63-67 附近；`main_loop` 在 :429-465（忽略 `mk_cycle()` 回傳值）。
- runner：`scripts/run_copytrade.py`，`cycle()` 在 :717-803 回傳 `run_cycle` 的 report，`main_loop(cycle, ...)` 在 :808。
- close_all 請求檔 `$FILET_EXCHANGE_DIR/owner_close.json`（`{"requests":[...]}`），result 標記 `$FILET_EXCHANGE_DIR/engine/close_all_result/<account_id>.json`（`{"status","ts","request_issued_at"}`）；讀寫函式在 `src/spark/filet/close_all.py:188-258`。引擎側 `CloseAllApplier` 在 `src/spark/filet/close_all_apply.py`。
- API：`src/spark/publicapi/app.py` — `_dashboard_close_request(exchange_dir, account_id) -> {"state": "pending"|"expired"|"completed"} | None`（:558）；`_dashboard_status()`（:647-681，`tripped = ... if hb.fresh else None`，心跳過期時 `HeartbeatRead.data` 結構性為 None）；`leaders_select` 補寫 pending 的分支在 :2819-2880（`mine is not None → pass`）。
- watcher：`scripts/filet_auto_activate.py` — `process_entry()`（:341-425）；`in_manifest` 分支（:358-381）現況對 phase `started`/`None` 只清 pending＋warn「請人工 systemctl start」。`_latest_signed_leader(records, *, account_id, user_address)`（:105）、`_manifest_ref`（:324）、`remove_satisfied_leader_change`（`src/spark/filet/leader_change.py:337`）、`ensure_dir_secure`／`named_owner_ids` 已 import。watcher 以 root 跑；state 目錄 `state_base/<account_id>` 為 `filet-engine` 0700。
- systemd：`deploy/filet-follower@.service` `Restart=on-failure`；watcher 用 `systemctl start filet-follower@<id>`。
- 前端：`web/src/components/dashboard/StatusCard.tsx:185-196` 的 `closeAllDone` 指引卡；文案在 `web/src/lib/copy.ts:1463`（ZH）與 :3038（EN）附近 `halted`/`closeAllDone`。文案雙語結構必須對稱（`copy.test.ts` 驗）。

**裁決（已定，builder 不得改）：**
- D1（2026-09-07 使用者裁決）owner_close 收尾完成後**不論有無殘留暴險**都只發一則、引擎結束。有殘留暴險時那一則必須明列 `failures`／`orders_not_cancelled` 並註明「剩餘小額部位由用戶自行至 Hyperliquid 收尾」。終態判定＝`reason == owner_close and phase == complete`。
- D2 重新跟單的唯一入口＝用戶重新簽章選 leader（`leaders_select`）；API 重驗 READY；watcher 是唯一的（重）啟用者。
- D3 歸檔而非刪除：ARM＋lifetime peak＋samples 一起搬進歸檔目錄（新一輪跟單＝新基準）。
- D4 dashboard `state`：`tripped is True` **或** `close_request.state == "completed"` ⇒ `halted`。
- D5 引擎在 ARM 為 owner_close 終態時**每次啟動**都只跑一輪、發一則、退出（營運端誤 `systemctl start` 也 fail-closed）。

---

### Task 1 @inline：killswitch.py 終態判定與歸檔

**Files:**
- Modify: `src/spark/copytrade/killswitch.py`
- Test: `tests/test_copy_killswitch.py`

新增（放在 `halt_status()` 之後）：

```python
OWNER_CLOSE_ARCHIVE_RELPATH = Path("var/copytrade/owner_close_archive")

def owner_close_terminal(root: Path) -> dict | None:
    """ARM 檔為「owner_close 且 phase == complete」→ 回 payload dict（另加鍵
    `residual: bool`＝_read_arm_payload 算出的殘留暴險）；否則 None。
    判不出來（檔壞、缺欄位）→ None（fail-closed：不宣稱終態）。"""

def archive_owner_close(root: Path, *, now: datetime | None = None) -> Path:
    """把 ARM、equity_lifetime_peak.json、equity_samples.json 搬進
    OWNER_CLOSE_ARCHIVE_RELPATH/<tripped_at 去掉冒號>/ 並回傳該目錄。
    前置：owner_close_terminal(root) 非 None，否則 raise ValueError。
    缺 peak/samples 檔時略過不報錯。目錄名衝突時加 -2、-3 後綴。"""

def owner_close_history(root: Path) -> list[dict]:
    """歸檔目錄底下每個子目錄的 killswitch.tripped payload（依目錄名排序）；
    讀不到的略過。目錄不存在 → []。"""
```

- [ ] 測試（先寫、先跑紅）：`owner_close_terminal` 對 (a) 無 ARM → None；(b) reason=drawdown → None；(c) owner_close + phase=flatten_in_progress → None；(d) owner_close + complete + failures=["BTC"] → dict 且 `residual is True`；(e) owner_close + complete + 無殘留 → dict 且 `residual is False`。`archive_owner_close` 搬走三個檔、原位不存在、回傳目錄含三檔；對非終態 raise。`owner_close_history` 回 1 筆並含 `tripped_at`。
- [ ] 實作、`uv run pytest tests/test_copy_killswitch.py -q` 全綠。
- [ ] Commit：`feat: killswitch owner_close 終態判定與歸檔（owner_close_terminal/archive/history）`

### Task 2 @inline：引擎在 owner_close 終態自行結束

**Files:**
- Modify: `src/spark/copytrade/orders.py:381-389`（`CycleReport` frozen dataclass 定義在這裡，欄位 `halt_engine: bool = False` 加在 `tripped` 之後；2026-09-07 裁決：允許改此檔）
- Modify: `src/spark/copytrade/loop.py`（`tripped_report`、`run_cycle` :90-102、`main_loop` :444-451）
- Test: `tests/test_copy_loop.py`

- `CycleReport` 新增欄位 `halt_engine: bool = False`；`tripped_report(*, halt_engine: bool = False)`。
- `run_cycle` tripped 短路改為：
  ```python
  if is_tripped(root):
      terminal = owner_close_terminal(root)
      if terminal is not None:
          residual_note = ""
          if terminal["residual"]:
              residual_note = (f"⚠️ 有殘留暴險：平倉失敗 {terminal.get('failures')}、"
                               f"掛單未撤={terminal.get('orders_not_cancelled')}——"
                               f"剩餘小額部位請用戶自行至 Hyperliquid 收尾。")
          notifier.critical(
              "killswitch",
              f"平倉並撤銷已完成（{terminal['tripped_at']}，撤單 {terminal.get('cancelled')} 張、"
              f"平倉 {terminal.get('closed')}）——引擎結束運行，不再重複提醒。{residual_note}"
              f"重新跟單：用戶在站上重新選定 leader 並簽章後會自動重新啟用（舊記錄自動歸檔）",
              dedup_key="owner_close_done")
          return tripped_report(halt_engine=True)
      notifier.critical(... 既有訊息與 dedup_key="tripped" 不變 ...)
      return tripped_report()
  ```
- `main_loop`：`report = mk_cycle()` 後 `if getattr(report, "halt_engine", False): notifier.info("loop", "引擎已依 owner_close 終態結束"); return`。docstring 的「tripped 不停迴圈」補一句例外。
- [ ] 測試：(a) ARM owner_close 終態 → `run_cycle` 回 `halt_engine=True`、notifier 收到一則含「平倉並撤銷已完成」、零交易呼叫；(b) ARM owner_close 終態但 failures=["BTC"] → 仍 `halt_engine=True`，訊息含「殘留暴險」與 BTC；(b2) ARM reason=drawdown → `halt_engine=False`、訊息仍是既有「kill switch 已 tripped」；(c) `main_loop` 收到 `halt_engine=True` 的 report 後返回（用 `sleep_fn` 計數器證明沒有第二輪）；(d) 一般 tripped report 不返回（既有 `test_tripped_short_circuits_with_zero_calls` 不動）。
- [ ] `uv run pytest tests/test_copy_loop.py -q` 全綠。
- [ ] Commit：`feat: owner_close 終態引擎發一則完成通知後自行結束（exit 0）`

### Task 3 @inline：啟動時提示歸檔歷史

**Files:**
- Modify: `scripts/run_copytrade.py`（`main()` 內 notifier 與 `state_root` 就緒後、`main_loop` 之前）
- Test: `tests/test_copy_killswitch.py`（測 helper）

在 `killswitch.py` 加 helper 並在 runner 呼叫（runner 只接線，邏輯在 helper 便於測試）：

```python
def announce_owner_close_history(root: Path, notifier: Notifier) -> None:
    hist = owner_close_history(root)
    if not hist:
        return
    last = hist[-1].get("tripped_at")
    notifier.warn("killswitch",
                  f"提醒：此帳號先前曾平倉並撤銷 {len(hist)} 次（最近一次 {last}），"
                  f"記錄已歸檔；本次啟動為新一輪跟單，全期高水位重新起算",
                  dedup_key="owner_close_history")
```

- [ ] 測試：無歸檔 → 不發；有歸檔 → 發一則 warn 含次數與 tripped_at。
- [ ] runner 接線（`--once`／shadow 也照發，無副作用）。`uv run pytest tests/test_run_copytrade_state_dir.py tests/test_copy_killswitch.py -q` 綠。
- [ ] Commit：`feat: 引擎啟動時提示先前的 owner_close 歸檔紀錄`

### Task 4 @inline：close_all.py 清理函式

**Files:**
- Modify: `src/spark/filet/close_all.py`
- Test: `tests/test_close_all_result_marker.py`

```python
def remove_close_all_requests(path: str | Path, *, account_id: str) -> int:
    """從 owner_close.json 移除該帳號全部請求，回移除筆數；檔不存在 → 0。原子寫回（tmp + replace）。"""

def clear_close_all_result(result_path: str | Path) -> bool:
    """刪 result 標記檔；不存在 → False。"""
```

- [ ] 測試：移除只影響該帳號、其他帳號保留；檔不存在回 0；clear 兩次第二次 False。
- [ ] Commit：`feat: close_all 請求與 result 標記的清理函式（供 watcher 重新啟用時使用）`

### Task 5 @inline：API——重新跟單補寫 pending＋dashboard halted

**Files:**
- Modify: `src/spark/publicapi/app.py`（:558 附近新增 helper；:2826-2828 分支；:647-681 `_dashboard_status`）
- Test: `tests/test_api_leader_select.py`、`tests/test_me_dashboard.py`

- 新增 `def _close_all_completed(exchange_dir: str, account_id: str) -> bool: return ((_dashboard_close_request(exchange_dir, account_id) or {}).get("state") == "completed")`——單一定義，兩處共用。
- `leaders_select`：`if mine is not None:` 改為 `if mine is not None and not _close_all_completed(cfg.exchange_dir, account_id): pass` ；註解說明「已完成平倉並撤銷的帳號重新選 leader ＝ 重新跟單，走與新客戶相同的 READY 重驗＋寫 pending，由 watcher 歸檔舊記錄後重啟」。（`cfg` 裡交換目錄欄位名請看 `_dashboard_close_request` 的既有呼叫端。）
- `_dashboard_status`：先算 `close_request = _dashboard_close_request(...)`，`state = "halted" if tripped is True or (close_request or {}).get("state") == "completed" else ...`；回傳的 `close_request` 用同一個值。
- [ ] 測試：(a) 在 manifest＋result completed＋READY → pending 被寫；(b) 在 manifest＋無 result → 不寫（既有行為）；(c) dashboard：心跳 stale＋result completed → `state == "halted"`；(d) 心跳 fresh 未 tripped＋無 result → `following`（既有）。
- [ ] `uv run pytest tests/test_api_leader_select.py tests/test_me_dashboard.py -q` 綠。
- [ ] Commit：`feat: 平倉並撤銷後重新選 leader 視為重新跟單；dashboard 以 result 標記判 halted`

### Task 6 @inline：watcher 重新啟用分支

**Files:**
- Modify: `scripts/filet_auto_activate.py`（`process_entry` 的 `in_manifest` 分支開頭）
- Test: `tests/test_filet_auto_activate.py`

在 `if in_manifest:` 內、`phase = ...` 之前插入：

```python
state_root = state_base / account_id
terminal = owner_close_terminal(state_root)
if terminal is not None:
    leader = _latest_signed_leader(leader_changes, account_id=account_id,
                                   user_address=entry["user_address"])
    if leader is None:
        return "waiting_leader"          # 保留 pending，等他簽
    archive_dir = archive_owner_close(state_root)
    _chown_tree(archive_dir, owner, group)   # 歸檔目錄與檔案維持 filet-engine 可讀寫
    remove_close_all_requests(close_all_path_for(exchange_dir), account_id=account_id)
    clear_close_all_result(close_all_result_path_for(exchange_dir, account_id))
    state.set_phase(account_id, "starting")
    run_cmd(start_cmd, check=True)
    state.set_phase(account_id, "started")
    remove_satisfied_leader_change(changes_path, account_id=account_id, leader_address=leader)
    remove_pending_entry(pending_path, account_id)
    notifier.warn("auto-activate",
        f"account={account_id} 重新跟單：先前於 {terminal['tripped_at']} 平倉並撤銷，"
        f"記錄已歸檔至 {archive_dir}，引擎已重新啟動（leader={leader}）",
        dedup_key=f"auto-activate:refollow:{account_id}")
    return "reactivated"
elif is_tripped(state_root):
    # owner_close 尚未收尾完成（phase 非 complete）、或其他熔斷原因：不自動重啟
    remove_pending_entry(pending_path, account_id)
    notifier.critical("auto-activate",
        f"account={account_id} 出現 pending 但 state 有非終態的 kill switch 鎖定"
        f"（{state_root / ARM_FILE_RELPATH}），需人工處置後再啟用",
        dedup_key=f"auto-activate:tripped-pending:{account_id}")
    return "healed_no_start"
```

`exchange_dir` 需從 `run_once` 傳進 `process_entry`（run_once 已有該參數）。`_chown_tree`：對目錄與其下檔案 `os.chown` 成 `named_owner_ids(owner, group)`（rename 不改 owner，通常已是 filet-engine；仍統一做一次）。manifest 裡的 leader 若與新簽章不同，由引擎的 `LeaderWatch` 每輪消化 `leader_changes.json` 套用（既有機制），watcher 不改 manifest。

- [ ] 測試（沿既有 `test_filet_auto_activate.py` 的 fixture 與 `run_cmd` 替身）：(a) 在 manifest＋終態 ARM＋有簽章 → 回 `reactivated`、`systemctl start` 被呼叫、ARM 已搬進歸檔、result 標記與 owner_close.json 該帳號條目消失、pending 被清；(b) 終態 ARM 但無簽章 → `waiting_leader` 且 pending 保留、未 start；(c) ARM 為 drawdown → `healed_no_start`、critical 一則、未 start；(d) 既有「在 manifest、無 ARM」測試不變。
- [ ] `uv run pytest tests/test_filet_auto_activate.py -q` 綠。
- [ ] Commit：`feat: auto-activate 對 owner_close 終態帳號的重新跟單自動歸檔並重啟`

### Task 7 @inline：前端指引卡補「如何重新跟單」

**Files:**
- Modify: `web/src/lib/copy.ts`（ZH :1463 附近 `closeAllDone.steps`、EN :3038 附近）
- Test: `web/src/lib/copy.test.ts`（結構對稱既有測試）、`web/src/components/dashboard/StatusCard.test.tsx` 若存在

- `closeAllDone.steps` 末尾加一條：ZH「想再次跟單：到探索頁選定交易員並簽章即可，系統會自動重新啟用（先前的平倉紀錄會保留歸檔）」；EN "To follow again: pick a trader on Explore and sign — the engine restarts automatically (your previous close-out stays on record)."
- 確認 halted 狀態下探索／交易員頁的「跟單」按鈕沒有被前端擋掉（grep `state === "halted"`、`halted` 在 `web/src/app/traders`、`web/src/components` 的 follow 流程）；若有擋，改為允許並在回報說明。
- [ ] `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test` 全綠。
- [ ] Commit：`feat: 平倉並撤銷完成卡補充重新跟單方式`

### Task 8 @inline：文件

**Files:**
- Modify: `deploy/RUNBOOK.md:1460-1498`（「owner 收尾後的人工 re-arm 程序」整節改寫）
- Modify: `CLAUDE.md` 紅線 5 例外段

- RUNBOOK 新內容：(1) 引擎在 owner_close 終態自行結束（exit 0、unit inactive、最後一則 critical；殘留暴險明列於訊息、由用戶自行收尾），(2) 重新跟單全自動路徑（用戶重新選 leader → API 補 pending → watcher 歸檔＋清標記＋start → 引擎啟動提示歷史），歸檔目錄位置；若用戶已在 HL 移除 API wallet，READY 不過會回到 onboarding 重新授權，`onboard_verify` 寫 pending 後同樣走 watcher 路徑，(3) 舊的人工 re-arm 步驟 1-4 刪除，只留一句「ARM phase 停在 flatten_in_progress（收尾中途崩潰）才需人工：檢查帳戶後刪 ARM 檔重啟」，(4) 排障：`owner_close_archive/` 查歷史；`systemctl start` 一個仍有終態 ARM 的 unit 會只跑一輪就退出，屬預期。
- CLAUDE.md 紅線 5 例外追加一句：「2026-09-07：owner_close 後重新選 leader 的**重新啟用**亦走此自動路徑（API 重驗 READY、watcher 歸檔舊 ARM 後 start），同受對外開放前重審的約束。」
- [ ] Commit：`docs: owner_close 生命週期（自行結束、自動重新啟用）與 RUNBOOK 改寫`

### Task 9：全量驗證與審核（主線程）

- [ ] `uv run pytest -q` 全綠、`uv run ruff check src tests scripts` 乾淨、`cd web && npm test` 全綠（主線程親跑）。
- [ ] 派 `reviewer` 對照本檔審 `git diff main~N`，重點：(1) fail-open 方向——任何路徑會不會讓已撤銷的用戶在沒有新簽章時恢復交易；(2) 引擎退出與心跳/dashboard 一致性；(3) watcher 檔案權限。
- [ ] 部署（晨間檢查點，須使用者放行）：rsync 至正式機 → `systemctl restart filet-api`；watcher 每分鐘新進程自動用新碼；無需重啟任何 follower。既有 fbac652 錢包：unit 已 disabled、ARM 終態在檔——用戶重新選 leader 時新路徑會自動處理，無需人工。

## 狀態
- 2026-09-07：plan 完成，待使用者確認。
