# Filet／HL Leaderboard 重構施工規格

日期：2026-09-19（落檔 2026-09-20，原文由使用者提供，內容未改動）
用途：交給 Claude Code，在既有 repository 中完成 Explore leaderboard 的限流、更新與資料持久化重構。
狀態：設計與驗收規格；尚未檢視實際 repository，也未實作或部署。
對應 plan：`docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md`

## 0. 給 Claude Code 的工作指令

請依本文完成必要的程式修改、資料遷移與測試，不要只回覆架構建議。先讀取 repository 的 CLAUDE.md／AGENTS.md、現有 Explore 實作及共用 HL client，再依實際架構落地。

本文中的模組名稱、資料表及設定鍵是建議介面，沿用專案既有命名與基礎設施即可。不要假設有 Redis、PostgreSQL、多 worker 或特定 framework；先盤點後選最小可行方案。本文預算與更新頻率是起始配置，不是 HL 官方承諾，也不是必須準時完成的 SLA。

核心目標：排行榜讀本地資料；背景分欄位更新；同出口 IP 共用權重預算；成交分析可續跑且揭露完整性。優先保護交易引擎與 dashboard，不能讓 Explore 使用 429 當節流器。

## 0.1 最新範圍決定：直接完整重構

使用者已確認目前沒有外部用戶，因此不需要交付「長 TTL＋落盤＋單頁 fills」的過渡最小改版。可在不影響現行少數跟單用戶的情況下，直接完成本文的完整架構：共享權重准入、持久化分欄位資料、獨立排程、去重工作、可續跑增量成交與漸進發布。

施工階段代表依賴順序，不代表每階段都需要單獨上線。先停用舊 rebuild 以保護同 IP 的交易引擎／dashboard，再一次完成必要程式與測試；不要只停留在止血版就宣布工作完成。

目前無外部用戶，不需要為過渡產品體驗打造兩套長期並行機制。既有快照仍可保留作回退資料；現有前端與內部呼叫端需要的 API 相容性仍要檢查。沒有用戶不代表沒有引擎／其他服務流量，也不代表可以繞過全域限流。

成交一次只處理一頁是 worker 的公平排程單位，並非總共只取得一頁。持久化 continuation 後必須依預算繼續同步；受到公開 API 歷史留存限制時如實標記 partial／unknown，不以截斷統計替代完整 30 天績效。

不採用 600／分鐘作為未經全域協調的 Explore 預算。起始值沿用全域 900、Explore 300，待掌握全部同出口流量後才調整。

## 1. 已知現況與問題

下列是使用者提供的現況，施工前請對照程式確認，不要當成已完成的 code audit。

- `GET /api/public/explore` 的 `query()` 先回舊資料，若 `built_at` 超過 10 分鐘則開 background thread 執行 `build_sync`。目前 single-flight 的跨 process 範圍尚待確認。
- 候選來自 stats-data，按月 ROI 取前 300 名，排除 Filet 自營 leader。
- 每個地址序列抓取 `portfolio`、30 天 `userFillsByTime`（最多三頁）及 `clearinghouseState`；請求間隔 0.7 秒。
- 429 以 2／8／30 秒重試，可能整輪持續重試並干擾同 IP 的其他服務。
- 全部地址完成才原子換版，寫入 `/var/lib/filet-api/explore_index.json`。快照只有結果，沒有每地址資料及進度。
- 記憶體 LRU 容量 256，小於 300 人掃描池；TTL 30 分鐘又短於約 98 分鐘的建置時間，容易持續冷抓。

成本基準：三頁成交全滿時，每地址 `20 + 3 × 120 + 2 = 382` 權重；300 人共 114,600。在完全獨占 1,200／分鐘額度下，理論至少 95.5 分鐘。98 分鐘的實際耗時包含網路、sleep、重試與其他服務競爭，不能直接當成實際消耗權重的量測值。

## 2. 開工前盤點

先找出以下項目，將結果簡短記錄於施工摘要；不要因為路徑與本文不同就重建整套服務。

1. Explore route、`query()`、`build_sync`、enrich cache、snapshot reader／writer 與所有呼叫入口。
2. 所有打向 HL 的 REST client、底層自動重試，以及交易引擎、dashboard、排程是否共用同一個出口 IP／NAT。
3. process／container／worker 數量、現有 scheduler、資料庫、Redis 與可用的跨 process 鎖。
4. 排行榜與詳情頁實際使用哪些欄位；哪些排序／篩選／評分依賴完整成交歷史。
5. 是否涵蓋 HIP-3 dex、spot、subaccount；現有 ROI、PnL、勝率與交易次數的定義。

保留既有月 ROI 選池、自營 leader 排除及 API 相容性。需要調整評分語義時，明列差異，不要順便重寫交易策略或改變 leader 選擇規則。

## 3. 必須成立的系統不變條件

- 刷新 Explore 頁面不會直接發送 HL info 請求，也不會開啟整輪 rebuild。
- 任一同出口 IP 的受控 REST 請求，發送前都要取得共享權重額度；重試與分頁沒有例外。
- Explore 有獨立上限，且必須同時通過全域上限；不能占用其他服務的保留空間。
- 快取到期表示需要更新，不表示刪除最後成功資料。
- 某個地址／endpoint 失敗，不阻擋其他結果發布，不清空整份排行。
- 成功提交的頁面和進度可在重啟後沿用；失敗或未完成工作不能錯誤推進完整性游標。
- 缺失、舊資料、截斷及完整資料是不同狀態；缺失不等於零，也不能冒充新鮮或完整。

## 4. 目標資料流與責任

讀取流程：`GET Explore → 本地已發布版本 → 回傳結果與資料時間`。

更新流程：`獨立 scheduler → 去重工作佇列 → 權重准入 → HL client → 持久化資料／進度 → publisher 原子發布`。

| 元件 | 責任 |
|---|---|
| HL client／gateway | 統一 endpoint 權重、請求發送、錯誤分類與共享限流接入。 |
| Rate limiter | 原子檢查全域與 Explore 子預算，不負責業務資料計算。 |
| Scheduler | 依更新期限與優先級入列，不執行長時間網路工作。 |
| Worker | 一次處理一個 endpoint 或一頁成交，持久化後結束／續排。 |
| Repository | 保存候選、欄位快取、成交、coverage、工作與 lease。 |
| Publisher | 從本地資料產生相容結果，保留來源時間與完整性。 |

優先沿用現有 client／repository 抽象，避免讓 route 直接管理 thread、sleep、HTTP 或同步游標。

## 5. 同 IP 的共用權重限流

### 5.1 起始配置

| 設定 | 初始值／規則 |
|---|---|
| 全域 REST 預算 | 任意滾動 60 秒最多 900 權重。 |
| Explore 子預算 | 任意滾動 60 秒最多 300，另受全域可用量限制。 |
| 優先順序 | 交易／撤單 → 引擎必要查詢 → dashboard → Explore。 |
| `portfolio` | 預留 20。 |
| `clearinghouseState` | 每次預留 2；查多個 dex 分別計費。 |
| `userFillsByTime` | 每頁預留 120，第一版不依回傳筆數退款。 |
| Explore 網路併發 | 起始 1；未證明需要前不要增加。 |

使用原子加權 rolling-window，或能證明具有同等限制的演算法。不要把「900 tokens 容量＋每分鐘補 900」的普通 token bucket 直接當成任意 60 秒最多 900；突發容量可能使滑動區間超標。

多 process／服務可用 Redis 原子操作；單 process 方案只有在確認全部相關呼叫均由該 process 控制時才成立。計數 key 必須按實際出口 IP／egress group 隔離，不是按地址或登入者隔離。

取得額度後立即進入 transport，不要先扣額度再放到另一條長佇列。共享時間來源、reservation 與過期策略需避免跨 worker 時鐘差及長時間 inflight 造成低估；可保守地讓 inflight reservation 持續占額，完成後保留至少一個視窗。HTTP 必須設定有限 timeout。

### 5.2 重試、熔斷與既有服務

- 每次 HTTP 嘗試，包括底層 retry，都須經過 limiter。優先停用 client 內不可見的自動重試，交由可觀測的工作層處理。
- Explore 遇到 429，設定共享 `explore_paused_until`，至少暫停 60 秒；若有可解析的 `Retry-After`，遵守較長者，另加小幅正向 jitter。
- 不在 worker 中 sleep 60 秒，寫入 `next_attempt_at` 後釋放工作與 lease。連續 429 可逐步延長至可配置上限，半開時只允許一個探測工作。
- 同出口的其他服務觀察到 429，也應通知 Explore 暫停。不要因此停掉必要的交易／撤單處理或改動其既有安全策略。
- 5xx、timeout 使用有上限且帶 jitter 的延後重試；非重試型 4xx 記錄並隔離該工作，不無限重試。
- limiter 不可用時，Explore 不得直接繞過發送；仍提供本地資料。交易引擎的 limiter 故障策略要與原有安全設計整合，不能直接套用 Explore 的停發政策。

若無法接入同 IP 的所有高流量 client，必須如實標明無法保證全域上限。可以先完成停用 rebuild 與快照讀取的止血版本，待共用額度接好後再恢復 Explore 上游更新。

## 6. 分層更新與容量規劃

| 資料 | 更新目標 | 理論穩態平均 info 權重 |
|---|---|---:|
| stats-data | 每 10 分鐘抓一次候選及可直接使用的基本欄位。 | 依既有來源不計入 info；仍獨立快取與退避。 |
| 300 人 portfolio | 每地址每 60 分鐘。 | 100／分鐘。 |
| 300 人 clearinghouseState | 每地址每 15 分鐘，單一 dex。 | 40／分鐘。 |
| 成交增量 | 優先前 50 名及已接納的詳情需求，依成本與預算續排。 | 使用剩餘 Explore 額度。 |

每地址初次到期時間使用分散配置，後續加入約 ±10% jitter，避免整點或重啟瞬間全部到期。以上頻率是目標，超過預算時允許 stale，不用補發暴衝追上理論時程。

基礎資料平均約 140 權重／分鐘，300 的 Explore 預算平均剩約 160 可用於成交；仍以即時全域及子預算准入為準。多 dex、重試、候選進出及首次補資料都會增加成本。

首次補齊 300 人 portfolio＋state 需 6,600 權重；假設 Explore 可完全使用 300／分鐘，理論至少 22 分鐘。不要把這個數字當成延遲保證。冷啟動期間先提供現有快照或 stats-data 能支持的基本排行。

成交初始可採 4 小時更新目標，根據積壓與成交密度調整；不能承諾所有高頻地址都可在公開 API 留存上限內補齊。保留 300 人基本排行，降低的是成交分析更新優先級，不是偷偷刪減候選池。

## 7. 持久化與快取語義

已有 PostgreSQL 就沿用；單機且沒有資料庫時可用 SQLite WAL。SQLite 檔案不放在不支援其鎖定語義的共用網路檔案系統。資料模型依現有架構調整，至少涵蓋下列責任。

| 資料 | 必要資訊 |
|---|---|
| candidate | address、來源時間、來源排名／可用指標、是否 active、最後在榜時間。 |
| endpoint_cache | address、endpoint、dex／參數指紋、payload、fetched_at、source_as_of（若有）、refresh_after、last_error。 |
| fills | 原始成交識別欄位、交易時間、數值欄位及 market scope；具唯一約束以支援冪等寫入。 |
| sync_state | 固定查詢區間、分頁 cursor、已確認 coverage、synced_through、完整性與 gap／截斷原因。 |
| refresh_job | 去重 key、優先級、created_at、next_attempt_at、attempts、lease_until、owner／fencing token。 |
| published index | schema_version、published_at、各欄位來源時間、完整性及相容的結果列。 |

快取 key 包含 address、endpoint、dex、aggregation mode 及其他會改變語義的參數。地址正規化與參數序列化必須一致。

記憶體 LRU 只作加速層，容量可先提升到 2,048 個 endpoint entries，並依實際多 dex 數量調整；LRU 淘汰不等於刪除持久化資料。active candidate 的成功資料不因 refresh TTL 到期而丟棄。

離榜地址資料可先保留 7 天作為可配置起始值；成交原始資料至少保留 30 天分析窗口加重疊／重算緩衝，例如 35 天。清理程序不得刪除仍被未完成工作或 coverage 計算依賴的資料。

錯誤嘗試只更新錯誤及重試時間，不能覆寫 `fetched_at`。來源本身有時間戳時，與本地抓取時間分開保存；抓到了舊來源不等於資料本身新鮮。

## 8. 成交增量同步與完整性

### 8.1 API 限制

官方文件說明：`userFillsByTime` 每次最多回傳 2,000 筆，且只有最近 10,000 筆成交可查。傳入 30 天區間不代表取得完整 30 天，縮小時間片也不能突破歷史留存上限。

`aggregateByTime` 會改變成交聚合語義；第一版沿用既有設定並納入 cache／sync key，不要僅為節流切換後仍宣稱交易筆數／勝率可直接比較。fill 勝率也不自動等同完整交易往返的勝率。

### 8.2 每次工作的演算法

1. 載入既有 sync state。未完成區間繼續原本固定的 `endTime`；新區間才建立新的 `endTime`。
2. 已有可靠 `synced_through` 時，從該時間稍向前重疊開始；初次以所需歷史窗口為回補目標，但完整性預設 unknown。
3. 通過權重准入，最多抓一頁。校驗型別、時間範圍、順序與回傳模式。
4. 以經 fixture／資料驗證的穩定識別鍵去重，例如 `(address, coin, tid)`；不要只靠 timestamp。金額依既有精度規範保存，不新增二進位浮點誤差。
5. 同一資料庫 transaction 寫入 fills 與該頁 checkpoint；提交後才允許推進 cursor。未提交或失敗時重播同頁應安全。
6. 若尚未完成，排回下一頁並釋放 worker，讓其他地址有機會處理。頁數／時間上限代表 yield，不代表完成。
7. 僅在有足夠證據確認區間已遍歷、且不存在已知 gap／留存截斷時，推進已確認 coverage。抓到尾端、HTTP 200 或空頁本身不能證明整段歷史完整。

若每個工作最多三頁，第三頁後必須持久化 continuation；不可把舊版「三頁上限」繼續當成完整 30 天分析的依據。

### 8.3 時間邊界與無法證明完整的情況

`startTime`／`endTime` 為 inclusive。依文件以最後回傳 timestamp 作為下一頁起點，保留同毫秒重疊並去重，不直接 `last_time + 1` 跳過邊界。

如果同一毫秒的回傳上限或異常排序使 cursor 無法前進，必須有 no-progress guard：記錄原因、終止該輪並標記 partial／unknown，不能無限重試或默默跳過。時間切片可協助部分情境，但不能保證解決同毫秒超量或突破 10,000 筆留存。

API 沒有足夠證據讓你保證初次回補前綴完整時，保留 unknown；最早回傳時間、短頁或空頁皆不能自動排除更早資料已被截斷。持續增量收集也需偵測停機／高頻交易造成的留存缺口，不保證永不漏資料。

完整性至少包含 `unknown`、`partial`、`complete` 及 reason；新鮮度 stale／fresh 另行表示。對外保存分析窗口、已觀測範圍與 gap；不要以「抓到最早／最晚成交」直接代表整段 coverage。

不完整時可以展示「已取得成交的統計」與其範圍，但不得標成完整 30 天勝率／手續費／交易數。完整歷史需求需另行評估歷史來源，不在本次自動新增付費依賴。

## 9. Queue、排程及發布

### 9.1 排程與公平性

- scheduler 獨立於 HTTP route 啟動，使用跨 process lease；同一工作只允許一個有效 owner。
- dedupe key 表達地址＋endpoint＋scope，同 key 已 pending／running 時合併需求，不追加新的完整歷史工作。
- 更新來源快照可以原子切換候選池；地址進出不能觸發全池 cache invalidation。
- 優先基礎資料可用性，其次 hot fills／詳情需求；加入等待時間加權或固定公平份額，避免冷門地址永久飢餓。
- queue 有總量與詳情需求的 admission 上限，依 active scope 設定並公開指標；不要讓任意訪客輸入無限地址製造工作。
- lease 到期後可重新領取；用 owner／fencing 或條件更新阻止過期 worker 覆蓋新進度。不要在網路請求期間持有長資料庫 transaction。

### 9.2 讀取與發布

`GET /api/public/explore` 僅讀本地已發布版本，不隱式建立重建 thread。詳情需求若需刷新，必須經已去重且有容量限制的 queue，回應不等待上游完成。

publisher 每分鐘在資料有變更時合併本地資料並原子發布；不再等待 300 人全部完成。`published_at` 表示版本生成時間，不能替代每個欄位的 `as_of`。

維持 `/var/lib/filet-api/explore_index.json` 的快照相容讀取；新格式有 schema version，舊格式可作過渡回退。若檔案持久性有要求，用同目錄暫存檔、flush／fsync 與原子 rename，依既有平台規範處理目錄同步。

沒有 enrich 的新地址可以使用來源能支持的基本排行，進階欄位為 null／pending。既有 stale row 可沿用，但不重設來源時間。資料來源整體失敗時保留最後成功版本；若從未有成功資料，回傳明確 initializing 狀態，不能假造零值排行。

### 9.3 排名語義

保留目前基本月 ROI 排名及自營 leader 排除。不要在此次重構偷偷改成另一種績效算法。

若綜合評分依賴成交資料，要有明確 eligibility：一致 market scope、期間、完整性及資料年齡門檻。缺資料不能當成零分；未合格者可維持基本排行並顯示分析待完成，不與完整分析分數混比。

portfolio、state 與 fills 為不同時間取得的觀測，不宣稱是交易所同一瞬間的原子快照。跨欄位衍生指標必須檢查時間差與適用範圍。

## 10. 分階段施工與遷移

| 階段 | 工作 | 完成門檻 |
|---|---|---|
| P0 止血 | 以 feature flag 停用請求觸發 rebuild，保留本地快照讀取；舊工作可在請求邊界安全退出。 | 開頁不發 info；不需等待 98 分鐘才生效。 |
| P1 權重准入 | 統一 client、共享 limiter、重試計費及 Explore cooldown。 | 多 worker／服務的額度競爭測試通過；未接入流量明列。 |
| P2 資料持久化 | 拆 endpoint cache／fills／sync state，加入 migration 與相容 reader。 | 已提交頁面可在重啟後續跑。 |
| P3 分層更新 | 獨立 scheduler、去重 queue、基礎資料與成交分開。 | 到期不暴衝，冷門工作有公平性，候選變動不全冷抓。 |
| P4 漸進發布 | publisher 合併本地結果，加入時間／完整性 metadata。 | 單地址失敗不阻擋整頁，舊 API 所需欄位仍可用。 |
| P5 驗收與啟用準備 | 執行測試、更新設定與操作說明，以低預算配置準備觀測。 | 清楚交代實測結果、限制與回退方式。 |

遷移舊 JSON 時，只匯入能確定語義的結果與原始時間。不能因為存在結果列，就推導出原始 fills、完整 coverage 或成功的 endpoint cache。無法恢復的欄位標 unknown，低優先級補資料。

回退使用「停用新上游刷新＋保留本地讀取」，不要重新啟用無限額的舊 rebuild；保留已持久化資料供後續恢復。

## 11. 必要測試與驗收

測試使用 mock transport／fake clock，不對正式 HL 進行壓力測試。

| 情境 | 期望 |
|---|---|
| 連續 1,000 次 Explore GET | 上游 info 呼叫數為 0，不產生背景 rebuild；回應來自本地版本。 |
| 多 worker 混合權重並發准入 | 任意滾動 60 秒受控全域不超 900，Explore 不超 300；沒有 race condition。 |
| client retry、timeout 與 429 | 每次嘗試均計費；Explore 共用 cooldown；本地排行仍可讀。 |
| 高優先級請求與 Explore 同時排隊 | Explore 不越過全域／子預算；高優先級保有設計上的優先權。 |
| 頁面寫入前／後 crash | 未提交可冪等重播，已提交從 checkpoint 接續；無重複成交與游標跳躍。 |
| 同毫秒多筆、重疊頁、游標不前進 | 正確去重；不使用 +1 靜默漏單；無進展有界退出且非 complete。 |
| 滿頁、短頁、空頁及留存截斷 | 不因 HTTP 200／頁數用完就宣稱完整 30 天。 |
| 300 人超過舊 LRU 容量、TTL 到期及重啟 | 持久化成功資料保留；到期只排更新，不把整池當成無資料。 |
| 單一地址錯誤或沒有成交分析 | 其他地址可發布；缺值不是 0，時間與完整性正確。 |
| 多 scheduler、lease 失效、候選進出 | 不產生無限重複工作；過期 owner 不能覆寫進度。 |
| 舊 JSON／新 schema 相容與原子發布 | 讀不到半份 JSON；失敗保留上一版；來源時間不被改成發布時間。 |

必要指標：各服務／endpoint 的預留權重、REST 嘗試數、429 次數、limiter 等待與 cooldown、queue 深度／最老等待時間、欄位資料年齡、complete／partial／unknown 比率、cache 命中來源、最後成功發布時間。Metric labels 不直接放高基數地址；個別地址診斷留在可控日誌／查詢介面。

驗收以資料可用性、交易服務不受 Explore 競爭干擾及正確性為主，不以「一輪 300 人跑完」作為健康指標。無法控制的同 IP 外部流量、上游政策與網路異常仍可能造成 429，不能承諾絕對零 429。

## 12. Claude Code 最終交付格式

1. 列出修改的模組、migration、設定與相容性影響，以及已接入／未接入共享預算的呼叫來源。
2. 回報必要測試的實際結果；未執行的測試、未完成的完整性保證與阻礙需明列。
3. 給出本專案實際可用的啟動 scheduler／worker、觀察指標、停用刷新與回退步驟。
4. 說明仍需人工決策的實際阻礙；不要把測試通過寫成已在 production 驗證。

## 13. 官方依據與成本假設

以下規格於本次對話查閱（2026-09-19）；施工時若發現文件或實測行為改變，保留證據並修正配置。

- [HL Rate limits and user limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)：REST 同 IP 聚合 1,200 權重／分鐘；clearinghouseState 為 2；其他一般 documented info 為 20，列明例外另計；fills 類另按回傳筆數增加權重。不要把 userRateLimit 誤當 IP 權重餘額。
- [HL Info endpoint — userFillsByTime](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#retrieve-a-users-fills-by-time)：每頁最多 2,000 筆、最近 10,000 筆可查；時間邊界 inclusive；aggregation mode 影響回傳語義。
- [HL Info endpoint — portfolio](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#query-a-users-portfolio)：portfolio 欄位與各時間範圍的來源規格。

本文的 900／300 預算、更新週期、前 50 名優先級、LRU 2,048、7／35 天保留期皆為工程起始配置。stats-data 不計入 info 的判斷來自現有系統描述，不能延伸成它完全無限流、無成本或有可用性保證。
