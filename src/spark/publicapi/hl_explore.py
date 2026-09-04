"""src/spark/publicapi/hl_explore.py
`GET /api/public/explore`（M3 round3 Task 1）——可跟單對象探索榜。

背景與設計（主線程裁決 D1/D2/D3/D8/D10，plan
`docs/superpowers/plans/2026-08-30-m3-ui-round3.md`）
--------------------------------------------------------------------------
`/api/public/leaderboard`（`hl_leaderboard.py`）只裁切 stats-data 的 pnl/roi/vlm，
沒有回撤／勝率／交易日這類需要逐地址查詢才能算出的指標。本模組是「候選池選取 →
逐地址 enrich → 資格過濾／排序 → 分頁」整條管線的唯一出口：

1. **候選池**：stats-data month 窗（沿用 `hl_leaderboard` 既有的 36MB 快取，
   不重複下載——見 `app.py` 接線）依 **roi 降冪**取前 N 名（`ExploreConfig.
   candidate_pool`），排除 Filet 自營 leader（D8）。
2. **逐地址 enrich**（`enrich_candidate`，純函式）：`portfolio()` 的
   month/allTime 視窗（2026-08-31 I-15 起為 spot+perp 合併窗，原 perpMonth/
   perpAllTime，見下方「I-15」段）＋近 30 天成交（分頁抓原始形狀，見
   「2026-09-05」段）＋ `clearinghouse_state()` 目前持倉，算出 30D 損益金額／
   權益指數回撤／實盤天數／成交統計／集中度／曝險——**公式本身已全部移到**
   `spark.filet.trader_stats`（探索清單與交易員詳情共用，見「2026-09-04」段），
   本模組只負責組裝三份原始 HL 回應餵給它。任一地址讀不到 → 該列整筆跳過
   （`None`），不進榜、不編數字（工程原則 3 的展示版）。
3. **資格過濾與風險調整排序**（`qualify`／`sort_key`）**全在後端**（R2-01），
   前端只送布林 chip 開關，不自己算。
4. **`ExploreIndex`**：仿 `hl_leaderboard.LeaderboardCache` 的 TTL＋
   single-flight 模式，多一層 per-address enrich 結果快取（TTL 30 分鐘、
   LRU 上限 256）。建置在背景 thread 跑（`build_sync` 是實際工作，序列執行）；
   **從未成功建置過**時 `query()` 立即回 `building: True` ＋空 rows，不阻塞
   呼叫端。已有舊版時，即使背景正在重建或本輪上游故障，一律**回舊版**
   （fail-open，同 `LeaderboardCache` 檔頭精神）。

⚠️ 2026-08-30 mainnet 整合實跑事故（本機起 API 對真實 HL）：節流原本只設在
「地址與地址之間」（`batch_sleep_s`），同一地址內連續 3 個 HL 請求
（portfolio/fills/clearinghouse）**之間完全沒有間隔**，實測 burst 到約
60 req/s，觸發大量 429，enrich 把 429 當成「該地址失敗→跳過」燒完整個
候選池，index 以近乎 0 列完成建置＝空榜上線。修法（`_call_hl`）：
1. 節流改成「每個 HL 請求之間」（`ExploreConfig.enrich_call_interval_s`，
   預設 0.7s），不是地址之間——`batch_sleep_s` 已移除，不再併存兩套節流。
2. 429 視為 transient（讀操作冪等，工程原則 2）：指數退避重試
   `RATE_LIMIT_RETRY_DELAYS_S`（2s/8s/30s）。刻意**不**改
   `spark/resilience.py` 的 `_TRANSIENT_MARKERS` 去收 429——那是與實盤引擎
   共用的邊界，改寬鬆會連坐交易路徑；本模組自己在 `hl.py` 之上再包一層
   429 專屬重試（見 `_is_rate_limited`／`_call_hl`）。
3. 重試耗盡仍 429 → 判定「額度已被打穿，繼續燒剩餘候選只會全部繼續 429」，
   **中止整輪建置**（`_RateLimitedAbort`，非單一地址跳過）、保留舊 snapshot
   （fail-open，同上游故障的既有語意）、log 一行 `build aborted: rate
   limited`。單一地址的**非** 429 錯誤（真的讀不到、格式錯誤…）維持原本
   「跳過該列」語意，不觸發中止。

⚠️ 2026-08-30 review 修正輪殘洞（C4）：上一版 `_call_hl` 的節流只掛在成功路徑
（`fn()` 不丟例外才 `_sleep_fn`）。上游若大量回連線重置／5xx 這類**非** 429 的
錯誤，地址的第一個 HL 呼叫就失敗、立刻 `raise` 出去給 `_enrich_one` 跳過整列，
`_call_hl` 從未走到那行 sleep——節流形同虛設，退化回 burst（與本節開頭那次
事故同一種症狀，只是觸發條件從「429」換成「非 429 的 transient 故障」）。
修法：節流改掛在 `finally`，包住整個 `_call_hl` 呼叫（含其內部的 429 重試
迴圈）——不論最終是成功回傳、非 429 例外原樣往上拋、還是 429 退避耗盡拋出
`_RateLimitedAbort`，離開這個函式之前都會先睡滿一次
`enrich_call_interval_s`，讓節流不再取決於「這次呼叫有沒有成功」。

W1（trading_days → live_days）：`trading_days` 原本量 perpAllTime 降採樣序列
的 distinct UTC 曆日數——但 `leader_perf.py` 檔頭已言明長帳戶的降採樣間隔約
兩週一點，distinct 日數會隨上游取樣密度漂移（同一顆帳戶，取樣變稀疏，這個
數字就跟著掉，門檻判斷因此不穩），且新開倉、不動帳戶只要序列裡有夠多稀疏
的舊點也可能拿到偏高的值。改為**首末點的日曆跨距天數**（只依賴序列的頭尾
兩個時間戳，對中間取樣密度不敏感），欄位改名 `live_days`，語意＝「這顆帳戶
從第一筆到最後一筆觀測，已經實盤了多少天」；`EXPLORE_MIN_TRADING_DAYS` 門檻
語意同步改成「實盤 ≥ N 天」（N 由 EXPLORE_MIN_TRADING_DAYS 決定；2026-08-30 D15 預設 60→30）（env var 名稱本身保留，見 `ExploreConfig`）。

W2（成交統計分頁上限，已升級）：本函式曾建立在單次呼叫（HL
`userFillsByTime` 單頁上限 2000 筆）上，卻標「近 30D」，滿頁時只能把訂單數／
勝率／集中度降級成下限值。2026-09-05（D5）**已改走真分頁**：改抓原始 HL
成交形狀（含開平倉語意欄位），`ExploreConfig.fills_max_pages` 預設 3 頁
（≤ 6000 筆），`ExploreRow.fills_truncated` 現在反映連續多頁滿頁才會是
`True` 的真正截斷，不是單頁滿頁的近似判斷。

工程原則 1（同源同基準）的落地：每個窗的損益金額／權益指數回撤／
sparkline（見 `spark.filet.trader_stats.window_stats`，2026-09-04 起純公式
移至該共用模組，本檔不再自己算）三者出自**同一次** `portfolio()` 回應的
**同一個**該窗 `pnlHistory` 序列，不混用不同窗口的資料；
`live_days` 出自同一次回應的 allTime 序列首末點。曝險（`exposure`）與
帳戶規模 bucket 出自**同一次** `clearinghouse_state()` 回應。

R4-3（2026-08-30，plan `2026-08-30-m3-ui-round4.md` Task R4-3，使用者裁決 6）：
四窗自由切換＋門檻自由填寫。
----------------------------------------------------------------------------
- **四窗**：`portfolio()` 單次回應本就含 perpDay/perpWeek/perpMonth/
  perpAllTime——`enrich_candidate` 不多打上游，一次抽出四窗各自的
  `WindowStats`（ret/dd/spark，同源同基準原則見上）存進 `ExploreRow.windows`
  （鍵＝`WINDOW_KEYS`：`"day"/"week"/"month"/"allTime"`，映射見
  `WINDOW_TO_PERIOD`）。**gating 不變**：`month`／`allTime` 兩窗缺席或資料無效
  （首點非正／中途歸零）→ 整列跳過（沿舊版 `perpMonth`／`perpAllTime` 必要性）；
  `day`／`week` 是 best-effort 附加——缺席或無效只讓該鍵存 `None`，不連坐整列
  （新帳戶可能還沒有足夠的日/週窗資料）。前端據此鍵誠實顯示「—」，不得回退
  借用其他窗的數字冒充（工程原則：不編數字）。
  UI 標籤映射（相對 HL 實際窗口，不是使用者原始回饋字面的「7D/30D/90D」——
  HL `portfolio()` 沒有 90 天窗，見 plan 派工說明）：day→「1D」、week→「7D」、
  month→「30D」、allTime→「全部」。
- **`qualify`／`sort_key` 改吃 `window` 參數**：`max_dd_filter` 用**所選窗**的
  `max_dd_pct`（不是永遠用 month）；該窗對這一列剛好是 `None`（day/week 缺席）
  → 視為「無證據」，比照既有 `concentration_pct is None` 的既有慣例通過、不
  處罰（見 `qualify` docstring）。`sort_key` 缺該窗時退回 `month`（排序需要一個
  確定性的鍵，不能對缺資料的列直接報錯或任意排最後——退回月窗是最小驚訝的
  選擇，前端顯示仍誠實地對該列該窗顯示「—」，兩者不衝突：一個是「排序用什麼
  數字」、一個是「畫面上顯示什麼數字」）。`live_days`／30D 訂單數門檻／
  `concentration_pct` 三個樣本/集中度門檻維持與 window 無關（近 30D fills、
  allTime 日曆跨距，本就不隨顯示窗切換）。
  `_apply_tags`（`low_drawdown`／`concentrated` 批次分位數）固定用 `month`
  窗計算——這是批次建置時算好、寫死進 `ExploreRow.tags` 的離線標籤，不隨
  查詢時的 `window` 參數重算（同一列的 tag 不該因為使用者切换顯示窗就改變）。
- **端點參數化**：`min_live_days`／`min_fills`／`max_dd_pct`／
  `max_concentration_pct` 從三個布林 chip 改成四個自由數值（預設分別
  30/200/30/90，即 `DEFAULT_MIN_TRADING_DAYS`／`DEFAULT_MIN_FILLS`／
  `DEFAULT_MAX_DRAWDOWN_PCT`／`DEFAULT_MAX_CONCENTRATION_PCT`）。伺服器只**夾取**
  範圍（`clamp_explore_params`，防濫用，不是驗證錯誤）不 422：`min_live_days`
  ∈[0,365]、`min_fills`∈[0,100000]、`max_dd_pct`／`max_concentration_pct`∈
  [1,100]。前端「清空欄位＝不過濾」不需要額外的 sentinel/None 概念——清空時
  送邊界值（`min_live_days=0`／`min_fills=0`／`max_dd_pct=100`／
  `max_concentration_pct=100`）天然等於「這個維度永遠通過」。舊的三個布林
  chip 參數（`qualified`/`max_dd`/`exclude_concentrated`）**從公開端點移除**
  （不再是 HTTP 契約的一部分）；`qualify()`／`ExploreIndex.query()`
  的同名布林 kwargs 保留成內部/測試用逃生門（各自獨立開關整個過濾維度，
  預設 `True`），純粹為了不必為每個既有的純函式測試重寫成大量門檻組合。
- **index 結構版本**：`ExploreRow` 形狀變了（`ret_30d_pct`/`max_dd_30d_pct`/
  `spark` 三個頂層欄位→`windows` dict）。本模組沒有把 index 落盤（純記憶體，
  `ExploreIndex._rows` 只在 process 存活期間由 `build_sync()` 寫入，程式重啟
  必定從 `None` 重新建置一次——結構上不可能出現「半舊半新形狀」混雜的快照）。
  仍加 `EXPLORE_INDEX_VERSION` 版本標記＋`ExploreIndex._rows_version`，讓
  「偵測不相容→視為未建置、強制重建」這條語意變成可測試、可驗證的行為
  （`query()`／`_maybe_trigger_build()` 一旦看到 `_rows_version !=
  EXPLORE_INDEX_VERSION` 就當作沒有可用快照，忽略 TTL 立即回
  `building: True` 並觸發重建），也替未來若真的加上跨行程快取/落盤留一個
  現成的相容性檢查點。**與既有「中止保舊」語義正交、不衝突**：429 中止整輪
  建置那條路徑完全不動 `self._rows`/`self._rows_version`，版本仍相容的舊
  snapshot 照常繼續服務（fail-open，見上面 2026-08-30 事故記錄）。

I-15（2026-08-31，issue log 使用者裁決「改！」；**取代**上面 R4-3 段
`WINDOW_TO_PERIOD` 的映射值，其餘 R4-3 內容不變）
----------------------------------------------------------------------------
`WINDOW_TO_PERIOD` 原映射到 perp-only 窗（`perpDay/perpWeek/perpMonth/
perpAllTime`）；候選是任意鏈上地址，資金停泊 spot、經 spot↔perp 內部轉帳進出的
錢包用 perp-only 窗會把轉帳算成損益、產生幻影回撤／幻影波動（實證與理由見
`leader_perf.py` 檔頭「I-15」段）。改吃 HL `portfolio()` 的合併窗（`day/week/
month/allTime`，`leader_perf.COMBINED_PERIODS`）——`extract_window` 的閘門已
為此開放。本節以下（曾提及 perpDay/perpWeek/perpMonth/perpAllTime 的文字）
一律讀作對應的合併窗；`WINDOW_KEYS`／欄位形狀／gating 規則本身不變。

I-17（2026-08-31，issue log 使用者裁決）：候選池 100→300 ＋ 常駐磁碟快取。
----------------------------------------------------------------------------
`DEFAULT_CANDIDATE_POOL` 100→300（實測 60 天門檻下 300 候選才有夠多合格列，
見 D15 段）。原版 index 只在記憶體（見「index 結構版本」節「本模組沒有把
index 落盤」），程序重啟後第一個請求必定 `building: True` ＋空 rows、要等一輪
背景建置（300 址 enrich，數分鐘）才有資料——本輪加**磁碟快照快取**：

- `dump_snapshot`／`load_snapshot`：`ExploreIndex._rows` 的 JSON 序列化（含
  `EXPLORE_INDEX_VERSION` 與 `built_at`），原子寫入（`os.replace`，同
  `leader_change_apply` 等既有落檔慣例——先寫 `.tmp` 再換名，避免半寫壞檔）。
  序列化用 `ExploreRow.to_dict()` 現成形狀，反序列化 `_row_from_dict` 精確
  逆操作（含 `windows` dict／`exposure` 拆包／tuple 欄位）。
- `ExploreIndex.__init__` 新增可選 `snapshot_path`：非 `None` 時嘗試
  `load_snapshot`——版本相符 → 立即灌進 `self._rows`／`_rows_version`／
  `_built_at`／`_total_scanned`，程序重啟後第一個請求就有資料可查（不必等
  一輪背景建置）；版本不符／檔不存在／檔壞 → 忽略，`self._rows` 維持
  `None`，行為等同沒有快照（冷建，既有語意不變）。
- `build_sync` 成功建置一輪後（`self._rows` 換版的同一刻）順手落一份新快照
  （`snapshot_path` 有設才寫；寫入失敗只記錄、不影響本輪建置結果——快取是
  加速手段，不是資料正確性的一部分）。
- **與既有 TTL／stale-while-revalidate 語意正交**：`query()` 讀路徑本來就是
  「先讀目前快照 → 觸發背景重建（若已過期）→ 用讀到的快照回應」（見
  `query()` docstring「⭐ 讀值必須在觸發背景建置之前取得快照」段）——`
  building: True` ＋空 rows 只會出現在 `self._rows is None`（從未成功建置過
  **且**磁碟無可用快照）這唯一情況；TTL 過期時一律服務舊 rows、背景才重建。
  磁碟快照只是把「有沒有舊版可服務」這件事從「這個 process 有沒有跑過至少
  一輪」放寬成「這個 process **或前一個 process** 有沒有跑過至少一輪」，不
  改變上述判斷邏輯本身。
- ⚠️ 與 issue log 另一條裁決 I-04（「同步誤差不得落盤累積」）無關：I-04 限
  的是 dashboard 同步誤差這類**對帳指標**（落盤會讓誤差逐輪累積、失真），
  這裡落的是**榜單快照**（純展示排序結果），過期後照 stale-while-revalidate
  背景重建、不會累積誤差——兩者是不同資料種類、不同裁決範圍。
- `query()` 回應新增 `pool`（＝這一輪實際掃描的候選數，鏡射既有
  `total_scanned`——前端榜首常駐提示句要用這個數字，不寫死 300，見
  `explore/page.tsx`）。

2026-09-04／2026-09-05：探索清單與交易員詳情頁指標統一（D2/D3/D4/D5/D7）
----------------------------------------------------------------------------
本模組刪除了自己的損益／回撤／降採樣／成交統計公式，改呼叫
`spark.filet.trader_stats`（探索清單與交易員詳情**共用**的純函式模組）：
- 報酬指標從百分比報酬率改「損益金額」（`WindowStats.pnl_usd`＝該窗 HL
  `pnlHistory` 末值−首值）；回撤改用權益指數 MDD（`max_dd_pct`，算不出時
  `max_dd_reason` 帶原因字串）；`ExploreRow` 的 `windows[w]` 舊版百分比報酬
  欄位已不存在（D2）。
- 排序鍵（`sort_key`）改為所選窗 `pnl_usd` 降冪，不再做報酬÷回撤比值（D2）。
- 成交統計改用 Hyperbot 已驗證定義：30D 成交筆數欄位改語意＝distinct 訂單數
  （不是 fills 數，`ExploreRow.order_count_30d`）；新增 `closed_positions_30d`／
  `realized_pnl_30d_usd`；只算 perp 成交，spot 成交排除（D3/D4）。
- fills 改走真分頁（`hl.get_fills_raw_paged`，回傳未經欄位裁切的原始 HL
  形狀——`trader_stats.fills_stats` 需要開平倉語意欄位，`hl.py` 既有的展示用
  裁切分頁出口會裁掉這些欄位，見 2026-09-05 修正段；`ExploreConfig.
  fills_max_pages` 預設 3 頁 ≤ 6000 筆，D5）。
- `EXPLORE_INDEX_VERSION` 2 → 3（結構不相容，部署後強制重建，D7）。
詳見 `docs/superpowers/plans/2026-09-04-explore-trader-pnl-metrics.md`。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

from spark.filet.leader_perf import extract_window
from spark.filet.trader_stats import SPARK_POINTS  # noqa: F401 — 保留名稱給既有測試
from spark.filet.trader_stats import (FillsStats, WindowStats, fills_stats,
                                      live_days_from_av, window_stats)
from spark.publicapi import hl_leaderboard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 門檻常數（D3）：預設值＋環境變數可覆寫，見 `ExploreConfig.from_env`。
# ---------------------------------------------------------------------------
DEFAULT_CANDIDATE_POOL = 300  # I-17（2026-08-31 使用者裁決）：100 → 300
DEFAULT_MIN_TRADING_DAYS = 30  # 2026-08-30 使用者裁決：60 → 30（實測 60 天閘下 300 候選僅 2 合格）
DEFAULT_MIN_FILLS = 200
DEFAULT_MAX_DRAWDOWN_PCT = Decimal("30")
DEFAULT_MAX_CONCENTRATION_PCT = Decimal("90")
DEFAULT_PAGE_SIZE = 25
# D5（2026-09-05）：`hl.get_fills_raw_paged` 分頁上限，每頁 2000 筆，3 頁
# ≤ 6000 筆——`_call_hl` 的節流包住整個分頁呼叫，頁與頁之間沒有額外間隔，這是
# 已知的 burst 面，上限 3 就是為了壓它（見 Task 3a）。
DEFAULT_FILLS_MAX_PAGES = 3
# Task 8 Step 4（2026-09-05，reviewer Warning 3）：探索清單與交易員詳情頁原本
# 各自讀一份 `EXPLORE_FILLS_MAX_PAGES`（`ExploreConfig.fills_max_pages` 與
# app.py 的 `TRADER_FILLS_MAX_PAGES`）——同名 env var、兩處硬編預設值，改一邊
# 忘了改另一邊，兩頁的分頁上限就會悄悄分歧（D5「兩頁逐位一致」的前提被打破）。
# 改為單一來源函式，兩處呼叫端都指到這裡。
FILLS_MAX_PAGES_ENV = "EXPLORE_FILLS_MAX_PAGES"


def fills_max_pages_from_env(env: Mapping[str, str] | None = None) -> int:
    """D5：探索清單與交易員詳情**同一個**分頁上限（兩頁逐位一致的前提）。
    2026-09-05 複審修正（Task 10 Step 2）：原本無條件讀 `os.environ`，
    `ExploreConfig.from_env(env=...)` 傳進來的假 `env` 字典會被忽略——單元測試用
    假 env 驗這個欄位會靜默滲入真實程序環境。`env=None`（預設，`app.py` 正式呼叫路徑）
    才讀 `os.environ`；`ExploreConfig.from_env` 把自己收到的 `env` 原樣傳進來。"""
    src = os.environ if env is None else env
    v = src.get(FILLS_MAX_PAGES_ENV)
    return int(v) if v else DEFAULT_FILLS_MAX_PAGES

INDEX_TTL_S = 600.0          # 10 分鐘（D1）
ENRICH_CACHE_TTL_S = 1800.0  # 30 分鐘 per-address enrich 快取（D1）
ENRICH_CACHE_MAX = 256       # LRU 上限（D1）
FILLS_WINDOW_DAYS = 30

# 每個 HL 請求之間的節流間隔（2026-08-30 mainnet burst 429 事故修法，見模組檔頭）。
# 100 址 × 3 call ≈ 300 次請求 × 0.7s ≈ 3.5 分鐘一輪，相對 10 分鐘 index TTL 可接受。
DEFAULT_ENRICH_CALL_INTERVAL_S = 0.7
# 429（rate limited）指數退避重試序列（三次：2s/8s/30s）；耗盡仍 429 → 中止整輪建置。
RATE_LIMIT_RETRY_DELAYS_S = (2.0, 8.0, 30.0)

# ---------------------------------------------------------------------------
# R4-3：四窗（見模組檔頭「R4-3」節）。
# ---------------------------------------------------------------------------
# ⚠️ 2026-08-31 issue log I-15 使用者裁決：改吃 HL portfolio() 的**合併**家族
# （spot+perp，原本是 perpDay/perpWeek/perpMonth/perpAllTime）——探索榜的候選是
# 任意鏈上地址，資金停泊 spot、經 spot↔perp 內部轉帳進出的錢包用 perp-only 窗會
# 把轉帳算成損益、產生幻影回撤（實證與理由見 `leader_perf.py` 檔頭「I-15」段、
# `COMBINED_PERIODS`）。`extract_window` 的閘門已為此開放這四個期別。
WINDOW_KEYS = ("day", "week", "month", "allTime")
WINDOW_TO_PERIOD = {"day": "day", "week": "week",
                    "month": "month", "allTime": "allTime"}
DEFAULT_WINDOW = "month"

# 伺服器夾取範圍（R4-3：防濫用，不是驗證錯誤，見 `clamp_explore_params`）。
MIN_LIVE_DAYS_RANGE = (0, 365)
MIN_FILLS_RANGE = (0, 100_000)
MAX_DD_PCT_RANGE = (1, 100)
MAX_CONCENTRATION_PCT_RANGE = (1, 100)

# index 結構版本（R4-3：`ExploreRow` 形狀變更——`ret_30d_pct`/`max_dd_30d_pct`/
# `spark` 三個頂層欄位改成 `windows` dict）。見模組檔頭「index 結構版本」節。
# 2 → 3（2026-09-04／D7）：`windows[w]` 內部欄位改（損益金額＋權益指數回撤取代
# 舊版百分比報酬）、30D 訂單數欄位改語意＝distinct 訂單數並新增
# `closed_positions_30d`／`realized_pnl_30d_usd`——結構不相容，部署後強制重建。
EXPLORE_INDEX_VERSION = 3


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _clamp_float(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_explore_params(*, min_live_days: int, min_fills: int,
                         max_dd_pct: float, max_concentration_pct: float
                         ) -> tuple[int, int, float, float]:
    """伺服器夾取（R4-3，防濫用，不是驗證錯誤——超界值不 422，直接夾回邊界內）：
    `min_live_days`∈[0,365]、`min_fills`∈[0,100000]、`max_dd_pct`／
    `max_concentration_pct`∈[1,100]（見模組常數 `*_RANGE`）。前端「清空欄位」
    送邊界值（0/0/100/100）天然等於「不過濾」，不需要額外的 sentinel/None
    概念（見模組檔頭「端點參數化」節）。回傳夾取後的
    `(min_live_days, min_fills, max_dd_pct, max_concentration_pct)`。"""
    return (
        _clamp_int(min_live_days, *MIN_LIVE_DAYS_RANGE),
        _clamp_int(min_fills, *MIN_FILLS_RANGE),
        _clamp_float(max_dd_pct, *MAX_DD_PCT_RANGE),
        _clamp_float(max_concentration_pct, *MAX_CONCENTRATION_PCT_RANGE),
    )


class _RateLimitedAbort(Exception):
    """單一 HL 呼叫退避重試耗盡後仍 429——內部控制流訊號，不對外匯出。
    `_enrich_one` 讓它原樣往上傳，`build_sync` 是唯一的攔截點（中止整輪建置，
    保留舊 snapshot），不得被 `_enrich_one`／`_call_hl` 自己的 `except Exception`
    吞掉，否則會退化成「跳過這一個地址」，失去「額度已被打穿，停止繼續燒」
    的語意（見模組檔頭事故記錄）。"""


def _is_rate_limited(exc: Exception) -> bool:
    """429 偵測：不 import httpx（本模組的唯讀 HL 邊界在 `hl.py`，這裡只認
    錯誤訊息字串）——`httpx.HTTPStatusError` 的訊息固定含
    `"429 Too Many Requests"`（2026-08-30 對 mainnet 整合實跑的實測 log，見
    模組檔頭）。用字串比對而非 `isinstance`：測試與未來若換掉底層 HTTP client
    都不必依賴 httpx 這個實作細節。"""
    return "429" in str(exc)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExploreConfig:
    candidate_pool: int = DEFAULT_CANDIDATE_POOL
    # W1（2026-08-30 review 修正輪）：語意已從「distinct 交易日數」改為
    # 「perpAllTime 首末點日曆跨距天數」（`ExploreRow.live_days`），
    # 門檻語意＝「實盤 ≥ min_trading_days 天」。屬性名與 env var 名稱
    # （`EXPLORE_MIN_TRADING_DAYS`）保留不改，避免無謂的批次改名——
    # 這裡是唯一需要知道新語意的地方。
    min_trading_days: int = DEFAULT_MIN_TRADING_DAYS
    min_fills: int = DEFAULT_MIN_FILLS
    max_drawdown_pct: Decimal = DEFAULT_MAX_DRAWDOWN_PCT
    max_concentration_pct: Decimal = DEFAULT_MAX_CONCENTRATION_PCT
    page_size: int = DEFAULT_PAGE_SIZE
    # 每個 HL 請求之間的節流間隔（秒）。D3／2026-08-30 429 事故修法，見模組檔頭。
    enrich_call_interval_s: float = DEFAULT_ENRICH_CALL_INTERVAL_S
    # D5（2026-09-05）：`hl.get_fills_raw_paged` 分頁上限，每頁 2000 筆、3 頁
    # 上限 ≤ 6000 筆，供 `trader_stats.fills_stats` 用。
    fills_max_pages: int = DEFAULT_FILLS_MAX_PAGES

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ExploreConfig":
        """環境變數可覆寫、不寫死（D3）。全部 optional——缺一律落回模組預設值，
        與 `ApiConfig.from_env` 的必填清單不同（探索榜是展示功能，不該因為漏設
        一個門檻常數就讓整個 API 拒絕啟動）。"""
        env = os.environ if env is None else env

        def _int(key: str, default: int) -> int:
            v = env.get(key)
            return int(v) if v else default

        def _dec(key: str, default: Decimal) -> Decimal:
            v = env.get(key)
            return Decimal(v) if v else default

        def _float(key: str, default: float) -> float:
            v = env.get(key)
            return float(v) if v else default

        return cls(
            candidate_pool=_int("EXPLORE_CANDIDATE_POOL", DEFAULT_CANDIDATE_POOL),
            # 名稱保留（見 ExploreConfig.min_trading_days 欄位註記），語意已改
            # 為「live_days（日曆跨距）門檻」。
            min_trading_days=_int("EXPLORE_MIN_TRADING_DAYS", DEFAULT_MIN_TRADING_DAYS),
            min_fills=_int("EXPLORE_MIN_FILLS", DEFAULT_MIN_FILLS),
            max_drawdown_pct=_dec("EXPLORE_MAX_DRAWDOWN_PCT", DEFAULT_MAX_DRAWDOWN_PCT),
            max_concentration_pct=_dec("EXPLORE_MAX_COIN_CONCENTRATION_PCT",
                                       DEFAULT_MAX_CONCENTRATION_PCT),
            page_size=_int("EXPLORE_PAGE_SIZE", DEFAULT_PAGE_SIZE),
            enrich_call_interval_s=_float("EXPLORE_ENRICH_CALL_INTERVAL_S",
                                          DEFAULT_ENRICH_CALL_INTERVAL_S),
            fills_max_pages=fills_max_pages_from_env(env),
        )


# ---------------------------------------------------------------------------
# WindowStats（單一窗 day/week/month/allTime 之一的損益金額／權益指數回撤／
# sparkline）：2026-09-04 起改由 `spark.filet.trader_stats` 匯入，公式本身
# 移到那個模組（探索清單與交易員詳情頁共用，見模組檔頭「2026-09-04」段）。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ExploreRow
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExploreRow:
    address: str
    display_name: str | None
    label: str                     # display_name 有值就用它，否則縮寫地址（D10）
    coins: tuple[str, ...]         # 近 30D 成交額（perp only，D4）最大的前 2-3 個幣種
    account_bucket: str
    # R4-3：四窗（`WINDOW_KEYS`）各自的 `WindowStats`（`spark.filet.trader_stats`）；
    # `"month"`／`"allTime"` 是 enrich 的 gating 條件（缺席或不足兩點 → 整列跳過整個
    # ExploreRow 都不會被建構），保證這兩鍵在成功建構的列上恆非 None；`"day"`／
    # `"week"`是 best-effort，缺席／無效 → 該鍵存 None（不得用其他窗的數字冒充，見
    # 模組檔頭「R4-3」節）。
    windows: dict[str, "WindowStats | None"]
    live_days: int                 # W1：allTime 首末點日曆跨距天數（非 distinct 日數）
    order_count_30d: int           # D3：distinct 訂單數（不是 fills 數）
    closed_positions_30d: int      # D3：部位歸零的生命週期數（Hyperbot 定義）
    realized_pnl_30d_usd: float    # D3：Σ closedPnl（未扣手續費／funding）
    close_win_rate_pct: float | None   # None＝無結倉樣本（closed_positions_30d==0）
    concentration_pct: float | None
    exposure_dir: str | None       # "long" / "short" / None（無倉位或無法解析；
                                    # D14：locale 中性代碼，前端自行對映顯示文案）
    exposure_pct: float | None
    tags: tuple[str, ...] = ()     # 子集 {"low_drawdown", "concentrated"}（D14：
                                    # locale 中性代碼，前端自行對映顯示文案）
    fills_truncated: bool = False  # D5：分頁抓到 fills_max_pages 上限仍滿頁
                                    # → 成交統計三個 *_30d 欄位是下限值/樣本估計

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "display_name": self.display_name,
            "label": self.label,
            "coins": list(self.coins),
            "account_bucket": self.account_bucket,
            "windows": {k: (v.to_dict() if v is not None else None)
                       for k, v in self.windows.items()},
            "live_days": self.live_days,
            "order_count_30d": self.order_count_30d,
            "closed_positions_30d": self.closed_positions_30d,
            "realized_pnl_30d_usd": self.realized_pnl_30d_usd,
            "close_win_rate_pct": self.close_win_rate_pct,
            "concentration_pct": self.concentration_pct,
            "exposure": {"dir": self.exposure_dir, "pct": self.exposure_pct},
            "tags": list(self.tags),
            "fills_truncated": self.fills_truncated,
        }


def _row_from_dict(d: dict) -> ExploreRow:
    """`ExploreRow.to_dict()` 的精確逆操作（I-17 磁碟快照用）——不透過
    dataclasses 泛用工具（那些工具不知道 `windows`/`exposure` 這兩層需要
    拆包／重建成巢狀 `WindowStats`），逐欄位手寫對稱，欄位漂移時兩邊都要
    改，測試（round-trip）會抓到不對稱。"""
    windows = {k: (WindowStats.from_dict(v) if v is not None else None)
              for k, v in (d.get("windows") or {}).items()}
    exposure = d.get("exposure") or {}
    return ExploreRow(
        address=d["address"],
        display_name=d.get("display_name"),
        label=d["label"],
        coins=tuple(d.get("coins") or ()),
        account_bucket=d["account_bucket"],
        windows=windows,
        live_days=d["live_days"],
        order_count_30d=d["order_count_30d"],
        closed_positions_30d=d["closed_positions_30d"],
        realized_pnl_30d_usd=d["realized_pnl_30d_usd"],
        close_win_rate_pct=d.get("close_win_rate_pct"),
        concentration_pct=d.get("concentration_pct"),
        exposure_dir=exposure.get("dir"),
        exposure_pct=exposure.get("pct"),
        tags=tuple(d.get("tags") or ()),
        fills_truncated=bool(d.get("fills_truncated", False)),
    )


def dump_snapshot(path: str, *, rows: list[ExploreRow], built_at: float,
                  total_scanned: int) -> None:
    """I-17：原子寫入榜單快照（`.tmp` 寫完再 `os.replace`，避免行程被中斷時
    留下半寫壞檔——同 repo 既有落檔慣例）。寫入失敗（例如目錄不可寫）由
    呼叫端（`ExploreIndex.build_sync`）自行 try/except 決定要不要吞掉；本函式
    本身不吞錯，讓呼叫端能記錄清楚是哪一步壞的。"""
    payload = {"version": EXPLORE_INDEX_VERSION, "built_at": built_at,
              "total_scanned": total_scanned, "rows": [r.to_dict() for r in rows]}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)


def load_snapshot(path: str) -> dict | None:
    """I-17：讀快照。不存在／解析失敗／版本不符 → `None`（呼叫端視為「沒有
    可用快照」，忽略、走既有冷建語意，不拋例外——這是加速路徑，不是資料正確
    性的一部分，讀不到就當作沒發生過）。成功時回傳
    `{"rows": [ExploreRow, ...], "built_at": float, "total_scanned": int}`。"""
    try:
        raw = Path(path).read_text()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.error("explore index 快照解析失敗，忽略（冷建）: %s", path)
        return None
    if not isinstance(payload, dict) or payload.get("version") != EXPLORE_INDEX_VERSION:
        return None
    try:
        rows = [_row_from_dict(r) for r in payload["rows"]]
        built_at = float(payload["built_at"])
        total_scanned = int(payload["total_scanned"])
    except (KeyError, TypeError, ValueError) as e:
        logger.error("explore index 快照形狀不符，忽略（冷建）: %s", e)
        return None
    return {"rows": rows, "built_at": built_at, "total_scanned": total_scanned}


# ---------------------------------------------------------------------------
# 純函式：欄位計算（各自獨立、可單測，零網路）
# 2026-09-04：損益金額／權益指數回撤／降採樣／成交統計四組公式已全部移到
# `spark.filet.trader_stats`（探索清單與交易員詳情頁共用，見模組檔頭
# 「2026-09-04」段），本模組不再自己定義——只留下曝險／帳戶規模這類
# `clearinghouse_state()` 專屬、與損益公式無關的欄位計算。
# ---------------------------------------------------------------------------


def _account_value(ch_state: dict) -> Decimal | None:
    try:
        return Decimal(str(ch_state["marginSummary"]["accountValue"]))
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


def _account_bucket(account_value: Decimal | None) -> str:
    if account_value is None:
        return "—"
    if account_value < Decimal("10000"):
        return "<$10K"
    if account_value < Decimal("100000"):
        return "$10K–$100K"
    if account_value < Decimal("1000000"):
        return "$100K–$1M"
    return "$1M+"


def _parse_positions(ch_state: dict) -> list[dict] | None:
    """`assetPositions` → `[{"side": "long"/"short", "value": Decimal}, ...]`。
    `value = marginUsed × leverage`（同 `app.py._dashboard_positions_raw` 的
    既有欄位推導，欄位名已在該處驗證過，不是憑印象——刻意不 import 那支函式：
    `app.py` 會 import 本模組，import 回去會成環）。形狀不符 → `None`
    （呼叫端把曝險欄位個別降級成 `None`，不因持倉解析失敗連坐整列）。"""
    if not isinstance(ch_state, dict):
        return None
    raw = ch_state.get("assetPositions")
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    try:
        for item in raw:
            pos = item["position"]
            szi = Decimal(str(pos["szi"]))
            if szi == 0:
                continue
            leverage = pos["leverage"]
            lev_val = Decimal(str(leverage["value"]))
            margin_used = Decimal(str(pos["marginUsed"]))
            out.append({"side": "long" if szi > 0 else "short",
                       "value": margin_used * lev_val})
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return None
    return out


def _exposure(positions: list[dict] | None) -> tuple[str | None, float | None]:
    if not positions:
        return None, None
    total = sum((p["value"] for p in positions), Decimal("0"))
    if total <= 0:
        return None, None
    long_value = sum((p["value"] for p in positions if p["side"] == "long"), Decimal("0"))
    short_value = total - long_value
    if long_value >= short_value:
        pct = (long_value / total * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return "long", float(pct)
    pct = (short_value / total * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return "short", float(pct)


def exposure_from_clearinghouse(ch_state: dict) -> tuple[str | None, float | None]:
    """公開出口：`clearinghouse_state()` 原始回應 → `(dir, pct)`（`_parse_positions`
    ＋`_exposure` 的組合）。2026-09-05（Task 4）供 `publicapi/app.py`
    `public_trader_detail` 呼叫——它與 `enrich_candidate` 必須算出同一個曝險，
    不得各自重新解析 `assetPositions`（工程原則 1）。`app.py` 只呼叫這個公開名，
    不碰底線私有的 `_parse_positions`／`_exposure`。"""
    return _exposure(_parse_positions(ch_state))


def _abbreviate_address(address: str) -> str:
    if not (isinstance(address, str) and address.startswith("0x") and len(address) >= 10):
        return address
    return f"{address[:6]}…{address[-4:]}"


def enrich_candidate(address: str, display_name: str | None, portfolio_raw,
                     fills: list[dict], ch_state: dict, *,
                     fills_truncated: bool = False) -> ExploreRow | None:
    """純函式：候選地址的三份原始 HL 回應 → `ExploreRow`，或 `None`（該列整筆
    跳過，見模組檔頭第 2 點）。損益／回撤／sparkline／成交統計的公式全部委派
    給 `spark.filet.trader_stats`（`window_stats`／`fills_stats`），本函式只
    負責組裝與 gating（工程原則 1：同一窗的三個數字出自同一次 `window_stats`
    呼叫，不混用）。

    `portfolio_raw`：`hl.portfolio(address)` 的原始回應。
    `fills`：`hl.get_fills_raw_paged(address, start, end)` 的輸出（原始 HL
    `userFillsByTime` 形狀，含 `dir`/`oid`/`startPosition`/`closedPnl`——
    `trader_stats.fills_stats` 需要這些欄位，見 Task 3a）；`fills_truncated`：
    同一次分頁呼叫的截斷旗標，原樣透傳進 `ExploreRow.fills_truncated`（D5）。
    `ch_state`：`hl.clearinghouse_state(address)` 的原始回應。

    跳過整列的情況（讀不到就跳過，不編數字）：`month` 或 `allTime` 視窗缺席／
    形狀不符／不足兩個取樣點（`window_stats` 回傳 `None`）——不再檢查淨值
    首點是否為正（2026-09-04：報酬改用損益金額，不需要正分母，見 D2）；
    day／week 是 best-effort，缺席只讓 `windows["day"/"week"]` 為 `None`，
    不連坐整列（見模組檔頭「R4-3」節）。
    `tags` 留空（`()`）——集中度與低回撤兩個 tag 需要「這一批候選池」的相對
    資訊（門檻常數／同批分位數），由 `ExploreIndex.build_sync` 建完整批後
    再用 `_apply_tags` 統一補上，不在單一地址的純函式裡決定。
    """
    # D14（2026-08-30 主線程裁決）：`tags`／`exposure_dir` 一律用 locale 中性代碼
    # （"low_drawdown"/"concentrated"、"long"/"short"），不回傳中文顯示字串——
    # 顯示文案改由前端 `explore/page.tsx` 對映 `copy.ts`（見 `_exposure`／
    # `_apply_tags` 的實際賦值）。
    month = window_stats(portfolio_raw, WINDOW_TO_PERIOD["month"])
    if month is None:
        return None
    all_time_window = extract_window(portfolio_raw, WINDOW_TO_PERIOD["allTime"])
    if all_time_window is None:
        return None
    av_all, _ = all_time_window
    live_days = live_days_from_av(av_all)
    all_time_stats = window_stats(portfolio_raw, WINDOW_TO_PERIOD["allTime"])
    if all_time_stats is None:
        return None

    windows: dict[str, WindowStats | None] = {
        "day": window_stats(portfolio_raw, WINDOW_TO_PERIOD["day"]),
        "week": window_stats(portfolio_raw, WINDOW_TO_PERIOD["week"]),
        "month": month,
        "allTime": all_time_stats,
    }

    fs: FillsStats = fills_stats(fills or [], truncated=fills_truncated)
    account_value = _account_value(ch_state)
    bucket = _account_bucket(account_value)
    positions = _parse_positions(ch_state)
    exp_dir, exp_pct = _exposure(positions)

    return ExploreRow(
        address=address,
        display_name=display_name,
        label=display_name if display_name else _abbreviate_address(address),
        coins=fs.coins,
        account_bucket=bucket,
        windows=windows,
        live_days=live_days,
        order_count_30d=fs.order_count,
        closed_positions_30d=fs.closed_positions,
        realized_pnl_30d_usd=fs.realized_pnl_usd,
        close_win_rate_pct=fs.win_rate_pct,
        concentration_pct=fs.concentration_pct,
        exposure_dir=exp_dir,
        exposure_pct=exp_pct,
        tags=(),
        fills_truncated=fs.truncated,
    )


def _apply_tags(rows: list[ExploreRow], cfg: ExploreConfig) -> list[ExploreRow]:
    """整批 enrich 完成後才能算的兩個 tag（D14：locale 中性代碼，前端對映
    `copy.ts` 顯示文案）：
    - `"concentrated"`：`concentration_pct > cfg.max_concentration_pct`（逐列獨立）。
    - `"low_drawdown"`：本批 `"month"` 窗 `|max_dd_pct|` 最小的下四分位（含邊界）
      ——需要同批其他列的分佈才能定義，故不在 `enrich_candidate` 裡做（見該
      函式檔頭）。R4-3：固定用 `"month"` 窗計算（`enrich_candidate` 保證
      該鍵恆非 `None`），是批次建置時算好、寫死進 `tags` 的離線標籤，不隨
      查詢時的 `window` 參數變動（同一列的 tag 不因使用者切換顯示窗而改變，
      見模組檔頭「R4-3」節）。
    """
    if not rows:
        return rows
    # 2026-09-04：`max_dd_pct` 可能是 `None`（該窗 perf 非 ok，見
    # `trader_stats.WindowStats`）——分位數只用有證據的列計算，`None` 的列
    # 永不掛 `low_drawdown`（沒有回撤數字，無從判斷它是不是「低回撤」）。
    dds = sorted(abs(Decimal(str(r.windows["month"].max_dd_pct)))
                for r in rows if r.windows["month"].max_dd_pct is not None)
    threshold = dds[max(0, -(-len(dds) // 4) - 1)] if dds else None
    out = []
    for r in rows:
        tags = []
        month_dd = r.windows["month"].max_dd_pct
        if (threshold is not None and month_dd is not None
                and abs(Decimal(str(month_dd))) <= threshold):
            tags.append("low_drawdown")
        if (r.concentration_pct is not None
                and Decimal(str(r.concentration_pct)) > cfg.max_concentration_pct):
            tags.append("concentrated")
        out.append(dataclasses.replace(r, tags=tuple(tags)))
    return out


def qualify(row: ExploreRow, cfg: ExploreConfig, *, window: str = DEFAULT_WINDOW,
           require_sample: bool = True, max_dd_filter: bool = True,
           exclude_concentrated: bool = True) -> bool:
    """資格過濾（R2-01，全在後端）。三個布林是內部/測試逃生門（R4-3：公開端點
    已改成四個自由數值門檻，不再對外送布林 chip，見模組檔頭「R4-3」節）；
    `window` 決定回撤門檻要看**哪一窗**的 `max_dd_pct`（R4-3：不再永遠用
    month——使用者切換顯示窗，回撤過濾也跟著切換，"誠實揭露"見同節）。

    邊界（equal 一律算通過——常數描述的是「上限」/「下限」，卡在門檻上不該被
    無聲刷掉；本模組唯一的權威定義，測試逐條釘死）：
    - 樣本門檻：`live_days >= min_trading_days`（W1：live_days＝allTime
      首末點日曆跨距天數，門檻語意＝「實盤 ≥ min_trading_days 天」，與
      `window` 無關）且 `order_count_30d >= min_fills`（下限，"至少"語意，
      等於門檻通過；D5：`fills_truncated=True` 時 `order_count_30d` 本身是
      下限值，真實筆數只會更多，這條比較方向不受影響）。
    - 回撤上限：`abs(windows[window].max_dd_pct) <= max_drawdown_pct`（等於
      門檻通過）；該窗對這一列是 `None`（day/week best-effort 缺席），或該窗
      存在但 `max_dd_pct is None`（perf 非 ok，見 `WindowStats`）→ 皆視為
      通過——沒有證據代表回撤超標，比照下面集中度 `None` 的既有慣例，不得
      因為缺資料就先假設它超標。
    - 集中度上限：`concentration_pct <= max_concentration_pct`（等於門檻通過；
      `None`＝無成交量資料可算集中度，視為通過——沒有證據代表集中，不得因為
      缺資料就先假設它超標）。
    """
    if require_sample:
        if row.live_days < cfg.min_trading_days:
            return False
        if row.order_count_30d < cfg.min_fills:
            return False
    if max_dd_filter:
        stats = row.windows.get(window)
        if (stats is not None and stats.max_dd_pct is not None
                and abs(Decimal(str(stats.max_dd_pct))) > cfg.max_drawdown_pct):
            return False
    if exclude_concentrated:
        if (row.concentration_pct is not None
                and Decimal(str(row.concentration_pct)) > cfg.max_concentration_pct):
            return False
    return True


def sort_key(row: ExploreRow, *, window: str = DEFAULT_WINDOW) -> Decimal:
    """D2（2026-09-04）：所選窗 `pnl_usd` 降冪（金額，不再做報酬÷回撤比值——
    分母來自另一個指標且可能為 `None`）。R4-3：`window` 對這一列是 `None`
    （day/week best-effort 缺席）→ 退回 `"month"`（`enrich_candidate` 保證
    恆非 `None`）——排序需要一個確定性的鍵，前端該窗儲存格仍誠實顯示「—」，
    兩者不衝突（見模組檔頭「R4-3」節）。"""
    stats = row.windows.get(window) or row.windows["month"]
    return Decimal(str(stats.pnl_usd))


def paginate(rows: list[ExploreRow], page: int, page_size: int) -> list[ExploreRow]:
    """1-indexed 分頁；`page`/`page_size` 非正 → 空清單（呼叫端的端點層另外
    對這兩個參數做 422 驗證，這裡只負責純粹的切片語意）。"""
    if page < 1 or page_size < 1:
        return []
    start = (page - 1) * page_size
    return rows[start:start + page_size]


def _roi_sort_key(row: dict) -> Decimal:
    """候選池排序鍵：stats-data month 窗的 roi（降冪）。刻意重用
    `hl_leaderboard._window_perf`（同套件內部函式，解析的是同一份
    `windowPerformances` 配對清單——見該函式檔頭已驗證過的形狀假設，不重新
    發明一份可能漂移的複本）。缺窗／解析失敗／NaN 一律排到最後（鏡像
    `hl_leaderboard._pnl_sort_key` 的既有慣例）。"""
    perf = hl_leaderboard._window_perf(row, "month")
    try:
        value = Decimal(str(perf.get("roi", "")))
    except (InvalidOperation, TypeError):
        return Decimal("-Infinity")
    if value.is_nan():
        return Decimal("-Infinity")
    return value


def candidate_addresses(payload: dict, pool_size: int,
                        excluded: set[str]) -> list[tuple[str, str | None]]:
    """D1 候選池：stats-data month 窗依 roi 降冪取前 `pool_size` 名，排除
    `excluded`（Filet 自營 leader 地址，D8；比對前正規化小寫）。回傳
    `[(address, display_name), ...]`，address 原樣（不轉小寫——與
    `hl_leaderboard.top_rows` 對外欄位一致，前端顯示用；enrich 查詢用
    `HLGateway` 對大小寫不敏感）。"""
    rows = (payload or {}).get("leaderboardRows") or []
    sortable = [r for r in rows
               if isinstance(r, dict) and r.get("ethAddress")
               and r["ethAddress"].lower() not in excluded]
    sortable.sort(key=_roi_sort_key, reverse=True)
    return [(r["ethAddress"], r.get("displayName")) for r in sortable[:pool_size]]


# ---------------------------------------------------------------------------
# ExploreIndex：背景建置、原子換版（D1）
# ---------------------------------------------------------------------------
class ExploreIndex:
    """`GET /api/public/explore` 的資料索引。仿 `hl_leaderboard.LeaderboardCache`
    的 TTL 精神，但建置成本遠高於一次 GET（要序列 enrich 上百個地址），所以
    改用「背景 thread 建置、讀路徑永不阻塞」而非該類別的『等進行中那條 thread』
    模式——見 `query()`。

    `leaderboard_source_fn`：回傳 stats-data month 窗原始 payload 或 `None`
    （沿用既有 `LeaderboardCache` 實例，不重複下載 36MB，見 `app.py` 接線）。
    `hl`：需提供 `.portfolio()` / `.get_fills_raw_paged()` / `.clearinghouse_state()`
    （唯讀，見 `hl.py`；D5：`get_fills_raw_paged` 見 Task 3a，回傳未裁切的原始
    HL fills 形狀）。
    `excluded_fn`：回傳 Filet 自營 leader 地址集合（D8，見 `app.py` 接線，讀精選
    白名單）。
    `snapshot_path`：I-17 磁碟快照路徑，`None`＝不落盤（沿用純記憶體既有語意，
    多數測試直接構造 `ExploreIndex` 時不傳，行為不變）；有設時建構子會嘗試
    `load_snapshot` 立即灌一份舊資料（見模組檔頭「I-17」節），`build_sync`
    每次成功建置後會寫回一份新的。
    """

    def __init__(self, *, leaderboard_source_fn: Callable[[], dict | None],
                hl, excluded_fn: Callable[[], set[str]], cfg: ExploreConfig,
                now_fn: Callable[[], float], sleep_fn=time.sleep,
                index_ttl_s: float = INDEX_TTL_S,
                enrich_ttl_s: float = ENRICH_CACHE_TTL_S,
                enrich_cache_max: int = ENRICH_CACHE_MAX,
                fills_window_days: int = FILLS_WINDOW_DAYS,
                snapshot_path: str | None = None):
        self._leaderboard_source_fn = leaderboard_source_fn
        self._hl = hl
        self._excluded_fn = excluded_fn
        self._cfg = cfg
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._ttl_s = index_ttl_s
        self._enrich_ttl_s = enrich_ttl_s
        self._enrich_cache_max = enrich_cache_max
        self._fills_window_days = fills_window_days
        self._snapshot_path = snapshot_path

        self._lock = threading.Lock()
        self._rows: list[ExploreRow] | None = None   # 目前對外服務的一版
        # R4-3：`self._rows` 是用哪個 `EXPLORE_INDEX_VERSION` 建的（見模組檔頭
        # 「index 結構版本」節）；`None` 表示尚未建置過，與版本不相容視為同一種
        # 「沒有可用快照」——`query()`／`_maybe_trigger_build()` 兩處都檢查。
        self._rows_version: int | None = None
        self._built_at: float | None = None
        self._total_scanned = 0
        self._building = False                         # single-flight：背景建置中
        self._enrich_cache: dict[str, tuple[float, ExploreRow | None]] = {}

        # I-17：啟動時嘗試從磁碟快照灌一份舊資料，讓「程序重啟後第一個請求」
        # 不必等一輪背景建置（數分鐘）才有資料可查（見模組檔頭「I-17」節）。
        # 版本不符／檔不存在／檔壞 → `load_snapshot` 回 `None`，維持既有冷建
        # 語意（`self._rows` 留 `None`），不拋例外、不阻塞建構子。
        if self._snapshot_path is not None:
            snap = load_snapshot(self._snapshot_path)
            if snap is not None:
                self._rows = snap["rows"]
                self._rows_version = EXPLORE_INDEX_VERSION
                self._built_at = snap["built_at"]
                self._total_scanned = snap["total_scanned"]

    def _call_hl(self, fn: Callable[[], object], *, what: str) -> object:
        """單一 HL 呼叫的節流＋429 退避重試邊界（見類別所在模組檔頭 2026-08-30
        事故記錄＋ review 修正輪 C4 殘洞記錄）。每個請求之間（不是每個地址之間）
        睡 `cfg.enrich_call_interval_s`，保護與實盤引擎共用的 HL 額度。

        429（rate limited；讀操作冪等 → 視為 transient，工程原則 2）→ 指數退避
        `RATE_LIMIT_RETRY_DELAYS_S`（2s/8s/30s）；退避耗盡仍 429 → `_RateLimitedAbort`
        （額度已被打穿，往上傳給 `build_sync` 中止整輪建置，不是跳過這一個地址）。
        非 429 的其他錯誤 → 不重試，直接上拋（呼叫端 `_enrich_one` 既有的
        「跳過該列」語意，不變）。

        ⭐ C4 殘洞修法：節流 sleep 掛在 `finally`，包住**整個** `_call_hl`
        呼叫（含內部的 429 重試迴圈），而不是只掛在成功的那一行。這樣不論
        最終走哪條退出路徑——`fn()` 成功回傳、非 429 例外原樣往上拋、還是
        429 退避耗盡拋出 `_RateLimitedAbort`——離開這個函式之前都會先睡滿
        一次 `enrich_call_interval_s`。舊版把 sleep 放在 try 區塊內「成功」
        分支的最後一行，非 429 例外會直接從 `except` 的 `raise` 跳出整個
        函式、完全不經過那一行，節流因此對這條路徑形同不存在（見模組檔頭
        C4 記錄）。429 重試迴圈內部各次退避已有自己的延遲（2s/8s/30s，遠大於
        `enrich_call_interval_s`），多睡一次介於 finally 的間隔不影響整體
        退避節奏，只是多一層保底。
        """
        delays = RATE_LIMIT_RETRY_DELAYS_S
        try:
            for attempt in range(len(delays) + 1):
                try:
                    return fn()
                except Exception as e:
                    if not _is_rate_limited(e):
                        raise
                    if attempt == len(delays):
                        logger.error(
                            "build aborted: rate limited（%s，退避 %d 次仍 429）",
                            what, len(delays))
                        raise _RateLimitedAbort(what) from e
                    delay = delays[attempt]
                    logger.warning(
                        "explore %s：429 rate limited（第 %d/%d 次退避），%.0fs 後重試",
                        what, attempt + 1, len(delays), delay)
                    self._sleep_fn(delay)
            raise RuntimeError("unreachable")  # pragma: no cover
        finally:
            self._sleep_fn(self._cfg.enrich_call_interval_s)

    def _enrich_one(self, address: str, display_name: str | None) -> ExploreRow | None:
        """per-address enrich，帶 30 分鐘 TTL、LRU 256 上限快取（近似 LRU：
        淘汰最舊寫入時間，同 `app.py._cached_trader_data` 既有寫法）。任何一步
        （portfolio/fills/clearinghouse）非 429 失敗 → 整列跳過（`None`），記入
        快取，60 天內同一輪重建不會重複打壞地址的上游（enrich TTL 本身就是
        負面快取）。429 退避耗盡 → `_RateLimitedAbort` 原樣往上傳（不快取、
        不當成「這個地址壞掉」，見 `_call_hl` 與 `build_sync`）。
        """
        now = self._now_fn()
        with self._lock:
            cached = self._enrich_cache.get(address)
        if cached is not None and now - cached[0] < self._enrich_ttl_s:
            return cached[1]
        row: ExploreRow | None = None
        try:
            portfolio_raw = self._call_hl(lambda: self._hl.portfolio(address),
                                          what=f"portfolio address={address}")
            end = datetime.fromtimestamp(now, tz=timezone.utc)
            start = end - timedelta(days=self._fills_window_days)
            # D5（2026-09-05）：改走原始形狀分頁出口（Task 3a）——`fills_stats`
            # 需要 dir/oid/startPosition/closedPnl，`get_fills_detail_paged` 會
            # 把這些欄位裁掉，見 hl.get_fills_raw_paged docstring。
            fills, fills_truncated = self._call_hl(
                lambda: self._hl.get_fills_raw_paged(address, start, end,
                                                     max_pages=self._cfg.fills_max_pages),
                what=f"fills address={address}")
            ch_state = self._call_hl(lambda: self._hl.clearinghouse_state(address),
                                     what=f"clearinghouse address={address}")
            row = enrich_candidate(address, display_name, portfolio_raw, fills, ch_state,
                                   fills_truncated=fills_truncated)
        except _RateLimitedAbort:
            raise  # 中止整輪建置的訊號，不得被下面這個 except 吞成「跳過該列」
        except Exception as e:  # noqa: BLE001 — 展示端點：單一地址失敗不得中斷整批建置
            logger.error("explore enrich 失敗 address=%s: %r", address, e)
            row = None
        with self._lock:
            if (address not in self._enrich_cache
                    and len(self._enrich_cache) >= self._enrich_cache_max):
                oldest = min(self._enrich_cache, key=lambda k: self._enrich_cache[k][0])
                del self._enrich_cache[oldest]
            self._enrich_cache[address] = (now, row)
        return row

    def build_sync(self) -> None:
        """實際建置工作（背景 thread 的 target；亦可在測試中直接同步呼叫取得
        決定性行為，不必跑真線程）。

        上游候選池來源失敗／無資料 → 直接返回、**不動** `self._rows`
        （fail-open 到舊版；若本來就沒有舊版，`query()` 會繼續回
        `building: True`，見類別檔頭）。排除清單載入失敗 → 視為空清單
        （寧可這一輪意外把 Filet 自營地址也掃進候選池——下一輪排除清單恢復
        就會自然排除——也不要整個建置流程被一個旁支查詢拖垮）。

        任一地址的 HL 呼叫 429 退避耗盡（`_RateLimitedAbort`）→ **中止整輪建置**
        （不繼續掃剩餘候選——額度已被打穿，繼續燒只會全部繼續 429）、**不動**
        `self._rows`（fail-open 到舊版，同上游故障的既有語意），見模組檔頭
        2026-08-30 事故記錄。
        """
        try:
            payload = self._leaderboard_source_fn()
        except Exception as e:  # noqa: BLE001 — fail-open，見上
            logger.error("explore index：候選池來源查詢失敗: %r", e)
            payload = None
        if payload is None:
            logger.error("explore index：leaderboard 來源無資料，本輪建置跳過（沿用舊版）")
            return
        try:
            excluded = {a.lower() for a in (self._excluded_fn() or set())}
        except Exception as e:  # noqa: BLE001
            logger.error("explore index：Filet 自營地址排除清單載入失敗，本輪視為空清單: %r", e)
            excluded = set()

        candidates = candidate_addresses(payload, self._cfg.candidate_pool, excluded)
        rows: list[ExploreRow] = []
        try:
            for address, display_name in candidates:
                row = self._enrich_one(address, display_name)
                if row is not None:
                    rows.append(row)
        except _RateLimitedAbort as e:
            logger.error(
                "build aborted: rate limited（%s）——中止本輪建置，保留舊 snapshot", e)
            return
        rows = _apply_tags(rows, self._cfg)
        built_at = self._now_fn()
        total_scanned = len(candidates)

        with self._lock:
            self._rows = rows
            self._rows_version = EXPLORE_INDEX_VERSION
            self._built_at = built_at
            self._total_scanned = total_scanned

        # I-17：成功建置一輪後順手落一份磁碟快照，供下次程序重啟時立即可查
        # （見模組檔頭「I-17」節）。寫入失敗（例如目錄權限）只記錄、不影響
        # 本輪建置已經成功換版這件事——快取是加速手段，不是正確性的一部分。
        if self._snapshot_path is not None:
            try:
                dump_snapshot(self._snapshot_path, rows=rows, built_at=built_at,
                             total_scanned=total_scanned)
            except OSError as e:
                logger.error("explore index 快照落檔失敗（不影響本輪建置結果）: %s", e)

    def _maybe_trigger_build(self) -> None:
        """TTL 過期（或從未建置過，或現有快照的結構版本已不相容——R4-3，見
        `EXPLORE_INDEX_VERSION`）且目前沒有背景建置在跑 → 開一條 daemon
        thread 執行 `build_sync`；呼叫本身立即返回，不等 thread 結束
        （見類別檔頭：讀路徑永不阻塞）。"""
        now = self._now_fn()
        with self._lock:
            fresh = (self._built_at is not None
                     and now - self._built_at < self._ttl_s
                     and self._rows_version == EXPLORE_INDEX_VERSION)
            if fresh or self._building:
                return
            self._building = True

        def worker():
            try:
                self.build_sync()
            finally:
                with self._lock:
                    self._building = False

        threading.Thread(target=worker, daemon=True).start()

    def query(self, *, page: int = 1, window: str = DEFAULT_WINDOW,
             min_live_days: int | None = None, min_fills: int | None = None,
             max_dd_pct: float | None = None, max_concentration_pct: float | None = None,
             require_sample: bool = True, max_dd_filter: bool = True,
             exclude_concentrated: bool = True) -> dict:
        """讀路徑：永不阻塞（觸發背景建置後立即用目前狀態回應）。回傳形狀見
        `app.py` 端點層文件字串：`{rows, page, page_size, total_qualified,
        total_scanned, pool, updated_at, building}`（`pool`：I-17，鏡射
        `total_scanned`——這一輪實際掃描的候選數，前端榜首常駐提示句「自
        {pool} 個候選帳戶中列出…」用這個數字，不寫死候選池上限常數，見
        `explore/page.tsx`）。

        從未成功建置過，或現有快照的結構版本已不相容（`self._rows is None`
        或 `self._rows_version != EXPLORE_INDEX_VERSION`，R4-3，見模組檔頭
        「index 結構版本」節）→ `building: True`、空 rows、計數皆 0、
        `updated_at: None`（前端 R2·C 態二）——版本不相容的快照結構上不能
        安全地拿去 `qualify`/`sort_key`/`to_dict`（欄位形狀已經換過），視同
        「沒有可用快照」，觸發重建。

        `window`：所選期間（`WINDOW_KEYS` 之一），決定 `qualify` 的回撤過濾
        看哪一窗、`sort_key` 用哪一窗排序（R4-3；不影響候選池——候選池仍是
        `build_sync` 固定用 stats-data month 窗 roi 選出，見模組檔頭「R4-3」節
        「誠實揭露」段）。
        `min_live_days`／`min_fills`／`max_dd_pct`／`max_concentration_pct`：
        R4-3 自由門檻（`None`＝沿用 `self._cfg` 的預設值，供內部/測試呼叫端在
        不關心門檻時省略；`app.py` 端點層一律夾取後傳入明確數值，見
        `clamp_explore_params`）。

        ⭐ 讀值**必須**在觸發背景建置**之前**取得快照，不能反過來：`_maybe_trigger_
        build()` 開的背景 thread 若剛好在本次呼叫的極短時間內就跑完（例如注入的
        `leaderboard_source_fn`/`hl` 全同步、無阻塞——單元測試最常見的情境），
        會在本函式讀 `self._rows` 之前就把它從 `None` 換成新版，讓「從未建置過
        → building: True」這個判斷變成競態、非決定性（2026-08-30 全量跑
        `test_endpoint_never_built_returns_building_true` flake 的根因：機械可
        重現，見 commit message）。反過來寫（先讀快照、後觸發背景建置）本次呼叫
        的回應內容只取決於呼叫**當下**已完成的版本，與背景 thread 之後何時完成
        無關——讀路徑永不阻塞、且結果決定性，兩者同時成立。
        """
        with self._lock:
            rows = self._rows
            rows_version = self._rows_version
            built_at = self._built_at
            total_scanned = self._total_scanned
        self._maybe_trigger_build()
        if rows is None or rows_version != EXPLORE_INDEX_VERSION:
            return {"rows": [], "page": page, "page_size": self._cfg.page_size,
                   "total_qualified": 0, "total_scanned": 0, "pool": 0,
                   "updated_at": None, "building": True}
        cfg = self._cfg
        if (min_live_days, min_fills, max_dd_pct, max_concentration_pct) != (None, None, None, None):
            cfg = dataclasses.replace(
                cfg,
                min_trading_days=cfg.min_trading_days if min_live_days is None else min_live_days,
                min_fills=cfg.min_fills if min_fills is None else min_fills,
                max_drawdown_pct=(cfg.max_drawdown_pct if max_dd_pct is None
                                  else Decimal(str(max_dd_pct))),
                max_concentration_pct=(cfg.max_concentration_pct if max_concentration_pct is None
                                       else Decimal(str(max_concentration_pct))),
            )
        qualified_rows = [r for r in rows
                          if qualify(r, cfg, window=window, require_sample=require_sample,
                                    max_dd_filter=max_dd_filter,
                                    exclude_concentrated=exclude_concentrated)]
        qualified_rows.sort(key=lambda r: sort_key(r, window=window), reverse=True)
        page_rows = paginate(qualified_rows, page, self._cfg.page_size)
        return {"rows": [r.to_dict() for r in page_rows], "page": page,
               "page_size": self._cfg.page_size,
               "total_qualified": len(qualified_rows), "total_scanned": total_scanned,
               "pool": total_scanned,
               "updated_at": int(built_at) if built_at is not None else None,
               "building": False}
