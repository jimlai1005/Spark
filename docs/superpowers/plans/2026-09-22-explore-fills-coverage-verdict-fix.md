# Explore fills 覆蓋判準修正與吞吐重分配 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓成交量最大的帳戶不再被錯判為「不可能完整」，同時**不把錯判不完整換成錯判完整**——覆蓋結論只在三項證據同時具備時才成立；並把 fills 抓取吞吐從「83% 被增量輪吃掉」重新分配，使積壓在小時級而非週級收斂。

**Architecture:** 覆蓋結論改成「證據合成」而非「單點推論」：complete 需同時具備 (1) 窗口**左界**證據、(2) 分頁**無未解缺口**、(3) 游標抵達**固定的** `window_end`。左界證據來自一次獨立、可快取、對滾動窗口單調有效的留存邊界探測，存在 `fills_sync`（每地址一份），且是每次掃描的**第一個動作**——證據拿不到就不下結論，不是預設任一方向。預算耗盡、游標停滯、我方頁數上限一律只是「本系統停止回補」，不得產生完整性結論。吞吐面：增量週期依**近期成交速率**決定（不是只看名次），限流器子預算改成當前視窗內原子扣帳的可借用底線，父 cap 300 權重/分鐘不變。

**Tech Stack:** Python 3.11 + uv、SQLite（`/var/lib/filet-api/explore.db`）、pytest（全離線，autouse socket-ban）、systemd（`filet-api`）。

---

## ⚠️ 正式機狀態（2026-09-22）

**正式機上已有第一個跟單用戶，引擎正在跟單中。** 本次所有部署動作的第一約束是
「不得干擾 `filet-follower@*`」：

- follower 是獨立 systemd unit、**不經** `hl_budget` 限流器，重啟 `filet-api` 不會動到它——
  但兩者共用同一個對外 IP 的 HL 權重配額。本次 Task 6 的借用只在 `explore` 父 scope
  （300 權重/分鐘）**之內**重分配，全域上限 900（HL 為 1200）不變，follower 的餘裕分毫未動。
  這是硬不變式，Task 6 必須有測試釘死。
- 部署程序（Task 9）必須：部署前記錄 follower 基線、部署後確認 follower 未重啟且無新增失敗、
  觀察至少一個完整的 24 小時重排週期。
- 任何 429 或 follower 異常 → 立即回退，不等觀測期結束。

---

## 背景：根因與證據（2026-09-22 調查，builder 不必回頭讀對話）

現況（正式機 06:15 UTC 快照，`/home/ubuntu/explore-obs/samples.jsonl`）：300 個池內位址中
合格 75、資格待確認 145、不合格 80；覆蓋狀態 complete 97、partial 136、backfilling 67
——即 203 個位址在 `/explore` 顯示「分析待完成」。

### 根因 1（主因）：覆蓋判準建立在一個被證偽的上游前提

`explore_fills_sync.py:64-68` 依 HL 文件「`userFillsByTime` 只保留最近 10,000 筆」推出
`RETENTION_SAFETY_THRESHOLD = 8_000`；`:336-344` 在遍歷收尾時只要窗口內累計觀測筆數
≥ 門檻就判 `partial / retention_limit`。`:43` 的 `max_pages_per_round = 20` 也寫明理由是
「20 頁＝40,000 筆已超過留存上限」——同一個前提。

**實測推翻該前提**（2026-09-22，主線程自本機直接呼叫 HL public info API，唯讀）：
對 `0xbf732ea04197942783e34730ed6e0f6099575d58` 的 30 天窗口連續分頁，取回 **26,976 筆
不重複成交（14 頁）且尚未走完**。正式機 `fills_scan` 另有 8 個位址在同一窗口觀測到
13,000–18,000 筆，獨立佐證。

後果：成交筆數最多的帳戶被永久判為「不可能完整」；D14 契約規定 coverage ≠ complete 時
成交衍生欄位一律 null → 永遠「分析待完成」。且 `partial` 是吸收態，
`PARTIAL_RESCAN_AFTER_S`（24 小時）每天整窗重掃，重掃再得到同一個錯結論。

**但「門檻是錯的」不等於「筆數多就代表完整」。** 短頁收尾只證明「從游標起，上游不再給」，
不證明「窗口左段沒有被截掉」——若上游真的做了留存截斷，第一頁就會從較晚的時間開始，
而我們從頭到尾看不出來。本計畫的核心就是把這個盲區變成一個顯式、可觀測的證據項。

附帶缺陷：游標重疊沒有去重，`fills_in_window` 直接累加 `len(page)`，實測高估約 3.7%
（28,000 raw vs 26,976 unique）。門檻移除後它不再是判準輸入
（`grep -rn fills_in_window src/spark/publicapi/hl_explore.py src/spark/publicapi/explore_publisher.py`
無命中），降級為純觀測值，但仍需在文件註明它是上界不是精確值。

### 根因 2：fills 吞吐硬上限 60 頁/小時，其中 83% 被增量輪吃掉

一頁 `userFillsByTime` 權重 120（`hl_budget.py:79`），`explore_fills` 子預算 120／60 秒視窗
（`hl_budget.py:61`，正式機 `FILET_HL_EXPLORE_FILLS_WEIGHT_CAP=120`）→ 硬上限每分鐘 1 頁
＝ 60 頁/小時，實測 56。300 個位址每 6 小時一次增量（`FILET_EXPLORE_FILLS_PERIOD_S=21600`）
＝ 50 頁/小時。剩約 12 頁/小時要分給 65 個未完成遍歷 ＋ 131 個待核驗，平均每個位址 5 小時
才推進一頁（最老的遍歷 26 小時只做 1 頁）。

子 cap 是硬上限而非可借用的底線：`explore_base`（180）的實際需求**推估**約 120 權重/分鐘
（state 300/30min×2 ＋ portfolio 300/2h×20 ＋ ledger 300/2h×20），多出的額度 fills 借不到。
**此為推導值，Task 6 Step 0 必須實測後才定 floor。**

### 根因 3：今早 04:47 部署（7.9e）讓 131 列同時變灰

`04:45 complete 219 / pending 51` → `04:55 complete 92 / pending 153`。131 個 `fills_verify`
job 全排在 29–46 小時後才到期，且核驗只拿「每 9 頁 fills-like 給 1 頁」的輔助份額
（`explore_scheduler.py:142` `SPECIAL_SERVE_RATIO = 9`）→ 榜單將維持此狀態約 2.5 天。

---

## 使用者裁決

| 代號 | 決策 | 日期 |
|---|---|---|
| **D-A** | 覆蓋完整性改用**可觀測的邊界證據**：移除「筆數 ≥ 8,000 ⇒ partial」推論。 | 2026-09-22 |
| **D-B** | 吞吐：增量週期**分層** ＋ 子預算改為**可借用**的保留底線（父 cap 300 不變）。 | 2026-09-22 |
| **D-C** | 今早的 131 列：**提前核驗**，並暫時提高輔助份額。 | 2026-09-22 |
| **D-D** | 顯示契約（D14「coverage ≠ complete 則成交衍生欄位為 null」）**這次不改**。 | 2026-09-22 |
| **D-E** | **complete 需三項證據同時成立**：左界證據、分頁無未解缺口、抵達固定 `window_end`。預算耗盡與游標停滯不得當作完成。 | 2026-09-22（複審） |
| **D-F** | 語義限定：我方頁數上限只表示「本系統停止回補」，**不得**表述為上游留存不足；首次活動時間必須有可信來源與明確涵蓋範圍，**本機首次看到該地址的時間不算證據**；探測回空且證據不足 → 維持 **unknown**。 | 2026-09-22（複審） |
| **D-G** | 遷移只撤銷依賴舊判準的**結論**，保留原始 fills、去重資料與相容的游標；須可重跑、不重複建 job，並輸出遷移前後狀態分佈與待處理工作量。 | 2026-09-22（複審） |
| **D-H** | 週期不得只看名次：前 50 名以外的高頻地址不應一律延長到 24h；借用必須在當前預算視窗內原子扣帳，不得用平均閒置量放行，且不得讓 base 或 follower 飢餓。 | 2026-09-22（複審） |
| **D-I** | 驗收須重現 300 地址競爭負載（含增量、其他遍歷、verify、真實限流）；verify 加速設定**到期自動恢復**；部署前記錄基線、部署後至少觀察一個 24h 重排週期。 | 2026-09-22（複審） |

**D-D 與 D-E 的張力（已解）**：D-E 讓 complete 變嚴格，若左界證據排在事後取得，大戶會卡在
「遍歷完了但證據拿不到」而持續空白。解法是 Task 3 的**探測前置**：探測是掃描的第一個動作，
走 fills 預算（1 頁），不走輔助份額。證據對滾動窗口單調有效（見 Task 3），每地址一生一次。

---

## 覆蓋結論的單一判準（所有 task 的共同契約）

```python
def scan_verdict(scan: FillsScan, boundary: LeftBoundary) -> tuple[str, str]:
    """唯一的覆蓋結論來源。三項證據同時成立才 complete（D-E）。
    任何「我方停止」的情形都不得產生完整性結論（D-F）。"""
    if scan.unresolved_gap:
        return ("partial", "unresolved_gap")            # 分頁有未解缺口
    if scan.cursor_ms < scan.window_end_ms:
        return ("partial", scan.stop_reason)            # 沒抵達固定終點：本系統停止回補
    if boundary.state == "earlier_fills_seen":
        return ("complete", "left_boundary_verified")
    if boundary.state == "no_earlier_activity":
        return ("complete", "left_boundary_no_activity")
    if boundary.state == "truncation_suspected":
        return ("partial", "left_boundary_truncated")   # 上游截斷嫌疑
    return ("partial", "left_boundary_unknown")         # 證據不足：不宣稱完整，也不宣稱截斷
```

`stop_reason` 值域（**封閉集合，全部語義為「本系統停止」，不描述上游**）：
`local_page_cap`（達到我方單次掃描頁數硬上限 `MAX_PAGES_PER_SCAN`）、
`no_progress`（游標停滯）、`same_ms_overflow`（整頁同一毫秒，無法在不漏單的前提下前進）。

⚠️ **「本輪頁數用完」（`max_pages_per_round`）不在這個集合裡，也不得寫任何
`stop_reason`**（主線程裁決 2026-09-22，builder 第一版誤寫成 `stop_reason="page_cap"`）。
它是暫停不是停止：`result`、`stop_reason` 兩者皆維持 `None`，游標推進，scan 仍 running。
寫了的後果是 `scan_verdict` 每 `max_pages_per_round` 頁就對大戶吐出一次
`("partial", "page_cap")`——這正是 D-E 禁止的「預算耗盡當作結論」。
測試 `test_round_cap_pauses_without_any_verdict` 就是釘死這件事的。

---

## File Structure

| 檔案 | 本次責任 |
|---|---|
| `src/spark/publicapi/explore_fills_sync.py` | 純函式層：`apply_scan_page` 收尾、`scan_verdict`、週期函式、reason 常數。Task 1、2、5。 |
| `src/spark/publicapi/explore_store.py` | 持久化：`left_boundary` 三欄、`unresolved_gap`、schema v4 遷移、探測候選查詢。Task 1、3、4。 |
| `src/spark/publicapi/explore_scheduler.py` | 排程：探測前置、verdict 落地、週期單一來源、輔助份額。Task 1、3、5、8。 |
| `src/spark/publicapi/hl_budget.py` | 限流器：子 scope 改 floor＋當前視窗原子借用。Task 6。 |
| `src/spark/publicapi/config.py` | 新設定與 env。Task 5、6、8。 |
| `scripts/ops_expedite_verify.py`（新增） | 一次性運維（D-C），含自動到期。Task 8。 |
| `deploy/RUNBOOK.md` | §5.8f 第八次部署程序。Task 9。 |

**不得改動**：`src/spark/copytrade/`、`src/spark/filet/`、`/Users/jim/projects/hl-copytrader`
（唯讀紅線）、`web/`（D-D）。

---

## Task 1: 覆蓋結論改為三項證據合成 `@inline`

> **主線程裁決（2026-09-22，builder 回報 plan 缺口後）**：
> **Task 1 與 Task 2 合併為同一個 commit 邊界，不各自獨立驗收。**
> 原因：Task 1 讓 `apply_scan_page` 不再寫 `result`，而 `complete_scan` 有
> 「`result is None` 就 raise」的 7.9d-D 守門（刻意的），接線在 Task 2。
> 兩者之間必然有一段 `tests/test_explore_scheduler.py` 紅燈的中間態——這是本 plan
> 的切分錯誤，不是實作錯誤。Task 1 的驗收條件 2（scheduler 測試全綠）延後到 Task 2 結束時一併驗。
>
> 連帶修正 Task 1 驗收條件 4：`REASON_COUNT_BELOW_RETENTION_THRESHOLD` 等三個舊常數
> **在 Task 3 拆掉舊探測路徑之前不能刪**（`explore_scheduler.py` 仍在用），
> 保留並標註「僅供 v3 遷移與舊探測路徑，勿在新路徑使用」即可。原本要求「零命中」的寫法有誤。

**Files:**
- Modify: `src/spark/publicapi/explore_fills_sync.py:25-45`、`:64-68`、`:306-374`
- Modify: `src/spark/publicapi/explore_store.py:62-70`（reason 常數）
- Test: `tests/test_explore_fills_sync.py`

- [ ] **Step 1: 寫失敗測試——四種「不得判 complete」與兩種「可判 complete」**

```python
def test_short_page_alone_is_not_complete():
    """D-E：短頁只證明遍歷結束，左界證據不到位就不得宣稱完整。
    （這是本次事故的反向風險：把錯判不完整換成錯判完整。）"""
    scan = _scan(cursor_ms=9000, window_end_ms=9000, unresolved_gap=0)
    assert scan_verdict(scan, _boundary("unknown")) == ("partial", "left_boundary_unknown")


def test_complete_requires_left_boundary_evidence():
    scan = _scan(cursor_ms=9000, window_end_ms=9000, unresolved_gap=0)
    assert scan_verdict(scan, _boundary("earlier_fills_seen")) == (
        "complete", "left_boundary_verified")
    assert scan_verdict(scan, _boundary("no_earlier_activity")) == (
        "complete", "left_boundary_no_activity")


def test_unresolved_gap_beats_every_other_evidence():
    """分頁有未解缺口 → 不論左界證據多強都不是完整。"""
    scan = _scan(cursor_ms=9000, window_end_ms=9000, unresolved_gap=1)
    assert scan_verdict(scan, _boundary("earlier_fills_seen")) == ("partial", "unresolved_gap")


def test_local_stop_never_yields_completeness():
    """D-F：我方停止回補（頁數上限／游標停滯／同毫秒溢位）一律不是完整，
    且 reason 必須描述我方行為，不得描述上游留存。"""
    for stop in ("local_page_cap", "no_progress", "same_ms_overflow"):
        scan = _scan(cursor_ms=5000, window_end_ms=9000, unresolved_gap=0, stop_reason=stop)
        state, reason = scan_verdict(scan, _boundary("earlier_fills_seen"))
        assert (state, reason) == ("partial", stop)
        assert "retention" not in reason        # 不得把我方上限說成上游留存


def test_truncation_suspected_is_partial_not_complete():
    scan = _scan(cursor_ms=9000, window_end_ms=9000, unresolved_gap=0)
    assert scan_verdict(scan, _boundary("truncation_suspected")) == (
        "partial", "left_boundary_truncated")


def test_apply_scan_page_short_page_does_not_decide_completeness():
    """收尾函式只負責「遍歷到此結束」，結論交給 scan_verdict。"""
    plan = ScanPlan(start_ms=1000, end_ms=9000, scan=_scan(fills_in_window=50_000))
    result = apply_scan_page(plan, [_fill(2000, 1)], page_limit=3, now_ms=NOW)
    assert result.done is True
    assert result.scan.cursor_ms == 9000          # 抵達固定終點
    assert result.scan.result is None             # 不在這裡下結論
```

`_boundary(state)` 是新 helper，回傳 `LeftBoundary(state=state, window_start_ms=1000, at=NOW)`。

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_fills_sync.py -k "short_page_alone or left_boundary or unresolved_gap or local_stop or truncation_suspected or does_not_decide" -v`
Expected: FAIL，`ImportError: cannot import name 'scan_verdict'`。

- [ ] **Step 3: 新增型別與 reason 常數**

`explore_store.py` 的 reason 區塊：**刪除** `REASON_COUNT_BELOW_RETENTION_THRESHOLD`，
新增（並加日期註記 `<!-- 2026-09-22 D-A/D-E：門檻推論移除，改證據合成 -->`）：

```python
REASON_LEFT_BOUNDARY_VERIFIED = "left_boundary_verified"        # 探測到窗口起點之前仍有可查成交
REASON_LEFT_BOUNDARY_NO_ACTIVITY = "left_boundary_no_activity"  # 帳戶在窗口起點前確無活動
REASON_LEFT_BOUNDARY_TRUNCATED = "left_boundary_truncated"      # 上游截斷嫌疑
REASON_LEFT_BOUNDARY_UNKNOWN = "left_boundary_unknown"          # 證據不足，不下結論
REASON_UNRESOLVED_GAP = "unresolved_gap"                        # 分頁有未解缺口
```

`REASON_RETENTION_BOUNDARY_VERIFIED` 保留為**歷史值**（遷移會改寫成新名，見 Task 4），
但不再被新程式碼寫入；在常數旁註明「僅供 v3 遷移讀取，勿在新路徑使用」。

`explore_fills_sync.py` 新增：

```python
@dataclasses.dataclass(frozen=True)
class LeftBoundary:
    """窗口左界證據。`state` 值域見 `scan_verdict`；`window_start_ms` 是這份
    證據適用的窗口起點（單調性見 Task 3 docstring）；`at` 是取得時間。"""
    state: str
    window_start_ms: int | None
    at: float | None
```

並實作上面「覆蓋結論的單一判準」那段 `scan_verdict`。

- [ ] **Step 4: 移除門檻，`apply_scan_page` 不再下結論**

刪除 `HL_FILLS_RETENTION_LIMIT` / `RETENTION_SAFETY_MARGIN` / `RETENTION_SAFETY_THRESHOLD`
三個常數與 `apply_scan_page` 的 `retention_threshold` 參數。短頁分支改為：

```python
    if len(page) < page_limit:
        # D-E：短頁只代表「從游標起上游不再給」。游標推進到固定終點，
        # 結論交給 scan_verdict（需左界證據與無缺口才可能 complete）。
        new_scan = dataclasses.replace(
            scan, cursor_ms=end_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, result=None, reason=None, last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True,
                              note="reached_window_end")
```

`no_progress` / `same_ms_overflow` 兩個分支改成寫 `stop_reason`（新欄位）而非 `result`：
`dataclasses.replace(scan, stop_reason="no_progress", ...)`，`result` 維持 `None`。
`same_ms_overflow` 另外設 `unresolved_gap=1`（整頁同毫秒代表我們無法在不漏單的前提下
前進，是真正的缺口）。

模組 docstring `:25-45` 整段改寫：說明門檻推論已被實測推翻（附本 plan 路徑），
並寫明新判準的三項證據與「我方停止 ≠ 上游沒有」的語義界線。

- [ ] **Step 5: 修既有測試**

移除所有 `retention_threshold=` 參數（:120、:132、:142、:154、:169、:178、:194、:207、
:227、:245、:251、:256、:261）；刪除 `test_retention_constants_named_and_derived`（:52）
與 `test_apply_scan_page_short_page_over_retention_threshold_is_partial`（:165）。
`grep -rn "count_below_retention_threshold\|RETENTION_SAFETY" src/ tests/` 應只剩
Task 4 遷移那一處。

- [ ] **Step 6: 跑測試**

Run: `uv run pytest tests/test_explore_fills_sync.py -v`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add src/spark/publicapi/explore_fills_sync.py src/spark/publicapi/explore_store.py tests/test_explore_fills_sync.py
git commit -m "fix: 覆蓋結論改為左界證據＋無缺口＋抵達終點三項合成（D-A／D-E）"
```

---

## Task 2: 頁數上限與停止語義 `@inline`

**Files:**
- Modify: `src/spark/publicapi/explore_fills_sync.py`（page cap 分支）
- Modify: `src/spark/publicapi/explore_scheduler.py:1079-1120`（收尾分派）
- Test: `tests/test_explore_fills_sync.py`、`tests/test_explore_scheduler.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_round_cap_pauses_without_any_verdict():
    """每輪預算耗盡不是結論（D-E）：本輪結束、游標推進、result 與 stop_reason 皆 None。"""
    scan = _scan(pages_done=2, cursor_ms=1000)
    plan = ScanPlan(start_ms=1000, end_ms=9000, scan=scan)
    result = apply_scan_page(plan, [_fill(1000, 1), _fill(2000, 2)],
                             page_limit=2, max_pages_per_round=3, now_ms=NOW)
    assert result.done is True
    assert result.scan.result is None and result.scan.stop_reason is None
    assert result.scan.cursor_ms == 2000


def test_absolute_cap_is_a_local_stop_not_an_upstream_claim():
    """D-F：400 頁只表示本系統停止回補。reason 必須是 local_page_cap，
    且不得出現任何 retention 字樣（那是對上游的主張，我們沒有證據）。"""
    scan = _scan(pages_done=MAX_PAGES_PER_SCAN - 1, cursor_ms=1000)
    plan = ScanPlan(start_ms=1000, end_ms=9000, scan=scan)
    result = apply_scan_page(plan, [_fill(1000, 1), _fill(2000, 2)],
                             page_limit=2, max_pages_per_round=3, now_ms=NOW)
    assert result.done is True
    assert result.scan.stop_reason == "local_page_cap"
    assert result.scan.result is None                     # 結論仍由 scan_verdict 給
    assert "retention" not in (result.scan.stop_reason or "")
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_fills_sync.py -k "round_cap_pauses or absolute_cap" -v`
Expected: FAIL，`NameError: MAX_PAGES_PER_SCAN`。

- [ ] **Step 3: 實作**

```python
# D-A／D-F（2026-09-22）：單輪頁數上限只決定「本輪做到哪」；MAX_PAGES_PER_SCAN 是
# **我方**的回補上限（防止單一地址無上限佔用額度），達到時的語義是「本系統停止回補」，
# 不是「上游沒有更多資料」——我們沒有任何證據支持後者。實測大戶 30 天窗口約需 18 頁。
MAX_PAGES_PER_SCAN = 400
```

page cap 分支寫 `stop_reason="local_page_cap"`、`result=None`；
`new_pages_done % max_pages_per_round == 0` 分支只推進游標、`result`／`stop_reason` 皆 None。

- [ ] **Step 4: 收尾分派——三條路徑要分清楚**

`explore_scheduler.py` 的 `_run_scan`，在 `finished_scan = ...` 之前插入：

```python
        if res.done and res.scan.result is None and res.scan.stop_reason is None:
            # (a) 本輪頁數用完：scan 仍 running，不寫結論、不排重掃。
            #     重排時間與續頁路徑同源（7.8 教訓：到期條件與重排時間必須同一來源）。
            self._store.insert_scan_page(job.address, res.accepted, res.scan)
            self._reschedule(job, now, bump_attempts=False)
            self._notify_dirty()
            return f"ran:{result_kind}"
```

(b) 抵達終點或有 `stop_reason` → 取左界證據、算結論、落地：

```python
        boundary = self._store.get_left_boundary(job.address, res.scan.window_start_ms)
        completeness, reason = scan_verdict(res.scan, boundary)
        finished_scan = dataclasses.replace(
            res.scan, finished_at=now, result=completeness, reason=reason)
```

其餘 `complete_scan` / `ScanWriteback` 處理維持原樣。

- [ ] **Step 5: 跑測試**

Run: `uv run pytest tests/test_explore_fills_sync.py tests/test_explore_scheduler.py -v`
Expected: PASS。既有
`test_apply_scan_page_hits_page_cap_on_20th_consecutive_full_page`（:218）改寫為斷言
`result.scan.result is None`、`stop_reason is None`（20 頁只是本輪上限）。

- [ ] **Step 6: Commit**

```bash
git add src/spark/publicapi/explore_fills_sync.py src/spark/publicapi/explore_scheduler.py tests/
git commit -m "fix: 頁數上限改為本輪暫停／我方停止語義，不產生上游主張（D-E／D-F）"
```

---

## Task 3: 左界證據——探測前置、可快取、單調有效 `@inline`

**Files:**
- Modify: `src/spark/publicapi/explore_store.py`（`left_boundary` 三欄、讀寫、候選查詢）
- Modify: `src/spark/publicapi/explore_scheduler.py:1164-1229`（`_run_probe`）、領工分派
- Test: `tests/test_explore_scheduler.py`、`tests/test_explore_store.py`

### 設計要點（builder 必讀）

**為什麼前置**：左界證據是 complete 的必要條件（D-E）。若證據排在遍歷之後、又走輔助份額
（每 9 頁 fills 才給 1 頁），大戶會「遍歷完 18 頁卻卡在最後 1 頁證據」而持續空白。
因此探測是掃描的**第一個動作**：`left_boundary` 為 `unknown` 或不適用當前窗口時，
`fills_scan` job 的下一個動作是探測（走 `explore_fills` 預算，1 頁），不是抓頁。

**為什麼存在 `fills_sync`（每地址）而不是每次掃描**：證據對滾動窗口具**單調性**——
「窗口起點之前仍有可查成交」這件事，在窗口起點往前滾之後只會更成立（新的起點更晚，
更早的成交只會更多）。`no_earlier_activity`（帳戶在舊起點前無活動）在窗口往前滾後，
新起點前可能出現的是我們自己已抓到的成交，仍不構成截斷。所以**正面證據一旦取得就永久有效**，
每個地址一生只需要一次探測；只有 `truncation_suspected` 與 `unknown` 需要重探。
這是本設計不會變成新容量黑洞的關鍵。

**首次活動時間的來源與涵蓋範圍（D-F）**：只接受 HL `portfolio` 回應的 `allTime` 序列首點
（與 `hl_explore.py` 算 `live_days` 的同一個來源，工程原則 1：同源同基準）。
**明確不接受**：`candidate.source_as_of`、`candidate.last_seen_at`、`endpoint_cache.fetched_at`
——那些是「本機第一次看到這個地址」，不是帳戶年齡。
涵蓋範圍限制（必須寫進 docstring）：allTime 是**權益**歷史且經降採樣（見既有教訓
`hl-portfolio-series-traps`），首點不等於首筆成交，時間粒度可能到天。因此只在它
**明顯**早於或晚於窗口時採信，落在模糊帶一律 `unknown`。

- [ ] **Step 1: 寫失敗測試**

```python
def test_probe_runs_before_first_page_of_a_new_scan():
    """探測前置：新建的 scan 第一個動作是探測，不是抓頁。"""
    store = _store_with(address=ADDR, left_boundary="unknown")
    sched = _scheduler(store, fills_pages=[[_fill(EARLIER_MS, 1)]])
    sched._run_scan(_scan_job(ADDR), now=NOW)
    assert sched.probes_total == 1
    assert sched.scan_pages_total == 0
    assert store.get_left_boundary(ADDR, WINDOW_START).state == "earlier_fills_seen"


def test_positive_boundary_evidence_survives_window_roll_forward():
    """單調性：窗口往前滾之後，正面證據仍適用，不得重探。"""
    store = _store_with(address=ADDR, left_boundary="earlier_fills_seen",
                        left_boundary_window_start_ms=WINDOW_START)
    later = WINDOW_START + 7 * DAY
    assert store.get_left_boundary(ADDR, later).state == "earlier_fills_seen"
    assert store.next_probe_candidate() is None


def test_probe_empty_with_clearly_older_account_is_truncation_suspected():
    store = _store_with(address=ADDR, portfolio_first_activity_ms=WINDOW_START - 30 * DAY)
    sched = _scheduler(store, fills_pages=[[]])
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert store.get_left_boundary(ADDR, WINDOW_START).state == "truncation_suspected"


def test_probe_empty_with_clearly_newer_account_is_no_earlier_activity():
    store = _store_with(address=ADDR, portfolio_first_activity_ms=WINDOW_START + 3 * DAY)
    sched = _scheduler(store, fills_pages=[[]])
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert store.get_left_boundary(ADDR, WINDOW_START).state == "no_earlier_activity"


def test_probe_empty_in_ambiguous_band_stays_unknown():
    """帳戶首次活動落在探測窗內（理應探得到卻回空）＝資料互相矛盾 → 維持 unknown。"""
    store = _store_with(address=ADDR, portfolio_first_activity_ms=WINDOW_START - 12 * 3600_000)
    sched = _scheduler(store, fills_pages=[[]])
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert store.get_left_boundary(ADDR, WINDOW_START).state == "unknown"


def test_probe_never_uses_local_first_seen_as_evidence():
    """D-F：沒有 portfolio 就是不知道；不得拿 candidate.last_seen_at 頂替。"""
    store = _store_with(address=ADDR, portfolio_first_activity_ms=None,
                        candidate_last_seen_at=NOW - 400 * DAY / 1000)
    sched = _scheduler(store, fills_pages=[[]])
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert store.get_left_boundary(ADDR, WINDOW_START).state == "unknown"
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_scheduler.py -k "probe" -v`
Expected: FAIL。

- [ ] **Step 3: store 層：三欄 ＋ 讀寫 ＋ 首次活動時間**

`fills_sync` 新增（schema v4，Task 4 的遷移負責 ALTER）：
`left_boundary TEXT NOT NULL DEFAULT 'unknown'`、`left_boundary_window_start_ms INTEGER`、
`left_boundary_at REAL`。`fills_scan` 新增 `stop_reason TEXT`、`unresolved_gap INTEGER NOT NULL DEFAULT 0`。

```python
    def get_left_boundary(self, address: str, window_start_ms: int) -> LeftBoundary:
        """回傳適用於 `window_start_ms` 的左界證據。單調性（見 Task 3 設計要點）：
        正面證據（earlier_fills_seen／no_earlier_activity）在其取得時的窗口起點
        **不晚於**查詢窗口起點時仍然適用；否則視為 unknown（需重探）。
        truncation_suspected 不具單調性，一律只對取得時的窗口起點有效。"""

    def set_left_boundary(self, address: str, state: str, window_start_ms: int,
                          at: float) -> bool:
        """冪等寫入。只允許 unknown → 其他；已有正面證據時不得被 unknown 覆蓋
        （證據只能加強，不能被無證據抹掉）。"""

    def first_activity_ms(self, address: str) -> int | None:
        """帳戶首次活動時間＝快取 portfolio 的 allTime 序列首點。
        來源與涵蓋範圍限制見 Task 3 設計要點；任何取不到／結構不符一律回 None＝未知。
        **不得**改用 candidate.last_seen_at／source_as_of 之類的本機時間。"""
```

⚠️ 實作 `first_activity_ms` 前先讀 `hl_explore.py` 解析 portfolio 的那段
（`grep -n "allTime" src/spark/publicapi/hl_explore.py`），欄位名以該處為準——
工程原則 1：「欄位名是假設，不是事實」，未經真實 payload 驗證的欄位名與未驗證的公式同等可疑。

- [ ] **Step 4: scheduler 層：探測前置與結論分支**

`_run_scan` 開頭，取得／建立 scan 之後、`plan_scan` 之前插入：

```python
        boundary = self._store.get_left_boundary(job.address, window_start_ms)
        if boundary.state == "unknown":
            # 探測前置（D-E）：左界證據是 complete 的必要條件，必須在結論之前到手。
            # 走 explore_fills 預算而非輔助份額——輔助份額留給 fills_verify。
            if not self._run_probe((job.address, scan.scan_id), now):
                self._reschedule(job, now, bump_attempts=False)   # 額度不足：下個 tick 再試
                return "deferred"
            return f"ran:{result_kind}"
```

`_run_probe` 的結論分支改為：

```python
        if page:
            state = "earlier_fills_seen"
        else:
            first_ms = self._store.first_activity_ms(address)
            if first_ms is None:
                state = "unknown"                                  # D-F：不知道就是不知道
            elif first_ms >= scan.window_start_ms:
                state = "no_earlier_activity"
            elif first_ms < scan.window_start_ms - _PROBE_WINDOW_MS:
                state = "truncation_suspected"
            else:
                state = "unknown"                                  # 模糊帶：證據互相矛盾
        self._store.set_left_boundary(address, state, scan.window_start_ms, now)
```

`_run_probe` 不再呼叫 `apply_probe_result`（該函式與 `old_reason` 參數一併移除——
結論已改由 `scan_verdict` 統一產生，留著就是第二個結論來源，違反工程原則 1）。

⚠️ **候選查詢兩處**：`next_probe_candidate`（`explore_store.py:1240-1247`）與
`count_probe_candidates`（`:1250` 起，同一份條件複製了一份）目前都以
`sc.reason = REASON_COUNT_BELOW_RETENTION_THRESHOLD AND s.evidence_unknown = 0` 挑候選。
該 reason 已刪除、且 `evidence_unknown=1` 的列會被排除在外——**兩個條件都要改**，
否則探測永遠挑不到人，整個 Task 3 等於沒上線。新條件：
`s.left_boundary = 'unknown'`（或適用窗口不符）`AND c.active = 1`。
兩份查詢收斂成同一個 SQL 片段常數（工程原則 1）。
驗證：`grep -c "count_below_retention_threshold" src/spark/publicapi/*.py` 應為 0。

- [ ] **Step 5: 拆掉 Task 2 留下的三處 monkeypatch 掩體（必做）**

Task 2 期間 `get_left_boundary` 是恆回 `unknown` 的佔位，有三條既有測試改用
monkeypatch 注入假的正面證據，才能測到它們原本的不變式：

- `tests/test_explore_scheduler.py` `test_s7a_complete_address_never_rescans_across_candidate_rounds`
- `tests/test_explore_scheduler.py` `test_s7d_repro_rescan_three_day_thirty_minute_rounds_no_repeated_full_scan`
- `tests/test_explore_scheduler.py` `test_w2_deferred_verify_job_is_dropped_once_evidence_is_filled_in`

真實實作落地後，**這三處 monkeypatch 必須移除**，改成在 harness 裡餵一個真的會讓探測
回到 `earlier_fills_seen` 的上游回應。理由：monkeypatch 繞過的正是本次要驗證的那條路徑，
留著就等於「程式改了但正式流程永遠走不到」還測得過——這是使用者在複審時特別點名的風險。

Run: `grep -n "get_left_boundary = lambda" tests/`
Expected: 無命中。

- [ ] **Step 6: 跑測試**

Run: `uv run pytest tests/test_explore_scheduler.py tests/test_explore_store.py -v`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add src/spark/publicapi/explore_scheduler.py src/spark/publicapi/explore_store.py tests/
git commit -m "feat: 左界證據前置、可快取、對滾動窗口單調有效；未知不下結論（D-E／D-F）"
```

---

## Task 3b: 證據到齊時重算結論，不靠整窗重掃 `@inline`

> **主線程裁決（2026-09-22，Task 3 完成後複查發現）**：Task 3 之後，`scan_verdict`
> 全專案只在 `_run_scan` 的遍歷收尾被呼叫一次。若左界證據是**事後**才由獨立探測路徑
> （`next_probe_candidate`）解出的，`fills_sync` 的結論不會重算——該位址會一直停在
> `partial / left_boundary_unknown`，直到 24 小時後的 `partial_rescan` 整窗重掃。
>
> **為什麼非修不可**：Task 4 遷移後約 165 個位址（基線表的 152 + 13 列）正是這個狀態
> ——遍歷資料都在、只差 1 頁探測。沒有重算機制的話，它們每一個都要付一次**整窗重掃**
> （大戶 18 頁以上）才能翻身，合計數百頁；有重算機制則是 165 頁探測 ＋ 零額外遍歷。
> 以實測 60 頁/小時的上限計，差距是「約 3 小時」對「8–13 小時且排擠其他所有工作」。
> 這正是 D-G 要求「避免再次大量變灰卻沒有處理容量」所指的情形。
> 順帶也修好「探測暫時失敗（429／連線錯誤）→ 寫入 unknown → 該窗口永久放棄證據」這條路徑
> （工程原則 2：暫時性失敗不得轉成永久結論）。

**Files:**
- Modify: `src/spark/publicapi/explore_store.py`（`set_left_boundary` 的轉移規則與重算）
- Modify: `src/spark/publicapi/explore_scheduler.py`（探測成功解出證據後觸發重算）
- Test: `tests/test_explore_store.py`、`tests/test_explore_scheduler.py`

**不變式（不可違反）**：
1. 覆蓋結論**只能**由 `scan_verdict` 產生（工程原則 1：一個判斷不留兩個來源）。
   重算是「拿同一筆已完成的 scan ＋ 新的 `LeftBoundary` 再跑一次 `scan_verdict`」，
   不是另寫一套升級邏輯。
2. 只對**當前生效**的那一次遍歷重算：`fills_scan.scan_id == fills_sync.scan_id`
   且 `status='done'`。被取代（stale）或進行中的 scan 不得參與。
3. **不得重新遍歷**。重算完全不發任何上游請求。
4. 正面證據（`earlier_fills_seen`／`no_earlier_activity`）是終局：不得被任何後續寫入
   覆蓋（現行 `set_left_boundary` 只擋了 `unknown`，`truncation_suspected` 也要擋）。

- [ ] **Step 1: 寫失敗測試**

```python
def test_verdict_is_recomputed_when_evidence_arrives_later():
    """探測事後解出正面證據 → 同一筆已完成的遍歷立刻重算成 complete，
    不需要整窗重掃（D-G 的容量要求）。"""
    store = _store_with_done_scan(address=ADDR, cursor_ms=WINDOW_END, window_end_ms=WINDOW_END,
                                  unresolved_gap=0, completeness="partial",
                                  reason="left_boundary_unknown", left_boundary="unknown")
    assert store.set_left_boundary(ADDR, "earlier_fills_seen", WINDOW_START, NOW) is True
    sync = store.get_sync(ADDR)
    assert (sync.completeness, sync.reason) == ("complete", "left_boundary_verified")


def test_recompute_never_touches_a_superseded_scan():
    """不是當前生效的那次遍歷（scan_id 不符）→ 不重算。"""
    store = _store_with_done_scan(address=ADDR, scan_id="old", sync_scan_id="new", ...)
    store.set_left_boundary(ADDR, "earlier_fills_seen", WINDOW_START, NOW)
    assert store.get_sync(ADDR).completeness == "partial"


def test_recompute_respects_unresolved_gap():
    """有未解缺口時，再強的左界證據也不能翻成 complete。"""
    store = _store_with_done_scan(address=ADDR, unresolved_gap=1, completeness="partial",
                                  reason="unresolved_gap", left_boundary="unknown")
    store.set_left_boundary(ADDR, "earlier_fills_seen", WINDOW_START, NOW)
    assert store.get_sync(ADDR).reason == "unresolved_gap"


def test_positive_evidence_is_terminal():
    """正面證據不得被 unknown 或 truncation_suspected 覆蓋。"""
    store = _store_with(address=ADDR, left_boundary="earlier_fills_seen")
    assert store.set_left_boundary(ADDR, "unknown", WINDOW_START, NOW) is False
    assert store.set_left_boundary(ADDR, "truncation_suspected", WINDOW_START, NOW) is False
    assert store.get_left_boundary(ADDR, WINDOW_START).state == "earlier_fills_seen"


def test_transient_probe_failure_does_not_forfeit_the_window():
    """429／連線錯誤寫入 unknown 之後，該位址必須仍是探測候選（可重試），
    不得因為「這個窗口已嘗試過」而永久放棄（工程原則 2）。"""
    store = _store_with_done_scan(address=ADDR, left_boundary="unknown")
    store.set_left_boundary(ADDR, "unknown", WINDOW_START, NOW)
    assert store.next_probe_candidate() is not None


def test_recomputed_complete_drops_the_pending_partial_rescan_job():
    """重算成 complete 之後，原本排著的整窗重掃要因狀態推導而被丟棄，
    不得留下一次沒必要的 18 頁重掃。"""
    h = _harness(); h.seed_partial_with_pending_rescan(ADDR)
    h.resolve_probe(ADDR, "earlier_fills_seen")
    h.run_for(minutes=5)
    assert h.job_kinds(ADDR) == set()
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_store.py tests/test_explore_scheduler.py -k "recompute or evidence_arrives or positive_evidence_is_terminal or transient_probe_failure" -v`
Expected: FAIL。

- [ ] **Step 3: 實作**

`set_left_boundary` 在成功寫入且**新狀態不是 `unknown`** 時，於**同一個 transaction 內**：
讀 `fills_sync.scan_id` 指向的那筆 `fills_scan`（須 `status='done'`），用它與新的
`LeftBoundary` 呼叫 `scan_verdict`，把結果寫回 `fills_sync.completeness/reason`。
沒有相符的 scan、scan 未完成、或算出來與現值相同 → 不寫。

轉移規則收緊為：正面證據是終局（任何覆蓋一律拒絕並回 `False`）；
`unknown` → 任何狀態可；`truncation_suspected` → 只能轉成正面證據。

⚠️ `explore_store` import `scan_verdict` 會形成 store→fills_sync 的相依。先確認
`explore_fills_sync` 沒有 import `explore_store` 的執行期相依（目前只 import 常數，
若會造成循環 import，改成由 `explore_scheduler` 在探測成功後呼叫一個
`store.recompute_verdict(address, boundary, verdict_fn)` 之類的注入形式——
**但結論仍只能出自 `scan_verdict`**，不得在 store 裡重寫一套判斷）。

- [ ] **Step 4: 跑測試**

Run: `uv run pytest -q`
Expected: 全綠。

Run: `uv run ruff check src tests`
Expected: 無錯誤。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/explore_store.py src/spark/publicapi/explore_scheduler.py tests/
git commit -m "fix: 左界證據事後到齊時就地重算結論，不必整窗重掃；正面證據終局化（D-G）"
```

---

## Task 4: schema v4 遷移——只撤銷結論，保留抓取進度 `@inline`

**Files:**
- Modify: `src/spark/publicapi/explore_store.py:98`、`:383-392`、新增 `_migrate_v3_to_v4`
- Test: `tests/test_explore_store.py`

**原則（D-G）**：`fills` 原始成交列**一筆都不刪**；`fills_scan` 的游標、`pages_done`、
`observed_*` 在窗口與 `params_fp` 相容時**全部保留**；只清掉依賴舊判準的**結論欄位**。

正式機現況（2026-09-22 06:10 UTC 實測）與遷移後歸屬：

| 現況 | 列數 | 遷移動作 | 遷移後 |
|---|---|---|---|
| `complete` / `retention_boundary_verified`（evidence_unknown=0） | 131 | 探測已見更早成交＝新判準的正面證據，**結論保留** | `complete` / `left_boundary_verified`，`left_boundary='earlier_fills_seen'` |
| `complete` / `count_below_retention_threshold_probe_empty`（evidence_unknown=1） | 152 | 舊的「探測回空」沒有帳戶年齡佐證＝新判準的 unknown。**已遍歷過，游標保留**，只清結論 | `partial` / `left_boundary_unknown`（對外本來就是 partial，**不新增變灰**） |
| `complete` / `count_below_retention_threshold` | 6 | 純門檻推論，結論作廢；游標保留 | `partial` / `left_boundary_unknown` |
| `partial` / `retention_limit` | 10 | 被錯門檻中斷，結論作廢；**游標保留**，續抓 | `backfilling`，`result=NULL` |
| `backfilling`（進行中） | 95 | 不動結論（本來就沒有）；`left_boundary='unknown'` | `backfilling` |

新增變灰只有 6 列（`count_below_retention_threshold`）。**不得**沿用早期草案的
「running scan 一律作廢重跑」——那會丟掉 118 頁已付出的抓取成本。

- [ ] **Step 0: 接上兩個欄位的持久化，並刪掉 Task 3b 的反推（必做）**

Task 3b 期間 `FillsScan.stop_reason`／`unresolved_gap` 還沒持久化，
`ExploreStore._recompute_verdict_locked` 用 `reason`／`cursor_ms`／`window_end_ms` **反推**
這兩欄。那個反推是「給定 `scan_verdict` 目前的分支順序才成立」的隱性耦合——
有人重排分支就會靜默壞掉，且現有測試抓不到（工程原則 5：別用「要記得」來維持正確性）。

本 task 把兩欄真正接進 `_SCAN_COLUMNS` 的讀寫之後，**必須刪除該反推**，改直接用持久化的值。

Run: `grep -n "unresolved_gap = 1 if\|stop_reason = scan.reason if" src/spark/publicapi/explore_store.py`
Expected: 無命中。

另補一條測試，釘死「往返不失真」：
```python
def test_scan_round_trip_preserves_stop_reason_and_unresolved_gap():
    for stop, gap in (("local_page_cap", 0), ("no_progress", 0), ("same_ms_overflow", 1), (None, 0)):
        scan = _scan(stop_reason=stop, unresolved_gap=gap)
        store.upsert_scan(scan)
        got = store.get_scan(scan.scan_id)
        assert (got.stop_reason, got.unresolved_gap) == (stop, gap)
```

- [ ] **Step 1: 寫失敗測試**

```python
def test_migrate_v3_to_v4_revokes_verdicts_but_keeps_progress():
    db = _v3_db_with(
        sync_rows=[("0xaa", "complete", "count_below_retention_threshold", 1),
                   ("0xbb", "complete", "count_below_retention_threshold_probe_empty", 1),
                   ("0xcc", "complete", "retention_boundary_verified", 0),
                   ("0xdd", "partial", "retention_limit", 0)],
        scan_rows=[("0xdd", "done", "partial", "retention_limit", 5, CURSOR_MS)],
        fills_rows=[("0xdd", 1, 111), ("0xdd", 2, 222)])
    store = ExploreStore(db)
    assert store.schema_version() == 4
    assert store.get_sync("0xaa").completeness == "partial"
    assert store.get_sync("0xaa").reason == "left_boundary_unknown"
    assert store.get_sync("0xbb").completeness == "partial"
    assert store.get_sync("0xcc").completeness == "complete"          # 正面證據保留
    assert store.get_left_boundary("0xcc", WINDOW_START).state == "earlier_fills_seen"
    assert store.get_sync("0xdd").completeness == "backfilling"
    assert store.get_scan_for("0xdd").cursor_ms == CURSOR_MS          # 游標保留
    assert store.count_fills("0xdd") == 2                             # 原始成交一筆不少


def test_migrate_v3_to_v4_is_rerunnable_and_creates_no_jobs():
    db = _v3_db_with(...)                       # 同上
    before_jobs = _job_rows(db)
    ExploreStore(db); ExploreStore(db); ExploreStore(db)
    assert _job_rows(db) == before_jobs         # 遷移本身不建任何 job
    assert _sync_rows(db) == _sync_rows_after_first_migration


def test_migrate_v3_to_v4_reports_workload():
    """D-G：遷移必須輸出遷移前後狀態分佈與待處理工作量，
    避免再次大量變灰卻沒有處理容量。"""
    report = ExploreStore(_v3_db_with(...)).last_migration_report()
    assert report["before"]["complete"] == 3
    assert report["after"]["partial"] == 2
    assert report["work"]["probes_needed"] == 3
    assert report["work"]["scans_to_resume"] == 1
    assert report["work"]["verify_needed"] == 0
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_store.py -k migrate_v3_to_v4 -v`
Expected: FAIL，`assert 3 == 4`。

- [ ] **Step 3: 實作遷移**

`_SCHEMA_VERSION = 4`。`_migrate_v3_to_v4` 比照 `_migrate_v2_to_v3`（`:476`）的
**原子＋冪等**寫法（顯式 transaction、版本更新在同一個 transaction 內）：

```sql
ALTER TABLE fills_sync ADD COLUMN left_boundary TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE fills_sync ADD COLUMN left_boundary_window_start_ms INTEGER;
ALTER TABLE fills_sync ADD COLUMN left_boundary_at REAL;
ALTER TABLE fills_scan ADD COLUMN stop_reason TEXT;
ALTER TABLE fills_scan ADD COLUMN unresolved_gap INTEGER NOT NULL DEFAULT 0;

-- 正面證據：探測已見更早成交，直接成為新判準的左界證據（結論保留）
UPDATE fills_sync SET left_boundary='earlier_fills_seen',
       left_boundary_window_start_ms=(SELECT sc.window_start_ms FROM fills_scan sc
                                      WHERE sc.scan_id = fills_sync.scan_id),
       left_boundary_at=:now, reason='left_boundary_verified'
 WHERE reason='retention_boundary_verified' AND completeness='complete';

-- 門檻推論與無佐證的探測回空：撤銷結論，保留游標與 fills
UPDATE fills_sync SET completeness='partial', reason='left_boundary_unknown',
       left_boundary='unknown', evidence_unknown=0
 WHERE reason IN ('count_below_retention_threshold',
                  'count_below_retention_threshold_probe_empty');

-- 被錯門檻中斷的遍歷：結論作廢、回到續抓（游標不動）
UPDATE fills_sync SET completeness='backfilling', reason=NULL, evidence_unknown=0
 WHERE reason='retention_limit';
UPDATE fills_scan SET status='running', result=NULL, reason=NULL, finished_at=NULL
 WHERE result='partial' AND reason='retention_limit';
```

`ALTER TABLE` 在 SQLite 會自動提交，因此遷移必須**先檢查欄位是否已存在**再 ALTER
（`PRAGMA table_info`），讓整個遷移可重跑自癒——沿用 v2→v3 既有的處理方式。
遷移**不建立任何 job**：排程觸發條件一律由 `_ensure_scan_job` 從狀態推導
（7.9c 教訓：三輪複審都栽在「從 job 存在與否推導」）。

`last_migration_report()` 回傳遷移前後各 completeness 的列數，以及
`probes_needed`（`left_boundary='unknown'` 且 active 的地址數）、
`scans_to_resume`（status='running' 的 scan 數）、`verify_needed`（`evidence_unknown=1` 的列數）。
`_migrate_v3_to_v4` 結束時以 `logger.warning` 印出整份 report（部署當下一定看得到）。

- [ ] **Step 4: 跑測試**

Run: `uv run pytest tests/test_explore_store.py -v`
Expected: PASS。

- [ ] **Step 5: 對正式機複本實跑遷移（唯讀取得、本機執行）**

```bash
ssh -i ~/Downloads/LightsailDefaultKey-ap-northeast-1-spark.pem ubuntu@52.197.137.3 \
  'sudo cp /var/lib/filet-api/explore.db /tmp/explore.copy.db && sudo chmod 644 /tmp/explore.copy.db'
scp -i ~/Downloads/LightsailDefaultKey-ap-northeast-1-spark.pem \
  ubuntu@52.197.137.3:/tmp/explore.copy.db /tmp/explore.copy.db
uv run python -c "
from spark.publicapi.explore_store import ExploreStore
import json; print(json.dumps(ExploreStore('/tmp/explore.copy.db').last_migration_report(), indent=1))"
```

**實跑結果（2026-09-22，主線程在 08:24 UTC 複本上獨立重現兩次，數字一致）**：

```
report = {"before": {"backfilling": 83, "complete": 300, "partial": 14},
          "after":  {"backfilling": 97, "complete": 135, "partial": 165},
          "work":   {"probes_needed": 198, "scans_to_resume": 97, "verify_needed": 0}}
fills 1,051,273 → 1,051,273（EQUAL）；83 個 running scan 游標漂移 0 筆；
left_boundary: earlier_fills_seen 135 / unknown 262；schema_version 4
```

⚠️ **消化時間的算式修正（主線程 2026-09-22 自我糾錯）**：本 plan 原本寫
「`probes_needed` ÷ 60 頁/小時」，**那是錯的**——獨立探測路徑（`_serve_special`／
`next_probe_candidate`）走的是**輔助份額**（`SPECIAL_SERVE_RATIO = 9`，每 9 頁 fills-like
才 1 次），不是 fills 全速。只有「有 `fills_scan` job 在跑」的位址才走探測前置的全速路徑。

實測分流（active 且待探測 198 個）：

| 路徑 | 位址數 | 速率 | 消化時間 |
|---|---|---|---|
| inline（有 scan job，吃 fills 全速） | 61（backfilling） | ~56 頁/小時 | < 1.5 小時 |
| standalone（輔助份額） | 137（partial／等證據） | ~6 頁/小時 | **≈ 22 小時** |

22 小時低於 24 小時門檻，但榜單會難看整整一天。處置見 Task 8（D-C 授權的「暫時提高輔助份額」
——把 `SPECIAL_SERVE_RATIO` 暫調為 3 可壓到約 10 小時，且逾期自動恢復）。

**副作用（好的）**：`verify_needed = 0`——根因 3 的 131 個待核驗列被本次遷移**吸收**了。
它們的 `evidence_unknown` 歸零，改成「缺左界證據」，而補證據只要 **1 頁探測**，
不再是一次多頁的核驗遍歷。既有的 131 個 `fills_verify` job 會被既有的狀態推導邏輯
自動判為 obsolete 丟棄（`_verify_job_obsolete`）。因此 **Task 8 原本的
`scripts/ops_expedite_verify.py` 不再需要**，D-C 的目標從「提前核驗」改為「加速探測」，
機制（暫時調整輔助份額、逾期自動恢復）不變。

- [ ] **Step 6: Commit**

```bash
git add src/spark/publicapi/explore_store.py tests/test_explore_store.py
git commit -m "feat: explore.db schema v4——撤銷舊判準結論、保留抓取進度、輸出工作量報告（D-G）"
```

---

## Task 5: 增量週期依近期成交速率決定 `@inline`

**Files:**
- Modify: `src/spark/publicapi/explore_fills_sync.py`（週期函式與常數）
- Modify: `src/spark/publicapi/config.py`、`src/spark/publicapi/explore_scheduler.py`
- Test: `tests/test_explore_scheduler.py`

**D-H**：不得只看名次。一個高頻地址若被排成 24 小時，下一輪增量要補 24 小時的成交，
很可能超過一頁（2000 筆）而變成多頁補抓——比每 6 小時抓一頁更貴。**週期的目的是讓
「一個週期內的預期成交筆數」剛好落在一頁之內**：

```
period_s(address) = clamp(
    TARGET_FILL_RATIO * PAGE_LIMIT / max(fills_per_hour, EPS) * 3600,
    MIN_PERIOD_S,      # 前 50 名：6h（新鮮度需求）；其餘：6h（下界，不是只給 hot）
    MAX_PERIOD_S)      # 24h
```

`fills_per_hour` 來源：`fills_sync.fills_in_window / (window 小時數)`（30 天窗口的實測速率，
資料已在手，不需額外請求）；沒有資料時取保守值 = `MIN_PERIOD_S`（不確定就抓密一點）。
`TARGET_FILL_RATIO = 0.8`（留 20% 餘裕給成交速率波動）。

- [ ] **Step 1: 寫失敗測試**

```python
def test_period_keeps_expected_fills_within_one_page():
    """核心不變式：任何地址的預期單週期成交筆數 ≤ PAGE_LIMIT。"""
    for fph in (1, 50, 500, 2_000, 20_000):
        p = fills_period_s(fills_per_hour=fph, rank=None)
        assert fph * (p / 3600) <= PAGE_LIMIT


def test_high_frequency_cold_address_is_not_blanket_24h():
    """D-H：名次在 50 名外但高頻的地址，不得一律延長到 24h。"""
    assert fills_period_s(fills_per_hour=600, rank=200) == MIN_PERIOD_S
    assert fills_period_s(fills_per_hour=2, rank=200) == MAX_PERIOD_S


def test_hot_rank_never_exceeds_min_period():
    assert fills_period_s(fills_per_hour=0.1, rank=1) == MIN_PERIOD_S


def test_unknown_rate_is_conservative():
    assert fills_period_s(fills_per_hour=None, rank=200) == MIN_PERIOD_S


def test_due_check_and_reschedule_share_one_period_source():
    """7.8 教訓：改到期條件必同改重排時間，且必須同一來源。"""
    sched = _scheduler()
    job = _fills_job(address=COLD_ADDR)
    sched._run_increment(job, now=NOW)
    expected = sched.fills_period_s_for(COLD_ADDR)
    assert sched._store.get_job(job.key).next_attempt_at >= NOW + expected
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_scheduler.py -k "period" -v`
Expected: FAIL，`ImportError: cannot import name 'fills_period_s'`。

> **主線程裁決（2026-09-22，builder 回報既有測試衝突後）**：
>
> **(1) 兩條斷言舊全域週期行為的既有測試授權改寫**，它們斷言的正是本 task 要移除的東西：
> - `test_fills_every_s_default_is_default_fills_period_s_constant`（`:647`）——
>   純屬性存在性測試，改寫成「無速率資料的位址回 `MIN_PERIOD_S`」。
> - `test_fills_period_single_source_ties_reschedule_and_plan_incremental`（`:1284`）——
>   ⚠️ **這條守的是 7.8 事故的不變式（到期條件與重排時間同源），只能「改寫期望值的來源」，
>   絕對不准刪**。新形式：對一個 hot 與一個 cold 位址各跑一次，斷言實際重排間隔
>   ≈ `sched.fills_period_s_for(address)`，而不是 ≈ 建構子傳入的全域值。
>
> **(2) 否決「保留建構子參數但內部完全不使用」的相容方案。** 靜默接受一個不起作用的參數，
> 會讓正式機 drop-in 裡的 `FILET_EXPLORE_FILLS_PERIOD_S=21600` 變成設定了卻無效果的死旋鈕
> ——下一個上機的人會以為改它有用。改用**語義相容**的做法：
> - `FILET_EXPLORE_FILLS_PERIOD_S` **改為下界** `MIN_PERIOD_S` 的來源（正式機現值 21600
>   ＝6 小時，剛好等於新規格的下界，語義相容、drop-in 不必改值）。
> - 新增 `FILET_EXPLORE_FILLS_MAX_PERIOD_S` 作為上界（預設 86400）。
> - 建構子參數改名為 `fills_min_period_s` / `fills_max_period_s`。
> **授權改動 `scripts/run_api.py` 與 `app.py` 的呼叫點**（原本不在檔案清單內），
> 確保沒有任何一個參數是「傳進去但沒人讀」。
> Task 9 的部署步驟要加一條：檢查 drop-in 裡沒有任何已無讀取者的 env。

- [ ] **Step 3: 實作**

`explore_fills_sync.py` 加 `MIN_PERIOD_S = 6 * 3600`、`MAX_PERIOD_S = 24 * 3600`、
`TARGET_FILL_RATIO = 0.8` 與純函式 `fills_period_s(fills_per_hour, rank)`。
`explore_scheduler.py` 只保留**一個**取用點 `fills_period_s_for(address)`（查 store 拿速率與
名次後轉呼叫純函式），所有到期判斷與重排都必須經它。
`config.py` 以 env 覆寫上下界（`FILET_EXPLORE_FILLS_MIN_PERIOD_S` /
`FILET_EXPLORE_FILLS_MAX_PERIOD_S`），沿用既有 `explore_fills_period_s` 的寫法。

- [ ] **Step 4: 驗證沒有殘留呼叫點**

Run: `grep -n "_fills_every_s\|DEFAULT_FILLS_PERIOD_S" src/spark/publicapi/explore_scheduler.py`
Expected: 無命中（全部改走 `fills_period_s_for`）。

Run: `uv run pytest tests/test_explore_scheduler.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/explore_fills_sync.py src/spark/publicapi/config.py src/spark/publicapi/explore_scheduler.py tests/test_explore_scheduler.py
git commit -m "feat: 增量週期依近期成交速率決定，單週期預期筆數不超過一頁（D-B／D-H）"
```

---

## Task 5b: 速率的分母改成「實際觀測跨度」，否則 Task 5 完全失效 `@inline`

> **主線程裁決（2026-09-22，Task 5 完成後複查發現）**：Task 5 的
> `fills_period_s_for` 用 `fills_sync.fills_in_window ÷ 窗口小時數（30 天 = 720h）`
> 當成交速率。**分母錯了**：`fills_in_window` 只計入「我們實際走過的頁」，
> 對仍在回補中的位址是部分計數，除以整整 30 天必然低估。
>
> **實測影響（正式機複本，300 個 active 位址）**：
>
> | 速率來源 | 最大值 | 落入「需要短週期」區間（≥66.7 筆/小時）的位址數 |
> |---|---|---|
> | 目前實作 `fills_sync ÷ 720h` | 16.7 筆/小時 | **0** |
> | 觀測密度 `fills_scan.fills_in_window ÷ (observed_to − observed_from)` | 897.5 筆/小時 | 2 |
>
> 低估最嚴重者 `0xa483470a…`：真實密度 897.5、被算成 13.4（**差 67 倍**）。
> 它會被排成 24 小時，而 24 小時累積約 21,500 筆＝**11 頁**——正是 D-H 明文要禁止的
> 「高頻地址被一律延長到 24h，把一頁的工作變成多頁補抓」。
> 換句話說，不修這一條，Task 5 對所有非前 50 名的位址等於沒有生效。

**Files:**
- Modify: `src/spark/publicapi/explore_scheduler.py`（`fills_period_s_for` 的速率取得）
- Modify: `src/spark/publicapi/explore_store.py`（若需要新增一個取觀測密度的讀取方法）
- Test: `tests/test_explore_scheduler.py`

**新的速率取得順序（fallback chain，每一層都要有測試）**：

1. 該地址**當前生效**的那次遍歷（`fills_scan.scan_id == fills_sync.scan_id`）若
   `observed_to_ms > observed_from_ms` 且 `fills_in_window > 0`
   → `rate = fills_in_window ÷ ((observed_to_ms − observed_from_ms) / 3600000)`。
2. 否則，若該列的遍歷已完成且窗口長度為正 → 退回 `fills_sync.fills_in_window ÷ 窗口小時數`。
3. 否則 → `None`＝未知 → `MIN_PERIOD_S`（不確定就抓密一點）。

**為什麼這個分母在兩種情況下都正確或偏保守**（寫進 docstring）：
- 遍歷未完成：觀測跨度就是我們真正看過的區間，密度正確。
- 遍歷已完成：觀測跨度可能比窗口短（帳戶在窗口邊緣沒交易），密度會**高估** →
  週期估短 → 抓得更密。方向安全（工程原則：寧可多抓一頁，不可漏成多頁補抓）。
- `fills_in_window` 含游標重疊、是上界（高估約 3.7%）→ 同樣是偏保守的方向。

- [ ] **Step 1: 寫失敗測試**

```python
def test_rate_uses_observed_span_not_nominal_window():
    """回補中的位址：4,000 筆分布在 2 天的觀測跨度內 → 2,000 筆/天 ≈ 83 筆/小時，
    不得因為除以 30 天而被當成 5.6 筆/小時。"""
    sched = _scheduler_with_scan(ADDR, fills_in_window=4_000,
                                 observed_from_ms=T0, observed_to_ms=T0 + 2 * DAY,
                                 window_start_ms=T0, window_end_ms=T0 + 30 * DAY,
                                 status="running", rank=200)
    assert sched.fills_period_s_for(ADDR) < MAX_PERIOD_S      # 關鍵：不得落在 24h


def test_high_density_partial_traversal_gets_min_period():
    """密度高到一個週期內必然超過一頁 → 直接壓到下界。"""
    sched = _scheduler_with_scan(ADDR, fills_in_window=18_000,
                                 observed_from_ms=T0, observed_to_ms=T0 + 21 * DAY,
                                 window_start_ms=T0, window_end_ms=T0 + 30 * DAY,
                                 status="running", rank=200)
    assert sched.fills_period_s_for(ADDR) == MIN_PERIOD_S


def test_falls_back_to_window_hours_when_no_observed_span():
    sched = _scheduler_with_scan(ADDR, fills_in_window=720, observed_from_ms=None,
                                 observed_to_ms=None, window_start_ms=T0,
                                 window_end_ms=T0 + 30 * DAY, status="done", rank=200)
    assert sched.fills_period_s_for(ADDR) == fills_period_s(fills_per_hour=1.0, rank=200)


def test_no_data_at_all_stays_conservative():
    sched = _scheduler_with_scan(ADDR, fills_in_window=0, observed_from_ms=None,
                                 observed_to_ms=None, status="running", rank=200)
    assert sched.fills_period_s_for(ADDR) == MIN_PERIOD_S


def test_period_source_is_still_single():
    """7.8 不變式不得因本次改動而失守：到期判斷與重排仍同源。"""
    # 沿用 Task 5 的 test_due_check_and_reschedule_share_one_period_source 手法
```

- [ ] **Step 2–4: 執行 → 實作 → 複跑**

Run: `uv run pytest -q` → 全綠；`uv run ruff check src tests scripts` → 無錯誤。

- [ ] **Step 5: 用正式機複本重算需求（與 Task 5 的數字對照）**

唯讀開啟 `<scratchpad>/explore.copy.db`，用**新的**速率取得方式重算：
(i) 週期分佈（6h／中段／24h 各幾個）——**中段不得再是 0**；
(ii) 改動後的增量需求（Σ 3600/period）頁/小時；
(iii) 以 56 頁/小時計，留給遍歷軌多少。
把三個數字與 Task 5 的舊數字並列回報（舊：50.0 → 23.38 → 32.62）。

- [ ] **Step 6: Commit**

```bash
git add src/spark/publicapi/explore_scheduler.py src/spark/publicapi/explore_store.py tests/
git commit -m "fix: 成交速率改用實際觀測跨度為分母，修正高頻位址被誤排成 24h（D-H）"
```

---

## Task 6: 限流器子預算可借用 —— ❌ 使用者裁決放棄（2026-09-22）

> **結論：機制正確但實測零效益，程式碼已 revert，不留未接線的死程式碼。**
> 保留本節是為了讓之後的人不必重做一遍這個分析。

**做過什麼**：完整實作了 `WeightLimiter` 的 `scope_floors`／`_child_cap_effective`／
建構期防呆，五條測試全過（含真實 8 執行緒壓測，並用「把檢查與扣帳拆成兩次持鎖 →
`peak=840 > 300` 轉紅 → revert」證明它抓得到 race）。機制本身沒有問題。

**為什麼零效益**（實測，非推論）：

| 量測 | 結果 |
|---|---|
| `explore_base` 穩態每分鐘預留權重（300 地址 harness，暖機到 base 逾期連續 3 次為 0 才開始統計） | p50 **164**、p95 **180**（多個分鐘貼齊現行 180 cap） |
| 解除 base 自身 cap、只受父 cap 300 制約時的瞬時尖峰 | **250 權重/分** |
| `child_cap_effective(explore_fills) = max(120, 300 − 180)` | **120 權重/分 = 1 頁/分 = 60 頁/小時**（與現行相同） |
| 暖機後 1 小時 `total_fills_pages`（舊硬 cap vs 新 floor，同 seed 分岔） | **53 → 53**，零差異 |

**根本原因**：base 的需求不是背景段推導的平滑 ~120 權重/分，而是**尖峰式**的
（`state` 1800s／`portfolio`、`ledger` 7200s × 300 個離散位址）。正式機觀測到
「base 逾期恆為 0」**不代表有閒置額度可借**，而是現行 180 cap 剛好吸收得住尖峰。
兩個子 floor 相加又恰好貼齊父 cap 300，借用公式因此退化成與今天完全相同的數字。

**真正的天花板是 `explore` 父預算 300**，而 base 實際需要其中一大半。要再往上只有三條路，
都已呈使用者裁決（2026-09-22）：(a) 接受現狀（**採用**）；(b) 放寬 portfolio／ledger 週期
2h→4h，拿榜上權益／損益／回撤的新鮮度換吞吐；(c) 提高父預算——碰 2026-09-19 事故紅線。

**使用者選 (a)**：Task 5b 已把遍歷軌從 6 提到 **33.66 頁/小時**（5.6 倍），足以在 1–2 天內
消化積壓（198 個探測 ＋ 大戶遍歷），不值得為了 13–50% 的額外增益，在有真實用戶跟單時
去動限流器這個全案風險最高的元件。

**若未來要重啟這條路**：先實測 follower 引擎自己用掉多少權重（它是獨立進程、不經限流器，
全靠全域 900 與 HL 1200 的差額保護），不要沿用任何推導值。

---

## Task 7: 300 地址競爭負載下的整合驗收 `@inline`

**Files:**
- Modify: `tests/test_explore_scheduler.py`（整合 harness）

**D-I**：驗收必須重現真實競爭——300 個候選、增量輪、其他地址的遍歷、verify、探測，
全部共用**真實限流模型**（60 秒視窗、權重 120、settle 依實際筆數）。rng 種子釘死
（7.9 教訓：harness 沒有預算模型／churn／多頁就抓不到問題；種子不釘死結果不可複現）。

- [ ] **Step 1: 寫失敗測試（六條）**

```python
def _harness():
    return SchedulerHarness(seed=20260922, candidates=300, budget=RealWeightBudget(),
                            fills_page_weight=120, window_s=60)


def test_whale_reaches_complete_within_24h_under_full_contention():
    """回歸根因：30 天窗口 20,000 筆的地址，在 300 地址競爭＋真實限流下，
    必須在模擬 24 小時內成為對外 complete。"""
    h = _harness(); h.set_fill_count("0xwhale", window_fills=20_000)
    h.run_for(hours=24)
    assert h.published_row("0xwhale")["fills_coverage"]["state"] == "complete"
    assert h.published_row("0xwhale")["win_rate"] is not None


def test_missing_left_boundary_evidence_can_never_publish_complete():
    """反向護欄：探測一直拿不到證據的地址，24 小時後仍不得是 complete。"""
    h = _harness(); h.set_probe_always_empty("0xmystery"); h.set_portfolio_missing("0xmystery")
    h.run_for(hours=24)
    assert h.published_row("0xmystery")["fills_coverage"]["state"] != "complete"


def test_same_millisecond_fills_survive_page_boundary():
    """同毫秒跨頁不漏單：游標 inclusive 重疊＋(time, tid) 去重。"""
    h = _harness(); h.set_fills_all_same_ms("0xburst", count=4_500)
    h.run_for(hours=6)
    assert h.stored_fill_count("0xburst") == 4_500


def test_restart_resumes_scan_from_persisted_cursor():
    h = _harness(); h.set_fill_count("0xwhale", window_fills=20_000)
    h.run_for(hours=3); before = h.scan_cursor("0xwhale")
    h.restart()                                   # 重建 scheduler，只留 DB
    h.run_for(hours=1)
    assert h.scan_cursor("0xwhale") >= before
    assert h.pages_refetched_after_restart() <= 1  # 至多重抓游標那一頁


def test_traversal_track_gets_at_least_half_the_fills_pages():
    """D-B 驗收：修改前實測 12/56 ≈ 21%。"""
    h = _harness(); h.run_for(hours=6)
    assert h.scan_pages / h.total_fills_pages >= 0.5


def test_base_and_verify_are_not_starved_under_fills_pressure():
    h = _harness(); h.run_for(hours=6)
    assert h.overdue_p95_s("state") < 1800
    assert h.verify_completed > 0


def test_massive_same_ms_cluster_degrades_to_partial_not_silent_loss():
    """Task 7a 揭露的系統性質（主線程 2026-09-22 補）：單一毫秒超過 2×PAGE_LIMIT
    筆時，`same_ms_overflow` 這個刻意的保護會讓遍歷無法前進。此時**必須**誠實降級
    成 partial／unresolved_gap，絕不可以少抓了卻宣稱 complete——這是「錯判完整」
    的最後一道防線，要有整合測試釘住，不能只靠 Task 1 的單元測試。"""
    h = _harness(); h.set_fills_all_same_ms(BURST, count=6_000)   # > 2×PAGE_LIMIT
    h.run_for(hours=6)
    row = h.published_row(BURST)
    assert row["fills_coverage"]["state"] == "partial"
    assert row["fills_coverage"]["reason"] == "unresolved_gap"
    assert row["win_rate"] is None          # D-14：非 complete 不得給成交衍生數字
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_scheduler.py -k "under_full_contention or never_publish_complete or same_millisecond or restart_resumes or at_least_half or not_starved" -v`
Expected: FAIL。

- [ ] **Step 3: 補齊 harness 直到測試通過**

只補測試需要的觀測接口與負載模型，不改排程邏輯。若測試揭露排程真的做不到，
**回報主線程裁決，不得放寬斷言門檻**（judgment.md §4：放寬驗收不是修復）。

- [ ] **Step 4: 全套回歸**

Run: `uv run pytest`
Expected: 全綠，integration 標記照常 skip。

Run: `uv run ruff check src tests scripts`
Expected: 無錯誤。

- [ ] **Step 5: Commit**

```bash
git add tests/test_explore_scheduler.py
git commit -m "test: 300 地址競爭負載下的覆蓋收斂、反向護欄與重啟續抓驗收（D-I）"
```

---

## Task 8: 新判準真的被正式流程執行——兩個端到端測試 ＋ 輔助份額臨時加速 `@inline`

> **主線程裁決（2026-09-22，Task 4 實跑後）**：`verify_needed = 0`——根因 3 的 131 個
> 待核驗列已被 schema v4 遷移吸收（改成「缺左界證據」，補證據只要 1 頁探測）。
> 因此 **Step 5 的 `scripts/ops_expedite_verify.py` 取消，不要寫**。
> D-C 授權的「暫時提高輔助份額」仍然要做，但目標從 verify 改為 **probe**：
> 遷移後有 137 個位址要走輔助份額拿證據，預設 9:1 約需 22 小時，調成 3:1 約 10 小時。
> `SPECIAL_SERVE_RATIO` 的預設值 9 是使用者 2026-09-21 的裁決，**不得更動**；
> 只能加一個帶到期時間、逾期自動恢復的 env 覆寫。

**Files:**
- Modify: `tests/test_explore_scheduler.py`
- Create: `scripts/ops_expedite_verify.py`
- Modify: `src/spark/publicapi/explore_scheduler.py`、`src/spark/publicapi/config.py`

使用者指定：這兩個測試比「函式回傳正確」更能防止「程式改了但正式流程永遠走不到」。
本次已經現場抓到兩個這種缺口（候選查詢仍用被刪除的 reason、`evidence_unknown=0` 把
待核驗列排除在探測母體外）。

- [ ] **Step 1: 寫失敗測試（兩條端到端）**

```python
def test_new_verdict_path_is_actually_reached_by_the_scheduler():
    """端到端：跑完排程迴圈後，探測候選查詢必須真的挑到人、
    左界證據必須真的被寫入、結論必須真的由 scan_verdict 產生。
    （單元測試證明函式對，這條證明流程走得到。）"""
    h = _harness()
    h.run_for(hours=2)
    assert h.probes_executed > 0                       # 候選查詢挑得到人
    assert h.addresses_with_left_boundary() > 0        # 證據真的落地
    assert h.verdicts_from_scan_verdict > 0            # 結論來自新判準
    assert h.verdicts_from_legacy_path == 0            # 沒有第二條結論來源


def test_evidence_unknown_rows_actually_leave_unknown_via_verify():
    """待核驗列必須真的能經 verify 軌離開 unknown——不是只在單元測試裡能。"""
    h = _harness()
    h.seed_rows(evidence_unknown=40)
    h.run_for(hours=12)
    assert h.rows_with_evidence_unknown() == 0
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `uv run pytest tests/test_explore_scheduler.py -k "actually_reached or actually_leave_unknown" -v`
Expected: FAIL。

- [ ] **Step 3: 補齊觀測接口直到通過**

`verdicts_from_legacy_path` 需要在舊結論路徑（若有殘留）放一個計數器；
若 Task 1–3 做對，這個計數器永遠是 0，且應在實作結束後**把舊路徑整段刪除**
（判準只能有一個來源，工程原則 1）。

- [ ] **Step 4: verify 加速——到期自動恢復（D-I）**

`SPECIAL_SERVE_RATIO`（`explore_scheduler.py:142`）改為可由 env 暫時覆寫，
且**帶到期時間、逾期自動恢復預設值**，不依賴任何人記得移除：

```python
FILET_EXPLORE_SPECIAL_SERVE_RATIO=3
FILET_EXPLORE_SPECIAL_SERVE_RATIO_UNTIL=2026-09-24T00:00:00Z
```

```python
    def _special_serve_ratio(self, now: float) -> int:
        """暫時加速只在期限內有效；逾期自動回到使用者 2026-09-21 裁決的預設 9。
        設定缺漏或時間無法解析一律回預設（fail-safe 往保守方向）。"""
```

**預設值不得更動**——它是使用者的既有裁決。加一條測試：
`test_special_serve_ratio_reverts_after_deadline`。

- [ ] **Step 5: 運維腳本（預設 dry-run）**

`scripts/ops_expedite_verify.py`：把 `kind='fills_verify' AND lease_until IS NULL` 的 job
`next_attempt_at` 壓到現在起 N 分鐘內**均勻散開**（避免同一分鐘擠爆額度）。
`--apply` 才寫入，預設只印「會改幾列、最早／最晚新到期時間」。可重複執行。
只改 `next_attempt_at`，不碰任何結論欄位。

對複本驗證（不碰正式機）：

```bash
uv run python scripts/ops_expedite_verify.py --db /tmp/explore.copy.db
```

Expected: 印出可提前的列數與時間範圍，且未寫入。

- [ ] **Step 6: Commit**

```bash
git add tests/test_explore_scheduler.py scripts/ops_expedite_verify.py src/spark/publicapi/explore_scheduler.py src/spark/publicapi/config.py
git commit -m "test: 新判準端到端可達性與待核驗列離開 unknown；verify 加速到期自動恢復（D-I）"
```

---

## Task 8b: 遷移後形狀必須走得完整條獨立探測鏈 `@inline`

> **主線程裁決（2026-09-22，Task 8 完成後複查發現）**：Task 8 的
> `test_new_verdict_path_is_actually_reached_by_the_scheduler` 只要 **inline** 探測前置
> 能跑就會過（builder 的「改壞→轉紅」證據必須**同時**破壞 inline 與獨立候選查詢才轉紅）。
> 而 Task 3b 那條「模擬獨立路徑」的測試是**直接呼叫 `store.set_left_boundary`**，
> 繞過了 `_serve_special` → `next_probe_candidate`（SQL）→ `_run_probe` 這整條鏈。
>
> 結果：遷移後最大的一群位址（137 個，形狀＝`partial / left_boundary_unknown`、
> `evidence_unknown=0`、scan 已 done、**沒有** scan job）要靠的那條路，
> 沒有任何測試讓真實排程器從頭走到尾。單獨破壞 `_PROBE_CANDIDATE_WHERE`
> ——本次已在正式機現場抓到過的同型錯——現在的測試抓不到。

**Files:**
- Modify: `tests/test_explore_scheduler.py`（擴充 `SchedulerHarness`，新增一條測試）

- [ ] **Step 1: 寫失敗測試**

```python
def test_migrated_rows_reach_complete_via_standalone_probe_path(tmp_path):
    """遷移後 137 個位址的真實路徑：無 scan job → 只能靠 `_serve_special` 的輔助份額
    → `next_probe_candidate` 挑中 → `_run_probe` → `set_left_boundary` →
    Task 3b 就地重算 → complete。全程由真實排程器驅動，零整窗重掃。"""
    h = _t7a_harness(tmp_path)
    addrs = h.seed_migrated_unknown_rows(n=20)     # 精確複製 v4 遷移後的列形狀
    h.run_for(hours=24)   # 主線程 2026-09-22 更正：原寫 6h 是沒算過的臨界值（見下）
    resolved = [a for a in addrs if h.published_row(a)["fills_coverage"]["state"] == "complete"]
    assert len(resolved) == 20
    assert h.scan_pages_for(addrs) == 0            # 只有探測頁，沒有任何遍歷頁
    assert h.probes_executed >= 20
```

`seed_migrated_unknown_rows` 種的形狀必須與 Task 4 遷移的 SQL 逐欄一致：
`fills_sync.completeness='partial'`、`reason='left_boundary_unknown'`、`left_boundary='unknown'`、
`evidence_unknown=0`、`scan_id` 指向一筆 `status='done'`、游標已抵達 `window_end_ms`、
`unresolved_gap=0` 的 `fills_scan`；`refresh_job` 裡**沒有**該位址的 `fills_scan`／`fills_verify`。
上游要讓探測窗回至少一筆合法成交（→ `earlier_fills_seen`）。

> **時限更正（主線程 2026-09-22，builder 實跑 6h 得 19/20 後停下回報）**：獨立探測走輔助份額，
> 每 9 頁 fills-like 才 1 個名額，56 頁/小時下約 6 個/小時；6h ≈ 36 個名額，再扣掉與 280 個
> 新位址 inline 探測的競爭，貼著 20——時限設在臨界點是設計錯誤。本測試證明的是**可達性**
> （那條鏈走得到、不重掃），不是吞吐（Task 7b 已證吞吐類斷言不穩定），所以時限取
> 「名額遠大於需求而飽和」：24h ≈ 144 個名額 ≫ 20。不縮候選池、不預解其他候選的證據
> （正式機遷移當下 198 個位址同時缺證據，比 harness 更擠不是更鬆）。

- [ ] **Step 2: 確認失敗、實作 harness 接口、轉綠**

- [ ] **Step 3: 證明它抓得到單獨破壞候選查詢**

只在 `_PROBE_CANDIDATE_WHERE` 加 `AND 1=0`（**不動** inline 探測前置），測試必須轉紅；
revert 後 `git diff --stat src/` 為空。這一步是本 task 存在的理由。

- [ ] **Step 4: Commit**

```bash
git add tests/test_explore_scheduler.py
git commit -m "test: 遷移後形狀經真實排程器走完整條獨立探測鏈才 complete（D-I）"
```

---

## Task 9: RUNBOOK §5.8f 與部署（跟單中，加倍小心）`@inline`

**Files:**
- Modify: `deploy/RUNBOOK.md`

- [ ] **Step 1: 寫 §5.8f**

必含：

1. **部署前基線**（D-I，全部要有輸出留存）：
   `systemctl --failed`、`systemctl is-active 'filet-follower@*'`、follower 最近一次成功對帳時間、
   `samples.jsonl` 最後一筆的 `public` / `scan` / `overdue_by_kind`、
   `sudo cp /var/lib/filet-api/explore.db /var/lib/filet-api/explore.db.pre-v4.bak`。
2. **快照預熱**（2026-09-05 使用者裁決，RUNBOOK §5.8c）：本次覆蓋結論大量變動會讓探索快照
   失效——先在本機用新版程式建好 300 池快照、`install` 到正式機快取路徑，再重啟 `filet-api`。
3. **只重啟 `filet-api`**。`filet-follower@*` 與四個 timer 一律不動；重啟後立即確認
   follower 未被牽連（`systemctl show filet-follower@<id> -p ActiveEnterTimestamp` 與部署前相同）。
4. **env 變動**（以實際 commit 為準，Task 6 已放棄）：`FILET_EXPLORE_FILLS_PERIOD_S` 語義改為
   **下界**（值 21600 不變）；新增 `FILET_EXPLORE_FILLS_MAX_PERIOD_S`（預設 86400，可不設）；
   新增 `FILET_EXPLORE_SPECIAL_SERVE_RATIO=3` ＋ `FILET_EXPLORE_SPECIAL_SERVE_RATIO_UNTIL=2026-09-24T00:00:00Z`
   （逾期自動回 9）。**`FILET_HL_EXPLORE_*_WEIGHT_CAP` 完全不動**（Task 6 放棄，語義仍是硬 cap）。
   部署前 `grep` drop-in 確認沒有任何已無讀取者的 env。
5. **遷移報告**：啟動日誌必定印出 `last_migration_report()`，部署後第一件事是讀它並與
   Task 4 Step 5 在複本上算出的數字比對；不符就回退。
6. **回退**：停 `filet-api` → 還原 `explore.db.pre-v4.bak` → 還原上一版程式碼 → 啟動 →
   確認快照與 follower 正常。回退**不需要**動 follower。

- [ ] **Step 2: 觀測門檻（部署後由主線程親跑驗證，至少涵蓋一個 24h 重排週期）**

| 指標 | 來源 | 門檻 | 不達標的動作 |
|---|---|---|---|
| follower 存活與對帳 | `journalctl -u 'filet-follower@*'` | 無新增失敗、未重啟 | **立即回退** |
| 429 | `samples.jsonl` 的 `api.r429_15m` | 連續 2 小時為 0 | **立即回退** |
| Traceback | `api.traceback_15m` | 連續 2 小時為 0 | 立即回退 |
| explore 父 scope 權重 | 限流器 status | 任一分鐘 ≤ 300 | 立即回退 |
| 全域權重 | 限流器 status | 任一分鐘 ≤ 900 | 立即回退 |
| 探測進度 | `probe.executed` 累計 | 部署後 2 小時 > 0 | 查「流程走不到」類缺口 |
| 遍歷軌頁面占比 | `scan_pages_15m / pages_consumed_15m` | 24 小時後 ≥ 0.5 | 檢討週期與 floor |
| 對外 complete | `public.coverage_counts.complete` | 24 小時後 ≥ 200（現況 97） | 查探測積壓 |
| base 逾期 | `overdue_by_kind.state.overdue_p95_s` | < 1800 | 調高 base floor |

- [ ] **Step 3: Commit**

```bash
git add deploy/RUNBOOK.md
git commit -m "docs: RUNBOOK §5.8f 第八次部署程序（schema v4、證據判準、子預算借用、跟單中注意事項）"
```

---

## Task 10: 審核修正（reviewer 2026-09-22：C1＋W1／W2／W3／W5）`@inline`

> fresh-context opus reviewer 的報告在 `<scratchpad>/review-2026-09-22.md`，重現腳本 `probe1–4.py`。
> **主線程已親自重跑 probe3／probe4 與 W1／W3 的一行驗證，全部成立**；W4 延後（見末）。
> 部署判斷：**修完 C1 再部署**。

### C1（Critical）：重算結論繞過了左界證據的單調性閘門 → 錯判完整

`explore_store._recompute_verdict_locked` 直接把 `set_left_boundary` 剛寫入的 `(state, window_start_ms)`
組成 `LeftBoundary` 餵給 `scan_verdict`；而 `get_left_boundary(address, query_ws)` 有單調性閘門
（正面證據只在 `stored_ws <= query_ws` 時適用，否則視為 unknown）。重算用的 scan 是
`fills_sync.scan_id` 指向的那筆——遷移後是**舊的**已完成 scan（窗口起點 W_old），但探測前置
是對 `partial_rescan` **新建**的 scan（W_new > W_old）做的：「W_new 之前有成交」不能證明
W_old 的左段沒被截斷，卻把舊 scan 判成 `complete / left_boundary_verified`。
實跑：舊 scan `[1000, 9000]`，`set_left_boundary(ws=5000, earlier_fills_seen)` → 舊結論變 complete；
`get_left_boundary(A, 1000)` 依規則應為 unknown。遷移後 165 列全部立刻 `partial_rescan_due`（W2），
部署後數分鐘內大量發生。

**修法**：閘門只能有一個來源。把 `get_left_boundary` 的適用判斷抽成純 helper
`_applicable_boundary(state, stored_ws, stored_at, query_ws) -> LeftBoundary`，`get_left_boundary`
與 `_recompute_verdict_locked` **都**經它；重算時 `query_ws = scan.window_start_ms`（該筆 scan 自己的
窗口起點）。閘門判為 unknown → 不重算（結論維持 partial，等新 scan 自己收尾）。

測試：
- `test_recompute_ignores_evidence_from_a_later_window`：probe4 的情境，重算後仍 `partial/left_boundary_unknown`。
- `test_rescan_probe_does_not_flip_the_superseded_verdict`（harness）：種一個遷移形狀列、讓 `partial_rescan` 到期
  → 新 scan 探測前置寫入 W_new 證據 → 斷言舊結論**不變**，直到新 scan 以短頁收尾才 complete。
- 反向護欄：只把 `_recompute_verdict_locked` 改回直接組 `LeftBoundary`（繞過 helper）→ 第一條轉紅。

### W2：遷移列立即 `partial_rescan_due` → 165 次整窗重掃，繞過了 Task 3b 的便宜路徑

`_migrate_v3_to_v4` 撤銷結論後，這些列的 `fills_scan.finished_at` 是幾天前 → `PARTIAL_RESCAN_AFTER_S` 早已到期
→ 第一輪 candidates 就建 `fills_scan` job → 每列一次 30 天整窗遍歷（不在任何容量估算內；Task 8b 的
harness 刻意種成沒有 job，覆蓋不到這個形狀）。實跑 probe3 證實。

**修法**：遷移對「撤銷結論」的那批列，把其 `fills_sync.scan_id` 指向的 `fills_scan.finished_at` 設為
遷移當下 `:now`——**只動這一個時間戳**（它是結論時間的 metadata，不是抓取進度；D-G 的游標／
`fills`／`observed_*` 一律不動），讓便宜的獨立探測路徑先有 24 小時處理它們；探測解出證據後
Task 3b 就地重算成 complete，`_needs_scan_job` 依狀態推導自然不再排重掃。
`last_migration_report()["work"]` 新增 `rescans_deferred`。

測試：遷移後 `partial_rescan_due(now)` 為 False、`now + PARTIAL_RESCAN_AFTER_S` 為 True；
`next_probe_candidate()` 仍回得到該列；連跑兩次遷移 `finished_at` 不再變（版本閘門）。

### W1：`rank=None` 時只套上界不套下界 → 重啟後（rank 快取為空）週期可低於 6h

實跑 `fills_period_s(897.5, None) = 6417.8s`、`(5000, None) = 1152s`；`MIN = 21600`。
**修法**：`fills_period_s` 一律 clamp 到 `[MIN_PERIOD_S, MAX_PERIOD_S]`，rank 只負責「熱門強制取 MIN」。
測試：`fills_period_s(897.5, None) == MIN_PERIOD_S`、`fills_period_s(5000.0, None) == MIN_PERIOD_S`。

### W3：`scan_verdict` 可回 `("partial", None)`，違反 `tuple[str, str]`

`cursor < window_end` 且 `stop_reason is None`（例如 v3 的 `page_cap` 列被重算）→ reason 寫成 NULL。
**修法**：該分支回 `("partial", REASON_TRAVERSAL_INCOMPLETE)`（新常數 `"traversal_incomplete"`，語義＝
遍歷未抵達終點且沒有記錄到停止原因）。測試釘死回傳型別永不含 None。

### W5：模糊帶不對稱，寬的那側落在錯判完整方向

`no_earlier_activity` 只要 `first_ms >= window_start_ms`（零緩衝），但 `truncation_suspected` 要
`first_ms < window_start_ms − 1d`。allTime 首點是降採樣的權益歷史，粒度可到一天（plan Task 3 設計要點
明寫「只在**明顯**晚於時採信」）。**修法**：`no_earlier_activity` 需 `first_ms >= window_start_ms + _PROBE_WINDOW_MS`；
`[window_start − 1d, window_start + 1d)` 一律 unknown。既有 `test_probe_empty_with_clearly_newer_account_is_no_earlier_activity`
用 +3 天仍過；新增 `+12h → unknown` 的測試。

### W1／W5 裁決（主線程 2026-09-22，builder 回報與既有 plan 測試衝突後）

**W1——plan 自己的兩條規格互相矛盾，物理限制贏**：Task 5 同時寫了「下界 `MIN_PERIOD_S`=6h 對所有位址」
與「單週期預期成交筆數 ≤ 一頁」（`test_period_keeps_expected_fills_within_one_page`）。對 897.5 筆/小時的
位址，一頁只夠 0.8×2000/897.5 ≈ 1.8 小時；強制 6h 就是 5,385 筆＝3 頁的補抓——正是 D-H 禁止的形狀。
裁決：
- **熱門**（`rank <= hot_rank`）：固定 `MIN_PERIOD_S`（6h，新鮮度需求，D-B）。
- **非熱門或 `rank is None`**（重啟後快取為空即此情形，兩者同規則，重啟不再造成行為差異）：
  `clamp(0.8×PAGE_LIMIT/fph×3600, MIN_PERIOD_COLD_S, MAX_PERIOD_S)`，新常數 **`MIN_PERIOD_COLD_S = 3600`**
  （與 `config.py` 既有 `>= 3600` 驗證同值——reviewer W1 指出的正是穿過了這條驗證）。
- 一頁不變式測試改寫為分段：`fph <= 0.8×PAGE_LIMIT/1h = 1600` 時預期筆數 ≤ `PAGE_LIMIT`；
  `fph > 1600` 時 `period == MIN_PERIOD_COLD_S`（下界主導，多頁補抓不可避免，docstring 明寫理由）。
- Task 5b 的 `test_high_frequency_cold_address_is_not_blanket_24h` 意圖是「不得一律 24h」，斷言
  `fills_period_s(600, 200) == MIN_PERIOD_S` 改為 `== 9600`（0.8×2000/600×3600）並 `< MAX_PERIOD_S`。
- 新增：`fills_period_s(5000.0, None) == MIN_PERIOD_COLD_S`、`fills_period_s(5000.0, 200) == MIN_PERIOD_COLD_S`、
  `fills_period_s(897.5, None) == fills_period_s(897.5, 200)`（rank 未知與非熱門同規則）。
- Task 7b 政策測試（≤ 30 頁/小時）預期仍過：正式機分佈中 fph ≥ 267 的只有 2 個位址。若轉紅，回報實數。

**W5——修 harness fixture，不放寬判準**：`_t7a_default_portfolio(ws)` 把 `first_activity_ms` 設成**恰好等於**
`window_start_ms`，在收緊後落在模糊帶是 fixture 不真實，不是判準錯。改成 `ws + 2*DAY`（帳戶明顯在窗口內才
開始活動 → `no_earlier_activity`，可判定），並在 fixture docstring 寫明「W5 收緊後 ±1 天是模糊帶，fixture
必須落在可判定區」。不得改成 `ws − 2*DAY`：若探測窗回空，那會變成 `truncation_suspected`，大戶測試會以
另一種方式失敗。受影響的多條測試共用同一 fixture，改一處即可；改完 Task 7a／7b／8／8b 全部仍須綠。

### W4（延後，不在本 task）
`truncation_suspected → unknown` 被允許且降級不觸發重算，`left_boundary` 與 `reason` 可能不一致。
兩者都是 partial，不影響對外正確性；Task 3b 的 builder 有「防探測飢餓」的理由。部署後再議。

- [ ] 全部修完：`uv run pytest -q` 全綠、`ruff` 過、上述測試全部存在；C1 的反向護欄轉紅證據。
- [ ] Commit：`fix: 審核修正——重算結論套用單調性閘門（C1）、遷移延後重掃、週期下界、verdict 型別、模糊帶對稱（W1/W2/W3/W5）`

---

## Task 11: `no_earlier_activity` 改為窗口綁定（部署前，使用者裁決 2026-09-22）`@inline`

> 兩輪審核都指出：`_applicable_boundary` 對正面證據一律 `stored_ws <= query_ws`，但單調性只對
> `earlier_fills_seen` 成立（更早的起點之前有成交 ⇒ 更晚的起點之前必也有）。`no_earlier_activity`
> 不成立：帳戶若在舊窗口**內**才開始活動，窗口往前滾後「新起點之前無活動」是假的，卻會被套用；
> 配合「正面證據終局＋不重探」→ 永久鎖定 complete。觸發需要 HL 真的截斷（實測不會），
> 但落在錯判完整方向，使用者裁決部署前修。W4 仍延後。

**規格（三處必須一起改，缺一不自洽）**：

1. `explore_store._applicable_boundary`：`earlier_fills_seen` 維持 `stored_ws <= query_ws`（單調）；
   **`no_earlier_activity` 改為 `stored_ws == query_ws`**（窗口綁定）；`truncation_suspected` 維持 `==`。
   docstring 逐狀態寫明為什麼（單調 vs 窗口綁定）。
2. `_PROBE_CANDIDATE_WHERE`：新增 `(s.left_boundary='no_earlier_activity' AND
   s.left_boundary_window_start_ms != sc.window_start_ms)`，與既有 `truncation_suspected` 那句同形——
   否則窗口不符的位址會停在 `partial/left_boundary_unknown` 直到 24h 後的 `partial_rescan` 才被重探。
3. `set_left_boundary` 轉移規則：「正面證據終局」只對**同一窗口起點**成立；stored 為 `no_earlier_activity`
   且新寫入的 `window_start_ms` 不同 → 允許任何狀態（換窗口＝新問題）。`earlier_fills_seen` 仍**無條件終局**
   （它是單調的，任何窗口都適用）。同窗口的既有規則不變。

**測試**：
- `test_no_earlier_activity_does_not_carry_to_a_later_window`：stored `no_earlier_activity@1000`，
  `get_left_boundary(addr, 5000).state == "unknown"`；同一設定 `earlier_fills_seen@1000` → 仍 `earlier_fills_seen`。
- `test_recompute_does_not_apply_no_earlier_activity_across_windows`：舊 scan ws=1000、
  `set_left_boundary(no_earlier_activity, ws=5000)` → 結論維持 `partial/left_boundary_unknown`。
- `test_window_mismatched_no_earlier_activity_is_a_probe_candidate`：`next_probe_candidate()` 挑得到。
- `test_no_earlier_activity_may_be_overwritten_for_a_new_window`：stored `no_earlier_activity@1000`，
  `set_left_boundary(unknown, ws=5000)` → True；`set_left_boundary(unknown, ws=1000)` → False（同窗口仍終局）；
  stored `earlier_fills_seen@1000`，任何窗口寫 unknown → False。
- harness `test_rescan_reprobes_when_prior_evidence_was_window_bound`：partial 列帶 `no_earlier_activity@W_old`，
  `partial_rescan` 到期建新 scan（W_new）→ 探測前置**必須**發出探測（`probes_executed` +1），不得沿用舊證據。
- 反向護欄：只把 `no_earlier_activity` 的規則改回 `<=` → 第一、二條轉紅。

> **主線程裁決（2026-09-22，builder 實作後回報 Task 8 回歸）**：規格第 2 項**撤回**，並補一條排他規則。
>
> builder 隔離出的根因：`left_boundary` 是**每地址一格**，而 `no_earlier_activity` 改成窗口綁定後，
> `partial_rescan` 期間同一地址有兩個窗口需要證據——舊的生效 scan（`fills_sync.scan_id`，W_old）與
> 新的進行中 scan（W_new）。第 2 項那句「窗口不符即為候選」讓獨立探測拿**舊窗口**去探、寫回 `@W_old`，
> 而新 scan 的探測前置拿**新窗口**去探、寫回 `@W_new`；第 3 項又放行了跨窗口覆寫 → 兩者對同一格
> 互相覆寫、反覆進候選池（harness 實測 `probes_executed=760`），把輔助份額吃光，Task 8 的
> `test_evidence_unknown_rows_actually_leave_unknown_via_verify` 因此失守。這不是 harness 假象。
>
> **修正後的規格**：
> 1. `_applicable_boundary`：維持第 1 項（`no_earlier_activity` 用 `==`）。
> 2. **撤回**「`no_earlier_activity AND 窗口不符` 進候選」那句。窗口不符的地址等它自己的 `partial_rescan`
>    （≤24h），由新 scan 的探測前置在新窗口重探——那才是唯一正確的窗口。代價：這類地址（年齡不滿一個
>    掃描窗且仍 partial 的帳戶）多等最多 24h；每次 rescan 多付 1 頁探測，直到滿 30 天取得 `earlier_fills_seen`
>    後永久終局。有界、可接受，寫進 Task 3 設計要點的成本模型。
> 3. **新增排他規則**：`_PROBE_CANDIDATE_WHERE` 加 `AND NOT EXISTS (select 1 from fills_scan r where
>    r.address=s.address and r.status='running')`——有 scan 在跑的地址，證據由該 scan 的探測前置獨占；
>    獨立探測只服務「沒有 job 在跑但證據仍缺」的地址（這正是 `_run_probe` docstring 原本描述的職責，
>    Task 3 時因正面證據皆單調而沒暴露）。這條同時封掉「inline 探測落在模糊帶回 unknown → 獨立探測用舊窗口
>    再寫一次」的殘餘乒乓。
> 4. 第 3 項轉移規則維持（換窗口＝新問題可覆寫；`earlier_fills_seen` 無條件終局）——有了排他規則後，
>    同一時間只有一個寫入者，跨窗口覆寫不再造成競爭。
>
> 測試調整：`test_window_mismatched_no_earlier_activity_is_a_probe_candidate` **反轉**為
> `test_window_mismatched_no_earlier_activity_waits_for_its_rescan`（不是候選）；新增
> `test_address_with_running_scan_is_never_a_standalone_probe_candidate`；harness 那條
> `test_rescan_reprobes_when_prior_evidence_was_window_bound` 維持（新 scan 的探測前置必須真的探）。
> Task 8 的 `test_evidence_unknown_rows_actually_leave_unknown_via_verify` 必須**不改**就回綠。

**驗收**：`uv run pytest -q` 全綠、ruff 過、上述測試齊、反向護欄轉紅輸出、
`grep -n "stored_ws <= query_ws\|stored_ws == query_ws" src/spark/publicapi/explore_store.py` 仍只在 helper 內。
**Commit**：`fix: no_earlier_activity 改為窗口綁定——單調性只對 earlier_fills_seen 成立（審核待辦 1）`

---

## 狀態表（實作期間由主線程更新）

| Task | 狀態 | 驗收證據 |
|---|---|---|
| 1 覆蓋結論三項證據合成 | ✅ `70c4f7c` | 主線程複跑 `pytest -q` 3315 passed、ruff 全過、`stop_reason` 值域封閉 |
| 2 頁數上限與停止語義 | ✅ `70c4f7c`（與 1 同 commit） | 同上；builder 實測補上 `cursor_ms < window_end_ms` 第四條件 |
| 3 左界證據前置與快取 | ✅ `0a58e6d` | 主線程複跑 3319 passed；`get_left_boundary = lambda` 零命中（掩體已拆） |
| 3b 證據到齊就地重算 | ✅ `511f727` | 主線程複跑 3325 passed；`scan_verdict` 呼叫點確認只有 2 處 |
| 4 schema v4 遷移 | ✅ `c3651ad` | 主線程在複本獨立重現遷移：fills 1,051,273 前後相同、83 個游標零漂移、report 與 builder 一致 |
| 5 週期依成交速率 | ✅ `7c458b1`（速率分母有缺陷，見 5b） | 主線程複跑 3334 passed；7.8 不變式測試經「改壞→轉紅→revert」驗證有效 |
| 5b 速率分母改觀測跨度 | ✅ `eaa801d` | 主線程用**正式程式碼**在複本實算：週期 6h=78／中段=4／24h=218，增量需求 **22.34 頁/小時**（原 50），留給遍歷軌 **33.66**（原 6）；`0xa483470a` 897.5 筆/小時 → 正確判 MIN |
| 6 子預算可借用 | ❌ 使用者裁決放棄 | 實測零效益（base p50=164／p95=180 貼頂、fills 有效上限仍 120 權重/分、`total_fills_pages` 53→53）；程式碼已 revert，分析保留在 Task 6 節 |
| 7a harness ＋四條正確性驗收 | ✅ `65ac81f` | 主線程複跑 3343 passed；只動測試檔、`src/` 零漂移；反向護欄經「改壞→轉紅→revert」驗證 |
| 7b 政策需求＋不飢餓＋同毫秒降級 | ✅ `0ca3160` | 主線程複跑 3346 passed；新政策測試實算 22.458 頁/小時（正式機 22.34）；harness 下界保真度 bug 修正 3600→21600 後 7a 四條仍全綠 |
| 8 端到端可達性＋份額自動到期 | ✅ `02d07e3` | 主線程複跑 3357 passed、ruff 全過；  預設 9 不動；drop-in 加 `SPECIAL_SERVE_RATIO=3` ＋ `_UNTIL=2026-09-24T00:00:00Z` |
| 8b 遷移後形狀走完整條獨立探測鏈 | ✅ `05fad87` | 只破壞 `_PROBE_CANDIDATE_WHERE`（不動 inline）→ 0/20 轉紅（23.9h 乾淨隔離；24h 因與 `PARTIAL_RESCAN_AFTER_S` 重合得 1/20，仍紅）；同 seed 下 **6.5 小時** 20/20（1h=2、3h=8、5h=15、6h=19） |
| 10 審核修正 C1＋W1/W2/W3/W5 | ✅ `71f89fd`；主線程複跑 3367 passed、probe3/probe4 重現已封；針對性複審（opus）**無 Critical、判可部署** | 閘門抽成 `_applicable_boundary` 單一來源；遷移只動 `finished_at`；非熱門下界 1h；reason 永不 None；模糊帶對稱 |
| 11 `no_earlier_activity` 窗口綁定 | ✅ `04e7c78`（主線程複跑中） | 閘門 `==`、撤回窗口不符進候選、加「有 scan 在跑不進獨立探測」排他；反向護欄轉紅；Task 8 測試一字不改回綠，`probes_executed` 340（基線 301，非 760） |
| 9 RUNBOOK §5.8f＋**部署** | ✅ **已部署 2026-09-22 15:45:03 UTC**（`bbd7adf`） | stop→裝快照→start 3 秒；follower 時間戳與基線相同；遷移報告 before {69/322/23} → after {92/144/178}，`verify_needed 0`、`rescans_deferred 178`；`building False/300`；Traceback 0 | 主線程逐段讀過並修 3 處可執行性問題（ops/health 需 admin session、取樣器無 `probe` 欄位、誤入的 commit 區塊）；取樣器 v4 相容已唯讀查證 |

**待填實測值**：`BASE_FLOOR`（Task 6 Step 0）、遷移後分佈與 `probes_needed`（Task 4 Step 5）。

**遷移前基線**（正式機線上一致性備份，2026-09-22 08:24 UTC；本機複本
`<scratchpad>/explore.copy.db`，schema v3，遷移 dry-run 用）：

| completeness | reason | evidence_unknown | 列數 |
|---|---|---|---|
| complete | count_below_retention_threshold_probe_empty | 1 | 146 |
| complete | count_below_retention_threshold_probe_empty | 0 | 6 |
| complete | retention_boundary_verified | 0 | 135 |
| complete | count_below_retention_threshold | 0 | 7 |
| complete | count_below_retention_threshold | 1 | 6 |
| backfilling | NULL | 0 | 83 |
| partial | retention_limit | 0 | 9 |
| partial | retention_limit | 1 | 5 |

合計 397 列（active candidates 300）、`fills` 1,051,273 筆、進行中遍歷 83 個。
遷移必須保住這 105 萬筆成交與 83 個遍歷的游標（D-G）。

## 部署前追加要求（使用者 2026-09-22，收到於 Task 11 派工後）

> 「除了先修部署後待辦第 1 項再部署，請修掉 Warning 1、針對性測試通過、確認背景工作可獨立停用與
> 資料庫回滾步驟，然後再部署。不必等所有 Warning 都消失，但不能把已知錯判完整問題留給正式環境驗證。」

| 要求 | 處置 | 證據 |
|---|---|---|
| 修掉 Warning 1 | Task 11（派工中） | 五條針對性測試＋反向護欄轉紅 |
| 背景工作可獨立停用 | `EXPLORE_UPSTREAM_REFRESH=0` → `run_api.py:95` 不起 scheduler thread、快照照端；寫成 RUNBOOK §5.8f **Step 7-pre**（比整體回退輕的第一道） | 補 `tests/test_run_api_wiring.py` 釘住「為 0 時不起 thread」（Task 12，主線程自做） |
| 資料庫回滾步驟 | 本機完整預演：v3 備份 → 遷移 v4 → 清 wal/shm 還原 → **舊程式 `dc76440` 開啟**讀到 v3、fills 1,051,273、舊 reason 179 列 | 記於 RUNBOOK §5.8f Step 7 |
| 已知錯判完整不留給正式環境 | Task 10 C1 已封（reviewer 反向護欄驗證）；Task 11 封 `no_earlier_activity` 單調性；W4 兩側皆 partial 不屬錯判完整，延後 | — |


| 01:00 | 0 | 0 | 126／142／32 | 51 | 0／0／0 | ok | scan_pages 8、pages 11（一波到期，脈衝） |
| 01:15 | 0 | 0 | 126／142／32 | 51 | 0／0／0 | ok | |
| 01:30 | 0 | 0 | 126／142／32 | 49 | 0／0／0 | ok | |
| 01:45 | 0 | 0 | 127／142／31 | 49 | 0／1／0 | hb 59s（單筆，週期內） | |

**01:55 排程檢查（+10h10m）**：unknown 97 → 100（新入池 +3；池內 31 → 35）、`earlier_fills_seen` 181 → 183、
`no_earlier_activity` 20、`truncation_suspected` 147；completeness 73／186／191；`fills_verify` job 52 → 48；
running scans 69 → 73；Traceback 0；429 0；follower 不變；cron.err 0。**判定：安全面正常，不回退、不走 Step 7-pre。**
遍歷軌呈預期的脈衝（Task 12 排程缺陷），探測仍在解出（+2 earlier_fills_seen、verify job 持續遞減）。

## 部署後待辦（Task 11 候選，來自兩輪審核；均非本次回歸，不擋部署）

1. **`no_earlier_activity` 的單調性不成立**（`_applicable_boundary` 對正面證據一律 `<=`）：帳戶在舊窗口內
   才開始活動，窗口往前滾後「新起點之前無活動」並不成立；配合「正面證據終局＋不重探」會永久鎖定 complete。
   觸發需要 HL 真的截斷（本次實測不會），落在錯判完整方向。修法：該狀態改為 `==` 才適用（complete 位址不重掃、
   窗口不滾，實務上不會造成重探洪峰）。與 W4（`truncation_suspected → unknown` 降級不重算）併案。
2. `config.py` 對 `FILET_EXPLORE_FILLS_PERIOD_S` 的「下界」註解已與程式脫節（只管熱門）；RUNBOOK 已更正，config 註解待改。
3. Task 7b 政策測試的合成分佈與 docstring 未隨 W1 更新（實算 26.54，門檻 30）；正式機真實分佈實算 22.76。
4. 單位址增量需求上限從 1/6h 升到 1/1h：非熱門且 fph ≥ 267 的位址從 2 個增到約 30 個就會破 30 頁/小時，
   線上無指標——RUNBOOK §5.8f 已加一條 DB 查詢當觀測。
5. 被推遲的 `finished_at` 會出現在對外 `evidence.finished_at`（觀測失真，不影響判準）。

## 部署後觀測日誌（第八次部署 2026-09-22 15:45:03 UTC；24h 窗至 2026-09-23 15:45 UTC）

| 時間 (UTC) | 429 | Traceback | complete／partial／backfilling（對外） | verify_backlog | fills 類 due | follower | 備註 |
|---|---|---|---|---|---|---|---|
| 15:30（基線） | 0 | 0 | 144／131／25 | 113 | 50／22／17 | ok | 部署前最後一筆 |
| 16:00 | 0 | **5**（皆非本次） | 109／145／46 | 112 | 38／24／22 | hb 1s、errors 0 | `evidence.unknown 0`、`external_complete 144`；`scans_finished_15m 180` |
| 16:15 | 0 | 1（已知類） | 109／146／45 | 111 | 18／21／22 | hb 0s、errors 0 | overdue p95 fills 3207→1680s |
| 16:30 | 0 | 0 | 109／149／42 | 108 | 18／17／20 | ok | |
| 16:45 | 0 | 0 | 109／149／42 | 104 | 19／17／16 | ok | overdue p95 fills 1214s、fills_scan 1161s（持續下降） |

**16:55 排程檢查（+70 分）**：DB `left_boundary` unknown **272 → 261**、`earlier_fills_seen` 144、
**`truncation_suspected` 12**、`no_earlier_activity` 1；completeness 90／144／184；`fills_verify` job 112 → 102（遞減中）；
running scans 95 → 86；follower 時間戳不變、hb 0–1s、errors 0；1 小時內 Traceback 6 個**全部**是已知的
`PermissionError fbac652…`（使用者瀏覽 ops 面板），非已知類 0；cron.err 0。**判定：正常，不回退。**

觀察：探測解出率約 9 個/小時（3:1 份額下估 14/小時，同量級偏慢，尚在 fills 積壓消化期）。
`truncation_suspected` 12 個占已解出探測的一半以上——這些是「探測窗回空、但 portfolio 首次活動明顯早於窗口」
的位址。合理懷疑其中一部分不是 HL 截斷，而是帳戶**先入金、很久之後才開始交易**（`first_activity_ms` 是權益
歷史起點，不是首筆成交；Task 3 設計要點已標註此限制）。方向安全（判 partial 不判 complete），
但會壓低大戶轉 complete 的數量——列入部署後待辦：為 `truncation_suspected` 補一個便宜的二次證據
（例如把探測窗從 1 天拉到 7 天再探一次；仍回空且首筆成交可由其他來源證實晚於窗口 → 改判 `no_earlier_activity`）。

| 17:00 | 0 | 0 | 108／149／43 | 101 | 10／17／13 | ok | |
| 17:15 | 0 | 0 | 108／151／41 | 98 | 11／15／10 | hb 59s（單筆，週期內） | |
| 17:30 | 0 | 0 | 110／152／38 | 94 | 9／12／6 | ok | complete 首次上升 |
| 17:45 | 0 | 0 | 110／153／37 | 91 | 13／12／5 | ok | overdue p95 fills 635s、fills_scan 647s |

**17:55 排程檢查（+2h10m）**：unknown **261 → 248**（穩定約 −13/h）、`earlier_fills_seen` 144 → 147、
`truncation_suspected` 12 → **23**、`no_earlier_activity` 1 → 2；completeness 83／146／191；`fills_verify` job 102 → 89；
running scans 86 → 79；1 小時內 Traceback **0**；429 0；follower 不變；cron.err 0。**判定：正常，不回退。**

**`truncation_suspected` 量化**（唯讀查 DB，`fills.time_ms`）：23 個之中，**0 個本地 `fills` 表已存有早於探測窗的成交**。本地 `fills` 表**沒有**任何一個存有早於探測窗的成交（3 個完全無成交）——本地資料無法反證，需靠更寬的探測窗（7 天）才能分辨「低頻」與「截斷」。
**待辦升級為部署後第一優先**：探測前先查本地 `fills` 是否已有早於窗口的成交（零 API 成本、直接判 `earlier_fills_seen`）；
沒有才發探測，且探測窗改 7 天。方向仍安全（現況只是少判 complete）。

| 18:00 | 0 | 0 | 109／155／36 | 87 | 5／10／2 | ok | overdue p95 fills 244s、verify 1150s |
| 18:15 | 0 | 0 | 110／156／34 | 84 | 5／10／0 | ok | fills_verify 逾期歸零 |
| 18:30 | 0 | 0 | 110／157／33 | 84 | 6／9／0 | ok | |
| 18:45 | 0 | 0 | **112**／159／29 | 84 | 0／6／0 | ok | **fills 類逾期幾乎清空**（fills_scan p95 13s） |

**18:55 排程檢查（+3h10m）**：unknown **248 → 230**（−18/h，加速）、`earlier_fills_seen` 147 → 153、
`truncation_suspected` 23 → 33、`no_earlier_activity` 2 → 4；completeness 73／152／195；`fills_verify` job 89 → 84；
running scans 79 → 71；Traceback 0；429 0；follower 不變；cron.err 0。**判定：正常，不回退。**
fills 類積壓已從部署時的 50／22／17（due_n）收斂到 0／6／0——這是 Task 5b 週期分層釋出額度的直接效果。

| 19:00 | 0 | 0 | 114／161／25 | 84 | 1／1／0 | ok | |
| 19:15 | 0 | 0 | 117／161／22 | 84 | 0／0／0 | ok | 全部 kind 逾期為 0 |
| 19:30 | 0 | 0 | 118／160／22 | 81 | 0／0／1 | ok | |
| 19:45 | 0 | 0 | **120**／159／21 | 80 | 0／0／0 | ok | |

**19:55 排程檢查（+4h10m）**：unknown **230 → 193**（−37/h，積壓清空後探測加速）、`earlier_fills_seen` 153 → 163、
`truncation_suspected` 33 → **60**、`no_earlier_activity` 4 → 5；completeness 67／160／194；`fills_verify` job 84 → 78；
running scans 71 → 66；Traceback 0；429 0；follower 不變；cron.err 0。**判定：正常，不回退。**
對外 complete 部署時 109 → 120，穩定上升；`truncation_suspected` 已達 60，是 complete 增長的主要天花板（1 天探測窗待辦）。

| 20:00 | 0 | 0 | 122／158／20 | 78 | 0／0／1 | ok | |
| 20:15 | 0 | 0 | 123／158／19 | 76 | 5／0／0 | ok | |
| 20:30 | 0 | 0 | 124／157／19 | 76 | 0／0／0 | ok | |
| 20:45 | 0 | 0 | **125**／156／19 | 76 | 0／0／0 | ok | eligible 80 → 82 |

**20:55 排程檢查（+5h10m）**：unknown **193 → 160**（−33/h）、`earlier_fills_seen` 163 → 166（+3）、
`truncation_suspected` 60 → **89**（+29）、`no_earlier_activity` 5 → 9；completeness 65／164／195；`fills_verify` job 78 → 75；
running scans 66 → 65；Traceback 0；429 0；follower 不變；cron.err 0。**判定：正常，不回退。**
觀察：這一小時解出的探測 ~9 成落在 `truncation_suspected`——1 天探測窗對這批（多為低頻）帳戶幾乎必然回空。
不影響安全（全部判 partial），但代表 complete 的自然上限在 ~170 附近，剩下要靠待辦（本地 fills 反證＋7 天探測窗）。

| 21:00 | 0 | 0 | 122／157／21 | 74 | 0／0／0 | ok | complete 125→122：候選池換血的小幅波動 |
| 21:15 | 0 | 0 | 122／157／21 | 74 | 1／1／1 | ok | |
| 21:30 | 0 | 0 | 125／156／19 | 70 | 0／0／0 | ok | |
| 21:45 | 0 | 0 | **127**／154／19 | 70 | 0／0／0 | ok | eligible 85 |

**21:55 排程檢查（+6h10m）**：unknown **160 → 124**（−36/h）、`earlier_fills_seen` 166 → 169、`no_earlier_activity` 9 → 16、
`truncation_suspected` 89 → 117；completeness 65／171／190；`fills_verify` job 75 → 69；running scans 65；
Traceback 0；429 0；follower 不變；cron.err 0。**判定：正常，不回退。**

| 22:00 | 0 | 0 | 128／152／20 | 68 | 0／0／0 | ok | |
| 22:15 | 0 | 0 | 130／150／20 | 67 | 0／0／0 | ok | |
| 22:30 | 0 | 0 | 133／147／20 | 67 | 0／0／0 | ok | |
| 22:45 | 0 | 0 | **133**／148／19 | 67 | 0／0／0 | ok | |

**22:55 排程檢查（+7h10m）**：unknown **124 → 102**（−22/h）、`earlier_fills_seen` 169 → 176（+7）、`no_earlier_activity` 16、
`truncation_suspected` 117 → 135；completeness 65／178／186；`fills_verify` job 69 → 64；running scans 65；
Traceback 0；429 0；follower 不變；cron.err 0。**判定：正常，不回退。**

| 23:00 | 0 | 0 | 135／144／21 | 64 | 0／2／0 | ok | |
| 23:15 | 0 | 0 | 137／144／19 | 64 | 0／0／0 | ok | |
| 23:30 | 0 | 0 | 138／144／18 | 64 | 0／0／0 | ok | |
| 23:45 | 0 | 0 | **138**／144／18 | 63 | 0／0／0 | ok | |

**23:55 排程檢查（+8h10m）**：unknown **102 → 102（持平）**、`earlier_fills_seen` 176 → 179、`no_earlier_activity` 16 → 17、
`truncation_suspected` 135 → 138；completeness 65／182／189；`fills_verify` job 64 → 61；running scans 65；
Traceback 0；429 0；follower 不變；cron.err 0。**判定：正常，不回退；Step 7-pre 不觸發（見下）。**

**unknown 持平的診斷（唯讀）**：102 個 unknown 裡 **70 個是已退池（inactive）列**（39 帶著永遠不會再被服務的
running scan、31 partial）——它們不在候選查詢的母體（`c.active=1`），不影響榜單。**池內 unknown 只有 31**
（25 個 backfilling 帶 running scan、6 個 partial）。這一小時仍解出 +7 個證據（179/17/138 各增），只是被新入池的
unknown 抵銷，探測**沒有停**。base 軌活著（15 分鐘內 clearinghouseState 152、portfolio/ledger 各 29 筆更新）、
增量軌活著（fills_sync 60 分鐘 20 筆更新，與 22/h 政策一致）。

**一個要盯的異常：遍歷軌近 45 分鐘 `scan_pages_15m = 0`**（23:00 還有 3），而 `pages_consumed_15m` 只有 2–7
（上限 ~15）＝**fills 額度大多閒置，卻沒有拿去抓遍歷頁**。53 個 `fills_scan` job attempts 全 0、無 last_error、
0 個到期但 min next due ≈ 0、隨時 1 個持有 lease——型態像「領到 job → 探測前置／續抓被 deferred → 重排到 now」
的空轉。無法從 DB 判定原因（限流器暫停狀態、deferred 計數、probe 計數都只在 `/api/ops/health` 的
`explore_refresh`／`hl_budget` 區塊，需 admin session）。**不是安全問題**（429 0、無錯誤、榜單 complete 仍在升），
但若下一小時 `scan_pages_15m` 仍為 0 且 `fills_scan` 到期 job > 0，要請使用者貼 `/api/ops/health` 的
`explore_refresh` 與 `hl_budget` 兩段來判讀；不走 Step 7-pre（停背景工作對吞吐問題沒有幫助）。

| 00:00 | 0 | 0 | 128／146／26 | 61 | 1／1／1 | ok | scan_pages 7、pages 8（一波到期） |
| 00:15 | 0 | 0 | 131／145／24 | 60 | 0／0／0 | ok | **pages_consumed 0** |
| 00:30 | 0 | 0 | 131／145／24 | 60 | 0／0／0 | ok | pages 0 |
| 00:45 | 0 | 0 | 131／145／24 | 60 | 0／0／0 | ok | pages 0 |

**00:55 排程檢查（+9h10m）**：unknown 102 → 97（池內 31 不變）、`earlier_fills_seen` 179 → 181、`no_earlier_activity` 17 → 20、
`truncation_suspected` 138 → 147；completeness 69／185／191；`fills_verify` job 61 → 52；running scans 69；
Traceback 0；429 0（journal 任何層級 0 個限流／暫停訊號）；follower 不變；cron.err 0。**判定：安全面正常，不回退、不走 Step 7-pre。**

**遍歷軌停擺的根因已查明（唯讀，DB＋程式碼）——是排程缺陷，不是額度或限流**：
- 三個軌都活著：base 15 分鐘 150／37／48 筆更新；增量軌 15 分鐘 11 筆；API 正常端出。
- 但 **60 個 `fills_scan` job 全部 0 個到期、最近到期在 33 分鐘後、最遠 23.9h**；池內 16 個 `pages_done=0` 的新 scan 全部
  在等 >30 分鐘的 job，**15 個遍歷到一半（pages 5–7）的 scan 的 job 也排在 4–7 小時後**（attempts 0、無錯誤）。
- 機制：`_enqueue_address_jobs`（`explore_scheduler.py:1044-1054`）把 `fills_scan` 與 `fills` 一起用
  `now + _spread(address, fills_period)` 排——`_spread` 是「地址尾碼 % 週期」的首次到期分散。Task 5b 之後非熱門週期
  最長 24h，所以新入池地址的**第一頁遍歷**可以等到 24 小時後。**程式碼查證**：`_ensure_scan_job`（`:678`）補排
  resume／initial 的 job 本來就用 `now`（只有 `verify_needed` 才分散）——但同一輪 candidates 裡 `_enqueue_address_jobs`
  **先**跑（對剛 bootstrap／再入池的地址建了延後的 job），`_ensure_scan_job` **後**跑看到 job 已存在就跳過；
  `store.enqueue` 對既有 job 只會 `MIN` 提早、不會延後，所以那個延後的 job 就一直活著。遍歷到一半的 15 個 scan
  正是「退池→再入池」的地址：退池時 job 被刪、running scan 列保留，再入池時被 `_enqueue_address_jobs` 用
  `now + _spread(period)` 重建。部署前同一段程式用全域 6h 週期（spread ≤ 6h），本次把它放大到 24h。
- 影響：吞吐（榜單 complete 從 109 升到 131 後趨緩），**非安全**（429 0、follower 正常、無錯誤）。fills 額度大多閒置。

**Task 12（待使用者核可後再部署）**：遍歷軌的節奏是「逐頁」，不該用增量週期分散——**只改一處**：
`_enqueue_address_jobs`（`explore_scheduler.py:1044-1054`）對 `fills_scan` 排 `now`（至多 ≤60s jitter 防同秒），
`fills`（增量）維持 `_spread(period)`；`_ensure_scan_job` 不動（已是 `now`）。加測試：(1) 新 bootstrap 地址的 initial
scan job 在 60 秒內到期；(2) harness 候選池換血情境——退池（job 被刪、running scan 保留）→ 再入池 → scan job 在
60 秒內到期且續跑同一 `scan_id`；(3) `fills` job 仍被 `_spread` 分散（不得順手改壞增量分散）。**本次觀測期內不部署**；下一輪檢查只看安全指標，
吞吐指標（scan_pages）預期在 job 陸續到期時呈脈衝式，不再視為異常。


**16:00 的 5 個 Traceback 已查明與本次部署無關**：全部是 `GET /api/ops/trade-quality` → `ops.py:157 load_skipped_notional`
→ `PermissionError: /opt/filet/state/fbac652…/var/copytrade/skipped/2026-09-21.json`。該端點在部署範圍內零改動
（`git diff dc76440..HEAD -- app.py | grep trade-quality` 無命中）、路徑在 `/opt/filet/state`（不在 rsync／chown 的
`/opt/filet/spark` 範圍）、`fbac652…` 是 2026-09-07 手動 disable 的舊 follower（unit disabled/inactive）；
部署前 24h 該端點 0 次請求所以 0 次 500——是使用者部署後開 ops 面板才觸發。**不構成回退條件**；
觀測排程 v2 已把這類 Traceback 排除在回退判準外，只計數。非 `/api/ops/` 路徑的 5xx 為 0。

**新增待辦（非本次回歸）**：`ops.py:157` 讀取已歸檔／已停用 follower 的 `skipped/*.json` 應容錯
（`FileNotFoundError`／`PermissionError` → 視為 0 或標 unknown），不得讓 ops 面板 500。

## 資料極限與未決事項（誠實標註）

1. 「HL 不強制 10,000 筆留存」是**單一地址、單一時點**的實測（26,976 筆）＋正式機 8 個
   地址的旁證。HL 文件仍寫著 10,000。這正是本計畫不把「筆數多」當成完整證據、
   而改用左界探測的原因：若上游真的在某些帳戶上截斷，會被判 `truncation_suspected`，
   不會冒充 complete。
2. `first_activity_ms` 來自 portfolio `allTime` 序列首點——是**權益**歷史、經降採樣，
   不等於首筆成交時間。因此只用於「明顯早於／明顯晚於窗口起點」的粗判斷，模糊帶維持 unknown。
   若之後發現有更可信的帳戶年齡來源，這是第一個該換掉的輸入。
3. `BASE_FLOOR` 在 Task 6 Step 0 實測前是推導值（約 120 權重/分鐘），不得直接採用。
4. 本次不動 D14 顯示契約（D-D）。修完後仍停在 `left_boundary_unknown` /
   `left_boundary_truncated` / `local_page_cap` 的位址會繼續顯示「分析待完成」——
   這是刻意的：沒有證據就不宣稱完整。屆時再議是否改成標註觀測區間後顯示。
5. `fills_in_window` 自本次起是**上界**（含游標重疊，實測高估約 3.7%），不是精確筆數；
   它已不是任何判準的輸入，僅供觀測與 Task 5 的速率估算（估高 → 週期估短 → 偏保守，方向安全）。
