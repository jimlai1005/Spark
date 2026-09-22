"""src/spark/publicapi/explore_fills_sync.py
遍歷軌／增量軌雙游標同步演算法（spec
`docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md` §8；plan
`docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 2.2／7.9b B2）。

純函式，**零網路、零 sleep**、不碰 `ExploreStore` 以外的任何 IO——worker 每輪只抓一頁，
呼叫端（`explore_scheduler`）負責：規劃這輪抓哪一段 → 打 HL `userFillsByTime` → 套用
一頁算出新狀態與是否結束 → 呼叫端自行落地並視 `done` 決定要不要排下一頁。

Task 7.9b（2026-09-21 使用者第二輪裁決）起，舊版單一 `plan_page`／`apply_page`
（同時承載「全區間遍歷」與「增量」兩種輪）拆成兩軌互不覆蓋：

- **遍歷軌**（`plan_scan`／`apply_scan_page`，對映 `ExploreStore.FillsScan`）：
  一次全區間遍歷（`initial`／`partial_rescan`／`verify`），完成後 `result`／
  `reason` 透過 `ExploreStore.complete_scan` 的 CAS 寫回 `fills_sync`。三種
  `kind` 的頁面套用邏輯完全相同，差別只在窗口與 CAS 之後的排程動作，見
  `explore_scheduler._run_scan`。Task 1 起（見下）`apply_scan_page` 本身
  不再判定 `result`／`reason`——那是 `scan_verdict` 的職責，`apply_scan_page`
  只回報遍歷進度與停止原因。
- **增量軌**（`plan_incremental`／`apply_incremental_page`，對映
  `ExploreStore.FillsSyncState`）：只延伸 `synced_through_ms`，**永不**判定
  `completeness`／`reason`——這兩欄完全由遍歷軌的 CAS 決定，增量軌讀寫時
  原樣帶著目前值（`dataclasses.replace` 不動它們），確保重掃期間增量仍能
  持續保存新成交、不被遍歷覆蓋、也不會覆蓋遍歷（B7 (i)）。

Task 1（2026-09-22，D-A／D-E 裁決，見
`docs/superpowers/plans/2026-09-22-explore-fills-coverage-verdict-fix.md`
「背景：根因與證據」）：舊版留存判準——HL 文件「`userFillsByTime` 只保留最近
10,000 筆可查」推出的 `RETENTION_SAFETY_THRESHOLD`（8,000）——**已被實測推翻**：
對成交筆數最多的位址做 30 天窗口連續分頁，實測取回 26,976 筆不重複成交且尚未
走完。沿用該門檻會把成交量最大的帳戶永久誤判為「不可能完整」。三個相關常數
（`HL_FILLS_RETENTION_LIMIT`／`RETENTION_SAFETY_MARGIN`／
`RETENTION_SAFETY_THRESHOLD`）與 `apply_scan_page` 的 `retention_threshold`
參數已一併移除；`apply_scan_page` 不再對短頁收尾下任何完整性結論。

新判準（`scan_verdict`，本模組）：覆蓋結論改成「證據合成」——`complete` 需
同時具備三項證據：(1) 窗口**左界**證據（`LeftBoundary`，由
`explore_scheduler._run_probe` 前置探測產生，Task 3）、(2) 分頁**無未解缺口**
（`FillsScan.unresolved_gap`）、(3) 游標抵達**固定的** `window_end_ms`
（`FillsScan.cursor_ms >= window_end_ms`）。`apply_scan_page` 只負責回報「這一輪
跑到哪、為什麼停」，**不判斷完整性**——`result`／`reason` 完全交給呼叫端在取得
`LeftBoundary` 之後呼叫 `scan_verdict` 決定，本函式對這兩欄不寫入任何值。

**我方停止 ≠ 上游沒有**（D-F 語義界線）：`FillsScan.stop_reason` 的值域
（`local_page_cap`／`no_progress`／`same_ms_overflow`）全部描述「本系統為什麼
不再往下抓」，**不描述**上游留存邊界——我們對上游的留存範圍沒有任何直接證據，
唯一的證據來源是 `LeftBoundary` 探測。任何以 `stop_reason` 為由宣稱「上游資料
不足」都是本模組明確禁止的表述。

`cursor_ms` 推進採 inclusive 重疊（下一頁 `startTime` = 上一頁最後一筆的 `time`，
**游標不額外遞增**）——同一毫秒可能跨頁被拆散，去重交給 `fills` 表的 `(address, coin, tid)`
PRIMARY KEY，本模組不做去重判斷。同毫秒佔滿整頁（`same_ms_overflow`）代表我們
無法在不漏單的前提下前進，是真正的分頁缺口，標記 `stop_reason="same_ms_overflow"`
且 `unresolved_gap=1`（`scan_verdict` 一律判 `partial`，不論左界證據多強）；理論上
不可能發生的「滿頁但游標未推進」（`no_progress`，防禦用）只標記 `stop_reason`，
不影響 `unresolved_gap`。兩者都終止本輪，避免無界重試同一頁。

`max_pages_per_round`（預設 20）不是停止條件，是**暫停**：達到時只推進游標與
`pages_done`，`result`／`stop_reason` 兩者皆維持 `None`，`scan` 仍是
`running`——呼叫端（`explore_scheduler._run_scan`）落地進度後直接重排，下一輪
從同一個游標續抓，不產生任何完整性結論（D-E 明文禁止「預算耗盡當結論」）。
真正會終止整次遍歷的是 `MAX_PAGES_PER_SCAN`（我方單次遍歷的絕對頁數上限，
Task 2）：達到時標記 `stop_reason="local_page_cap"`（**不是** `page_cap`——
`local_` 前綴刻意表達「本系統停止回補」而非對上游留存的主張，D-F）並終止本輪。

`partial` 的復原路徑：增量軌不做完整性判定，`partial` 若像 `complete` 一樣被增量軌
悄悄「延伸」也不會改變它的證據狀態——真正的復原只能靠遍歷軌重新做一次全區間遍歷
（`partial_rescan`，`explore_scheduler` 在遍歷完成 `result=="partial"` 時自動排程
下一次，見 `PARTIAL_RESCAN_AFTER_S`／`partial_rescan_due`）。
"""
from __future__ import annotations

import dataclasses
from typing import NamedTuple

from spark.exchange.base import USER_FILLS_PAGE_LIMIT
from spark.publicapi.explore_store import (REASON_LEFT_BOUNDARY_NO_ACTIVITY,
                                           REASON_LEFT_BOUNDARY_TRUNCATED,
                                           REASON_LEFT_BOUNDARY_UNKNOWN,
                                           REASON_LEFT_BOUNDARY_VERIFIED, REASON_UNRESOLVED_GAP,
                                           ExploreStore, FillsScan, FillsSyncState)

PAGE_LIMIT = USER_FILLS_PAGE_LIMIT   # HL userFillsByTime 單頁上限（同 hl.py 常數來源，Task 3.5 D）

# Task 2（D-A／D-F，2026-09-22）：單輪頁數上限只決定「本輪做到哪」；
# `MAX_PAGES_PER_SCAN` 是**我方**的回補上限（防止單一地址無上限佔用額度），
# 達到時的語義是「本系統停止回補」，不是「上游沒有更多資料」——我們沒有任何
# 證據支持後者。實測大戶 30 天窗口約需 18 頁，400 頁留了充足餘裕。
MAX_PAGES_PER_SCAN = 400

WINDOW_DAYS = 30
OVERLAP_MS = 1
_DAY_MS = 86_400_000

# Task 7.9a 補（2026-09-21 主線程裁決）：`plan_incremental` 沒有收到明講的
# `period_s` 時的保底值。Task 5（2026-09-22，D-B／D-H）起，生產路徑
# （`explore_scheduler._run_increment`）已改為每次呼叫都經
# `ExploreScheduler.fills_period_s_for(address)` 逐地址算出 `period_s`
# 顯式傳入——這個常數不再是「單一字面值來源」，只是呼叫端沒有明講時的保底值
# （例如測試直接呼叫 `plan_incremental` 不帶 `period_s`）。
DEFAULT_FILLS_PERIOD_S = 6 * 3600

# 這個值只在呼叫端沒有明講 `period_s` 時才會用到（見上）。
_DEFAULT_INCREMENTAL_PERIOD_S = DEFAULT_FILLS_PERIOD_S

# Task 7.7 W2（partial 復原路徑）／Task 7.9b B3：`partial` 遍歷完成後，
# `explore_scheduler` 自動排程下一次 `partial_rescan`，間隔即此常數。
# Task 7.9c C1／D1（2026-09-22 裁決）：**秒制唯一來源**——7.9b 的毫秒版期限
# 常數被直接加到秒制的 `now` 上（`now + 86_400_000` 秒），重掃排到 1,000 天後。
# 排程時間一律用秒，毫秒只在「呼叫 planner」那一個邊界才換算。
# Task 7.9d-D D1（7.9c 複審 W3）：過渡期的毫秒別名已刪除，本模組不再提供任何
# 毫秒版本的重掃期限——排程期限只有這一個秒制常數可用（測試
# `test_partial_rescan_after_s_is_the_single_source_in_seconds` 守住它不復活）。
PARTIAL_RESCAN_AFTER_S = 24 * 3600


def partial_rescan_due(finished_at: float | None, now: float) -> bool:
    """`partial` 地址是否該重新做一次全區間遍歷（Task 7.9c D1）——單一判斷點，
    排程端不得另寫一份時間比較（工程原則 1：比較的兩個量同源、同單位）。

    `finished_at`＝該地址**最近一次完成**的 `fills_scan.finished_at`（秒）；
    `None`（從未完成過任何一次遍歷，或遷移列尚無完成時間）→ `True`
    （視為早該重掃，交給呼叫端的其他條件——例如「有沒有 running scan」——
    把關）。兩個參數都必須是秒制 epoch。"""
    if finished_at is None:
        return True
    return now - finished_at >= PARTIAL_RESCAN_AFTER_S


# Task 5（2026-09-22，D-B／D-H，見 plan
# docs/superpowers/plans/2026-09-22-explore-fills-coverage-verdict-fix.md
# 「背景：根因與證據」根因 2）：增量週期不再是單一常數——300 個位址每 6 小時
# 各抓一次已吃掉 fills 吞吐上限的 83%。改依「近期成交速率」逐地址決定週期，
# 目標是讓一個週期內的預期成交筆數落在一頁（`PAGE_LIMIT`）之內；`MIN_PERIOD_S`／
# `MAX_PERIOD_S` 是這個估計值的上下界（可被 `ApiConfig.explore_fills_period_s`／
# `explore_fills_max_period_s` 透過 `ExploreScheduler(fills_min_period_s=,
# fills_max_period_s=)` 覆寫，見 `explore_scheduler.fills_period_s_for`）。
MIN_PERIOD_S = 6 * 3600
MAX_PERIOD_S = 24 * 3600

# 留 20% 餘裕給成交速率波動——單週期預期筆數目標抓 `PAGE_LIMIT` 的 80%，不是
# 貼著上限算（貼滿的話速率稍微上升就會變成多頁補抓）。
TARGET_FILL_RATIO = 0.8

# `fills_per_hour` 分母保護，避免 0 造成除以零；不是「多小算沒有」的業務門檻。
_EPS_FILLS_PER_HOUR = 1e-6

# D-H：「前 50 名」的新鮮度需求——與 `explore_scheduler.ExploreScheduler` 建構子
# 的 `hot_rank`（預設同為 50，決定 job priority）是概念上相關但刻意獨立的兩個
# 常數：`fills_period_s` 的簽名只有 `(fills_per_hour, rank)` 兩個參數（見下、
# 與 plan Task 5 Step 1 的測試一致），沒有 hot_rank 參數可調；目前沒有呼叫端
# 會把 `hot_rank` 設成非 50，兩者數值上不會漂移，但這裡先誠實記下這個耦合，
# 不是憑空假設「反正一樣」。
_HOT_RANK = 50


def fills_period_s(fills_per_hour: float | None, rank: int | None, *,
                    min_period_s: float = MIN_PERIOD_S,
                    max_period_s: float = MAX_PERIOD_S) -> float:
    """增量週期的唯一公式（D-B／D-H）——`explore_scheduler.fills_period_s_for`
    是本函式在排程端**唯一**的取用點，所有到期判斷與重排都必須經它（Task 7.8
    教訓：到期條件與重排時間不得各自寫一份常數）。

    `fills_per_hour`：近期成交速率，本函式不關心怎麼算出來（呼叫端
    `explore_scheduler.ExploreScheduler.fills_period_s_for` 的責任，見該方法
    docstring——Task 5b 起優先用「當前生效那次遍歷」的觀測跨度密度，拿不到
    才退回 `fills_sync` 名目窗口小時數；兩者都含 `fills_in_window` 這個
    **上界**——游標重疊，實測高估約 3.7%——估高會讓這裡估出的週期偏短
    （更頻繁），方向保守）。`None`（尚無任何速率資料，例如位址剛入池）
    → 取保守值 `min_period_s`（不確定就抓密一點）。

    `rank`：`candidate.source_rank`（1-based，越小越熱門）。`None`（尚未併入
    最近一輪候選排名）與「已知但在前 50 名外」是兩種不同語意：
    - `rank` 落在前 `_HOT_RANK` 名：新鮮度需求優先於速率估計，一律
      `min_period_s`，不論實際成交速率多低（D-H：不得因為低速率就把熱門位址
      排到 24 小時）。
    - `rank` 已知但在 `_HOT_RANK` 名外：套用速率公式並夾在
      `[min_period_s, max_period_s]`——下界對這一類位址也適用（D-H：「其餘：
      6h（下界，不是只給 hot）」），代價是極端高速率的位址可能單週期預期筆數
      超過一頁、下一輪要多頁補抓；這是刻意的取捨，不是本函式要保的不變式。
    - `rank` 為 `None`（尚未有排名資訊）：只套用 `max_period_s` 上限，
      **不套用下界**——讓「單週期預期成交筆數 ≤ PAGE_LIMIT」這個核心不變式
      （見 `test_period_keeps_expected_fills_within_one_page`）在沒有名次可
      仰賴時仍然成立。"""
    if fills_per_hour is None:
        return min_period_s
    if rank is not None and rank <= _HOT_RANK:
        return min_period_s
    raw_s = (TARGET_FILL_RATIO * PAGE_LIMIT
             / max(fills_per_hour, _EPS_FILLS_PER_HOUR) * 3600)
    if rank is None:
        return min(max_period_s, raw_s)
    return max(min_period_s, min(max_period_s, raw_s))


@dataclasses.dataclass(frozen=True)
class LeftBoundary:
    """窗口左界證據（Task 1，D-E／D-F）。`state` 值域見 `scan_verdict`：
    `unknown`（尚未探得證據）／`earlier_fills_seen`（探測到窗口起點之前仍有
    可查成交）／`no_earlier_activity`（帳戶在窗口起點前確無活動）／
    `truncation_suspected`（上游截斷嫌疑）。`window_start_ms` 是這份證據適用
    的窗口起點（單調性見 Task 3 `explore_scheduler._run_probe` 的 docstring），
    `at` 是取得時間（epoch 秒）。探測機制本身是 Task 3 的範圍——本模組只定義
    這個型別與消費它的 `scan_verdict`。"""
    state: str
    window_start_ms: int | None
    at: float | None


def scan_verdict(scan: FillsScan, boundary: LeftBoundary) -> tuple[str, str]:
    """唯一的覆蓋結論來源（Task 1，D-A／D-E）。三項證據同時成立才 `complete`：
    (1) 分頁無未解缺口、(2) 游標抵達固定的 `window_end_ms`、(3) 左界證據為
    正面（`earlier_fills_seen`／`no_earlier_activity`）。任何「我方停止」的
    情形（`scan.stop_reason` 非 `None`，即游標未抵達終點）都不得產生完整性
    結論（D-F）——`apply_scan_page`／`explore_scheduler` 不得繞過本函式另外
    寫 `result`／`reason`（工程原則 1：結論只能有一個來源）。"""
    if scan.unresolved_gap:
        return ("partial", REASON_UNRESOLVED_GAP)            # 分頁有未解缺口
    if scan.cursor_ms < scan.window_end_ms:
        return ("partial", scan.stop_reason)                 # 沒抵達固定終點：本系統停止回補
    if boundary.state == "earlier_fills_seen":
        return ("complete", REASON_LEFT_BOUNDARY_VERIFIED)
    if boundary.state == "no_earlier_activity":
        return ("complete", REASON_LEFT_BOUNDARY_NO_ACTIVITY)
    if boundary.state == "truncation_suspected":
        return ("partial", REASON_LEFT_BOUNDARY_TRUNCATED)   # 上游截斷嫌疑
    return ("partial", REASON_LEFT_BOUNDARY_UNKNOWN)         # 證據不足：不宣稱完整，也不宣稱截斷


# Task 7.5 點 4／7.6 點 8（查詢參數留證）：`hl.get_fills_page` 實際請求體只送
# `type/user/startTime/endTime`——刻意不送 `aggregateByTime`。字串只描述「送了
# 什麼參數」這個事實本身，**不宣稱**上游會用什麼值當預設。
PARAMS_FP = "aggregateByTime=<omitted>"


def fresh_scan_window(now_ms: int, window_days: int = WINDOW_DAYS) -> tuple[int, int]:
    """新的全區間遍歷窗口 `[now - window_days, now]`——`initial`／
    `partial_rescan`／`verify` 三種 scan 建立時共用同一份算法（Task 7.9b B1／B3）。"""
    return now_ms - window_days * _DAY_MS, now_ms


class IncrementalPlan(NamedTuple):
    """`plan_incremental()` 的輸出：增量軌這一輪要向 HL 要哪一段 `[start_ms,
    end_ms]`。`next_due_ms` 只有 noop 計畫才會填，值＝下次應該重新呼叫
    `plan_incremental()` 的時刻（`window_end_ms + period_ms`）——排程端只能
    用它重排，不得另算一份週期常數（工程原則 1：比較的兩個量要同源，
    Task 7.8 的教訓）。"""
    start_ms: int
    end_ms: int
    state: FillsSyncState
    next_due_ms: int | None = None

    @property
    def is_noop(self) -> bool:
        return self.start_ms == self.end_ms


class IncrementalResult(NamedTuple):
    """`apply_incremental_page()` 的輸出——`state` 只更新增量軌自己的欄位
    （`cursor_ms`／`synced_through_ms`／`observed_from_ms`／`observed_to_ms`／
    `pages_done`／`last_error`／`updated_at`），**不動** `completeness`／
    `reason`／`scan_id`／`evidence_unknown`／`coverage_gap`（遍歷軌專屬，
    見模組檔頭）。"""
    state: FillsSyncState
    accepted: list[dict]
    done: bool
    note: str | None


def plan_incremental(state: FillsSyncState, *, now_ms: int, period_s: float = None,
                     overlap_ms: int = OVERLAP_MS) -> IncrementalPlan:
    """增量軌：只延伸 `synced_through_ms`，永不覆蓋遍歷軌的 `completeness`／
    `reason`。`state` 必為非 `None`（Task 7.9b B1：新地址建立時
    `ExploreStore.bootstrap_address_fills` 已同時建好 `fills_sync` 列，增量軌
    从此恆有列可讀）。

    「輪進行中」：`cursor_ms` 大於目前基準（`synced_through_ms`，首次尚未
    完成任何一輪時用 `inc_from_ms`）代表本輪尚未跑到底，續抓，不看週期。

    Task 7.9c D6：`state.inc_from_ms` **保證非空**——非空性在寫入邊界
    （`ExploreStore.bootstrap_address_fills`／`insert_fills_page`／v2→v3 遷移）
    與啟動檢查（`ExploreStore.__init__`）就守住了，本函式不再對 `None` 留
    退路（舊版的 `baseline or state.inc_from_ms` 退路會在 `baseline == 0`
    時誤取 `inc_from_ms`，而且讓「不變式破了」變成靜默的錯誤資料）。
    """
    period_s = DEFAULT_FILLS_PERIOD_S if period_s is None else period_s
    period_ms = int(period_s * 1000)
    baseline = state.synced_through_ms if state.synced_through_ms is not None \
        else state.inc_from_ms
    if state.cursor_ms > baseline:
        return IncrementalPlan(start_ms=state.cursor_ms, end_ms=state.window_end_ms, state=state)
    if now_ms - state.window_end_ms >= period_ms:
        new_end = now_ms
        new_cursor = max(state.inc_from_ms, baseline - overlap_ms)
        new_state = dataclasses.replace(
            state, window_end_ms=new_end, cursor_ms=new_cursor, pages_done=0)
        return IncrementalPlan(start_ms=new_cursor, end_ms=new_end, state=new_state)
    return IncrementalPlan(start_ms=baseline, end_ms=baseline, state=state,
                           next_due_ms=state.window_end_ms + period_ms)


def validate_page(page: list[dict], start_ms: int, end_ms: int) -> str | None:
    """回傳 `None` 代表合法；否則回傳簡短原因字串。公開名（Task 7.6 B5：
    `explore_scheduler._run_probe` 也要用它驗證探測回應是否真的落在探測窗內，
    不再是本模組私有的實作細節）。"""
    if not isinstance(page, list):
        return "not_a_list"
    prev_time: int | None = None
    for i, f in enumerate(page):
        if not isinstance(f, dict):
            return f"item_not_dict:{i}"
        for key in ("coin", "tid", "time"):
            if key not in f:
                return f"missing_field:{key}"
        try:
            t = int(f["time"])
        except (TypeError, ValueError):
            return "time_not_int"
        try:
            int(f["tid"])
        except (TypeError, ValueError):
            return "tid_not_int"
        if prev_time is not None and t < prev_time:
            return "time_not_ascending"
        if t < start_ms or t > end_ms:
            return "time_out_of_range"
        prev_time = t
    return None


def apply_incremental_page(plan: IncrementalPlan, page: list[dict], *, page_limit: int = PAGE_LIMIT,
                           max_pages_per_round: int = 20, now_ms: int) -> IncrementalResult:
    """套用一頁到增量軌。短頁／空頁 → `synced_through_ms` 推到 `end_ms`、本輪
    結束；滿頁 → 續抓（同遍歷軌的同毫秒溢位／無進展／頁數上限三個終止條件，
    純防禦，正常增量缺口極小很少觸發）。全程不判定 `completeness`／`reason`。

    Task 7.9c D2：本頁看到的成交時間併入 `observed_from_ms`／`observed_to_ms`
    （min／max，與 `apply_scan_page` 同一份規則）——7.9b 拆軌時漏掉這段，
    導致「首次遍歷之後才有成交」的新地址對外 `observed_from/to` 永遠是
    `None`（遍歷軌算出的極值也只留在 `fills_scan`，沒有併回 `fills_sync`；
    併回的那一半在 `ExploreStore.complete_scan`）。空頁不動極值。"""
    state = plan.state
    start_ms, end_ms = plan.start_ms, plan.end_ms

    invalid_reason = validate_page(page, start_ms, end_ms)
    if invalid_reason is not None:
        # Task 7.9d-D 補（2026-09-22 主線程裁決）：非法頁**不終止本輪**
        # （`done=False`）——非法頁是上游回應有問題（例如回傳窗口外的成交），
        # 不是「這一輪跑完了」的結論。游標、`synced_through_ms`、`pages_done`
        # 一律不動，只留 `last_error`，下一次從同一個游標再試；一頁都不收
        # （`accepted=[]`，不信任這一頁的任何內容）。遍歷軌同形處理見
        # `apply_scan_page`（那裡的舊行為更嚴重：`done=True` 但 `result` 仍
        # `None`，寫回時撞 `completeness` NOT NULL）。
        new_state = dataclasses.replace(
            state, last_error=f"invalid_page:{invalid_reason}", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=[], done=False, note=invalid_reason)

    times = [int(f["time"]) for f in page]
    observed_from = state.observed_from_ms
    observed_to = state.observed_to_ms
    if times:
        observed_from = min(times) if observed_from is None else min(observed_from, min(times))
        observed_to = max(times) if observed_to is None else max(observed_to, max(times))

    if len(page) < page_limit:
        new_state = dataclasses.replace(
            state, cursor_ms=end_ms, synced_through_ms=end_ms, observed_from_ms=observed_from,
            observed_to_ms=observed_to, last_error=None, updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True, note=None)

    if all(t == start_ms for t in times):
        new_state = dataclasses.replace(
            state, cursor_ms=start_ms, synced_through_ms=start_ms,
            observed_from_ms=observed_from, observed_to_ms=observed_to,
            last_error="same_ms_overflow", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True,
                                 note="same_ms_overflow")

    new_cursor = times[-1]
    if new_cursor == start_ms:
        new_state = dataclasses.replace(
            state, cursor_ms=start_ms, synced_through_ms=start_ms,
            observed_from_ms=observed_from, observed_to_ms=observed_to,
            last_error="no_progress", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True,
                                 note="no_progress")

    new_pages_done = state.pages_done + 1
    if new_pages_done >= max_pages_per_round:
        new_state = dataclasses.replace(
            state, cursor_ms=new_cursor, synced_through_ms=new_cursor,
            observed_from_ms=observed_from, observed_to_ms=observed_to,
            pages_done=new_pages_done, last_error="page_cap", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True,
                                 note="page_cap")

    new_state = dataclasses.replace(
        state, cursor_ms=new_cursor, observed_from_ms=observed_from,
        observed_to_ms=observed_to, pages_done=new_pages_done, last_error=None,
        updated_at=now_ms / 1000)
    return IncrementalResult(state=new_state, accepted=list(page), done=False, note=None)


class ScanPlan(NamedTuple):
    """`plan_scan()` 的輸出：遍歷軌這一輪要抓 `[start_ms, end_ms]`。"""
    start_ms: int
    end_ms: int
    scan: FillsScan


class ScanPageResult(NamedTuple):
    """`apply_scan_page()` 的輸出。`done=True` 時 `scan.status` 應由呼叫端
    （`explore_scheduler`）透過 `ExploreStore.complete_scan` 落地為
    `'done'`——本函式只回傳新的 `FillsScan`（`dataclasses.replace`，`status`
    欄位維持傳入值不變，呼叫端另外設）。"""
    scan: FillsScan
    accepted: list[dict]
    done: bool
    note: str | None


def plan_scan(scan: FillsScan) -> ScanPlan:
    """遍歷軌續抓：一律從 `scan.cursor_ms` 抓到 `scan.window_end_ms`——
    `initial`／`partial_rescan`／`verify` 三種 `kind` 的續抓邏輯完全相同。"""
    return ScanPlan(start_ms=scan.cursor_ms, end_ms=scan.window_end_ms, scan=scan)


def apply_scan_page(plan: ScanPlan, page: list[dict], *, page_limit: int = PAGE_LIMIT,
                    max_pages_per_round: int = 20, now_ms: int) -> ScanPageResult:
    """套用一頁到遍歷軌，算出新 `FillsScan` 與是否結束本輪。呼叫端負責把
    `plan.start_ms`／`plan.end_ms` 當成這次 `userFillsByTime` 的
    `startTime`／`endTime`。

    Task 1（D-A／D-E）：本函式**不判定完整性**——`result`／`reason` 全程不寫
    （只有 `scan_verdict` 有資格寫），終止本輪時只記錄 `stop_reason`（我方為何
    停止）與（同毫秒溢位時）`unresolved_gap`。呼叫端取得 `LeftBoundary` 之後
    呼叫 `scan_verdict(new_scan, boundary)` 才能得到真正的覆蓋結論。"""
    scan = plan.scan
    start_ms, end_ms = plan.start_ms, plan.end_ms

    invalid_reason = validate_page(page, start_ms, end_ms)
    if invalid_reason is not None:
        # Task 7.9d-D 補（2026-09-22 主線程裁決；S builder 在正式機情境抓到）：
        # 非法頁**不得終止遍歷**。舊版回 `done=True` 但 `result` 仍是 `None`
        # ——呼叫端據此把 scan 送進 `ExploreStore.complete_scan`，
        # `fills_sync.completeness` 被寫 NULL 撞 NOT NULL → `IntegrityError`
        # → job 隔離 24 小時、這一頁的資料也沒落地。改為續跑：游標／
        # `pages_done`／`fills_in_window` 都不動（這一頁完全不採信），只留
        # `last_error`，下一次 `plan_scan` 從同一個游標再試；`result`／`reason`
        # 維持 `None`（只有真正的終止條件才配寫結論）。
        new_scan = dataclasses.replace(scan, last_error=f"invalid_page:{invalid_reason}")
        return ScanPageResult(scan=new_scan, accepted=[], done=False, note=invalid_reason)

    times = [int(f["time"]) for f in page]
    observed_from = scan.observed_from_ms
    observed_to = scan.observed_to_ms
    if times:
        observed_from = min(times) if observed_from is None else min(observed_from, min(times))
        observed_to = max(times) if observed_to is None else max(observed_to, max(times))
    fills_in_window = scan.fills_in_window + len(page)

    if len(page) < page_limit:
        # D-E：短頁只代表「從游標起上游不再給」。游標推進到固定終點，
        # 結論交給 scan_verdict（需左界證據與無缺口才可能 complete）。
        new_scan = dataclasses.replace(
            scan, cursor_ms=end_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True,
                              note="reached_window_end")

    # 滿頁。
    if all(t == start_ms for t in times):
        # D-F：整頁同一毫秒，我們無法在不漏單的前提下前進——這是真正的分頁
        # 缺口（unresolved_gap=1），不是完整性結論；scan_verdict 對此一律 partial。
        new_scan = dataclasses.replace(
            scan, cursor_ms=start_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, stop_reason="same_ms_overflow", unresolved_gap=1,
            last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True,
                              note="same_ms_overflow")

    new_cursor = times[-1]
    if new_cursor == start_ms:
        new_scan = dataclasses.replace(
            scan, cursor_ms=start_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, stop_reason="no_progress", last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True, note="no_progress")

    new_pages_done = scan.pages_done + 1

    # Task 2（D-A／D-F）：`MAX_PAGES_PER_SCAN` 是本次遍歷的絕對硬上限（防止
    # 單一地址無上限佔用額度），達到才終止並標記 `stop_reason`——語義是「本
    # 系統停止回補」，不是對上游留存的主張，`local_` 前綴刻意表達這一點。
    # 這個檢查必須排在 `max_pages_per_round` 的暫停判斷之前：兩者的門檻若
    # 恰好同時整除（例如預設 20 整除 400），絕對上限要贏，才能確保
    # `local_page_cap` 只在真正達到絕對上限時出現。
    if new_pages_done >= MAX_PAGES_PER_SCAN:
        new_scan = dataclasses.replace(
            scan, cursor_ms=new_cursor, observed_from_ms=observed_from,
            observed_to_ms=observed_to, fills_in_window=fills_in_window,
            pages_done=new_pages_done, stop_reason="local_page_cap", last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True,
                              note="local_page_cap")

    # `max_pages_per_round`（預設 20）不是停止條件，是**暫停**：本輪做太多頁
    # 就先把已抓到的落地、把控制權交還給呼叫端（scheduler 下一次 tick 再從
    # 同一個游標續抓）——`result`／`stop_reason` 兩者皆不寫，`scan` 仍是
    # running（D-E：預算耗盡不是結論）。
    if new_pages_done % max_pages_per_round == 0:
        new_scan = dataclasses.replace(
            scan, cursor_ms=new_cursor, observed_from_ms=observed_from,
            observed_to_ms=observed_to, fills_in_window=fills_in_window,
            pages_done=new_pages_done, last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True,
                              note="round_paused")

    new_scan = dataclasses.replace(
        scan, cursor_ms=new_cursor, observed_from_ms=observed_from, observed_to_ms=observed_to,
        fills_in_window=fills_in_window, pages_done=new_pages_done, last_error=None)
    return ScanPageResult(scan=new_scan, accepted=list(page), done=False, note=None)


def external_coverage_state(sync: FillsSyncState) -> tuple[str, str | None]:
    """Task 7.9b B1／B6：對外 `completeness`／`reason`——`complete` 需同時
    `result == complete AND coverage_gap == 0 AND evidence_unknown == 0`；
    否則依序降級為 `partial`／`reason` 為 `coverage_gap` 或
    `evidence_unknown`。`backfilling`／`partial` 內部值本來就等於對外值，
    原樣透傳（gap／unknown 只有在內部值是 `complete` 時才可能造成降級）。

    Task 7.9c D7（gap 語義，2026-09-22 裁決，維持 7.9b 的「覆蓋區間」定義）：
    `coverage_gap` 問的是「**遍歷軌的窗口有沒有接上增量軌的起點**」
    （`scan.window_end_ms >= inc_from_ms`，見 `ExploreStore.complete_scan`），
    **不是**「增量軌有沒有落後」。增量軌落後（`synced_through_ms` 距今很久）
    只是 **stale**——資料舊，但 `[inc_from, synced_through]` 這段區間仍然
    是完整覆蓋的，下一輪增量就會補上，不該對外報成缺口。真正的 gap 只在
    遍歷窗口未接上增量軌起點時成立，來源只有三種：v2→v3 遷移把增量起點
    設錯（7.9b 第一版的事故）、時鐘異常、人工修資料。"""
    if sync.completeness != "complete":
        return sync.completeness, sync.reason
    if sync.coverage_gap:
        return "partial", "coverage_gap"
    if sync.evidence_unknown:
        return "partial", "evidence_unknown"
    return "complete", sync.reason


def build_fills_coverage(sync: FillsSyncState | None, store: "ExploreStore | None") -> dict:
    """對外 `fills_coverage` 字典的唯一組裝點（Task 7.9b B6）——探索清單
    （`explore_publisher.compose_rows`）與交易員詳情頁（`app.py
    ._local_trader_data`）共用（工程原則 1）。`sync is None`（尚未 enrich 過
    的候選／非本地路徑）→ 全部鍵為 `None`（`state="backfilling"`，與既有
    `DEFAULT_FILLS_COVERAGE` 語意一致）。

    `window_start`／`window_end`：**Task 7.9b 起語義變更**——改為增量軌覆蓋
    區間 `[inc_from, synced_through]`（不再是「目前這一輪」的查詢區間，那個
    語意現在放進 `evidence.window_start`／`evidence.window_end`，指向「建立
    目前 completeness 的那次遍歷」）。`evidence` 全部鍵可為 `None`：
    `sync.scan_id` 尚未指向任何完成的遍歷（例如全新地址仍在首次 backfilling）
    時，`store` 查無列或 `sync.scan_id is None` → `evidence=None`。"""
    if sync is None:
        return {
            "state": "backfilling", "observed_from": None, "observed_to": None,
            "reason": None, "synced_through": None, "last_success_at": None,
            "window_start": None, "window_end": None, "params_fp": None, "evidence": None,
        }
    state, reason = external_coverage_state(sync)
    evidence = None
    if sync.scan_id is not None and store is not None:
        scan = store.get_scan(sync.scan_id)
        if scan is not None:
            evidence = {
                "scan_id": scan.scan_id, "kind": scan.kind,
                "window_start": scan.window_start_ms, "window_end": scan.window_end_ms,
                "finished_at": scan.finished_at, "reason": scan.reason,
                "gap": bool(sync.coverage_gap), "unknown": bool(sync.evidence_unknown),
            }
    return {
        "state": state, "observed_from": sync.observed_from_ms,
        "observed_to": sync.observed_to_ms, "reason": reason,
        "synced_through": sync.synced_through_ms, "last_success_at": sync.updated_at,
        "window_start": sync.inc_from_ms, "window_end": sync.synced_through_ms,
        "params_fp": sync.params_fp, "evidence": evidence,
    }
