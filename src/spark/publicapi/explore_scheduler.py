"""src/spark/publicapi/explore_scheduler.py
Explore 排行榜背景排程（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§6 頻率／jitter／前 50 優先、§9.1 lease／fencing／dedupe／admission；
plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 3.1／7.9b）。

單一 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走
`ExploreStore`；所有 HL 呼叫走建構子傳入的 `hl`（正式接線必須是
`gateway.scoped("explore")`，wait_s=0——本模組不自己等額度，額度不夠就讓位給
下一輪 tick，spec §3 條件「超過預算就 stale、不暴衝」）。

Task 7.9b（2026-09-21 使用者第二輪裁決）：遍歷軌／增量軌分離之後，job kind 從
5 種變成 7 種：`candidates`（priority 0）、`state`（priority 0）、`portfolio`／
`ledger`（priority 1）、`fills`（**增量軌**，前 `hot_rank` 名 priority 2，其餘 3）、
`fills_scan`（**遍歷軌**：`initial` 建立時 priority 2、`partial_rescan` 排程時
priority 3）、`fills_verify`（**核驗遍歷**，遷移產生，只在同一 tick 沒有任何
`candidates`／`state`／`portfolio`／`ledger`／`fills`／`fills_scan` 到期時才領——
嚴格讓位，見 `_tick_once`）。`fills`／`fills_scan` 共用 `explore_fills` 保留額度
（`_fills_available()`），`fills_verify` 領工前也要求同樣的額度充足（避免用
「反正閒著」的錯覺擠掉真正該優先的核驗以外預算）。

Task 3（2026-09-22，D-E／D-F 裁決，見
docs/superpowers/plans/2026-09-22-explore-fills-coverage-verdict-fix.md）：
**左界證據探測前置**——`complete` 需要左界證據（`LeftBoundary`）是必要條件
（D-E），若證據排在遍歷完成之後才取得、又走輔助份額（每 9 頁才給 1 頁），
成交量最大的帳戶會遍歷完十幾頁卻卡在最後一步證據而持續空白。因此
`_run_scan` 在**每次**推進遍歷前先檢查 `ExploreStore.get_left_boundary`：
仍是 `unknown`（或證據不適用當前窗口）就先做一次探測（走這個 job 本來就在
消耗的 `explore_fills` 預算，不占輔助份額），探測成功或額度不足都直接收尾
這一輪，下一輪才續抓真正的分頁。證據對滾動窗口具**單調性**（正面證據一旦
取得永久有效，見 `explore_store.ExploreStore.get_left_boundary` docstring）
——多數地址一生只需要探測一次。

**這條前置路徑之外**仍保留舊版「純 DB 推導＋9:1 節流」的獨立探測機制
（`_serve_special`／`ExploreStore.next_probe_candidate()`）——它服務的是
「已經 `complete`／`partial`、目前沒有 job 在推進、但左界證據仍是 `unknown`」
的殘留地址（例如尚未到下一次 `partial_rescan` 的地址），走輔助份額（與
`fills_verify` 輪流）。兩條路徑共用同一個 `_run_probe`（Task 3 起改為寫
`ExploreStore.set_left_boundary`，不再是 scan_id 範圍的雙 CAS
`apply_probe_result`——左界證據是地址層級的冪等寫入，見該方法 docstring）。
`_tick_once` 每個 tick 開頭先問「這次機會給 fills-like（`fills`／`fills_scan`）
還是輔助類別（探測／`fills_verify`）」：雙方都有積壓時，輔助類別每 9 **頁**
fills-like 才輪到 1 次（`_fills_pages_since_special` 計數器，Task 7.9d-S S3 把
單位從 tick 改成頁面）；fills-like 這一側沒有到期工作時，輔助可以直接借用這次
機會（不必空等）。探測與工作領取互斥（同一個 tick 只做其中一件）。

容量估算與 Task 7.4b 修正（2026-09-21 使用者裁決：正式機證實 fills 類別級飢餓——
嚴格優先級＋沒有為 fills 一頁 120 weight 的大請求保留額度，state/portfolio/ledger
的穩態流量長期貼著 300 上限，fills 一小時拿不到一次額度）：
- 限流器父子 scope（`hl_budget.WeightLimiter` Task 7.4a）：`explore`（父，300）／
  `explore_base`（子，180）／`explore_fills`（子，120）。有 fills-like 待處理時，
  基礎類別走 `hl_base`（scoped `explore_base`，上限 180）、fills-like 走
  `hl_fills`（scoped `explore_fills`，保證每分鐘至少擠進一頁 120 weight 的
  `userFillsByTime`）；無 fills-like 待處理時基礎類別改走父 scope `hl`
  （可用滿 300）。
- 領工優先序＋等待加權：`store.claim_due` 的排序公式對等待越久的 job
  「升級」有效 priority，避免同 priority 內先到期的 job 被持續插隊的新到期
  job 永久排擠。
- 啟動時重排既有 overdue：第一個 tick 對 state／portfolio／ledger 各自呼叫
  `store.rebalance_overdue`。

Task 7.9c-S（2026-09-22 使用者第二輪裁決，7.9b 複審兩個 Critical 的修法）——
**遍歷軌 job 的生命週期一律由「狀態」推導，不由「job 列是否存在」推導**：
- `fills_scan` job 只有三個來源：(a) `_enqueue_address_jobs` 中
  `bootstrap_address_fills` 回 `True`（該地址第一次入池，真的新建增量軌）；
  (b) `_run_scan` 的 `partial` 收尾排 `now + PARTIAL_RESCAN_AFTER_S`（秒制單一
  來源，見 `explore_fills_sync`）；(c) `_run_increment` 跑完後的修復路徑
  （`partial` 且重掃已到期、沒有進行中的 scan、也沒有 `fills_scan` job）。
  既有地址在每個 candidates 輪（30 分）**不再**無條件補建 scan job——舊行為讓
  `complete` 地址每 ~3.5 小時被整窗重掃一次（正式機推估 2,000 次/日，吃光
  `explore_fills` 保留額度、餓死 `fills_verify`）。
- `_run_scan` 領到 `fills_scan` job 卻沒有進行中的 scan 時，只有
  「`completeness == "partial"` 且 `partial_rescan_due(最近一次 done scan 的
  finished_at, now)`」才開 `partial_rescan`；否則丟棄該 job（`scan_job_dropped`）。
- 秒／毫秒轉換只發生在呼叫 planner 的邊界（`int(now * 1000)`）；排程時間一律秒制。
- 準入（`ADMISSION_MULTIPLIER`）改為「先依狀態算出需要哪些 kind → 扣掉已存在的
  → 逐項准入」：容量滿只跳過**單一個** job（計 `admission_skipped`），不再讓整批
  候選失去補排；新候選（bootstrap 回 True）的 job 不受 cap 限制。
- `fills_verify` 仍先讓位，但改為**有界等待**：最舊的到期 verify job 等待
  `VERIFY_MAX_WAIT_S` 之後，本 tick 的 fills 類名額改給一個 verify
  （計 `verify_served_by_deadline`），避免永久飢餓。
- `complete_scan` 的回寫結果（`ScanWriteback`）四種各自收尾：`applied` 照舊、
  `duplicate` 記 info 後照常收尾、`stale` 記 warning 且**不**排下一次重掃、
  `missing` 記 warning 並丟棄 job——CAS 落空不再被當成正常收尾。

Task 7.9d-S（2026-09-22 使用者第二輪裁決，7.9c 複審的 Critical＋4 Warning）——
**狀態與工作對帳取代單點修復路徑**：
- `reconcile_scan_jobs(now)`：對每個 **active** 地址比對「狀態需要一次遍歷嗎」與
  「有沒有可推進的 `fills_scan` job」（`_needs_scan_job` 的三種原因碼
  `resume_running`／`initial_missing`／`partial_due`），缺 job 就補排。啟動首
  tick 與每一次 candidates 更新後各跑一次，冪等（重跑零變更）。這修掉 7.9c 的
  Critical：地址掉出候選池時 `delete_jobs` 刪掉 `fills_scan` job，回池時
  `bootstrap_address_fills` 回 False、舊修復路徑只認 `partial` → 回補中
  （`backfilling`）的地址永遠拿不回 scan job（正式機 69 個地址）。
  `resume_running` 補的 job 會**續跑同一個 `scan_id` 與游標**（`_run_scan` 見到
  進行中的 scan 就接著抓），不建新 scan、不整窗重抓。
- 退池（非 active）地址**發送前**就丟棄工作：`_run_scan`／`_run_increment`／
  `_run_probe` 在打上游之前查 `is_active`，非 active → 收尾 job、計
  `inactive_job_dropped`、回 `"dropped"`（多頁續頁同樣，每一頁都是一次新的領工）。
- `fills_verify` 與探測共用同一份**輔助份額**，以**頁面**計（不是 tick、不是
  job）：雙方都有積壓時每 `SPECIAL_SERVE_RATIO` 次實際發出的 fills-like 頁請求
  （增量／遍歷，含多頁的每一頁）才給一次輔助；`VERIFY_MAX_WAIT_S` 逾期只讓
  verify 排在探測**之前**，不觸發整批優先（7.9c 的「逾期即 claim」讓 8 件逾期
  verify 連佔 8 個名額、搶光增量）。fills 類沒有到期工作時輔助可連續。
- `status()` 的母體直接用 `ExploreStore` 的彙總查詢（`count_running_scans`／
  `count_jobs_by_kind`／`count_due_by_kind`，Task 7.9d-D），不再用
  `active_candidates()` 逐址點查（母體錯：正式機 129 筆 `fills_verify` 顯示
  112；代價 21ms/次）。
- 拆掉所有過渡相容層：`PARTIAL_RESCAN_AFTER_S`／`partial_rescan_due`／
  `ScanWriteback`／`store.job_kinds`／`latest_done_scan`／`running_scan`／
  `oldest_due_at(kinds=)` 一律直接用；`complete_scan` 回非 `ScanWriteback`
  時拋 `ScanWritebackContractError`（`TypeError` 子類，逸出不吞）。

單一 job 失敗不影響其他 job：例外分類（`BudgetExhausted`／`ScopePaused`／429／
transient／其他）各自決定下一次 `next_attempt_at`，thread 本身只在
`run_forever` 層被保護——不因單一 tick 的未預期例外死掉（spec §3 條件五）。
"""
from __future__ import annotations

import dataclasses
import logging
import random
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from spark.publicapi.explore_fills_sync import (MAX_PERIOD_S, MIN_PERIOD_S, PARAMS_FP,
                                                PARTIAL_RESCAN_AFTER_S, apply_incremental_page,
                                                apply_scan_page, fills_period_s,
                                                fresh_scan_window, partial_rescan_due,
                                                plan_incremental, plan_scan, scan_verdict,
                                                validate_page)
from spark.publicapi.explore_store import (ADMISSION_MULTIPLIER, JOB_KINDS, ExploreStore, Job,
                                           ScanWriteback)
from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused, is_rate_limited, weight_for
from spark.publicapi.hl_explore import ExploreConfig, _roi_sort_key, candidate_addresses

logger = logging.getLogger(__name__)

# Task 7.4b（2026-09-21 使用者裁決，fills 類別級飢餓修法）：一頁 `userFillsByTime`
# 預留的上限權重（與 `hl_budget.ENDPOINT_WEIGHTS["userFillsByTime"]` 同源，工程原則
# 1）——`_fills_available()` 用它判斷 `explore_fills` 保留額度是否夠讓一整頁擠進去。
FILLS_PAGE_WEIGHT = weight_for("userFillsByTime")

# Task 7.5 點 3（留存邊界探測）：查詢窗口起點之前一天——與
# `explore_fills_sync._DAY_MS` 同數值，不 import 該私有名稱（避免跨模組耦合
# 私有常數），供 `_run_probe` 算探測窗口用。
_PROBE_WINDOW_MS = 86_400_000

# Task 7.9b B4／7.9d-S S3：輔助類別（`fills_verify` 核驗遍歷＋留存邊界探測）與
# fills-like（`fills`／`fills_scan`）不可各半——雙方都有積壓時，每這麼多次**實際
# 發出的 fills-like 頁請求**（多頁遍歷的每一頁都算）才給輔助類別一次（使用者裁決
# 9:1；7.9d 第二輪裁決把計數單位從「tick」釘死成「頁面准入」，因為一個多頁 job
# 只算一次 tick 會讓輔助份額被低估）。
SPECIAL_SERVE_RATIO = 9


def _parse_iso8601_utc_epoch_s(value: str | None) -> float | None:
    """Task 8（D-C／D-I）：把 `FILET_EXPLORE_SPECIAL_SERVE_RATIO_UNTIL` 的
    ISO8601 UTC 字串解析成 epoch 秒。缺漏或格式錯誤一律回 `None`——呼叫端
    （`ExploreScheduler._special_serve_ratio`）據此 fail-safe 回預設值，不得
    讓一個打錯的 env 字串變成排程例外或啟動失敗。"""
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()

# Task 7.4b：非 fills 的四種 kind，領工時一起用 `kinds=` 限定（`claim_due` 的
# IN 子句）——candidates 本身也走這個集合，它不吃 explore_base／explore_fills
# 的區分（`_run_job` 直接用 `leaderboard_source_fn`，不經任何 scoped gateway）。
_BASE_KINDS = ("candidates", "state", "portfolio", "ledger")

# Task 7.9b：增量軌／遍歷軌共用同一份 `explore_fills` 保留額度（B3：「`fills`
# （增量）與 `fills_scan` 都走 `explore_fills` 保留額度」）。
_FILLS_LIKE_KINDS = ("fills", "fills_scan")

# Task 7.9a A2（2026-09-21 使用者裁決）：暫時性錯誤（連線／逾時／5xx）連續
# 達到這麼多次「實際嘗試」→ 改隔離 24 小時，不再無限期每次退避封頂 900 秒
# 後仍持續重試同一個地址。只計「真的發送過一次請求且失敗」的暫時性錯誤——
# `BudgetExhausted`／`ScopePaused`（既有 `bump_attempts=False`）與 429 都不算
# 一次嘗試，見 `_tick_once` 的例外分類。
MAX_JOB_ATTEMPTS = 8

# 準入上限（`refresh_job` 總數 ≤ `ADMISSION_MULTIPLIER` × active 候選數）的常數自
# Task 7.9e-D D3 起住在 `explore_store`（資料層無依賴，`app.py` 的詳情頁準入也用
# 它，不必為一個常數 import 排程模組）——見該處 docstring 的 6 種 per-address
# kind 說明。cap 只擋既有地址的補建，新候選一律放行（見 `_enqueue_address_jobs`）。

# Task 7.9e-S S1：`verify_needed` 補排時的首次到期分散窗口——與遷移產生 129 筆
# 核驗時的攤法同源（`ExploreStore._migration_spread_s`，48 小時內依地址雜湊），
# 一次補上幾十筆核驗不能全部排在同一秒。
VERIFY_SPREAD_S = 48 * 3600

# Task 7.9c-S S4／7.9d-S S3：`fills_verify` 的有界等待門檻——最舊的到期 verify job
# 等超過這個秒數時，**在輔助類別內**排到探測之前（7.9d 第二輪裁決：逾期只調整順序，
# 不觸發整批優先；舊版「逾期即 claim」讓 8 件逾期 verify 連佔 8 個名額、搶光增量）。
VERIFY_MAX_WAIT_S = 2 * 3600

# 每個地址可能存在的 job kind（準入 docstring 與 `status()` 的單一來源＝
# `ExploreStore.JOB_KINDS`，Task 7.9d-S S4：不在這裡另寫一份清單）；`candidates`
# 是全域 job（`address IS NULL`），不屬於任何地址。
_PER_ADDRESS_KINDS = tuple(k for k in JOB_KINDS if k != "candidates")

class _Unset:
    """`_needs_scan_job(running_scan_id=...)` 的哨兵——`None` 本身是有意義的值
    （「這個地址沒有進行中的遍歷」），不能拿來表示「呼叫端沒帶入」。"""


_UNSET = _Unset()


class ScanWritebackContractError(TypeError):
    """`ExploreStore.complete_scan` 沒有回傳 `ScanWriteback`（Task 7.9d-S S4：
    拆掉 fail-silent 的相容層——介面不符必須明確失敗，不得把未知回傳值悄悄
    當成某一種結果收尾）。刻意繼承 `TypeError` 並在 `_tick_once` 的例外分類
    之前重新拋出：這不是「這個 job 失敗」，而是程式介面錯誤。"""


# kind → endpoint_cache 的 endpoint 名稱（quarantine 時 `put_cache_error` 用；
# `fills`／`fills_scan`／`fills_verify` 不在這裡——它們的錯誤落地在
# `fills_sync.last_error`／`fills_scan.last_error`，見 `set_sync_error`／
# `set_scan_error`）。
_ENDPOINT_BY_KIND = {
    "state": "clearinghouseState",
    "portfolio": "portfolio",
    "ledger": "ledger",
}


def _spread(address: str, period_s: float) -> float:
    """首次到期分散（spec §6）：用地址尾碼打散，避免 300 個地址在同一秒到期。
    `period_s <= 0` 時不分散（避免 `% 0`，測試可能傳極端值關掉某個 kind）。"""
    if period_s <= 0:
        return 0.0
    return float(int(address[-8:], 16) % int(period_s))


def _is_5xx(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status is not None and 500 <= status < 600


def _roi_lookup(payload: dict, excluded: set[str]) -> dict[str, float | None]:
    """位址（小寫）→ roi（`hl_explore._roi_sort_key` 讀 stats-data month 窗；
    缺窗／解析失敗回傳的哨兵 `-Infinity` 一律轉成 `None`，不落一個假的極端值
    進 `candidate.source_roi`）。"""
    rows = (payload or {}).get("leaderboardRows") or []
    out: dict[str, float | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = row.get("ethAddress")
        if not addr or addr.lower() in excluded:
            continue
        roi = _roi_sort_key(row)
        out[addr.lower()] = None if roi == Decimal("-Infinity") else float(roi)
    return out


class ExploreScheduler:
    """單 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走
    ExploreStore，所有 HL 呼叫走 `hl`（必須是 gateway.scoped("explore")，wait_s=0）。

    Task 7.4b：`hl_base`／`hl_fills` 是 `hl_base=gateway.scoped("explore_base")`／
    `hl_fills=gateway.scoped("explore_fills")` 的保留額度視圖，皆可省略（`None`）
    ——省略時基礎類別與 fills-like 一律退回 `hl`（父 scope），與改動前行為一致，
    供未接父子 scope 的舊呼叫端／測試沿用。"""

    def __init__(self, *, store: ExploreStore, hl, leaderboard_source_fn: Callable[[], dict | None],
                 excluded_fn: Callable[[], set[str]], cfg: ExploreConfig, now_fn, sleep_fn,
                 on_dirty: Callable[[], None], owner: str = "api", lease_s: float = 60.0,
                 hl_base=None, hl_fills=None,
                 candidates_every_s: float = 1800,
                 state_every_s: float = 1800,
                 portfolio_every_s: float = 7200,
                 ledger_every_s: float = 7200,
                 # Task 5（2026-09-22，D-B／D-H，主線程裁決）：增量週期不再是
                 # 單一常數——改由 `fills_period_s_for(address)` 逐地址依近期
                 # 成交速率與名次決定（見該方法／`explore_fills_sync.fills_period_s`
                 # docstring）。這兩個參數只覆寫該公式的上下界，預設值 import
                 # `explore_fills_sync.MIN_PERIOD_S`／`MAX_PERIOD_S`（單一來源），
                 # 不得在這裡另寫一份秒數字面值。生產路徑由 `run_api.py` 顯式傳入
                 # `cfg.explore_fills_period_s`／`cfg.explore_fills_max_period_s`
                 # （`FILET_EXPLORE_FILLS_PERIOD_S` 正式機現值 21600 語意相容地
                 # 改為下界來源，drop-in 不必改值）。
                 fills_min_period_s: float = MIN_PERIOD_S,
                 fills_max_period_s: float = MAX_PERIOD_S,
                 hot_rank: int = 50,
                 jitter_pct: float = 0.10,
                 rng: Callable[[], float] = random.random,
                 on_tick: Callable[[], None] | None = None,
                 # Task 8（2026-09-22，D-C／D-I）：`SPECIAL_SERVE_RATIO`（預設 9，
                 # 使用者 2026-09-21 裁決，不得更動）的臨時覆寫——只在
                 # `special_serve_ratio_until`（ISO8601 UTC）之前有效，逾期或
                 # 設定缺漏／不合法一律回預設，見 `_special_serve_ratio`。
                 special_serve_ratio: int | None = None,
                 special_serve_ratio_until: str | None = None):
        self._store = store
        self._hl = hl
        self._hl_base = hl_base
        self._hl_fills = hl_fills
        self._leaderboard_source_fn = leaderboard_source_fn
        self._excluded_fn = excluded_fn
        self._cfg = cfg
        self._now = now_fn
        self._sleep = sleep_fn
        self._on_dirty = on_dirty
        self._owner = owner
        self._lease_s = lease_s
        self._candidates_every_s = candidates_every_s
        self._state_every_s = state_every_s
        self._portfolio_every_s = portfolio_every_s
        self._ledger_every_s = ledger_every_s
        self._fills_min_period_s = fills_min_period_s
        self._fills_max_period_s = fills_max_period_s
        self._hot_rank = hot_rank
        self._jitter_pct = jitter_pct
        self._rng = rng
        self._on_tick = on_tick
        # Task 8（D-C／D-I）：覆寫值本身不合法（<=0）就當沒設，直接 fail-safe
        # 回預設——不必每次呼叫 `_special_serve_ratio` 都重新判斷這一條。到期
        # 時間只解析一次（建構期），行為與 `special_serve_ratio` 一致：解析
        # 失敗 → 視為沒有覆寫。
        self._special_serve_ratio_override = (
            special_serve_ratio if special_serve_ratio and special_serve_ratio > 0 else None)
        self._special_serve_ratio_until_ts = (
            _parse_iso8601_utc_epoch_s(special_serve_ratio_until)
            if self._special_serve_ratio_override is not None else None)
        if (self._special_serve_ratio_override is not None
                and self._special_serve_ratio_until_ts is None):
            self._special_serve_ratio_override = None
        # Task 8：結論真的由 `scan_verdict` 產生的次數（`_run_scan` 唯一呼叫
        # 點）——端到端測試用它證明「新判準真的被排程迴圈走到」，不是只在
        # 單元測試裡對。
        self._verdicts_total = 0
        # Task 5：最近一輪 candidates 的 `source_rank` 快取（小寫位址 → 名次，
        # 1-based）——`fills_period_s_for` 的名次來源，只在 `_run_candidates`
        # 寫入（見該方法），不落 DB（`candidate.source_rank` 已經是權威持久化
        # 來源，這裡只是排程端的讀取捷徑，重啟後第一輪 candidates 就會重建）。
        self._rank_by_address: dict[str, int] = {}

        self._bootstrapped = False
        self._first_tick_done = False
        self._ticks = 0
        self._last_tick_at: float | None = None
        self._last_result: str | None = None
        self._results: dict[str, int] = {}
        self._last_candidates_ok_at: float | None = None
        self._candidates_empty_streak = 0
        # Task 7.4b：fills 保留額度的類別感知領工觀測值。
        self._fills_pages_total = 0
        # Task 3（2026-09-22）：`_run_scan` 實際發出的「遍歷分頁」請求數——
        # 與探測（`_run_probe`）的請求分開計，讓「探測前置吃掉了這次 tick、
        # 沒有真的抓頁」這件事可觀測、可測試（`test_probe_runs_before_first_
        # page_of_a_new_scan`）。不含增量軌（`_run_increment`）的頁數。
        self._scan_pages_total = 0
        self._last_fills_at: float | None = None
        self._base_scope_in_use = "explore"
        self._rebalanced: dict[str, int] = {}
        # 2026-09-21 主線程裁決：`_fills_available()` 無 limiter 時的交替旗標
        # ——初值 False，第一次呼叫翻成 True（回 120），第二次翻回 False（回 0）。
        self._fallback_turn = False
        # Task 7.9b B4：留存邊界探測的觀測計數器與 9:1 節流計數器。
        self._probe_total = 0
        self._probe_verified = 0
        self._probe_empty = 0
        self._probe_failed = 0
        self._probe_stale = 0
        # Task 7.9d-S S3：輔助份額的計數單位是「實際發出的 fills-like 頁請求」
        # （增量／遍歷的每一頁），不是 tick、也不是 job——多頁 job 只算一次 tick
        # 會讓 verify／探測的份額被低估到接近 0。
        self._fills_pages_since_special = 0
        # Task 7.9d-S S3（2026-09-22 整合模擬 Warning）：輔助名額在 verify 與
        # 探測之間輪流的旗標——固定順序會讓候選多的那一邊吃光名額。
        self._special_turn = False
        # Task 7.9a A2：連續暫時性失敗達到 `MAX_JOB_ATTEMPTS` 而被隔離的次數
        # （與語意錯誤的立即隔離分開計，見 `_quarantine` 的 `max_attempts` 參數）。
        self._quarantined_max_attempts = 0
        # Task 7.9c-S：遍歷軌生命週期與準入的觀測計數器（皆經 `status()` →
        # `/api/ops/health` 的 `explore_refresh` 曝光）。
        self._scan_job_dropped = 0          # 領到 scan job 但狀態顯示不該重掃
        self._admission_skipped = 0         # 準入滿而跳過的單一 job 數
        self._verify_served_by_deadline = 0  # 有界等待到期後讓 verify 插隊的次數
        self._scan_writeback_duplicate = 0
        self._scan_writeback_stale = 0
        self._scan_writeback_missing = 0
        # Task 7.9d-S S1／S2：狀態與工作對帳的成果（原因碼 → 補排筆數、掃除的
        # 殘留 job 數）與「領到非 active 地址的工作、發送前就丟棄」的次數。
        # `None` ＝上一輪對帳失敗（沒有結果），`{}` ＝跑過但沒補任何東西。
        self._reconciled: dict[str, int] | None = {}
        self._inactive_job_dropped = 0
        # Task 7.9e-S：kind 不相容時 verify job 被延後（不刪）的次數、`fills_scan`
        # job 因 kind 不相容被丟棄的次數（與良性丟棄分開計），對帳失敗次數。
        self._verify_job_deferred = 0
        self._verify_job_obsolete = 0
        self._scan_job_dropped_kind_mismatch = 0
        self._reconcile_errors = 0
        # 7.9e 複審 W3：本次輔助名額給的 verify job 當時是否已逾期（真的發頁後
        # 才計入 `verify_served_by_deadline`）。
        self._verify_overdue_served = False
        self._invalid_pages = 0   # 7.9d：非法頁（亂序／窗外）退避次數，見 `_backoff_invalid_page`
        # Task 7.9a A3：`on_dirty` callback 拋例外的次數（`_notify_dirty` 吞例外
        # 後計數）——job 本身不因此遺失，這個計數器讓 callback 本身壞掉這件事
        # 變成可觀測（health 可見）。
        self._dirty_errors = 0

    # Task 3：測試用公開只讀 view（plan
    # `2026-09-22-explore-fills-coverage-verdict-fix.md` Task 3 Step 1）——
    # 內部仍以 `_probe_total`／`_scan_pages_total` 這兩個私有計數器記帳
    # （與既有 `status()["probe"]["total"]` 等觀測鍵共用同一份資料，不重複計數）。
    @property
    def probes_total(self) -> int:
        return self._probe_total

    @property
    def scan_pages_total(self) -> int:
        return self._scan_pages_total

    @property
    def verdicts_total(self) -> int:
        return self._verdicts_total

    def _special_serve_ratio(self, now: float) -> int:
        """Task 8（D-C／D-I）：暫時加速只在期限內有效；逾期自動回到使用者
        2026-09-21 裁決的預設 `SPECIAL_SERVE_RATIO`（9）。設定缺漏、覆寫值不
        合法（<=0）或到期時間無法解析——皆已在建構期正規化成
        `_special_serve_ratio_override is None`——一律回預設（fail-safe 往
        保守方向）。"""
        if self._special_serve_ratio_override is None:
            return SPECIAL_SERVE_RATIO
        if now >= self._special_serve_ratio_until_ts:
            return SPECIAL_SERVE_RATIO
        return self._special_serve_ratio_override

    # ---- jitter ----
    def _jit(self, period_s: float) -> float:
        return period_s * (1 + (self._rng() * 2 - 1) * self._jitter_pct)

    def fills_period_s_for(self, address: str) -> float:
        """Task 5（D-B／D-H）：增量週期的**唯一**取用點——`_enqueue_address_jobs`
        （fills／fills_scan 的排程間隔）與 `_run_increment`（到期判斷傳給
        `plan_incremental` 的 `period_s`、以及完成一輪後的重排間隔）都必須經
        這裡，不得各自算一份（Task 7.8 教訓：到期條件與重排時間不同源）。

        Task 5b（2026-09-22，主線程裁決：Task 5 完成後複查發現原版分母錯誤，
        對回補中位址嚴重低估速率，實測差 67 倍）：`fills_per_hour` 改成三層
        fallback，見 `_observed_span_rate`／`_nominal_window_rate`：
        1. 觀測跨度密度（優先——對回補中位址是唯一正確的分母）。
        2. 退回 `fills_sync` 名目窗口（Task 5 原本的算法，資料不可用時的
           次選）。
        3. 都沒有 → `None`（`fills_period_s` 內部取保守值 `min_period_s`）。

        名次讀 `self._rank_by_address`（最近一輪 `_run_candidates` 寫入的
        `source_rank` 快取）——地址尚未出現在任何一輪 candidates（例如剛入池、
        還沒跑過 candidates）時為 `None`。"""
        fills_per_hour = self._observed_span_rate(address)
        if fills_per_hour is None:
            fills_per_hour = self._nominal_window_rate(address)
        rank = self._rank_by_address.get(address.lower())
        return fills_period_s(fills_per_hour, rank,
                              min_period_s=self._fills_min_period_s,
                              max_period_s=self._fills_max_period_s)

    def _observed_span_rate(self, address: str) -> float | None:
        """Task 5b 第一層：用「當前生效的那次遍歷」——進行中就用它
        （`ExploreStore.get_active_scan`），沒有進行中的就用最近一次完成的
        （`ExploreStore.latest_done_scan`）——的觀測跨度算密度。這是對「回補中」
        位址**唯一正確**的分母：`fills_sync` 的名目窗口在回補未完成時橫跨整整
        30 天，但我們只真的看過 `observed_from_ms～observed_to_ms` 這一段，用
        30 天當分母會嚴重低估速率（正式機複本實測：`0xa483470a…` 真實密度
        897.5 筆/小時被原版算成 13.4，差 67 倍——24 小時週期會累積約 21,500
        筆＝11 頁，正是 D-H 明文禁止的「高頻地址被排成 24h，一頁工作變多頁
        補抓」）。

        兩種情形這個分母都正確或偏保守：
        - 遍歷進行中：觀測跨度就是我們真正看過的區間，密度正確。
        - 遍歷已完成：觀測跨度可能比窗口短（帳戶在窗口邊緣沒交易），密度會
          **高估** → 週期估短 → 抓得更密。方向安全（工程原則：寧可多抓一頁，
          不可漏成多頁補抓）。`fills_in_window` 本身也含游標重疊、是上界
          （實測高估約 3.7%）——同樣是偏保守的方向。

        資料不可用（沒有任何遍歷過、觀測欄位為 `None`、跨度非正、或這次遍歷
        實際上沒觀測到任何成交）→ `None`，交給 `_nominal_window_rate` 接手。"""
        scan = self._store.get_active_scan(address) or self._store.latest_done_scan(address)
        if scan is None:
            return None
        if scan.observed_from_ms is None or scan.observed_to_ms is None:
            return None
        if scan.observed_to_ms <= scan.observed_from_ms or scan.fills_in_window <= 0:
            return None
        span_hours = (scan.observed_to_ms - scan.observed_from_ms) / 3_600_000
        return scan.fills_in_window / span_hours

    def _nominal_window_rate(self, address: str) -> float | None:
        """Task 5b 第二層（Task 5 原本的算法，降級為 fallback）：
        `_observed_span_rate` 拿不到可用資料時（例如這個地址從未觀測到任何
        成交），退回 `fills_sync.fills_in_window` 除以該列自己的名目窗口
        小時數。地址沒有 `fills_sync` 列或窗口長度非正時回 `None`（無資料，
        `fills_period_s` 取保守值）。"""
        st = self._store.get_sync(address)
        if st is None:
            return None
        window_hours = (st.window_end_ms - st.window_start_ms) / 3_600_000
        if window_hours <= 0:
            return None
        return st.fills_in_window / window_hours

    @staticmethod
    def _key(address: str, kind: str) -> str:
        return f"{address.lower()}:{kind}"

    # ---- 對外 ----
    def tick(self) -> str:
        now = self._now()
        result = self._tick_once(now)
        self._ticks += 1
        self._last_tick_at = now
        self._last_result = result
        self._results[result] = self._results.get(result, 0) + 1
        return result

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                r = self.tick()
                if self._on_tick is not None:
                    self._on_tick()
                if r in ("idle", "no_budget", "retry"):
                    delay = 1.0
                elif r == "paused":
                    delay = 5.0
                else:
                    delay = 0.0
                self._sleep(delay)
            except Exception:
                logger.exception("explore scheduler: tick 拋出未預期例外，繼續下一輪")
                # W1/W2 修法：例外路徑也要節流，否則是緊迴圈（連續拋例外時
                # 會不斷佔用 CPU 狂打 claim_due）。
                self._sleep(1.0)

    def status(self) -> dict:
        """`/api/ops/health` 的 `explore_refresh`（`app.py` 直接 `**` 展開）。

        Task 7.9d-S S5：`scans_running`／`verify_remaining`／`due_by_kind` 的母體
        改成 `refresh_job`／`fills_scan` **表本身**（`ExploreStore.count_scans`／
        `count_jobs_by_kind`／`count_due_by_kind`，各一句 SQL），不再用
        `active_candidates()` 逐址點查——舊版母體是「目前 active 候選」，退池但
        job／遍歷還沒收尾的地址整個從觀測值消失（正式機 129 筆 `fills_verify`
        顯示 112），而那些列仍會被 `claim_due` 領走、仍消耗名額（工程原則 1：
        被追蹤的量與排程實際作用的集合同源）。`active` 只用來**分列**
        （`active_rows`／`inactive_rows`／`orphan_rows`），不縮小母體。"""
        stats = self._store.stats()
        now = self._now()
        oldest = self._store.oldest_due_at(now)
        active = {c.address for c in self._store.active_candidates()}
        jobs_by_kind = self._store.count_jobs_by_kind(active)
        scans = self._store.count_scans(active)
        due_by_kind = self._store.count_due_by_kind(now)
        return {
            "last_tick_at": self._last_tick_at,
            "last_result": self._last_result,
            "ticks": self._ticks,
            "results": dict(self._results),
            "queue_depth": stats["refresh_job"],
            "oldest_due_age_s": None if oldest is None else max(0.0, now - oldest),
            "last_candidates_ok_at": self._last_candidates_ok_at,
            "candidates_empty_streak": self._candidates_empty_streak,
            "fills_pages_total": self._fills_pages_total,
            "last_fills_at": self._last_fills_at,
            "base_scope_in_use": self._base_scope_in_use,
            "rebalanced": dict(self._rebalanced),
            # Task 7.9a A2／A3：與 `probe` 一樣經 `**scheduler.status()` 自然
            # 出現在 `/api/ops/health` 的 `explore_refresh`。
            "quarantined_max_attempts": self._quarantined_max_attempts,
            "dirty_errors": self._dirty_errors,
            # Task 7.9c-S S6／7.9d-S S5：遍歷軌生命週期／準入／核驗飢餓的可觀測
            # 面，母體＝表本身（見本方法 docstring）。`scans_running` 是
            # `fills_scan` 的 running 列數；`scans` 另外給地址數、孤兒數（running
            # 但沒有任何 job 會推進它）與退池後仍 running 的列數。
            "scans_running": scans["running_rows"],
            "scans": dict(scans),
            # `verify_remaining` ＝ `fills_verify` 的 **job 列數**，不代表核驗
            # 完成度：job 被刪／被掃除時它會歸零，但該地址對外仍是
            # `evidence_unknown`（7.9d 複審的兩個 Critical 就長這樣）。核驗真正
            # 做完的判準是 `verify_needed["rows"] == 0`（Task 7.9e-S S6）。
            "verify_remaining": jobs_by_kind["fills_verify"]["rows"],
            # `verify_needed`（Task 7.9e-D D2 的單句 SQL）：`rows == 0` 才是
            # 「核驗真的做完」，`unserved == 0` 才是「排程健康」。
            "verify_needed": dict(self._store.verify_needed(active)),
            "jobs_by_kind": {k: dict(jobs_by_kind[k]) for k in JOB_KINDS},
            "due_by_kind": {k: due_by_kind[k] for k in JOB_KINDS},
            "scan_job_dropped": self._scan_job_dropped,
            "scan_job_dropped_kind_mismatch": self._scan_job_dropped_kind_mismatch,
            "verify_job_deferred": self._verify_job_deferred,
            "verify_job_obsolete": self._verify_job_obsolete,
            "inactive_job_dropped": self._inactive_job_dropped,
            "invalid_pages": self._invalid_pages,
            "admission_skipped": self._admission_skipped,
            "verify_served_by_deadline": self._verify_served_by_deadline,
            "reconciled": None if self._reconciled is None else dict(self._reconciled),
            "reconcile_errors": self._reconcile_errors,
            "scan_writeback_duplicate": self._scan_writeback_duplicate,
            "scan_writeback_stale": self._scan_writeback_stale,
            "scan_writeback_missing": self._scan_writeback_missing,
            # Task 7.9b B4：留存邊界探測觀測值，經 app.py `**scheduler.status()`
            # 自然出現在 `/api/ops/health` 的 `explore_refresh`。
            "probe": {
                "total": self._probe_total,
                "verified": self._probe_verified,
                "empty": self._probe_empty,
                "failed": self._probe_failed,
                "stale": self._probe_stale,
                "candidates": self._store.count_probe_candidates(),
            },
        }

    # ---- 對外：狀態與工作對帳（Task 7.9d-S S1） ----
    def reconcile_scan_jobs(self, now: float) -> dict[str, int]:
        """對帳：每個 **active** 地址的「狀態需要一次遍歷嗎」對上「有沒有可推進
        的 `fills_scan` job」，缺的補排；並掃除非 active 地址的殘留 job。

        2026-09-22 使用者第二輪裁決點 1／3。啟動首 tick 與每次 candidates 更新
        後各跑一次；**冪等**——補排過的 job 讓 `_needs_scan_job` 下一次回 `None`，
        重跑零變更。四種原因碼見 `_needs_scan_job`；`resume_running` 補的 job 會
        續跑同一個 `scan_id` 與游標（`_run_scan` 見到進行中的遍歷就接著抓），
        不建新 scan、不整窗重抓。

        回傳 `{原因碼: 補排筆數, "inactive_jobs_deleted": n}`（`status()
        ["reconciled"]` 揭露）。準入 cap 不套用在這條路徑上：它補的是**狀態已經
        要求**的工作，且每個地址最多一個遍歷軌 job（結構上受
        `ADMISSION_MULTIPLIER` 的 6 種 per-address kind 約束），不會膨脹。

        補的 job kind 必須與進行中的遍歷**同類**（2026-09-22 主線程整合模擬抓到的
        Critical）：`verify` 遍歷要補 `fills_verify`、`initial`／`partial_rescan`
        要補 `fills_scan`。舊版一律補 `fills_scan`，於是核驗遍歷被 `fills_scan`
        job 接手跑完、原本的 `fills_verify` job 卻還留著 → 下次被服務時看不到
        running scan 就再開一個 verify 遍歷：3 天模擬跑出 654 次 verify 遍歷
        （只有 129 件工作），核驗永遠做不完。"""
        active = {c.address for c in self._store.active_candidates()}
        out: dict[str, int] = {}
        deleted = self._store.delete_inactive_jobs(active)
        if deleted:
            logger.warning("explore scheduler: 對帳掃除 %d 筆非 active 地址的殘留 job", deleted)
        out["inactive_jobs_deleted"] = deleted
        for target in self._store.scan_job_targets(active):
            # `ScanTarget`（Task 7.9e-D D4）一句 SQL 帶來 `running_scan_id`；
            # `completeness`／`evidence_unknown` 仍由 `_needs_scan_job` 的
            # `get_sync` 一次讀齊（同一來源，不讓同一個事實有兩個出處）。
            running = (None if target.running_scan_id is None
                       else self._store.get_scan(target.running_scan_id))
            reason = self._ensure_scan_job(target.address, now, running_scan=running)
            if reason is not None:
                out[reason] = out.get(reason, 0) + 1
        return out

    def _ensure_scan_job(self, address: str, now: float, *, running_scan=_UNSET) -> str | None:
        """把 `_needs_scan_job` 的判斷落成一個 job（Task 7.9e-S S1：對帳與
        `_run_increment` 收尾共用同一個實作，不各寫一份條件）。回傳真的新建了
        job 的原因碼，否則 `None`（已存在 → `enqueue` 回 False，不重排也不提前）。

        `verify_needed`（新工作）用**地址雜湊攤開**到 48 小時內，與遷移產生 129
        筆核驗時的攤法同源（`ExploreStore._migration_spread_s`）——一次補上幾十筆
        不能全部排在同一秒。其餘原因碼都是「已經在半路上或已到期」，一律 `now`。"""
        need = self._needs_scan_job(address, now, running_scan=running_scan)
        if need is None:
            return None
        reason, job_kind = need
        priority = 4 if job_kind == "fills_verify" else 3
        next_at = now + (_spread(address, VERIFY_SPREAD_S) if reason == "verify_needed" else 0.0)
        if not self._store.enqueue(self._key(address, job_kind), address, job_kind, priority,
                                   next_at):
            return None
        logger.warning("explore scheduler: 對帳補排 %s 的 %s job（%s）",
                       address, job_kind, reason)
        return reason

    def _needs_scan_job(self, address: str, now: float, *,
                        running_scan=_UNSET,
                        ignore_existing_job: bool = False) -> tuple[str, str] | None:
        """「這個地址的**狀態**現在需要一次遍歷，而且沒有可推進它的 job」嗎？
        回傳 `(原因碼, 該補的 job kind)` 或 `None`（Task 7.9d-S S1）：

        - `"resume_running"`：有進行中的遍歷卻沒有**同類**的 job ——遍歷停在
          半路，沒有任何工作會推進它（7.9c 的 Critical：地址退池時
          `delete_jobs` 刪掉 job、回池時 `bootstrap_address_fills` 回 `False`
          → 回補中的地址永遠拿不回 job，5 天模擬仍 `backfilling`）。job kind
          必須對應 scan kind（`verify` → `fills_verify`，其餘 → `fills_scan`）：
          用錯 kind 會讓另一條軌的 job 接手別人的遍歷（見
          `reconcile_scan_jobs` docstring 的 654 次 verify 遍歷）。
        - `"initial_missing"`：沒有進行中的遍歷且 `completeness == "backfilling"`
          ——首次回補從未完成（孤兒：有 `fills_sync` 列、沒有 scan 列）。
        - `"partial_due"`：`partial` 且距離最近一次完成的遍歷已滿
          `PARTIAL_RESCAN_AFTER_S`，沒有進行中的遍歷、也沒有 job。
        - `"verify_needed"`（Task 7.9e-S S1）：`evidence_unknown == 1`（對外是
          `evidence_unknown` 降級）、沒有進行中的 verify 遍歷、也沒有
          `fills_verify` job ——「需要核驗」本來不是狀態推導的需求，verify job
          只有「遷移」與「resume」兩個來源：kind 不相容閘門刪掉一次、或退池掃除
          後回池只重建 `fills_scan`，該地址的核驗就**永久消失**（3 天模擬收尾
          `verify_remaining == 0` 但 17 個地址 `evidence_unknown` 仍是 1）。

        觸發條件一律**由狀態推導**，不由「job 列是否存在」推導（7.9b／7.9c／7.9d
        三個 Critical 都是這個形狀）；job 只用來去重（同一件事不要排兩次）。
        遍歷軌優先於核驗軌：任何一次完成的遍歷都會清掉 `evidence_unknown`
        （`ExploreStore.complete_scan`），已經要重掃的地址不必再排一次核驗。
        `ignore_existing_job=True` 給 `_run_scan` 用——呼叫端手上那個 job 就是
        「正在推進它的工作」，去重條件對它不適用。`running_scan` 可由呼叫端
        （對帳的單句 SQL ＋一次 `get_scan`）帶入，省一次點查。"""
        if isinstance(running_scan, _Unset):
            running_scan = self._store.running_scan(address)
        kinds: set[str] = set() if ignore_existing_job else self._store.job_kinds(address)
        if running_scan is not None:
            job_kind = "fills_verify" if running_scan.kind == "verify" else "fills_scan"
            return None if job_kind in kinds else ("resume_running", job_kind)
        st = self._store.get_sync(address)
        if st is None:
            # 沒有增量軌可掛載（從未 bootstrap，或資料被外部刪除）——遍歷無處
            # 寫回（`complete_scan` 會回 `MISSING`），不值得花一整輪頁面。
            return None
        if "fills_scan" not in kinds:
            if st.completeness == "backfilling":
                return "initial_missing", "fills_scan"
            if st.completeness == "partial":
                latest = self._store.latest_done_scan(address)
                if partial_rescan_due(None if latest is None else latest.finished_at, now):
                    return "partial_due", "fills_scan"
        if st.evidence_unknown and "fills_verify" not in kinds:
            return "verify_needed", "fills_verify"
        return None

    # ---- 內部：一次 tick ----
    def _tick_once(self, now: float) -> str:
        if not self._first_tick_done:
            # <!-- 2026-09-21 複審 W2 -->：旗標在重排**成功之後**才設；重排拋例外時
            # 大聲記錄並在下一 tick 再試（工程原則 #3：關鍵一次性動作不得靜默失敗）。
            try:
                self._rebalanced = self._rebalance_overdue_base_jobs(now)
                # Task 7.9d-S S1：啟動對帳——重啟時「遍歷停在半路、job 卻不見了」
                # 的地址（正式機 69 個 backfilling）要在第一個 tick 就拿回 job。
                self._reconciled = self.reconcile_scan_jobs(now)
            except Exception:
                logger.exception("explore scheduler: 逾期 job 重排／對帳失敗，下一 tick 重試")
                raise
            self._first_tick_done = True
            logger.warning(
                "explore scheduler: 重排逾期 job（新週期 state=%.0fs portfolio=%.0fs "
                "ledger=%.0fs）：%s", self._state_every_s, self._portfolio_every_s,
                self._ledger_every_s, self._rebalanced)

        if not self._bootstrapped:
            self._bootstrapped = True
            self._store.enqueue("candidates:candidates", None, "candidates", 0, now)
            return "idle"

        fills_like_due = any(self._store.due_count(k, now) > 0 for k in _FILLS_LIKE_KINDS)
        # `_fills_available()` 在沒有真實 limiter 時會翻轉 `_fallback_turn`
        # （見該方法），所以一個 tick 只問一次，所有判斷共用這個值。
        fills_budget_ok = self._fills_available() >= FILLS_PAGE_WEIGHT

        # Task 7.9b B4／7.9d-S S3：輔助類別（`fills_verify` ＋留存邊界探測）與
        # fills-like 共用同一份保留額度與同一份份額——雙方都有積壓時，每
        # `SPECIAL_SERVE_RATIO` 次實際發出的 fills-like **頁請求**才輪到一次輔助；
        # fills 類沒有到期工作時輔助可連續（借用這個 tick，不空等）。額度不足時
        # 兩邊都不動（省掉無意義的 DB 查詢）。
        job = None
        special_verify = False
        if fills_budget_ok and (not fills_like_due
                                or self._fills_pages_since_special
                                >= self._special_serve_ratio(now)):
            served, job = self._serve_special(now)
            if served == "probe":
                return "ran:probe"
            special_verify = job is not None

        # Task 7.4b（2026-09-21 主線程二次裁決）：類別感知的領工——有
        # fills-like（`fills`／`fills_scan`）待處理且 `explore_fills` 保留額度
        # 足夠擠進一整頁時，優先從 fills-like 領；否則優先從基礎類別領。但
        # 「優先」不是「只」：優先的那一類這一輪如果根本沒有到期 job，同一個
        # tick 改領另一類，不浪費這個 tick。
        self._base_scope_in_use = "explore_base" if fills_like_due else "explore"

        prefer_fills = fills_like_due and fills_budget_ok
        if job is None:
            job = self._store.claim_due(
                now, self._owner, self._lease_s,
                kinds=_FILLS_LIKE_KINDS if prefer_fills else _BASE_KINDS)
        if job is None:
            if prefer_fills:
                job = self._store.claim_due(now, self._owner, self._lease_s, kinds=_BASE_KINDS)
            elif fills_like_due:
                job = self._store.claim_due(
                    now, self._owner, self._lease_s, kinds=_FILLS_LIKE_KINDS)
        if job is None:
            return "idle"

        try:
            result = self._run_job(job, now, fills_due=fills_like_due)
        except ScanWritebackContractError:
            # Task 7.9d-S S4：介面不符不是「這個 job 失敗」——不隔離、不吞，
            # 直接往上拋（`run_forever` 會記錄完整 traceback 並讓下一輪繼續）。
            raise
        except BudgetExhausted:
            # <!-- 2026-09-21 複審 W3 -->：額度不足不是這個 job 的錯——保留原本的
            # `next_attempt_at`（不推到未來），等待加權才能持續累積、老 job 不會
            # 因為額度緊繃反而被自己的退避重置成「剛到期」。lease 照常釋放。
            self._reschedule(job, min(job.next_attempt_at, now), bump_attempts=False)
            return "no_budget"
        except ScopePaused:
            remaining = self._paused_remaining_s()
            # S1 裁決：限流暫停不是這個 job 自己的失敗，不計 attempts。
            self._reschedule(job, max(now + 5, now + remaining), bump_attempts=False)
            return "paused"
        except Exception as e:  # noqa: BLE001 — 唯一的分類點，見檔頭
            if is_rate_limited(e):
                # Task 7.9a A2（使用者修正）：429 走共享 cooldown（`note_429`／
                # `ScopePaused` 那一路），不算一次「實際嘗試」——不推進 attempts，
                # 與 `BudgetExhausted`／`ScopePaused` 既有的 `bump_attempts=False`
                # 一致。
                self._reschedule(job, now + 60, err=repr(e), bump_attempts=False)
                return "rate_limited"
            if isinstance(e, (ConnectionError, TimeoutError)) or _is_5xx(e):
                attempts = job.attempts + 1
                if attempts >= MAX_JOB_ATTEMPTS:
                    # Task 7.9a A2：第 `MAX_JOB_ATTEMPTS` 次實際嘗試仍失敗 → 隔離
                    # 24 小時，不再重試計畫中的退避。`job.attempts`（隔離前一刻
                    # 讀到的值）維持不變（`_quarantine(max_attempts=True)` 不
                    # 推進），隔離期滿只給一次恢復嘗試的額度。
                    self._quarantine(job, now, e, max_attempts=True)
                    return "quarantined"
                next_at = now + min(30 * (2 ** attempts), 900) + self._rng() * 10
                self._reschedule(job, next_at, err=repr(e))
                return "retry"
            self._quarantine(job, now, e)
            return "quarantined"

        if special_verify and result not in ("deferred", "dropped"):
            # 7.9e 複審 W3：輔助名額只在 verify **真的抓了一頁**時才算用掉，
            # `verify_served_by_deadline` 同理（延後／丟棄不算一次服務）。
            self._fills_pages_since_special = 0
            if self._verify_overdue_served:
                self._verify_served_by_deadline += 1
        return result

    def _serve_special(self, now: float) -> tuple[str | None, Job | None]:
        """輔助類別（`fills_verify` ＋留存邊界探測）的這一次名額給誰
        （Task 7.9d-S S3）。回傳 `("probe", None)`＝已經跑完一次探測、
        `("verify", job)`＝領到一個 verify job（由呼叫端跑，共用同一套例外分類）、
        `(None, None)`＝兩邊都沒有工作（名額留給 fills-like）。

        順序：`VERIFY_MAX_WAIT_S` 逾期 → verify 先；否則兩邊都在等就**輪流**
        （`_special_turn`）。逾期**只調整輔助類別內的順序**，不觸發整批優先
        （7.9c 的「逾期即 claim」讓 8 件逾期 verify 連佔 8 個名額、搶光增量軌）。
        輪流是 2026-09-22 主線程整合模擬的 Warning 修法：固定「探測優先」時，
        287 個地址的探測候選會把每一次輔助名額都吃掉，核驗軌 8 小時只拿到 2 頁
        （4 頁的 verify 遍歷永遠跑不完 → 129 件要拖數十天）。

        名額只在**真的發出一頁**時才算用掉（7.9e 複審 W2／W3）：探測看
        `_run_probe` 的回傳值；verify 則由呼叫端（`_tick_once`）在 `_run_scan`
        真的抓了頁（結果不是 `"deferred"`／`"dropped"`）之後才歸零計數器並計
        `verify_served_by_deadline`——kind 不相容的延後、證據已補齊的丟棄都一頁
        都沒打，不該讓核驗軌等下一個 10 頁。verify 自己那一頁也是 fills 類請求，
        但它屬於輔助份額，不計入 `_fills_pages_since_special`。"""
        verify_ready = self._store.due_count("fills_verify", now) > 0
        verify_overdue = verify_ready and self._verify_overdue(now)
        candidate = self._store.next_probe_candidate()
        if verify_ready and candidate is not None:
            # Task 7.9e-S S5（複審 S1）：兩邊都在等就**一律輪流**，逾期也不例外
            # ——verify 的續頁會保留舊的 `next_attempt_at`（見 `_run_scan`），所以
            # 核驗積壓期間「逾期」恆真；若逾期就一直優先，探測會零服務。
            self._special_turn = not self._special_turn
            order: tuple[str, ...] = (("verify", "probe") if self._special_turn
                                      else ("probe", "verify"))
        elif verify_ready:
            order = ("verify",)
        elif candidate is not None:
            order = ("probe",)
        else:
            return None, None
        for who in order:
            if who == "probe":
                # Task 7.9e-S S4（複審 S2）：名額只在**真的發出請求**時才歸零
                # ——候選剛退池／scan 列不見／額度不足時 `_run_probe` 一頁都沒打，
                # 舊版照樣把計數器歸零，等於白燒一次輔助名額。
                if candidate is not None and self._run_probe(candidate, now):
                    self._fills_pages_since_special = 0
                    return "probe", None
                continue
            if not verify_ready:
                continue
            job = self._store.claim_due(now, self._owner, self._lease_s,
                                        kinds=("fills_verify",))
            if job is not None:
                # 不在 claim 當下歸零：這個 job 可能一頁都不打（kind 不相容 →
                # 延後、證據已補齊 → 丟棄）。由 `_tick_once` 依結果決定。
                self._verify_overdue_served = verify_overdue
                return "verify", job
        return None, None

    def _verify_overdue(self, now: float) -> bool:
        """最舊的到期 `fills_verify` job 是否已等超過 `VERIFY_MAX_WAIT_S`
        ——只用來決定它在輔助類別內排在探測之前（Task 7.9d-S S3）。"""
        due_at = self._store.oldest_due_at(now, kinds=("fills_verify",))
        return due_at is not None and (now - due_at) >= VERIFY_MAX_WAIT_S

    def _run_job(self, job: Job, now: float, *, fills_due: bool = False) -> str:
        if job.kind == "candidates":
            return self._run_candidates(job, now)
        # Task 7.4b：fills-like 待處理時基礎抓取走受限的 `explore_base`（保留
        # 額度給 fills-like）；沒有 `hl_base`（舊呼叫端／測試未接父子 scope）
        # 一律退回 `hl`。
        base_hl = self._hl_base if (fills_due and self._hl_base is not None) else self._hl
        if job.kind == "state":
            return self._run_cache_kind(
                job, now, endpoint="clearinghouseState",
                fetch=lambda addr: base_hl.clearinghouse_state(addr),
                period_s=self._state_every_s)
        if job.kind == "portfolio":
            return self._run_cache_kind(
                job, now, endpoint="portfolio",
                fetch=lambda addr: base_hl.portfolio(addr),
                period_s=self._portfolio_every_s)
        if job.kind == "ledger":
            return self._run_cache_kind(
                job, now, endpoint="ledger",
                fetch=lambda addr: base_hl.non_funding_ledger_updates(addr, 0),
                period_s=self._ledger_every_s)
        if job.kind == "fills":
            return self._run_increment(job, now)
        if job.kind == "fills_scan":
            return self._run_scan(job, now, verify=False)
        if job.kind == "fills_verify":
            return self._run_scan(job, now, verify=True)
        raise ValueError(f"explore scheduler: unknown job kind {job.kind!r}")

    def _run_candidates(self, job: Job, now: float) -> str:
        payload = self._leaderboard_source_fn()
        if payload is None:
            self._reschedule(job, now + 60, err="no payload", bump_attempts=False)
            return "retry"

        excluded = self._excluded_fn()
        rows = candidate_addresses(payload, self._cfg.candidate_pool, excluded)
        if not rows:
            # Task 3.6 B(1)（Critical C1 修法）：候選來源整批回空（上游壞掉、
            # payload 格式跑掉、被排除清單濾光……）不能當成「所有人退池」——
            # 不呼叫 upsert_candidates／deactivate_missing，保留既有候選池，
            # 60 秒後重試。Task 3.7 B（W1 修法）：長期回空之前無外部證據——
            # 記警告與 streak，供 `status()` 揭露。
            self._candidates_empty_streak += 1
            logger.warning(
                "explore scheduler: 候選來源回空 rows（streak=%d），60s 後重試；"
                "候選池維持不動", self._candidates_empty_streak)
            self._reschedule(job, now + 60, err="empty candidate rows", bump_attempts=False)
            return "retry"
        roi_by_addr = _roi_lookup(payload, excluded)

        seen: set[str] = set()
        upsert_rows: list[tuple[str, str | None, int | None, float | None]] = []
        for rank, (address, display_name) in enumerate(rows, start=1):
            seen.add(address.lower())
            # Task 5：`fills_period_s_for` 的名次來源——寫在 `upsert_candidates`
            # 之前也沒關係（純記憶體快取，不依賴這次寫入是否成功）。
            self._rank_by_address[address.lower()] = rank
            upsert_rows.append((address, display_name, rank, roi_by_addr.get(address.lower())))
        self._store.upsert_candidates(upsert_rows, now)
        self._candidates_empty_streak = 0
        self._last_candidates_ok_at = now
        dropped = self._store.deactivate_missing(seen)
        for addr in dropped:
            self._store.delete_jobs(addr)

        # Task 7.9c-S S3（W4 修法）：準入從「整批候選一起被擋」改為「逐項准入」
        # ——cap 滿只跳過單一個 job，其餘候選照常補排；新候選（第一次入池）的
        # job 不受 cap 限制，否則一個被舊 job 撐滿的佇列會讓新地址永遠拿不到
        # 任何補排機會（既有 job 又因為地址還活著而不會被回收）。
        jobs, active_n = self._store.admission_counts()
        cap = ADMISSION_MULTIPLIER * active_n
        skipped_before = self._admission_skipped
        for rank, (address, _display_name) in enumerate(rows, start=1):
            jobs = self._enqueue_address_jobs(address, rank, now, jobs=jobs, cap=cap)
        skipped = self._admission_skipped - skipped_before
        if skipped:
            logger.warning(
                "explore scheduler: admission cap reached (%d jobs, %d active, cap %d) — "
                "本輪跳過 %d 個既有地址的補建 job（新候選不受限）",
                jobs, active_n, cap, skipped)

        # Task 7.9d-S S1：候選池剛換過血——立刻對帳（回池地址拿回 scan job 續跑
        # 同一個 scan、退池地址的殘留 job 掃除）。冪等，所以每輪跑一次沒有副作用。
        # Task 7.9e-S S3（複審 W2）：對帳失敗不得把 candidates job 拖進隔離——
        # 它是候選池的唯一更新來源，隔離 24 小時等於整池停更。大聲記錄＋計數
        # （`reconcile_errors`，health 可見），本輪 candidates 照常收尾續排。
        try:
            self._reconciled = self.reconcile_scan_jobs(now)
        except Exception:
            # 順手（7.9e 複審）：對帳失敗時不留上一輪的舊值——`None` ＝「這一輪
            # 沒有對帳結果」，與「對帳跑過但沒補任何東西（`{}`）」區分開。
            self._reconciled = None
            self._reconcile_errors += 1
            logger.error("explore scheduler: candidates 輪的對帳失敗（candidates job 照常收尾）",
                         exc_info=True)

        self._complete(job)
        self._store.enqueue(job.key, None, "candidates", job.priority,
                            now + self._candidates_every_s)
        return "ran:candidates"

    def _enqueue_address_jobs(self, address: str, rank: int, now: float, *,
                              jobs: int, cap: int) -> int:
        """依**狀態**決定這個候選現在需要哪些 job，去重後逐項准入（Task 7.9c-S
        S3）。回傳更新後的 `refresh_job` 估計總數（呼叫端逐個候選累加）。

        `fills_scan`（遍歷軌）只在 `bootstrap_address_fills` 回 `True`（該地址
        第一次入池、真的新建增量軌）時入列——既有地址一律不在這裡補建：
        `ExploreStore.complete` 是 DELETE，舊版「每輪無條件 enqueue」等於每 30
        分鐘把已經 `complete` 的地址重新排一次整窗遍歷（7.9b Critical C2）。
        既有地址缺 job 的情形由 `reconcile_scan_jobs`（Task 7.9d-S S1）以**狀態**
        判斷後補排——本輪 candidates 收尾時會跑一次。"""
        now_ms = int(now * 1000)
        window_start_ms, window_end_ms = fresh_scan_window(now_ms)
        is_new = self._store.bootstrap_address_fills(
            address, now, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            params_fp=PARAMS_FP)
        fills_priority = 2 if rank <= self._hot_rank else 3
        # Task 5：fills／fills_scan 的排程間隔改走單一取用點
        # `fills_period_s_for`（同一次呼叫內算一次即可——`rank` 在這次
        # candidates 輪已寫進 `_rank_by_address`，見 `_run_candidates`）。
        fills_period = self.fills_period_s_for(address)
        needed: list[tuple[str, int, float]] = [
            ("state", 0, self._state_every_s),
            ("portfolio", 1, self._portfolio_every_s),
            ("ledger", 1, self._ledger_every_s),
            ("fills", fills_priority, fills_period),
        ]
        if is_new:
            needed.append(("fills_scan", fills_priority, fills_period))

        existing = self._store.job_kinds(address)
        for kind, priority, period_s in needed:
            if kind in existing:
                continue   # 已經有這個 kind 的 job，不重排（也不把它提前）
            if not is_new and jobs >= cap:
                self._admission_skipped += 1
                continue
            if self._store.enqueue(self._key(address, kind), address, kind, priority,
                                   now + _spread(address, period_s)):
                jobs += 1
        return jobs

    def _run_cache_kind(self, job: Job, now: float, *, endpoint: str, fetch, period_s: float) -> str:
        """`state`／`portfolio`／`ledger`（endpoint_cache 三種基礎抓取）。

        Task 7.9e-S S7（2026-09-22 使用者裁決點 3 的字面要求「HTTP 發送前確認
        active」）：非 active 地址在**發送前**就丟棄。舊版是「先抓、抓完才用
        `is_active` 決定要不要續排」——退池地址的殘留 job 每次被領到都白打一次
        上游（`explore_base` 額度照樣被吃）。"""
        if not self._store.is_active(job.address):
            return self._drop_inactive(job)
        payload = fetch(job.address)
        refresh_after = now + self._jit(period_s)
        self._store.put_cache_ok(job.address, endpoint, payload, now, refresh_after)
        self._complete(job)
        # Task 7.9a A3（callback 不丟工作）：store 寫入＋續排都先做完，
        # `_notify_dirty()`（吞例外＋計數，見該方法）放在最後一步——`on_dirty`
        # 拋例外不得讓已經 `_complete` 的 job 漏排（工程原則 3 的反向：關鍵動作
        # 不因錦上添花的通知失敗而跟著失敗）。
        self._store.enqueue(job.key, job.address, job.kind, job.priority, refresh_after)
        self._notify_dirty()
        return f"ran:{job.kind}"

    def _run_increment(self, job: Job, now: float) -> str:
        """增量軌 job（kind='fills'）——Task 7.9b B2：只延伸
        `fills_sync.synced_through_ms`，完全不判定 `completeness`／`reason`
        （那是遍歷軌的事，見 `_run_scan`）。重掃期間增量照常前進（B7 (i)）
        ——本方法完全不查詢 `fills_scan`，兩軌互不依賴。

        Task 7.9d-S S2：**發送前**先確認地址還在候選池內，退池地址一頁都不抓
        （續頁同樣——每一頁都是一次新的領工）。"""
        if not self._store.is_active(job.address):
            return self._drop_inactive(job)
        st = self._store.get_sync(job.address)
        if st is None:
            # 防禦：`bootstrap_address_fills` 理論上保證這裡恆非 None（見
            # `_enqueue_address_jobs`）；地址仍存在 job 但 fills_sync 列缺席
            # 代表 store 資料被外部竄改，跳過不重試。
            self._complete(job)
            return "dropped"
        plan = plan_incremental(st, now_ms=int(now * 1000),
                                period_s=self.fills_period_s_for(job.address))
        if plan.is_noop:
            self._complete(job)
            # `plan.next_due_ms` 是 planner 的毫秒制輸出——秒／毫秒轉換只發生在
            # 這個邊界（Task 7.9c-S S1）。
            next_at = max(plan.next_due_ms / 1000, now + self._jit(60.0))
            self._store.enqueue(job.key, job.address, "fills", job.priority, next_at)
            self._ensure_scan_job(job.address, now)
            return "ran:fills"

        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        page = hl_fills.get_fills_page(job.address, plan.start_ms, plan.end_ms)
        self._fills_pages_total += 1
        self._fills_pages_since_special += 1
        self._last_fills_at = now
        res = apply_incremental_page(plan, page, now_ms=int(now * 1000))
        self._store.insert_fills_page(job.address, res.accepted, res.state)
        if not res.done:
            if (res.state.last_error or "").startswith("invalid_page"):
                return self._backoff_invalid_page(job, now, res.state.last_error)
            self._reschedule(job, now, bump_attempts=False)
            self._notify_dirty()
            return "ran:fills"
        self._complete(job)
        # Task 5／Task 7.8 教訓：重排間隔跟上面 `plan_incremental` 的到期判斷
        # 走同一個取用點 `fills_period_s_for`（在此重新呼叫而非沿用上面算好的
        # 值——本輪剛 `insert_fills_page`，用地址此刻最新的速率估計，同一個
        # 函式＝同一個來源，不是各自寫一份常數）。
        next_at = now + self._jit(self.fills_period_s_for(job.address))
        self._store.enqueue(job.key, job.address, "fills", job.priority, next_at)
        # Task 7.9d-S S1／7.9e-S S1：增量收尾也跑一次**同一個**對帳函式
        # （`_ensure_scan_job`）——主路徑是 `reconcile_scan_jobs`（啟動＋每次
        # candidates 更新），這裡讓「狀態需要遍歷／核驗但沒有 job」最多等一個
        # 增量週期就被補上，條件與對帳完全同源（不是另寫一份修復條件）。
        self._ensure_scan_job(job.address, now)
        self._notify_dirty()
        return "ran:fills"

    def _drop_inactive(self, job: Job) -> str:
        """領到非 active（退池／從未入池）地址的工作 → **發送前**收尾丟棄
        （Task 7.9d-S S2）。計 `inactive_job_dropped`，回 `"dropped"`。

        退池地址的殘留 job 仍會被 `claim_due` 領走、仍消耗 `explore_fills` 額度；
        主動掃除走 `reconcile_scan_jobs` 的 `delete_inactive_jobs`，這裡是
        「已經領到手上」那一刻的最後一道閘門（工程原則 #5：發送前分類，不靠
        每個呼叫點各自記得）。"""
        self._complete(job)
        self._inactive_job_dropped += 1
        logger.info("explore scheduler: 丟棄非 active 地址 %s 的 %s job（發送前檢查）",
                    job.address, job.kind)
        return "dropped"

    def _run_scan(self, job: Job, now: float, *, verify: bool) -> str:
        """遍歷軌 job（kind='fills_scan'／'fills_verify'）——Task 7.9b B2／B3＋
        7.9c-S S2＋7.9d-S S1／S2。

        進行中的遍歷一律**續跑同一個 `scan_id` 與游標**（重啟、退池再回池都一樣，
        不整窗重抓）。沒有進行中的遍歷時，新 scan 的 `kind` 由 `_needs_scan_job`
        的原因碼決定（狀態的單一來源）：`verify` job 一律建 `verify`；
        `initial_missing`（`backfilling`：首次回補從未完成）→ `initial`；
        `partial_due` → `partial_rescan`；`None`（`complete`、重掃未到期的
        `partial`、或沒有 `fills_sync` 列）→ 丟棄這個 job 並計 `scan_job_dropped`。
        三種 `kind` 的頁面套用邏輯相同，只有窗口與收尾後的排程動作不同。

        S2：非 active 地址在**發送前**就丟棄（續頁同樣）。"""
        if not self._store.is_active(job.address):
            return self._drop_inactive(job)
        result_kind = "fills_verify" if verify else "fills_scan"
        scan = self._store.running_scan(job.address)
        if scan is not None and (scan.kind == "verify") != verify:
            # 2026-09-22 主線程整合模擬的 Critical：**遍歷軌的兩條軌不得互相
            # 接手**。核驗遍歷（`verify`）只能由 `fills_verify` job 推進、
            # `initial`／`partial_rescan` 只能由 `fills_scan` job 推進；接錯了
            # 會把對方的遍歷跑完、對方的 job 卻還留著，下次被服務時就再開一個
            # 新遍歷（3 天模擬 654 次 verify 遍歷）。
            if verify:
                # Task 7.9e-S S2（複審 C1）：**不得刪掉 `fills_verify` job**——
                # 核驗工作的來源很窄（遷移、resume、`verify_needed` 對帳），刪一次
                # 就要等下一次對帳才補回來，而在那之前對外一直是
                # `evidence_unknown`。等進行中的遍歷（initial／partial_rescan）
                # 收尾就好，10 分鐘後再試，零上游呼叫。
                self._reschedule(job, now + 600, bump_attempts=False)
                self._verify_job_deferred += 1
                logger.warning(
                    "explore scheduler: %s 的 fills_verify job 延後 600s——該地址正在跑 %s 遍歷",
                    job.address, scan.kind)
                return "deferred"
            self._complete(job)
            self._scan_job_dropped_kind_mismatch += 1
            logger.warning(
                "explore scheduler: 丟棄 %s 的 %s job——該地址進行中的遍歷是 %s（kind 不相容；"
                "對帳會依 scan kind 補正確的 job）", job.address, job.kind, scan.kind)
            return "dropped"
        if scan is None:
            now_ms = int(now * 1000)
            window_start_ms, window_end_ms = fresh_scan_window(now_ms)
            if verify:
                # Task 7.9e 複審 W2：核驗需求同樣**由狀態推導**——延後（或排隊）
                # 期間若別的遍歷已經收尾（`complete_scan` 無條件清
                # `evidence_unknown`），這次核驗就沒有必要了：不開遍歷、零上游、
                # 收尾這個 job 並計 `verify_job_obsolete`。少了這一關就是「白跑
                # 一次 30 天整窗遍歷去確認一件已經確認的事」。
                st = self._store.get_sync(job.address)
                if st is None or not st.evidence_unknown:
                    self._complete(job)
                    self._verify_job_obsolete += 1
                    logger.info(
                        "explore scheduler: 丟棄 %s 的 fills_verify job——證據已由別的遍歷補齊"
                        "（evidence_unknown=0）", job.address)
                    return "dropped"
                kind = "verify"
            else:
                # Task 7.9c-S S2（Critical C2）／7.9d-S S1：**有 job 不等於該
                # 重掃**，也不等於該丟棄——一律回頭問狀態（`_needs_scan_job`）。
                # 舊版在這裡無條件建 `partial_rescan`，配合「每個 candidates 輪
                # 重建 scan job」讓已完成的地址每幾小時被整窗重掃一次；7.9c 則
                # 反過來把 `backfilling` 的孤兒也丟掉（回補永遠做不完）。
                need = self._needs_scan_job(job.address, now, running_scan=None,
                                            ignore_existing_job=True)
                reason = None if need is None else need[0]
                if reason == "initial_missing":
                    kind = "initial"
                elif reason == "partial_due":
                    kind = "partial_rescan"
                else:
                    self._complete(job)
                    self._scan_job_dropped += 1
                    st = self._store.get_sync(job.address)
                    logger.info(
                        "explore scheduler: 丟棄 %s 的 fills_scan job（completeness=%s，"
                        "狀態不需要一次遍歷）", job.address,
                        None if st is None else st.completeness)
                    return "dropped"
            scan = self._store.create_scan(
                job.address, kind=kind, window_start_ms=window_start_ms,
                window_end_ms=window_end_ms, cursor_ms=window_start_ms, started_at=now,
                params_fp=PARAMS_FP)

        # Task 3（探測前置，D-E／D-F）：左界證據是 complete 的必要條件，必須
        # 在結論之前到手——排在遍歷完成之後才探測會讓大戶遍歷完十幾頁卻卡在
        # 最後一步證據而持續空白。這個 job 本來就在消耗 `explore_fills`
        # 預算，探測沿用同一份預算，不占 `_serve_special` 的輔助份額。
        #
        # 只在這個 scan **還沒抓過任何一頁**（`pages_done == 0`，剛建立或續跑
        # 但尚未推進）、且**這個窗口起點還沒探測過**（`boundary.window_start_ms
        # != scan.window_start_ms`——`None` 也符合，代表這個地址從未探測過）
        # 時才探測——「探測前置」防的是「遍歷完了才發現卡在證據」，不是要求
        # 每一頁都先確認證據仍未知。一次探測沒解出結論（`unknown`，但
        # `set_left_boundary` 已把這次嘗試的窗口起點記下）不代表下一頁再探
        # 就會有答案：**必須**放行讓 scan 正常推進，否則地址的 portfolio 始終
        # 缺席時，`pages_done` 永遠停在 0、每個 tick 都重探、scan 永遠無法抵達
        # `window_end_ms`（這是本實作在整合測試中實測抓到的無限迴圈，不是
        # 假設性風險）。真正需要重探同一個窗口的殘留地址交給獨立的
        # `_serve_special`／`next_probe_candidate` 路徑（portfolio 之後補上
        # 證據、或下一次 `partial_rescan` 窗口往前滾時，這裡會自然再探一次）。
        if scan.pages_done == 0:
            boundary = self._store.get_left_boundary(job.address, scan.window_start_ms)
            if boundary.state == "unknown" and boundary.window_start_ms != scan.window_start_ms:
                if not self._run_probe((job.address, scan.scan_id), now):
                    # 額度不足／限流暫停：`_run_probe` 一頁都沒發出去，下個 tick
                    # 再試，不推進 attempts（與其餘「額度不足不是這個 job 的錯」
                    # 的處理一致）。
                    self._reschedule(job, now, bump_attempts=False)
                    return "deferred"
                # 探測用掉了這次領工但沒有推進分頁——job 必須立刻重排回可
                # 認領狀態，否則會卡在 lease 直到它自然到期（`_lease_s`，預設
                # 60 秒）才輪得到下一次，真正的分頁推進因此被延後（這是本
                # 實作在整合測試中實測抓到的問題，不是假設性風險）。
                self._reschedule(job, now, bump_attempts=False)
                self._notify_dirty()
                return f"ran:{result_kind}"

        plan = plan_scan(scan)
        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        page = hl_fills.get_fills_page(job.address, plan.start_ms, plan.end_ms)
        self._fills_pages_total += 1
        self._scan_pages_total += 1
        if not verify:
            # Task 7.9d-S S3：核驗遍歷的頁面屬於**輔助份額**，不計入「每 9 頁
            # fills-like 才給輔助一次」的分母（否則 verify 自己就能養出下一次
            # 輔助名額，比例失效）。
            self._fills_pages_since_special += 1
        self._last_fills_at = now
        res = apply_scan_page(plan, page, now_ms=int(now * 1000))
        if not res.done:
            self._store.insert_scan_page(job.address, res.accepted, res.scan)
            if (res.scan.last_error or "").startswith("invalid_page"):
                return self._backoff_invalid_page(job, now, res.scan.last_error)
            # Task 7.9d-S S3（2026-09-22 整合模擬 Warning）：核驗遍歷的續頁**不得
            # 把等待歸零**——`fills_verify` 的到期時間就是它爭取輔助名額順序
            # （`_verify_overdue`）與類內優先權（`claim_due` 的等待加權）的依據；
            # 每頁重設成 `now` 會讓進行中的多頁 verify 永遠排在其他 verify 之後、
            # 也永遠達不到 `VERIFY_MAX_WAIT_S`（實測 8 小時只推進 2 頁）。
            next_at = min(job.next_attempt_at, now) if verify else now
            self._reschedule(job, next_at, bump_attempts=False)
            self._notify_dirty()
            return f"ran:{result_kind}"

        if (res.done and res.scan.result is None and res.scan.stop_reason is None
                and res.scan.cursor_ms < res.scan.window_end_ms):
            # Task 2（D-E）：(a) 本輪頁數用完（`max_pages_per_round` 暫停）——
            # 預算耗盡不是結論。scan 仍 running：只落地進度並重排，不寫
            # `result`／`reason`、不排 `partial_rescan`。重排時間與續頁路徑
            # 同源（7.8 教訓：到期條件與重排時間必須同一來源）。
            #
            # `cursor_ms < window_end_ms` 是必要的第四個條件（2026-09-22
            # 主線程裁決字面稿漏掉、builder 實測抓到）：`apply_scan_page` 對
            # 「短頁抵達終點」與「本輪暫停」兩種情形都回傳
            # `result=None, stop_reason=None`（見 Task 1），只差在游標有沒有
            # 到 `window_end_ms`——少這個條件會把真正跑完的遍歷也當成暫停，
            # scan 永遠卡在 `running`、`fills_sync` 永遠 `backfilling`／
            # 舊結論不動（`test_s1_churned_partial_rescan_resumes_same_scan_id`
            # 實測到：短頁收尾後 `running_scan` 仍非 `None`）。與 `scan_verdict`
            # 判斷「有沒有抵達固定終點」用的是同一個條件（工程原則 1：同一個
            # 判斷不能有兩個不同源的版本）。
            self._store.insert_scan_page(job.address, res.accepted, res.scan)
            self._reschedule(job, now, bump_attempts=False)
            self._notify_dirty()
            return f"ran:{result_kind}"

        # (b) 抵達固定終點或有 `stop_reason`（我方停止）——取左界證據、
        # 呼叫 scan_verdict 算出真正的覆蓋結論，再走既有的 complete_scan 流程。
        boundary = self._store.get_left_boundary(job.address, res.scan.window_start_ms)
        completeness, reason = scan_verdict(res.scan, boundary)
        self._verdicts_total += 1
        finished_scan = dataclasses.replace(
            res.scan, finished_at=now, result=completeness, reason=reason)
        writeback = self._store.complete_scan(job.address, res.accepted, finished_scan)
        if not isinstance(writeback, ScanWriteback):
            # Task 7.9d-S S4：拆掉 fail-silent 相容層——回傳型別不符就明確失敗
            # （舊版把 `bool`／未知值悄悄映射成某一種結果，等於把介面錯誤變成
            # 靜默的排程行為差異）。
            raise ScanWritebackContractError(
                f"ExploreStore.complete_scan 必須回傳 ScanWriteback，實際得到 "
                f"{type(writeback).__name__}: {writeback!r}")
        # Task 7.9c-S S5：CAS 落空不得被當成正常收尾。fills 與 `fills_scan` 的
        # `done` 標記在任一種結果下都已由 store 落地（資料不丟），差別只在
        # 要不要把結論寫進 `fills_sync`、以及要不要排下一次重掃。
        if writeback is ScanWriteback.MISSING:
            logger.warning(
                "explore scheduler: %s 的遍歷完成但 fills_sync 列不存在（scan_id=%s）——"
                "丟棄 job，不排下一次重掃", job.address, finished_scan.scan_id)
            self._scan_writeback_missing += 1
            self._complete(job)
            return "dropped"
        if writeback is ScanWriteback.DUPLICATE:
            logger.info("explore scheduler: %s 的遍歷結論已套用過（scan_id=%s），照常收尾",
                        job.address, finished_scan.scan_id)
            self._scan_writeback_duplicate += 1
        elif writeback is ScanWriteback.STALE:
            logger.warning(
                "explore scheduler: %s 的遍歷結論過期（scan_id=%s，fills_sync 已指向更新的"
                "一次遍歷）——不覆寫、不排下一次重掃", job.address, finished_scan.scan_id)
            self._scan_writeback_stale += 1
        self._complete(job)
        active = self._store.is_active(job.address)
        if (active and not verify and writeback is not ScanWriteback.STALE
                and finished_scan.result == "partial"):
            # Task 7.9b B3／7.9c-S S1：`partial` 的主要復原路徑——排一次
            # `partial_rescan`，`PARTIAL_RESCAN_AFTER_S`（秒）之後再整窗重掃
            # 一次（同一個 `fills_scan` job key，`_run_scan` 屆時因為沒有進行
            # 中的 scan、completeness 仍是 `partial` 且重掃已到期而建出
            # `partial_rescan`）。`stale` 時不排：那一次遍歷的結論根本沒被採用，
            # 由採用中的那次遍歷自己決定要不要重掃。
            self._store.enqueue(self._key(job.address, "fills_scan"), job.address,
                                "fills_scan", 3, now + PARTIAL_RESCAN_AFTER_S)
        self._notify_dirty()
        return f"ran:{result_kind}" if active else "dropped"

    def _backoff_invalid_page(self, job: Job, now: float, last_error: str) -> str:
        """Task 7.9d（主線程裁決）：非法頁（時間亂序／窗外）自 7.9d-D 起不再讓輪次
        以 `result=None` 收尾，但也不能每 tick 免費重試（持續回壞頁的上游會白吃
        fills 類名額）——視同一次**實際嘗試過的暫時性失敗**：指數退避＋計
        `attempts`，達 `MAX_JOB_ATTEMPTS` 就走同一條隔離路徑（24 小時、期滿一次
        恢復額度），與 `_run_job` 對 `ConnectionError`／5xx 的處理同形。游標不動
        （planner 已保證），下次重試從同一頁再要。"""
        self._invalid_pages += 1
        attempts = job.attempts + 1
        if attempts >= MAX_JOB_ATTEMPTS:
            self._quarantine(job, now, ValueError(last_error), max_attempts=True)
            return "quarantined"
        next_at = now + min(30 * (2 ** attempts), 900) + self._rng() * 10
        self._reschedule(job, next_at, err=last_error)
        return "retry"

    # ---- 內部：例外收尾 ----
    def _quarantine(self, job: Job, now: float, exc: Exception, *,
                    max_attempts: bool = False) -> None:
        err = repr(exc)
        if max_attempts:
            # Task 7.9a A2：字首標記讓 ops／測試分得出「連續暫時性失敗達到
            # MAX_JOB_ATTEMPTS」與既有的語意錯誤立即隔離（下面 `bump_attempts`
            # 分支不同：語意錯誤仍照舊遞增 attempts，這裡刻意不再推進——
            # `job.attempts` 在被判定達到上限之前已經是 `MAX_JOB_ATTEMPTS - 1`
            # ，隔離期滿只釋放一次恢復嘗試，失敗一次就立即再達到上限重新隔離）。
            err = f"max_attempts:{err}"
            self._quarantined_max_attempts += 1
        endpoint = _ENDPOINT_BY_KIND.get(job.kind)
        if endpoint is not None:
            self._store.put_cache_error(job.address, endpoint, err, now, now + 86400)
        elif job.kind == "fills":
            self._store.set_sync_error(job.address, err, now)
        elif job.kind in ("fills_scan", "fills_verify"):
            scan = self._store.running_scan(job.address)
            if scan is not None:
                self._store.set_scan_error(scan.scan_id, err)
        else:
            logger.error("explore scheduler: %s job 隔離、無對應快取欄位可寫（%s）",
                         job.kind, err)
        self._reschedule(job, now + 86400, err=err, bump_attempts=not max_attempts)

    def _run_probe(self, candidate: tuple[str, str], now: float) -> bool:
        """左界證據探測（Task 3，D-E／D-F）：`candidate=(address, scan_id)`——
        來自 `_run_scan` 的探測前置（`scan_id` 是這個 job 正在推進的那筆，可能
        剛建立也可能續跑）或 `ExploreStore.next_probe_candidate()`（純 DB 推導
        的獨立路徑，服務沒有 job 在跑但證據仍缺的殘留地址，B7 (vii)：重啟後照常
        運作）。查詢 `[scan.window_start_ms - 1 天, scan.window_start_ms - 1]`
        是否仍有可查成交：

        - 回應非空 → `earlier_fills_seen`（最強證據，不必再看 portfolio）。
        - 回空頁 → 依 `ExploreStore.first_activity_ms`（D-F：只接受 portfolio
          allTime 首點，不得用本機時間頂替）判斷：明顯早於窗口起點（差距
          ≥ `_PROBE_WINDOW_MS`）→ `truncation_suspected`（上游截斷嫌疑）；
          明顯晚於／等於窗口起點 → `no_earlier_activity`（帳戶當時確無活動）；
          缺席或落在模糊帶 → `unknown`（證據不足，不宣稱任何一方）。

        回寫用 `ExploreStore.set_left_boundary`——地址層級的冪等寫入，不是
        scan_id 範圍的 CAS（左界證據不屬於某一次特定的遍歷，屬於這個地址；
        單調性本身已經防止「舊探測回應覆蓋新證據」，見該方法 docstring）。

        回傳「**這次真的發出了一個上游請求嗎**」（Task 7.9e-S S4）：候選剛退池、
        scan 列不見、額度不足／限流暫停都回 `False`，呼叫端（`_run_scan`／
        `_serve_special`）不把這次當成已服務（`_serve_special` 那條路徑不把
        輔助名額算成已用，舊版無條件歸零 `_fills_pages_since_special` 等於白燒
        一次名額）。"""
        address, scan_id = candidate
        if not self._store.is_active(address):
            # Task 7.9d-S S2：發送前確認地址還在候選池內（`next_probe_candidate`
            # 已含 `active=1` 條件，這裡是同一個判準的發送前閘門——查詢與發送
            # 之間候選池可能剛換血）。
            self._inactive_job_dropped += 1
            return False
        scan = self._store.get_scan(scan_id)
        if scan is None:
            self._probe_failed += 1
            return False
        probe_start = scan.window_start_ms - _PROBE_WINDOW_MS
        probe_end = scan.window_start_ms - 1
        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        try:
            page = hl_fills.get_fills_page(address, probe_start, probe_end)
        except (BudgetExhausted, ScopePaused):
            return False            # 額度／暫停：一頁都沒發出去
        except Exception as e:  # noqa: BLE001 — 唯一的分類點，見上方 docstring
            if is_rate_limited(e):
                # 429：請求確實發出去了（只是被限流擋回）——與下面的
                # 「失敗但已嘗試」同一套收尾，不特別區分。
                self._store.set_left_boundary(address, "unknown", scan.window_start_ms, now)
                return True
            logger.warning(
                "explore scheduler：左界證據探測失敗 address=%s window=[%d,%d]: %r",
                address, probe_start, probe_end, e)
            self._probe_total += 1
            self._probe_failed += 1
            # Task 3 補（整合測試實測抓到）：探測異常若完全不落地任何狀態，
            # `_run_scan` 的探測前置閘門（「這個窗口起點還沒探測過」）會在
            # 下一個 tick 對同一個持續失敗的上游再探一次、無限重複——這個地址
            # 的 `fills_scan` job 因此永遠卡在 pages_done=0、永遠走不到真正的
            # 分頁請求，也就永遠不會觸發既有的 quarantine 機制（工程原則 3：
            # 失敗路徑必須跟成功路徑一樣可見，不能被探測這一層悄悄吸收）。
            # 標記「這個窗口已嘗試過、仍未知」讓閘門放行——scan 照常推進到
            # 真正的分頁請求，同樣的上游失敗會在那裡被既有的例外分類／
            # quarantine 邏輯正常處理。
            self._store.set_left_boundary(address, "unknown", scan.window_start_ms, now)
            return True
        self._probe_total += 1
        invalid_reason = validate_page(page, probe_start, probe_end)
        if invalid_reason is not None:
            logger.warning(
                "explore scheduler：左界證據探測回應不合法 address=%s window=[%d,%d] "
                "reason=%s", address, probe_start, probe_end, invalid_reason)
            self._probe_failed += 1
            self._store.set_left_boundary(address, "unknown", scan.window_start_ms, now)
            return True
        if page:
            state = "earlier_fills_seen"
        else:
            first_ms = self._store.first_activity_ms(address)
            if first_ms is None:
                state = "unknown"                                    # D-F：不知道就是不知道
            elif first_ms >= scan.window_start_ms:
                state = "no_earlier_activity"
            elif first_ms < scan.window_start_ms - _PROBE_WINDOW_MS:
                state = "truncation_suspected"
            else:
                state = "unknown"                                    # 模糊帶：證據互相矛盾
        ok = self._store.set_left_boundary(address, state, scan.window_start_ms, now)
        if not ok:
            # 正面證據已存在、這次寫入被拒（見 `set_left_boundary` docstring），
            # 或地址已被 purge——兩者都是「這次探測白做了」，計 stale。
            self._probe_stale += 1
            return True
        self._notify_dirty()
        if page:
            self._probe_verified += 1
        else:
            self._probe_empty += 1
        return True

    def _notify_dirty(self) -> None:
        """Task 7.9a A3：`on_dirty`（publisher 用來決定要不要提前組版）是錦上
        添花的通知，不是關鍵寫入路徑——所有呼叫點都已經把 store 寫入與
        `_complete`／續排／enqueue 做完才呼叫這裡，例外只吞不逸出（工程原則 3
        的反向：關鍵動作不因這個失敗而跟著失敗），計 `dirty_errors`
        （`status()`／health 可見）供營運端發現 callback 本身壞掉。"""
        try:
            self._on_dirty()
        except Exception:
            logger.error("explore scheduler: on_dirty callback 拋出例外", exc_info=True)
            self._dirty_errors += 1

    def _paused_remaining_s(self) -> float:
        limiter = getattr(self._hl, "_limiter", None)
        if limiter is None:
            return 60.0
        return limiter.snapshot()["paused_remaining_s"].get("explore", 60.0)

    def _fills_available(self) -> int:
        """`explore_fills` 保留 scope 目前還能預留多少權重（Task 7.4b）——
        用它判斷「值不值得這一輪把工作分給 fills-like」。刻意只看
        `self._hl_fills`（不像 `_run_job` 那樣在 `None` 時退回 `self._hl`）。

        2026-09-21 主線程二次裁決：沒有接父子 scope 的舊呼叫端／測試沒有真正
        的限流器可查，只在「基礎類別也有到期 job」時才交替回
        `FILLS_PAGE_WEIGHT`／`0`（`self._fallback_turn`）——沒有基礎 job 在
        排隊時，交替毫無意義，只會把本來每 tick 都能跑的 fills-like 平白拖慢
        一半。有真實 limiter（生產接線）時完全不受影響，一律照
        `limiter.available("explore_fills")` 的真實保留額度判斷。"""
        limiter = getattr(self._hl_fills, "_limiter", None)
        if limiter is not None:
            # <!-- 2026-09-21 複審 W1 -->：用 gateway 自己的 scope 名，不硬編字串
            # ——判斷與實際發送同源（同一個 `_hl_fills`、同一個 scope）。
            return limiter.available(getattr(self._hl_fills, "_scope", "explore_fills"))
        now = self._now()
        base_due = any(self._store.due_count(kind, now) > 0 for kind in _BASE_KINDS)
        if not base_due:
            return FILLS_PAGE_WEIGHT
        self._fallback_turn = not self._fallback_turn
        return FILLS_PAGE_WEIGHT if self._fallback_turn else 0

    def _rebalance_overdue_base_jobs(self, now: float) -> dict[str, int]:
        """第一個 tick 對三個基礎 kind 各自呼叫 `store.rebalance_overdue`
        （Task 7.4b：週期改長之後，舊積壓若不重排會先把新週期的額度占滿）。
        用 default-arg 綁定當下迴圈的 `period`——lambda 直接閉包 `period`
        會在迴圈跑完後統一指到最後一次賦值（ledger 的週期），造成三個 kind
        全部用同一個週期打散。"""
        rebalanced: dict[str, int] = {}
        for kind, period in (("state", self._state_every_s),
                             ("portfolio", self._portfolio_every_s),
                             ("ledger", self._ledger_every_s)):
            rebalanced[kind] = self._store.rebalance_overdue(
                kind, now, period, lambda addr, period=period: _spread(addr, period))
        return rebalanced

    # ---- 內部：lease/fencing 收尾（輸家 log warning、不重試） ----
    def _complete(self, job: Job) -> bool:
        ok = self._store.complete(job, job.fencing)
        if not ok:
            logger.warning("explore scheduler: complete 競態落敗 key=%s fencing=%s",
                           job.key, job.fencing)
        return ok

    def _reschedule(self, job: Job, next_attempt_at: float, *, err: str | None = None,
                    bump_attempts: bool = True) -> bool:
        ok = self._store.reschedule(job, job.fencing, next_attempt_at, err=err,
                                    bump_attempts=bump_attempts)
        if not ok:
            logger.warning("explore scheduler: reschedule 競態落敗 key=%s fencing=%s",
                           job.key, job.fencing)
        return ok
