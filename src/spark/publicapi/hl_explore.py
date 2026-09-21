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
4. **`ExploreIndex`**（Task 3.4 起純讀路徑，見類別檔頭）：只服務
   `ExplorePublisher` 換上來的最新一版 `ExploreRow`；候選池選取、逐地址
   enrich、429／額度節流全部移交 `ExploreScheduler`（背景 thread，逐 job
   執行，見 `explore_scheduler.py`）與 `ExplorePublisher`（定期合成換版，
   見 `explore_publisher.py`）。**從未成功發布過**時 `query()` 立即回
   `building`／`initializing: True` ＋空 rows，不阻塞呼叫端；已有舊版時，
   即使排程正在更新或上游故障，一律**回舊版**（fail-open，同
   `LeaderboardCache` 檔頭精神）。

⚠️ 事故沿革（2026-08-30～2026-09-19，機制本身已於 2026-09-20 Task 1.3／
Task 3.4 全部移除——本段只留教訓，不再列出已刪除的內部函式／例外名稱）：
早期版本讓 `ExploreIndex` 自己在背景 thread 序列建置整個候選池，同一地址
連續數個 HL 請求之間完全沒有節流間隔，實測 burst 到約 60 req/s，觸發大量
429；enrich 把 429 誤判成「該地址失敗→跳過」，燒完整個候選池後以近乎
0 列完成建置＝空榜上線（2026-08-30 mainnet 整合實跑事故）。後續兩輪修法
（改成固定間隔 sleep、429 指數退避重試三次、退避耗盡才中止整輪並保留舊
snapshot）仍然是把 429 當節流器用——2026-09-19 正式機事故重演同一症狀，
且燒穿同 IP 額度殃及 dashboard／onboard。根本修法：Task 1.3 起 429／額度
節流全部移交 `spark.publicapi.hl_budget.WeightLimiter` ＋ `HLGateway`
（`ExploreIndex`／排程器拿到的 `hl` 必須是 `gateway.scoped("explore")`），
`ExploreIndex` 本身不再打任何上游、不再自己 sleep 或重試；Task 3.4 起連
「建置」這件事本身也移交 `ExploreScheduler`／`ExplorePublisher`，
`ExploreIndex` 只剩讀路徑與快照載入（見這兩個模組各自的檔頭）。

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
  `spark` 三個頂層欄位→`windows` dict）。`EXPLORE_INDEX_VERSION` 版本標記＋
  `ExploreIndex._rows_version` 讓「偵測不相容→視為未發布、等待重新換版」這條
  語意變成可測試、可驗證的行為（`query()` 一旦看到 `_rows_version !=
  EXPLORE_INDEX_VERSION` 就當作沒有可用快照，回 `building`／
  `initializing: True`），也替未來若真的加上跨行程快取/落盤留一個現成的
  相容性檢查點。

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
見 D15 段）。`ExploreIndex` 本身純記憶體、不落盤，程序重啟後第一個請求
在 `ExplorePublisher` 換上第一版之前必定 `building: True` ＋空 rows——本輪
加**磁碟快照快取**：

- `dump_snapshot`／`load_snapshot`：`ExploreIndex._rows` 的 JSON 序列化（含
  `EXPLORE_INDEX_VERSION` 與 `built_at`），原子寫入（`os.replace`，同
  `leader_change_apply` 等既有落檔慣例——先寫 `.tmp` 再換名，避免半寫壞檔）。
  序列化用 `ExploreRow.to_dict()` 現成形狀，反序列化 `_row_from_dict` 精確
  逆操作（含 `windows` dict／`exposure` 拆包／tuple 欄位）。
- `ExploreIndex.__init__` 新增可選 `snapshot_path`：非 `None` 時嘗試
  `load_snapshot`——版本相符 → 立即灌進 `self._rows`／`_rows_version`／
  `_built_at`／`_total_scanned`，程序重啟後第一個請求就有資料可查（不必等
  `ExplorePublisher` 換第一版）；版本不符／檔不存在／檔壞 → 忽略，
  `self._rows` 維持 `None`，行為等同沒有快照（冷啟，既有語意不變）。
- `ExplorePublisher.maybe_publish` 每次成功換版後（`ExploreIndex.set_published`
  的同一刻）順手落一份新快照（`snapshot_path` 有設才寫；寫入失敗只記錄、
  不影響本次換版結果——快取是加速手段，不是資料正確性的一部分，見
  `explore_publisher.py`）。
- 磁碟快照把「有沒有舊版可服務」這件事從「這個 process 有沒有成功發布過至少
  一次」放寬成「這個 process **或前一個 process** 有沒有成功發布過至少
  一次」，`query()` 的讀路徑判斷邏輯本身不變。
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
from dataclasses import dataclass, field
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
# ≤ 6000 筆——分頁呼叫之間沒有額外節流間隔（節流全在 `HLGateway`／
# `WeightLimiter` 這一層做），這是已知的 burst 面，上限 3 就是為了壓它
# （見 Task 3a）。
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

FILLS_WINDOW_DAYS = 30

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

# ---------------------------------------------------------------------------
# Task 11（2026-09-05，D11–D14）：`/api/public/explore` 後端 sort/order。
# 分頁在後端切，前端只拿得到當頁列——排序必須也在後端做，否則使用者點表頭
# 排序只能排到「這一頁」，翻頁後排序就散掉。
# ---------------------------------------------------------------------------
SORT_FIELDS = ("pnl", "max_dd", "live_days", "win_rate")
SORT_ORDERS = ("asc", "desc")
DEFAULT_SORT = "pnl"
DEFAULT_ORDER = "desc"

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
# 3 → 4（2026-09-20，Task 4.1，spec §9.2）：`ExploreRow` 新增 `as_of`／
# `fills_coverage` 兩欄（漸進發布：每列各自的資料新鮮度／成交完整性，不再靠
# 單一 `published_at` 冒充全部欄位同時新鮮）——`load_snapshot` 讀到 v3 快照
# 會就地補上這兩欄（`as_of` 全填 `built_at`、`fills_coverage` 為
# `backfilling`）後當 v4 載入，不因版號不符就整份丟棄（D7：不丟棄舊快照）。
EXPLORE_INDEX_VERSION = 4

# Task 4.1：`fills_coverage` 的預設值（`ExploreRow` 欄位預設值與 v3→v4 快照
# 遷移共用同一份常數，避免兩處手寫字面量漂移）。
# Task 7.1（2026-09-21，使用者裁決）：加 `synced_through`（epoch ms｜null，＝
# `fills_sync.synced_through_ms`——已確認同步到的游標，不代表最新成交）與
# `last_success_at`（epoch 秒｜null，＝`fills_sync.updated_at`——最近一次抓頁
# 成功時間）。`as_of.fills` 語意不變、仍＝`last_success_at`（相容）；兩者分開
# 是因為回補中時 `last_success_at` 會一直前進但 `synced_through` 可能落後很多。
# Task 7.5（2026-09-21，使用者裁決）：加 `window_start`／`window_end`（epoch ms｜null，
# ＝`fills_sync.window_start_ms`／`window_end_ms`，本輪固定查詢區間）與
# `params_fp`（string｜null，＝`fills_sync.params_fp`，查詢參數留證——見
# `explore_fills_sync.PARAMS_FP`）。三者都是「這個 completeness 判定是基於
# 哪一次查詢」的可追溯證據，不影響 `state`／`reason` 既有語意。
# Task 7.9b（2026-09-21，B6）：加 `evidence`（`{scan_id, kind, window_start,
# window_end, finished_at, reason, gap, unknown}`，全部可 None）——回溯「建立
# 目前 completeness 的那次全區間遍歷」，見 `explore_fills_sync.build_fills_coverage`。
# `window_start`／`window_end` 語義變更：改為增量軌覆蓋區間
# `[inc_from, synced_through]`（不再是「目前這一輪」的查詢區間，那個語意現在
# 放進 `evidence.window_start`／`evidence.window_end`）。
DEFAULT_FILLS_COVERAGE: dict = {"state": "backfilling", "observed_from": None,
                                "observed_to": None, "reason": None,
                                "synced_through": None, "last_success_at": None,
                                "window_start": None, "window_end": None, "params_fp": None,
                                "evidence": None}


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
    account_bucket: str | None     # Task 4.1：`ch_state` 缺席（尚未 enrich 過）→ None，
                                    # 不得冒充「—」（那個字面值原本代表「查過但算不出」）
    # R4-3：四窗（`WINDOW_KEYS`）各自的 `WindowStats`（`spark.filet.trader_stats`）；
    # `"month"`／`"allTime"` 是 enrich 的 gating 條件（缺席或不足兩點 → 整列跳過整個
    # ExploreRow 都不會被建構），保證這兩鍵在成功建構的列上恆非 None；`"day"`／
    # `"week"`是 best-effort，缺席／無效 → 該鍵存 None（不得用其他窗的數字冒充，見
    # 模組檔頭「R4-3」節）。
    windows: dict[str, "WindowStats | None"]
    live_days: int | None          # W1：allTime 首末點日曆跨距天數（非 distinct 日數）；
                                    # Task 4.1：`portfolio_raw` 缺席（尚未 enrich 過）→
                                    # None（「分析待完成」，不是 0——`qualify` 因此不合格）
    order_count_30d: int           # D3：distinct 訂單數（不是 fills 數）；P6：coverage
                                    # 非 complete 時仍是已觀測筆數的**下限**，語意由
                                    # `fills_coverage` 標示（見 `enrich_candidate` P6 段）
    closed_positions_30d: int | None   # D3：部位歸零的生命週期數；P6：coverage 非
                                    # complete → None（未知≠0，見 `enrich_candidate`）
    realized_pnl_30d_usd: float | None  # D3：Σ closedPnl；P6：coverage 非 complete → None
    close_win_rate_pct: float | None   # None＝無結倉樣本，或 P6：coverage 非 complete（未知）
    concentration_pct: float | None
    exposure_dir: str | None       # "long" / "short" / None（無倉位或無法解析；
                                    # D14：locale 中性代碼，前端自行對映顯示文案）
    exposure_pct: float | None
    tags: tuple[str, ...] = ()     # 子集 {"low_drawdown", "concentrated"}（D14：
                                    # locale 中性代碼，前端自行對映顯示文案）
    fills_truncated: bool = False  # D5：分頁抓到 fills_max_pages 上限仍滿頁
                                    # → 成交統計三個 *_30d 欄位是下限值/樣本估計
    # Task 4.1（spec §9.2）：漸進發布——每列各自的資料新鮮度／成交完整性，不能
    # 用單一 `published_at` 冒充全部欄位同時新鮮。`as_of` 三鍵
    # `portfolio`/`state`/`fills` 對應各自來源的 `fetched_at`/`updated_at`
    # （epoch 秒），缺該來源快取 → 該鍵 None。`fills_coverage` 同
    # `app.py._local_trader_data` 的 `coverage` 形狀（`state`／`observed_from`／
    # `observed_to`／`reason`），兩頁共用同一份定義（工程原則 1）。
    as_of: dict[str, float | None] = field(default_factory=dict)
    fills_coverage: dict = field(default_factory=lambda: dict(DEFAULT_FILLS_COVERAGE))
    # P6（2026-09-20，D13）：三態資格，取代單純布林 `qualify`——由 `classify()` 決定
    # （`ExploreIndex.query()` 依當次請求門檻動態算，見該函式；直接建構
    # `ExploreRow`（測試／舊快照遷移）沒有門檻可算，預設 `"eligible"`／`None`，
    # 之後一律被 `query()` 的 `classify()` 覆寫，僅 `eligibility_reason ==
    # "enrich_error"` 的列（`explore_publisher.compose_rows` 單一地址 enrich 失敗
    # 直接構造，見該函式）例外——`query()` 對這類列跳過重新分類，見該函式檔頭）。
    eligibility: str = "eligible"          # "eligible" | "pending" | "ineligible"
    eligibility_reason: str | None = None  # 值域：live_days/max_dd/min_fills/
                                            # concentration/portfolio_missing/
                                            # fills_unknown/enrich_error；eligible 為 None

    def to_dict(self) -> dict:
        row = mask_incomplete_fills(self)
        return {
            "address": row.address,
            "display_name": row.display_name,
            "label": row.label,
            "coins": list(row.coins),
            "account_bucket": row.account_bucket,
            "windows": {k: (v.to_dict() if v is not None else None)
                       for k, v in row.windows.items()},
            "live_days": row.live_days,
            "order_count_30d": row.order_count_30d,
            "closed_positions_30d": row.closed_positions_30d,
            "realized_pnl_30d_usd": row.realized_pnl_30d_usd,
            "close_win_rate_pct": row.close_win_rate_pct,
            "concentration_pct": row.concentration_pct,
            "exposure": {"dir": row.exposure_dir, "pct": row.exposure_pct},
            "tags": list(row.tags),
            "fills_truncated": row.fills_truncated,
            "as_of": dict(row.as_of),
            "fills_coverage": dict(row.fills_coverage),
            "eligibility": row.eligibility,
            "eligibility_reason": row.eligibility_reason,
        }


def mask_incomplete_fills(row: ExploreRow) -> ExploreRow:
    """Task 6.5（P6 契約 A，工程原則 5 結構性修法）：`fills_coverage.state !=
    "complete"` 時，成交衍生欄位一律 `None`／空——未知 ≠ 0，前端不得自行推算。
    這是**唯一輸出口**的把關（`ExploreRow.to_dict()`／`load_snapshot` v3 遷移／
    `sort_rows` 排序鍵取值都經過這裡），`enrich_candidate` 裡的等價遮蔽
    （P6，D13）是雙保險，不是被取代——兩處任一漏改，這裡仍兜底。

    `order_count_30d` 刻意不在遮蔽範圍：它是「本輪已觀測筆數的下限」，即使
    coverage 未完整仍是已知量（見 `enrich_candidate` P6 段的欄位註記）；
    `load_snapshot` 對 v3 舊快照另有專門處理（見該函式），因為那些值完全
    不是「本輪觀測」，不能只靠這裡的通用遮罩覆蓋。"""
    if row.fills_coverage.get("state") == "complete":
        return row
    return dataclasses.replace(
        row,
        close_win_rate_pct=None,
        concentration_pct=None,
        closed_positions_30d=None,
        realized_pnl_30d_usd=None,
        coins=(),
        tags=tuple(t for t in row.tags if t != "concentrated"),
    )


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
        as_of=dict(d.get("as_of") or {}),
        # Task 7.6 點 10（複審 S4 修法）：舊寫法 `dict(d.get("fills_coverage") or
        # DEFAULT_FILLS_COVERAGE)` 只在整個 `fills_coverage` 鍵缺席時才落回預設值
        # ——若快照的 `fills_coverage` 存在但缺 Task 7.1／7.5 才新增的五個鍵
        # （`synced_through`／`last_success_at`／`window_start`／`window_end`／
        # `params_fp`，皆屬 v4 快照新增早期版本可能沒有的鍵），這些鍵會直接從
        # `ExploreRow.fills_coverage` 消失，而不是「未知＝None」。改成逐鍵合併：
        # 先鋪 `DEFAULT_FILLS_COVERAGE`（五個新鍵皆 None），再蓋上快照裡實際
        # 有的鍵。
        fills_coverage=({**DEFAULT_FILLS_COVERAGE, **(d.get("fills_coverage") or {})}),
        # P6：舊快照（發布於本欄位新增之前）缺這兩鍵 → 落回 `ExploreRow` 的類別
        # 預設（"eligible"/None）——`ExploreIndex.query()` 下一次讀取會用當次
        # 門檻重新 `classify()`，不會讓過期的預設值長期冒充真正的資格。
        eligibility=d.get("eligibility", "eligible"),
        eligibility_reason=d.get("eligibility_reason"),
    )


def dump_snapshot(path: str, *, rows: list[ExploreRow], built_at: float,
                  total_scanned: int) -> None:
    """I-17：原子寫入榜單快照（`.tmp` 寫完再 `os.replace`，避免行程被中斷時
    留下半寫壞檔——同 repo 既有落檔慣例）。寫入失敗（例如目錄不可寫）由
    呼叫端（`ExplorePublisher.maybe_publish`）自行 try/except 決定要不要吞掉；
    本函式本身不吞錯，讓呼叫端能記錄清楚是哪一步壞的。"""
    payload = {"version": EXPLORE_INDEX_VERSION, "built_at": built_at,
              "total_scanned": total_scanned, "rows": [r.to_dict() for r in rows]}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)


def load_snapshot(path: str, *, cfg: "ExploreConfig | None" = None) -> dict | None:
    """I-17：讀快照。不存在／解析失敗／版本不符（且非可遷移的 v3） → `None`
    （呼叫端視為「沒有可用快照」，忽略、走既有冷建語意，不拋例外——這是加速
    路徑，不是資料正確性的一部分，讀不到就當作沒發生過）。成功時回傳
    `{"rows": [ExploreRow, ...], "built_at": float, "total_scanned": int}`。

    Task 4.1（D7）：`version == 3`（`ExploreRow` 尚無 `as_of`／`fills_coverage`
    兩欄的舊快照）不當成不相容直接丟棄——逐列補上 `as_of`（三鍵皆＝
    `built_at`，這份快照本身就是那一刻拍下的，沒有更精確的每欄位時間戳可用）
    與 `fills_coverage`（`backfilling`，尚未驗證完整性）後**當作 v4 載入**，
    不丟棄舊快照（部署當下不必等一輪全新背景建置才有資料）。其他版本
    （＜3 或介於 3 與 `EXPLORE_INDEX_VERSION` 之間、或未來版本）→ `None`，
    既有語意不變。

    Task 6.7（P6 reviewer W2）：v3 快照的 `eligibility` 欄位在來源檔裡根本
    不存在（`_row_from_dict` 落回 `ExploreRow` 類別預設值 `"eligible"`），
    沿用會讓遷移出來的舊列全部冒充合格——遷移完成後（含上方遮罩／
    `order_count_30d` 歸零）以 `cfg` 的預設門檻（`cfg` 省略 → `ExploreConfig()`
    模組預設值；`ExploreIndex.__init__` 呼叫本函式時傳自己的 `self._cfg`）
    重新 `classify()` 一次，寫回真實分類。`ExploreIndex.query()` 之後仍會依
    當次請求門檻重新分類（見該函式），這裡只是不讓快照落盤／剛載入那一刻的
    讀者看到失真的預設值。"""
    try:
        raw = Path(path).read_text()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.error("explore index 快照解析失敗，忽略（冷建）: %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if version not in (EXPLORE_INDEX_VERSION, 3):
        return None
    try:
        built_at = float(payload["built_at"])
        total_scanned = int(payload["total_scanned"])
        raw_rows = payload["rows"]
        if version == 3:
            # Task 6.5：v3 快照的 `order_count_30d` 是舊語意（`enrich_candidate`
            # 在成交完整時才會寫這欄，v3 時代沒有「本輪已觀測筆數下限」這個
            # 概念）——沿用舊值會讓 `classify` 把它當成「已滿足 min_fills」
            # 而誤判 eligible（主線程本機實測：886 筆舊值讓 96 個本應 pending
            # 的地址假合格）。歸零後與 `fills_coverage.state=="backfilling"`
            # 一致（未知，不是已知的 0，`classify` 的 fills_pending 分支會
            # 正確判定「未定」）。
            migrated_as_of = {"portfolio": built_at, "state": built_at, "fills": built_at}
            # <!-- 2026-09-21 複審 W1 -->：契約 A 規定 `fills_truncated ＝
            # coverage.state != "complete"`，遷移列 coverage 是 backfilling，
            # 旗標必須同步為 True（fallback 到舊旗標的消費者才不會把遮成 null
            # 的列當成「資料完整」）。
            raw_rows = [dict(r, as_of=migrated_as_of,
                            fills_coverage=dict(DEFAULT_FILLS_COVERAGE),
                            order_count_30d=0, fills_truncated=True)
                       for r in raw_rows]
        rows = [_row_from_dict(r) for r in raw_rows]
        if version == 3:
            # 讓快照本身（進了記憶體的 `ExploreRow` 集合）也符合契約 A——
            # `to_dict()` 序列化時會再遮一次（雙保險），這裡先做是為了
            # `sort_rows`／`classify` 等其他讀路徑不必個別記得呼叫遮罩。
            rows = [mask_incomplete_fills(r) for r in rows]
            # Task 6.7：遮罩之後才分類——`classify()` 依 `fills_coverage`／
            # 已遮罩過的 `order_count_30d` 等欄位判斷，順序不能反過來。
            eff_cfg = cfg if cfg is not None else ExploreConfig()
            min_live_days, min_fills, max_dd_pct, max_concentration_pct = (
                _effective_thresholds(eff_cfg, require_sample=True, max_dd_filter=True,
                                      exclude_concentrated=True))
            reclassified = []
            for r in rows:
                elig, reason = classify(r, eff_cfg, window=DEFAULT_WINDOW,
                                        min_live_days=min_live_days, min_fills=min_fills,
                                        max_dd_pct=max_dd_pct,
                                        max_concentration_pct=max_concentration_pct)
                reclassified.append(dataclasses.replace(r, eligibility=elig,
                                                        eligibility_reason=reason))
            rows = reclassified
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
                     fills: list[dict], ch_state: dict | None, *,
                     fills_truncated: bool = False,
                     as_of: dict[str, float | None] | None = None,
                     fills_coverage: dict | None = None) -> ExploreRow | None:
    """純函式：候選地址的三份原始 HL 回應 → `ExploreRow`，或 `None`（該列整筆
    跳過，見模組檔頭第 2 點）。損益／回撤／sparkline／成交統計的公式全部委派
    給 `spark.filet.trader_stats`（`window_stats`／`fills_stats`），本函式只
    負責組裝與 gating（工程原則 1：同一窗的三個數字出自同一次 `window_stats`
    呼叫，不混用）。

    `portfolio_raw`：`hl.portfolio(address)` 的原始回應，或 `None`（Task 4.1：
    `explore_publisher.compose_rows` 讀 `ExploreStore` 合成列時，該地址可能
    尚未被 scheduler enrich 過——`portfolio_raw is None` 不再整列跳過，而是
    `windows` 全部設為 `None`、`live_days` 設為 `None`（「分析待完成」，不是
    0——`qualify` 因 `live_days is None` 判定不合格，不會被誤判成不合格的
    0 天，也不會被誤判成合格）。
    `fills`：`hl.get_fills_raw_paged(address, start, end)` 的輸出（原始 HL
    `userFillsByTime` 形狀，含 `dir`/`oid`/`startPosition`/`closedPnl`——
    `trader_stats.fills_stats` 需要這些欄位，見 Task 3a）；空 list 是合法值
    （0 筆成交，照常算出 0），`fills_truncated`：同一次分頁呼叫的截斷旗標，
    原樣透傳進 `ExploreRow.fills_truncated`（D5）。
    `ch_state`：`hl.clearinghouse_state(address)` 的原始回應，或 `None`
    （Task 4.1：尚未 enrich 過）→ `account_bucket`／`exposure_dir`／
    `exposure_pct` 皆為 `None`（與「查過但算不出」的既有 `"—"`／`None` 語意
    分開，見 `ExploreRow.account_bucket` 欄位註記）。
    `as_of`／`fills_coverage`：Task 4.1，`compose_rows` 傳入的每欄位新鮮度／
    成交完整性描述，原樣透傳進 `ExploreRow`；`as_of` 省略（`None`）→ 沿用
    `ExploreRow` 的欄位預設值（空 dict）。`fills_coverage` 省略（`None`）
    **不是**「未知」——這代表呼叫端（既有直接呼叫本函式的呼叫端／測試）不參與
    `ExploreStore` 分頁追蹤，餵進來的 `fills` 本身就是一份完整清單，因此視為
    `"complete"`：成交衍生欄位（`coins`／`concentration_pct`／
    `closed_positions_30d`／`realized_pnl_30d_usd`／`close_win_rate_pct`）
    照常計算，不遮蔽（向下相容既有行為）。只有 `compose_rows`（正式資料流）
    **顯式**傳入非 `"complete"` 的 `fills_coverage`，才會觸發「未知≠0」遮蔽
    （見下方 P6 段的實作）——`ExploreRow.fills_coverage` 欄位本身的類別預設值
    （`DEFAULT_FILLS_COVERAGE`，`"backfilling"`）只用於 `_row_from_dict` 這類
    繞過本函式直接建構 `ExploreRow` 的路徑，不是本函式省略參數時的行為。

    跳過整列的情況（讀不到就跳過，不編數字；僅在 `portfolio_raw` 非 `None`
    時適用——見上）：`month` 或 `allTime` 視窗缺席／形狀不符／不足兩個取樣點
    （`window_stats` 回傳 `None`）——不再檢查淨值首點是否為正（2026-09-04：
    報酬改用損益金額，不需要正分母，見 D2）；day／week 是 best-effort，
    缺席只讓 `windows["day"/"week"]` 為 `None`，不連坐整列（見模組檔頭
    「R4-3」節）。
    `tags` 留空（`()`）——集中度與低回撤兩個 tag 需要「這一批候選池」的相對
    資訊（門檻常數／同批分位數），由呼叫端（`ExplorePublisher.compose_rows`）
    合成完整批後再用 `_apply_tags` 統一補上，不在單一地址的純函式裡決定。
    """
    # D14（2026-08-30 主線程裁決）：`tags`／`exposure_dir` 一律用 locale 中性代碼
    # （"low_drawdown"/"concentrated"、"long"/"short"），不回傳中文顯示字串——
    # 顯示文案改由前端 `explore/page.tsx` 對映 `copy.ts`（見 `_exposure`／
    # `_apply_tags` 的實際賦值）。
    if portfolio_raw is None:
        windows: dict[str, WindowStats | None] = {k: None for k in WINDOW_KEYS}
        live_days: int | None = None
    else:
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

        windows = {
            "day": window_stats(portfolio_raw, WINDOW_TO_PERIOD["day"]),
            "week": window_stats(portfolio_raw, WINDOW_TO_PERIOD["week"]),
            "month": month,
            "allTime": all_time_stats,
        }

    fs: FillsStats = fills_stats(fills or [], truncated=fills_truncated)
    if ch_state is None:
        bucket: str | None = None
        exp_dir: str | None = None
        exp_pct: float | None = None
    else:
        account_value = _account_value(ch_state)
        bucket = _account_bucket(account_value)
        positions = _parse_positions(ch_state)
        exp_dir, exp_pct = _exposure(positions)

    # P6（D13，2026-09-20）：呼叫端省略 `fills_coverage`（既有直接呼叫
    # `enrich_candidate` 的呼叫端／測試，不參與 `ExploreStore` 分頁追蹤——不是
    # 「未知」，是這次呼叫本身就餵了一份完整的 `fills`）→ 視為 `"complete"`，
    # 不遮蔽成交欄位（向下相容既有行為）。只有**顯式**傳入非 `"complete"` 的
    # `fills_coverage`（`explore_publisher.compose_rows` 的正式資料流）才會
    # 觸發下面的「未知≠0」遮蔽——`close_win_rate_pct`／`coins`／
    # `concentration_pct`／`closed_positions_30d`／`realized_pnl_30d_usd` 全部
    # 存 `None`（`[]` for coins），`order_count_30d` 仍保留已觀測筆數下限。
    effective_coverage = fills_coverage if fills_coverage is not None else {
        "state": "complete", "observed_from": None, "observed_to": None, "reason": None,
        "synced_through": None, "last_success_at": None,
        "window_start": None, "window_end": None, "params_fp": None}
    fills_complete = effective_coverage.get("state") == "complete"
    if fills_complete:
        coins = fs.coins
        closed_positions_30d: int | None = fs.closed_positions
        realized_pnl_30d_usd: float | None = fs.realized_pnl_usd
        close_win_rate_pct: float | None = fs.win_rate_pct
        concentration_pct: float | None = fs.concentration_pct
    else:
        coins = ()
        closed_positions_30d = None
        realized_pnl_30d_usd = None
        close_win_rate_pct = None
        concentration_pct = None

    extra_fields: dict = {}
    if as_of is not None:
        extra_fields["as_of"] = as_of
    if fills_coverage is not None:
        extra_fields["fills_coverage"] = fills_coverage
    else:
        # 同上：省略時把「complete」寫回 `ExploreRow.fills_coverage`本身
        # （不留 `DEFAULT_FILLS_COVERAGE` 的 `"backfilling"` 類別預設值）——否則
        # `classify()` 會誤判這批本來資料齊全的列成「成交未知」。
        extra_fields["fills_coverage"] = dict(effective_coverage)

    return ExploreRow(
        address=address,
        display_name=display_name,
        label=display_name if display_name else _abbreviate_address(address),
        coins=coins,
        account_bucket=bucket,
        windows=windows,
        live_days=live_days,
        order_count_30d=fs.order_count,
        closed_positions_30d=closed_positions_30d,
        realized_pnl_30d_usd=realized_pnl_30d_usd,
        close_win_rate_pct=close_win_rate_pct,
        concentration_pct=concentration_pct,
        exposure_dir=exp_dir,
        exposure_pct=exp_pct,
        tags=(),
        fills_truncated=fs.truncated,
        **extra_fields,
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
    # Task 4.1：`windows["month"]` 本身也可能是 `None`（`portfolio_raw` 缺席的
    # 「分析待完成」列，見 `enrich_candidate`）——同樣視為無證據，不掛 tag。
    dds = sorted(abs(Decimal(str(r.windows["month"].max_dd_pct)))
                for r in rows
                if r.windows["month"] is not None and r.windows["month"].max_dd_pct is not None)
    threshold = dds[max(0, -(-len(dds) // 4) - 1)] if dds else None
    out = []
    for r in rows:
        tags = []
        month_stats = r.windows["month"]
        month_dd = month_stats.max_dd_pct if month_stats is not None else None
        if (threshold is not None and month_dd is not None
                and abs(Decimal(str(month_dd))) <= threshold):
            tags.append("low_drawdown")
        # P6（D14）：集中度只在成交完整（`fills_coverage.state == "complete"`）
        # 才判——`enrich_candidate` 已在非 complete 時把 `concentration_pct`
        # 遮成 `None`，這裡的 coverage 檢查是防禦性的（例如直接建構
        # `ExploreRow` 略過 `enrich_candidate` 的測試/呼叫端）。
        if (r.fills_coverage.get("state") == "complete"
                and r.concentration_pct is not None
                and Decimal(str(r.concentration_pct)) > cfg.max_concentration_pct):
            tags.append("concentrated")
        out.append(dataclasses.replace(r, tags=tuple(tags)))
    return out


def classify(row: ExploreRow, cfg: ExploreConfig, *, window: str = DEFAULT_WINDOW,
            min_live_days: int, min_fills: int,
            max_dd_pct: float | None, max_concentration_pct: float | None
            ) -> tuple[str, str | None]:
    """P6（D13，2026-09-20）：三態資格判定，取代 `qualify` 的布林。`cfg` 目前不
    參與門檻計算（四個門檻改由呼叫端明確傳入——`ExploreIndex.query()` 用當次
    請求 clamp 過的值；`qualify()` 依 toggle 決定要不要覆寫成「不過濾」值，
    見該函式），保留只是與既有簽名慣例（`qualify(row, cfg, ...)`）一致。

    回傳 `(eligibility, reason)`，`eligibility ∈ {"eligible","pending","ineligible"}`：

    1. **已知條件先判**（任一命中 → 立刻 `("ineligible", reason)`，reason 值域
       `live_days`／`max_dd`／`min_fills`／`concentration`）：
       - `live_days is not None and live_days < min_live_days` → `"live_days"`。
       - 所選窗 `max_dd_pct` 存在且 `abs(...) > max_dd_pct` 門檻（`max_dd_pct`
         參數為 `None` → 整個維度不過濾，見下）→ `"max_dd"`。
       - `fills_coverage.state == "complete"` 時：`order_count_30d < min_fills`
         → `"min_fills"`；`concentration_pct` 存在且 `> max_concentration_pct`
         （參數為 `None` → 不過濾）→ `"concentration"`。
    2. **`fills_coverage.state != "complete"`**（成交統計未知，`enrich_candidate`
       已把 `concentration_pct` 等欄位遮成 `None`，見該函式 P6 段）：
       `order_count_30d >= min_fills`（下限）視為該條已滿足；否則「未定」。
       集中度視為「未定」，除非 `max_concentration_pct >= 100`（等於不過濾，
       此時視為已滿足）。
    3. 無 ineligible 但有「未定」（`live_days is None`＝`portfolio_missing`，
       或上一步的成交「未定」＝`fills_unknown`）→ `("pending", reason)`
       （`portfolio_missing` 優先於 `fills_unknown`）。
    4. 以上皆無 → `("eligible", None)`。

    `max_dd_pct`／`max_concentration_pct` 為 `None`：供 `qualify()` 的
    `max_dd_filter=False`／`exclude_concentrated=False` 逃生門使用，代表整個
    維度不參與判定（永遠視為已滿足，不產生 pending 也不產生 ineligible）；
    `ExploreIndex.query()` 的正式請求路徑一律傳明確數值。
    """
    portfolio_missing = row.live_days is None
    if not portfolio_missing and row.live_days < min_live_days:
        return "ineligible", "live_days"

    if max_dd_pct is not None:
        stats = row.windows.get(window)
        dd = stats.max_dd_pct if stats is not None else None
        if dd is not None and abs(Decimal(str(dd))) > Decimal(str(max_dd_pct)):
            return "ineligible", "max_dd"

    fills_complete = (row.fills_coverage or {}).get("state") == "complete"
    fills_pending = False
    if fills_complete:
        if row.order_count_30d < min_fills:
            return "ineligible", "min_fills"
        if (max_concentration_pct is not None and row.concentration_pct is not None
                and Decimal(str(row.concentration_pct)) > Decimal(str(max_concentration_pct))):
            return "ineligible", "concentration"
    else:
        if row.order_count_30d < min_fills:
            fills_pending = True
        if max_concentration_pct is not None and max_concentration_pct < 100:
            fills_pending = True

    if portfolio_missing:
        return "pending", "portfolio_missing"
    if fills_pending:
        return "pending", "fills_unknown"
    return "eligible", None


def _effective_thresholds(cfg: ExploreConfig, *, require_sample: bool,
                          max_dd_filter: bool, exclude_concentrated: bool
                          ) -> tuple[int, int, float | None, float]:
    """`qualify()`／`ExploreIndex.query()` 共用：三個舊版布林 chip → `classify()`
    的四個門檻參數（`None`＝該維度不過濾，見 `classify` 檔頭）。"""
    return (
        cfg.min_trading_days if require_sample else 0,
        cfg.min_fills if require_sample else 0,
        float(cfg.max_drawdown_pct) if max_dd_filter else None,
        float(cfg.max_concentration_pct) if exclude_concentrated else 100.0,
    )


def qualify(row: ExploreRow, cfg: ExploreConfig, *, window: str = DEFAULT_WINDOW,
           require_sample: bool = True, max_dd_filter: bool = True,
           exclude_concentrated: bool = True) -> bool:
    """資格過濾（R2-01，全在後端）——**薄包裝**：`classify(...)[0] == "eligible"`
    （P6，D13）。三個布林是內部/測試逃生門（R4-3：公開端點已改成四個自由數值
    門檻，不再對外送布林 chip，見模組檔頭「R4-3」節）；`window` 決定回撤門檻
    看**哪一窗**的 `max_dd_pct`。既有呼叫端／測試的語意不變：`True` ⇔
    `classify` 判定 `"eligible"`（`"pending"`／`"ineligible"` 皆為 `False`——
    D13：`qualify(None)` 不得回傳合格）。邊界值與 None-容忍慣例見 `classify`
    docstring，本函式不重複定義。
    """
    min_live_days, min_fills, max_dd_pct, max_concentration_pct = _effective_thresholds(
        cfg, require_sample=require_sample, max_dd_filter=max_dd_filter,
        exclude_concentrated=exclude_concentrated)
    eligibility, _ = classify(row, cfg, window=window, min_live_days=min_live_days,
                              min_fills=min_fills, max_dd_pct=max_dd_pct,
                              max_concentration_pct=max_concentration_pct)
    return eligibility == "eligible"


def sort_value(row: ExploreRow, *, window: str = DEFAULT_WINDOW,
              sort: str = DEFAULT_SORT) -> Decimal | None:
    """D13（2026-09-05）：排序取值。`pnl`／`max_dd` 是窗類欄位——缺該窗（day/week
    best-effort 缺席）退回 `"month"`（`enrich_candidate` 保證恆非 `None`）；
    `live_days`／`win_rate` 與 `window` 無關。值本身為 `None`（`max_dd_pct`
    算不出、或該帳戶沒有已歸零的成交生命週期）時原樣回傳 `None`——由
    `sort_rows` 決定「None 一律排最後」，這裡不做排序決策，只負責誠實取值。
    P6：`windows["month"]` 本身也可能是 `None`（`portfolio_missing` 的
    `pending` 列，見 `enrich_candidate`——這類列現在會被 `sort_rows` 分到
    pending 組一起排序，不再保證退回鍵恆非 `None`），一併回傳 `None`。"""
    if sort in ("pnl", "max_dd"):
        stats = row.windows.get(window) or row.windows["month"]
        v = None if stats is None else (stats.pnl_usd if sort == "pnl" else stats.max_dd_pct)
    elif sort == "live_days":
        v = row.live_days
    elif sort == "win_rate":
        v = row.close_win_rate_pct
    else:
        raise ValueError(f"unknown sort {sort!r}")
    return None if v is None else Decimal(str(v))


def _sort_group(rows: list[ExploreRow], *, window: str, sort: str, order: str) -> list[ExploreRow]:
    """單一資格分組內的排序：`sort_value` 缺值（`None`）不論 `order` 一律排最後；
    次排序鍵固定 `address` 升冪、與 `order` 無關（P6 契約 B）——先用 address
    做一次穩定排序墊底，再用主鍵排序，Python `sort` 的穩定性讓同值列維持
    address 升冪（標準的「先次鍵、後主鍵」技巧，不必手寫 tuple 比較函式）。"""
    # Task 6.5：排序鍵取值前先遮罩（`sort="win_rate"` 讀 `close_win_rate_pct`）
    # ——coverage 未完整的列即使欄位本身還沒被序列化遮蔽（例如尚未經
    # `to_dict()`／`mask_incomplete_fills` 的呼叫端直接建構的列），排序語意
    # 仍要視為「未知」（組尾），不得讓未遮蔽的舊值影響名次；回傳的仍是原始
    # `r`（遮罩只影響這裡的鍵計算，不影響輸出列本身，見 `to_dict`）。
    keyed = [(sort_value(mask_incomplete_fills(r), window=window, sort=sort), r) for r in rows]
    present = [(k, r) for k, r in keyed if k is not None]
    missing = [r for k, r in keyed if k is None]
    present.sort(key=lambda kr: kr[1].address)
    present.sort(key=lambda kr: kr[0], reverse=(order == "desc"))
    missing.sort(key=lambda r: r.address)
    return [r for _, r in present] + missing


def sort_rows(rows: list[ExploreRow], *, window: str = DEFAULT_WINDOW,
             sort: str = DEFAULT_SORT, order: str = DEFAULT_ORDER) -> list[ExploreRow]:
    """D12（2026-09-05）；P6（D13，2026-09-20）擴充分組：`ExploreIndex.query` 的
    排序＋分組責任——分頁在後端切，排序也必須在後端做（前端只拿得到當頁列，
    排不動全體）。**先依 `row.eligibility` 分組**（`"eligible"` 全部在前、
    `"pending"` 全部在後，`"ineligible"` 整批不列——呼叫端須已對每列跑過
    `classify()` 並用 `dataclasses.replace` 寫回 `eligibility`，見
    `ExploreIndex.query`），**組內**再依 `sort`／`order`／`address` 排序
    （見 `_sort_group`）。`None` 值（例如 `max_dd_pct` 算不出、或沒有已歸零的
    成交生命週期）不論 `order` 一律排該組最後。"""
    eligible = [r for r in rows if r.eligibility == "eligible"]
    pending = [r for r in rows if r.eligibility == "pending"]
    return (_sort_group(eligible, window=window, sort=sort, order=order)
           + _sort_group(pending, window=window, sort=sort, order=order))


def sort_key(row: ExploreRow, *, window: str = DEFAULT_WINDOW) -> Decimal:
    """D2（2026-09-04）舊介面，既有測試沿用：所選窗 `pnl_usd` 降冪，值恆非
    `None`（`enrich_candidate` 保證 month/allTime 兩窗的 `pnl_usd` 存在）。
    2026-09-05（Task 11）：實際排序責任已移交 `sort_rows`（`sort_value(...,
    sort="pnl")` 的等價寫法），本函式保留給呼叫端只需要單一鍵值比較的場合。"""
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
    """`GET /api/public/explore` 的資料索引——**純讀路徑**（Task 3.4／D6）：本類別
    不再打任何上游 HL 呼叫，也不再自己建置。資料的取得與更新全部交給
    `ExploreScheduler`（背景逐 job 更新 `ExploreStore`）與 `ExplorePublisher`
    （定期把 store 合成一批 `ExploreRow`、原子換版）；`ExploreIndex` 只保存
    `set_published()` 換上來的最新一版，`query()` 只讀這一份。

    `cfg`：`qualify()`／`sort_rows()` 用的門檻與分頁大小。
    `snapshot_path`：I-17 磁碟快照路徑，`None`＝不落盤（沿用純記憶體既有語意，
    多數測試直接構造 `ExploreIndex` 時不傳，行為不變）；有設時建構子會嘗試
    `load_snapshot` 立即灌一份舊資料（見模組檔頭「I-17」節），`ExplorePublisher.
    maybe_publish` 每次成功換版後會寫回一份新的（見 `explore_publisher.py`）。
    """

    def __init__(self, *, cfg: ExploreConfig, now_fn: Callable[[], float],
                snapshot_path: str | None = None):
        self._cfg = cfg
        self._now_fn = now_fn
        self._snapshot_path = snapshot_path

        self._lock = threading.Lock()
        self._rows: list[ExploreRow] | None = None   # 目前對外服務的一版
        # R4-3：`self._rows` 是用哪個 `EXPLORE_INDEX_VERSION` 建的（見模組檔頭
        # 「index 結構版本」節）；`None` 表示尚未建置過，與版本不相容視為同一種
        # 「沒有可用快照」——`query()` 據此判斷是否回 `initializing: True`。
        self._rows_version: int | None = None
        self._built_at: float | None = None
        self._total_scanned = 0
        # Task 4.1：`ExplorePublisher.maybe_publish` 換版時一併寫入的批次統計
        # （`candidates`／`with_portfolio`／`coverage_counts`／`as_of_oldest`，
        # 見 `explore_publisher.compose_rows`）；`query()` 的 `coverage_counts`
        # 讀這裡，無則 `{}`（尚未經 publisher 換過版，例如剛從舊版磁碟快照
        # 載入、還沒有 meta 可用——磁碟快照本身不落 meta，見模組檔頭「I-17」節）。
        self._meta: dict = {}

        # I-17：啟動時嘗試從磁碟快照灌一份舊資料，讓「程序重啟後第一個請求」
        # 不必等一輪背景建置（數分鐘）才有資料可查（見模組檔頭「I-17」節）。
        # 版本不符／檔不存在／檔壞 → `load_snapshot` 回 `None`，維持既有冷建
        # 語意（`self._rows` 留 `None`），不拋例外、不阻塞建構子。
        if self._snapshot_path is not None:
            snap = load_snapshot(self._snapshot_path, cfg=self._cfg)
            if snap is not None:
                self._rows = snap["rows"]
                self._rows_version = EXPLORE_INDEX_VERSION
                self._built_at = snap["built_at"]
                self._total_scanned = snap["total_scanned"]

    def status(self) -> dict:
        """`/api/ops/health` 揭露用（reviewer W3，2026-09-20；Task 3.4 起 `building`
        鍵已移除——本類別不再自己建置，`app.state.explore_scheduler`／
        `explore_publisher` 的 `status()` 才有進行中/最近一次的動態訊號）。
        ops 需要能看到「現在服務的是哪一版、建於何時」，否則一份已經凍結數天的
        榜單會被誤讀成「持續在更新」。"""
        with self._lock:
            return {"rows": None if self._rows is None else len(self._rows),
                    "built_at": self._built_at, "version": self._rows_version}

    def set_published(self, rows: list[ExploreRow], meta: dict) -> None:
        """Task 4.1：`ExplorePublisher.maybe_publish` 的原子換版入口——本類別
        唯一寫 `self._rows` 三件組的地方。持鎖設 `_rows`、
        `_rows_version`（＝目前的 `EXPLORE_INDEX_VERSION`，發布出來的列一律是
        最新結構）、`_built_at`（＝`meta["published_at"]`）、`_total_scanned`
        （＝`meta["candidates"]`）、`_meta`（原樣保留，供 `query()` 的
        `coverage_counts` 用）。呼叫端（`ExplorePublisher`）負責先組好
        `rows`/`meta` 再呼叫本方法——本方法本身不做任何 compose 或 IO，失敗
        與否全在呼叫端決定（compose 失敗就不呼叫本方法，見
        `explore_publisher.py`）。"""
        with self._lock:
            self._rows = rows
            self._rows_version = EXPLORE_INDEX_VERSION
            self._built_at = meta["published_at"]
            self._total_scanned = meta["candidates"]
            self._meta = meta

    def query(self, *, page: int = 1, window: str = DEFAULT_WINDOW,
             min_live_days: int | None = None, min_fills: int | None = None,
             max_dd_pct: float | None = None, max_concentration_pct: float | None = None,
             require_sample: bool = True, max_dd_filter: bool = True,
             exclude_concentrated: bool = True,
             sort: str = DEFAULT_SORT, order: str = DEFAULT_ORDER,
             eligibility: str = "all") -> dict:
        """讀路徑：**只讀本地已發布版本，永不觸發上游**（2026-09-20 spec §3／§9.2：
        Explore 頁面刷新不得發 HL info、不得開 rebuild——2026-09-19 事故：請求觸發
        的 300 池重建把同 IP 額度燒到 429，dashboard／onboard 一起失效）。上游更新
        全部交給 `ExploreScheduler`（背景逐 job 更新 `ExploreStore`）與
        `ExplorePublisher`（定期 `set_published` 原子換版，見 Task 3.4／4.1）；
        本函式**不再**自己觸發任何建置。回傳形狀見 `app.py` 端點層文件字串：
        `{rows, page, page_size, total_qualified, total_scanned, pool, updated_at,
        building}`（`pool`：I-17，鏡射 `total_scanned`——這一輪實際掃描的候選數，
        前端榜首常駐提示句「自 {pool} 個候選帳戶中列出…」用這個數字，不寫死候選池
        上限常數，見 `explore/page.tsx`）。

        從未成功發布過，或現有版本的結構版本已不相容（`self._rows is None`
        或 `self._rows_version != EXPLORE_INDEX_VERSION`，R4-3，見模組檔頭
        「index 結構版本」節）→ `building`／`initializing: True`、空 rows、
        計數皆 0、`updated_at: None`（前端 R2·C 態二）——版本不相容的快照結構
        上不能安全地拿去 `qualify`/`sort_key`/`to_dict`（欄位形狀已經換過），
        視同「沒有可用快照」，等待 `ExplorePublisher` 下一次成功換版。

        `window`：所選期間（`WINDOW_KEYS` 之一），決定 `qualify` 的回撤過濾
        看哪一窗、`sort_rows` 用哪一窗排序（R4-3；不影響候選池——候選池由
        `ExploreScheduler` 固定用 stats-data month 窗 roi 選出，見模組檔頭
        「R4-3」節「誠實揭露」段）。
        `min_live_days`／`min_fills`／`max_dd_pct`／`max_concentration_pct`：
        R4-3 自由門檻（`None`＝沿用 `self._cfg` 的預設值，供內部/測試呼叫端在
        不關心門檻時省略；`app.py` 端點層一律夾取後傳入明確數值，見
        `clamp_explore_params`）。
        `sort`／`order`：Task 11（D12／D13），排序在資格過濾**之後**、分頁
        **之前**做（對合格全集排序，不是只排當頁）；回傳 dict 原樣 echo 這兩個
        值（`"sort"`／`"order"` 鍵）供前端表頭箭頭顯示對照，見 `sort_rows`。

        `eligibility`：P6（D13）契約 B——`"all"`（預設）→ rows＝eligible＋pending
        （`sort_rows` 分組順序，eligible 在前）；`"eligible"` → 只 eligible。
        `"ineligible"` 一律不進 `rows`（不論本參數為何）。非法值由呼叫端
        （`app.py` 端點層）驗證，本函式不驗證，原樣 echo 回傳（`"eligibility"`
        鍵）。回應另加 `total_pending`／`total_ineligible`（`total_qualified`
        維持＝eligible 數，前端相容）。

        三態分類（`classify()`）在**每次查詢時**用當次生效門檻重新計算——
        `ExploreRow.eligibility`／`eligibility_reason` 欄位本身只是預設值／
        快照回填的佔位（見 `ExploreRow` 檔頭），不是發布時就凍結的最終結果，
        因為門檻本身可依請求覆寫（`min_live_days` 等四個參數）。唯一例外：
        `eligibility_reason == "enrich_error"` 的列（`explore_publisher.
        compose_rows` 單一地址 enrich 失敗時直接構造，見該函式）——這類列沒有
        可供 `classify()` 判斷的真實資料，維持原樣（`pending`／`enrich_error`），
        不被重新分類覆寫。
        """
        with self._lock:
            rows = self._rows
            rows_version = self._rows_version
            built_at = self._built_at
            total_scanned = self._total_scanned
            meta = self._meta
        if rows is None or rows_version != EXPLORE_INDEX_VERSION:
            # Task 4.1：`initializing`＝從未有可服務版本；`building` 保留同義
            # （前端相容，見類別檔頭「Task 4.1」段）。
            return {"rows": [], "page": page, "page_size": self._cfg.page_size,
                   "total_qualified": 0, "total_pending": 0, "total_ineligible": 0,
                   "total_scanned": 0, "pool": 0,
                   "updated_at": None, "building": True, "eligibility": eligibility,
                   "published_at": None, "initializing": True, "coverage_counts": {}}
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
        eff_min_live_days, eff_min_fills, eff_max_dd_pct, eff_max_concentration_pct = (
            _effective_thresholds(cfg, require_sample=require_sample, max_dd_filter=max_dd_filter,
                                  exclude_concentrated=exclude_concentrated))
        classified: list[ExploreRow] = []
        for r in rows:
            if r.eligibility_reason == "enrich_error":
                classified.append(dataclasses.replace(r, eligibility="pending"))
                continue
            elig, reason = classify(r, cfg, window=window, min_live_days=eff_min_live_days,
                                    min_fills=eff_min_fills, max_dd_pct=eff_max_dd_pct,
                                    max_concentration_pct=eff_max_concentration_pct)
            classified.append(dataclasses.replace(r, eligibility=elig, eligibility_reason=reason))
        eligible_rows = [r for r in classified if r.eligibility == "eligible"]
        pending_rows = [r for r in classified if r.eligibility == "pending"]
        ineligible_count = sum(1 for r in classified if r.eligibility == "ineligible")
        visible_rows = eligible_rows if eligibility == "eligible" else eligible_rows + pending_rows
        visible_rows = sort_rows(visible_rows, window=window, sort=sort, order=order)
        page_rows = paginate(visible_rows, page, self._cfg.page_size)
        return {"rows": [r.to_dict() for r in page_rows], "page": page,
               "page_size": self._cfg.page_size,
               "total_qualified": len(eligible_rows), "total_pending": len(pending_rows),
               "total_ineligible": ineligible_count, "total_scanned": total_scanned,
               "pool": total_scanned,
               "updated_at": int(built_at) if built_at is not None else None,
               "building": False, "sort": sort, "order": order, "eligibility": eligibility,
               # Task 4.1（spec §9.2）：`published_at`（原始 epoch 秒，未經
               # `int()` 截斷，供 `explore_publisher` 與 `query()` 呼叫端做
               # 精確比較）、`initializing: False`（已有可服務版本）、
               # `coverage_counts`（`ExplorePublisher.maybe_publish` 換版時
               # 寫入的 meta，見 `set_published`；沒有 meta 可用 → `{}`）。
               "published_at": built_at, "initializing": False,
               "coverage_counts": meta.get("coverage_counts", {})}
