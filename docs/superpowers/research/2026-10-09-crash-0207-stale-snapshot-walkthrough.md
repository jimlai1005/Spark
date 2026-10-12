# 2026-10-07 暴跌事件回顧：過期快照、來回洗單與 HL 500

> 目的：下次再遇到暴跌（或同型 CRIT），可以拿這份文件當**對照基準**回頭檢查——
> 看修法有沒有生效、行為是否和這次不同。本文含：一步一步的事件經過（沿著使用者原本的
> 認知邏輯講）、機制與公式、證據、損益估算、未解項、修法決策記錄，以及最後的**回顧清單**。
>
> 範圍：唯讀調查，2026-10-08 到 2026-10-09 完成。修法計畫見
> `docs/superpowers/plans/2026-10-09-fresh-leader-read-and-order-resilience.md`。
> 分析工具（可重跑）：`docs/superpowers/research/2026-10-09-crash-review-tools/`。
> 時間一律 UTC。數字出處見各節；「推論」一詞標示沒有直接證據的部分。

> **2026-10-10 註記（本文件自 `archive/2026-10-fresh-leader-read-v1` 挑回，內容除本註記與 §8 外未改）**
> - 第一版修法分支 `archive/2026-10-fresh-leader-read-v1` 已**整體放棄**：「扣掉落後鏡射單」的邏輯修了六輪仍不收斂（每輪都帶出新維度）。
> - 現行修法是第二版：plan `docs/superpowers/plans/2026-10-10-copytrade-safety-net-v2.md`（分支 `fix/copytrade-safety-net-v2`），
>   改用「增減方向不對稱」規則（減倉用新鮮目標、加倉只到 min(開頭, 新鮮)），不扣鏡射單。
> - 因此 **§7 決策記錄中與 D11–D21（第一版的裁決編號）相關的敘述已不適用**：A1 的「安全網前重抓＋扣掛單」做法、各項實作細節以 v2 plan §3 為準；
>   §7 只保留當時的問題診斷價值（W2／A2／B1／B2 的方向在 v2 仍成立，對應 v2 的 D15–D18、D22）。
> - **§8 回顧清單改引用 `deploy/RUNBOOK.md` §5.8m 的 cycle_trace 欄位**（v2 的欄位名與第一版不同，例如分支 `within_band` 取代 `within_tolerance`、
>   新增 `targets`／`t0`／`t1`／`lo`／`hi`）；§8 A 段已補上與 RUNBOOK 一致的 jq 範例。
> - 第一版的 `analyze_cascade.py`／`fetch_hl_window.py` 原樣保留於 `2026-10-09-crash-review-tools/`（只讀 HL 公開 API 與離線 JSON，與引擎版本無關）。

## 0. 一頁摘要

兩則 CRIT 是**兩件不同的事**，相隔約 14 小時，同一個 follower（`f6b3e7…`，跟 Filet Alpha
`0xfB9C…9760`）：

| | CRIT-1「掛單重試後仍不符：缺少 4」 | CRIT-2「連續 5 次同步失敗（500, 'null'）」 |
|---|---|---|
| 時間 | 10-07 02:01Z（ETH 一分鐘內 2644 → 低點 2588） | 10-07 15:53–15:57Z |
| 根因 | 一輪的快照拍在連環成交途中，28 秒後才用在部位安全網；掛單對帳又對已成交的單做 modify／重掛 | HL `/exchange` 短暫回 500；modify 不重試、例外中止整輪、五連敗退出 |
| 真實後果 | 安全網拿**過期的 leader 部位**對**新鮮的自己部位**，過度賣出、下一輪買回，反覆來回 | 引擎退出，10 秒後被 systemd 拉起，無部位損失 |
| 影響範圍 | f6b3e7 與 f438 同形（f438 權益 $53k，損失約 $0.5k–$1.9k）；fb8c 未受影響 | f6b3e7 5/5 退出；f438 4/5 差一次 |
| CRIT 的真偽 | 「缺少 4 張」是 leader 已成交的單，**誤報**；但同一原因造成的過賣是**真的** | CRIT 文案「需人工介入」不準確，實際自動恢復 |

## 1. 使用者的認知 vs 實際情況

使用者原本的理解（2026-10-08 提出）：

> 下跌過程中 f438 會一路吃完網格的開單部分，維持和 leader 一樣的部位大小；但可能因為
> leader 部位抓取的時間差，導致槓桿過高，也就是一邊吃單、一邊跟隨 leader 部位放大而從
> 安全網繼續加倉；等急劇下跌結束後，再把過多的倉位減倉。post-only 單因下跌而失效，
> 也因此避免了最後價格穩定時做過多倉位的減倉。

逐點對照：

| 認知 | 實際 |
|---|---|
| 下跌時一路吃完網格 | **對。** maker 買單 02:00:54–02:01:26 連環成交，和 leader 同步（f6b3e7 的倍率 ≈5.7×）。 |
| 時間差導致槓桿過高，之後減倉 | **對，而且很高。** f6b3e7 峰值約 6.1× 權益、f438 約 7.8×；減倉後目標約 2.8×。 |
| 安全網一邊跟 leader 放大一邊加倉 | **相反。** 第一次動作是**賣出**；加倉（買回）是下一輪才有，用來修正第一次賣過頭。 |
| post-only 失效避免了過多減倉 | **方向偏了。** 失效的 ALO 單是**重複**掛出早已成交的買單；它們被擋掉避免的是**重複加倉**（約 +0.48 ETH），而且是運氣，不是設計。 |

## 2. 一步一步發生了什麼（f6b3e7；f438 的比例完全相同）

> 讀表提醒：HL fills 的 `startPosition` 是**成交前**部位，不是成交後（調查時曾因此標錯一次）。

**步驟 1｜02:00:37 之前：網格掛單就位。** follower 的掛單是用崩盤前的 scale 掛的：
`0.1761 ETH ÷ leader 0.0308 = 5.72`；01:58 那一輪三個幣的比例 5.716／5.718／5.717
（CV 0.0001）。
**步驟 2｜02:00:37–02:00:54：follower 多持一格。** leader 02:00:37 撤掉 ETH 買單 2651.4；
follower 要等下一輪才知道，這張單 02:00:54 成交（0.1749 ETH，f438 是 10.9 ETH）。
**這一格是撤單延遲的固有後果，這次修法不處理。**
**步驟 3｜02:00:59–02:01:21：連環成交（你想的那一段）。** leader 在約 20 秒內被吃 6 格
（ETH 2643.3／2635.3／2627.3／2619.3／2611.3／2603.4，另有 BTC、HYPE），follower 同步以
maker 成交。ETH 該分鐘 K 線高 2644.5、低 2588.1、收 2621.1。
**步驟 4｜約 02:01:05：這一輪開始、拍快照。** 先讀 leader 掛單、leader 部位、leader 帳戶
（`loop.py:318–320`），再讀自己（`:322–324`），然後算 scale（`:380–393`）。快照當下連環
成交還沒結束。證據：CRIT 的部位行「BTC 0.01166、ETH 0.6969」恰等於 follower 在
02:01:08／09 兩筆成交的 `startPosition`＝快照早於這兩筆成交。
**步驟 5｜02:01:10–29：掛單對帳（A 段）用快照行動。**
- 02:01:10、02:01:13：`modify_order rejected: Cannot modify canceled or filled order`
  ——對**已經成交**的單做 modify。
- 02:01:14（ETH）、02:01:16（BTC）：`cancel_order rejected … already canceled, or filled`。
- 退回「撤舊單＋重掛」，於是把**早已成交**的 ETH 2635.3／2627.3／2619.3 與 BTC 84060 買單
  當成缺少，重新以 ALO 掛出。此時 ETH 賣一價 2612.8，買單價高於賣一價會吃單，post-only
  直接拒絕：`historicalOrders` 共 8 筆 `badAloPxRejected`（02:01:17–29 七筆＋02:03:09 一筆）。
- settle 重試用同一份過期規格再拒一次，發 CRIT。
- 若當時價格沒穿過這些價位，重複單會被接受＝多出約 0.48 ETH 暴險。
**步驟 6｜02:01:33–35：部位安全網（B 段）下錯方向的單。** `_safety_net`（`loop.py:410–425`）
重抓**自己的**部位（新鮮：ETH 1.1958、BTC 0.03359、HYPE 8.61），卻沿用**開頭的**
`leader_positions` 與 `scale`（過期）。以過期輸入計算，follower 看起來多了 2.5 倍，於是
下 reduce-only IOC 市價賣單（限價約低於市價 5%）：

| 幣 | 賣前 | 賣出 | 賣後（＝過期目標） | 當時 leader 實際（成交推算） |
|---|---|---|---|---|
| ETH | 1.1958 | 0.7226 | 0.4732 | 0.2463（快照用的是 0.0913） |
| BTC | 0.03359 | 0.02298 | 0.01061 | 0.00515 |
| HYPE | 8.61 | 5.87 | 2.74 | 1.61（快照用的是 0.53） |

快照到動手相隔約 28 秒。成交價 BTC≈83,715、ETH≈2,606.7、HYPE≈89.3。
**步驟 7｜02:02:15–22：下一輪輸入變新。** leader 部位已長大，但 scale 降到 3.43，目標變成
ETH 0.6302、BTC 0.01055、HYPE 5.51，高於剛賣完的部位，於是**買回**（ETH 0.2631 @2633.0，
比賣出價 2606.7 高）。
**步驟 8｜02:03–02:09：每一輪都再來回。** leader 重排網格，各輪比例 3.39、2.85、…、3.25
（逐輪三個幣的比例 CV ≤ 0.1%＝單一時間點的快照，快照年齡約 12–31 秒），全部是 taker。
到 02:10 才收斂。

**這個事件裡，哪些減倉是「該做的」？**
- 政策性降倉：目標槓桿從約 6.1× 降到約 2.8×（ETH 1.196 → 0.63，約 0.57），是設計行為。
- 因輸入過期而多出來的過賣與來回（約 0.4 ETH 及隨後的買回）：這是錯誤，V1 要修的。
- 重複掛單（約 0.48 ETH）：這次被 post-only 擋掉，是運氣；W2 要修。

## 3. 機制與公式

- **scale**（`sizing.py:79–113`）：`scale = 自己權益 × 資金使用率 × 波動權重 ÷ leader 的 perp accountValue`。
  `max_target_leverage` 預設 0（停用）；Filet Alpha 在 `leaders.json` 沒有 `max_leverage`，
  watcher 不注入。
- **資金使用率**（客戶簽章的資金設定，`state/<id>/var/filet/capital_settings_ledger.json`）：
  f6b3e7＝0.4000（2026-09-28 套用）；f438 最新一筆＝0.5000。`use_full_equity=true`。
- **波動權重**（`sizing.py`）：`1 − clip(z×0.2, 0, 0.7)`；z 來自 leader 的 portfolio「month」
  每日 |PnL|（14 天基準）。最後一個點是即時值，崩盤當天會快速變大。
- **等價說法**：follower 目標槓桿 ≈ 使用率 × 波動權重 × leader 的有效槓桿
  （名目 ÷ **perp accountValue**）。2026-10-09 實測：leader accountValue 122.37、名目
  820.4＝6.70×；follower 權益 994.09、名目 2401.4＝2.42×；公式 0.4×0.90×6.70＝2.41×。
  同一時間由 portfolio 算出 z=0.20、w=0.96，兩者差 6%，在 `size_tolerance` 8% 帶內。
- **unified account 的陷阱**：leader 的 portfolio 權益約 $1,370，但 perp accountValue 只有
  $122（空倉時為 0、開倉時＝保證金＋浮損；工程原則 #1 事故 #6）。scale 的分母是後者，
  所以 leader 每多開一格，分母就變大、scale 就變小。**這解釋了「leader 部位長大但
  follower 目標不跟著等比長大」的方向，但崩盤當下 5.72 → 3.4 各佔多少沒有紀錄，見 §6。**
- 每輪的資料是**分次**讀的：leader 掛單、leader 部位、leader 帳戶、自己掛單、自己部位、
  自己帳戶，各是一次 HTTP；之後 A 段（掛單對帳）要打十幾次寫入並兩次 `settle_seconds=2`
  的等待，B 段才開始。

## 4. CRIT-2：HL 500 'null'

- 時間線：f6b3e7 15:53:00 起連 5 輪 `modify_order` 回 `ServerError(500, 'null')`，15:57:02Z
  `SystemExit(1)`，systemd 15:57:12 拉起；f438 15:55:01–15:58:01 連 4 輪，差 1 次。
- **不是全面中斷**：每輪第一個 modify 成功（f438 每分鐘 1 筆 canceled＋1 筆 open；f6b3e7
  15:55:00 一筆），下一個才 500。恢復後整批一次改完（f6b3e7 15:57:18 起 19 張；f438
  15:59 起 16 張）。
- **觸發**：所有掛單比期望值小約 8%（ETH 0.0853 → 0.0922），剛好越過 `size_tolerance`
  0.08，一輪要改約 20 張。
- **放大**：`ResilientExchange.modify_order` 單次嘗試（`resilience.py:139–143`）；
  `_TRANSIENT_MARKERS` 只有 502／503／504（沒有 500）；`orders.py:757` 的 `ex.modify`
  例外沒人接，整輪（含 settle 驗證與部位安全網）中止；`loop.py:486–496` 連續 5 次退出。
- **排除項**：位址限流（`userRateLimit` 2525/99449、3559/11.5M）；主機過載（load 0.1–0.2、
  PSI 0）；總中斷（上述）。
- **歷史**：30 天內失敗輪次約 20（佔約 7 萬輪的 0.03%），集中在 4 個時段——09-19、09-26、
  10-03 三個週六 UTC 08:33–09:15（502/504），及 10-07 的 500。週六時段只有 3 個樣本、
  原因未驗證。
- **無法驗證**：500 `'null'` 的確切語意（HL 文件、網搜、SDK 都沒有）。「HL 端暫時故障」是
  從兩個 follower 同時刻出錯推論的；兩者同機同 IP，無法區分「HL 全域」與「我們這條路徑」。
- **CRIT 文案「需人工介入」不準確**：unit 是 `Restart=on-failure`，自動恢復。

## 5. 損益估算（粗估，方法有限制）

方法：`analyze_cascade.py cost`——taker 成交（多為安全網校正）對「不動作」基準：
Σ賣量×(賣價−標記價)＋Σ買量×(標記價−買價)，標記價取 K 線收盤。
**「不動作」不是正確反事實**（follower 本來就比 leader 重一格，部分校正是該做的），
只能當量級上限，且隨標記時點變動（急跌後有反彈）。

| 帳戶 | 權益 | taker 成交 | 手續費 | 對不動作（02:10 收盤） | 對不動作（02:15 收盤） |
|---|---|---|---|---|---|
| f6b3e7（01:58–02:11） | $1,096 | 33 | $5.7 | −$27.6（扣費 −$33.3） | −$14.2（扣費 −$19.9） |
| f438（02:01:30–02:11） | $53,357 | 78 | $310 | −$1,547（扣費 −$1,856） | −$804（扣費 −$1,114） |

- 第一輪對「用新鮮輸入重算」的基準：scale 取 3.43–5.18，過賣 ETH 0.37–0.80、BTC ≤0.016、
  HYPE 2.8–5.6，損失約 −$10 到 −$22（f6b3e7）；f438 按權益比約 50 倍，約 $0.5k–$1.1k，
  另加手續費 $0.2k–$0.3k。
- 上一輪我一度用固定 scale 5.18 做反事實，**高估了過賣**——新鮮的 scale 本來就會降，
  所以這裡改用範圍。
- f438 的 leader 沒有從設定直接確認；依成交價與時間和 f6b3e7 逐筆相同，**推論**是同一個
  leader（Filet Alpha）。
- fb8c：01:40–02:20 只有一筆 ZRO 成交，沒有 BTC／ETH／HYPE 部位，未受影響。

## 6. 未解項（修法不依賴它們）

1. **崩盤當下 scale 5.72 → 3.4 怎麼拆**（leader perp accountValue 變大 vs 波動權重下降）。
   當時沒有逐輪紀錄，`clearinghouseState` 沒有歷史，portfolio 解析度太粗，**這一分鐘無法補回**。
   機制本身會在下一個波動日由 `cycle_trace` 直接讀出。
2. **02:01:33 那一輪 BTC 的目標對不齊。** 若 scale 為 5.18：ETH 需要 leader 0.0913
   （＝02:01:08 成交**之前**）、HYPE 需要 0.53（＝02:01:09 成交之前）、BTC 卻需要 0.00205
   （＝02:01:09 成交**之後**；fills 顯示 BTC 成交前是 0.00102）。一個時間點的快照不可能
   同時如此。f438 與 f6b3e7 的 BTC 比例一模一樣（目標／賣前＝0.316），所以是系統性的，
   不是隨機噪音。其餘各輪（6 輪）三個幣都對得齊。尚未驗證的假設：同一回應內各幣部位的
   傳播時序不一致；或我對 fills 時戳的解讀有誤。需要同類連環成交再出現＋逐幣紀錄才能判定。
3. 500 'null' 的語意（§4）。
4. 週六 08:33–09:15Z 的 502/504 是否為 HL 例行維護（3 個樣本）。

## 7. 修法決策記錄（2026-10-09 共識）

| 項 | 決定 | 理由 |
|---|---|---|
| A1／V1 | 安全網前重抓 leader 部位＋帳戶（合併成單一 `user_state` 呼叫）；自己的部位也改用同一種合併呼叫；開頭的重複讀取也合併 | 解掉新舊混用。每輪 `user_state` 呼叫 5 次 → 4 次（今天：開頭 leader 2＋自己 2、安全網自己 1；之後：開頭 2＋安全網 2），**淨 −1 次**（權重 −2/分/每個 follower）；單次延遲中位數 28ms |
| A1 守衛 | 可疑讀值：2 秒後重讀一次；**連續兩輪**都空才當 leader 真的全平；跳過期間連續 3 次發警告、10 次 critical（去重）；每輪開頭的讀取套同一套規則 | V1 讓安全網依賴新讀取，壞讀值（空／0）在現行邏輯下會被當成 leader 全平→平掉自己所有部位 |
| A1 附帶 | 每輪寫 `cycle_trace-YYYYMMDD.jsonl`（彙總＋有動作或有差異時的逐幣明細），保留 14 天 | 解 §6 的 1、2 |
| V2 | 先蒐集資訊，之後再決定要不要「重抓與快照差距過大就延後減倉」 | 缺資料 |
| W2（併入 A2） | modify／cancel 被拒、原因為已成交或已撤時，視為已消耗，不退回重掛 | 根本原因；與 2c5e591 同類 |
| A2 | ALO 單被判會吃單（`Post only order would have immediately matched`）→ 市場已越過，延到下一輪，不計入失敗預算、不發 CRIT | 第二層防線，免費（不加請求） |
| B1 | modify 例外：只標記該單為不明、不退回重掛、不中止整輪；HTTP 500 納入暫時性錯誤 | 不明結果不能盲目重下（可能已落地，會重複） |
| B2 | CRIT 文案改為「已自動重啟、通常無需人工」，附連續失敗起迄時間 | 目前文案誤導 |
| W4 | **不動**（波動權重降倉屬風險政策） | V1／W2 不減少政策性降倉量，見 §2 末表 |
| 不做 | 部署、memory 紀錄：部署後一起寫 | 使用者裁決 |

V1 已知後遺症：新增呼叫失敗時跳過一輪校正（估計約每兩週一次、最多晚 1 分鐘）；同一輪內掛單用快照 scale、
部位用較新的 scale；波動權重不重抓（portfolio 權重 20），所以 scale 只有權益那一半是新的。

## 8. 下次暴跌的回顧清單

**A. 資料（皆唯讀，部署後的引擎還會多一份 cycle_trace）**

```bash
# 1) 引擎端：WARNING 以上的 journal（INFO 不會進 journald）
ssh -i <pem> ubuntu@<prod> 'sudo journalctl -u filet-follower@<id> --since "<T-5m>" --until "<T+15m>" --no-pager -o short-iso | grep -vE "^\S+ \S+ python\[[0-9]+\]:\s+(File|\^)"'
# 2) HL 資料（公開 API，經正式機）
ssh -i <pem> ubuntu@<prod> 'python3 - --acct f6b3=0x... --leader 0xfb9c... --start <ISO> --end <ISO> --coins ETH,BTC,HYPE' < docs/superpowers/research/2026-10-09-crash-review-tools/fetch_hl_window.py > window.json
# 3) 離線分析
python3 .../analyze_cascade.py window.json timeline --me f6b3
python3 .../analyze_cascade.py window.json rejects  --me f6b3
python3 .../analyze_cascade.py window.json fit      --me f6b3
python3 .../analyze_cascade.py window.json cost     --me f6b3 --marks 02:10,02:15
# 4) 部署後：cycle_trace（state/<id>/var/copytrade/cycle_trace-YYYYMMDD.jsonl；欄位逐鍵說明見 deploy/RUNBOOK.md §5.8m）
#    Decimal 存成字串，需 tonumber；開頭被守衛跳過的輪次 scale 為 null
F=/opt/filet/state/<id>/var/copytrade/cycle_trace-YYYYMMDD.jsonl
#    (a) 每輪的 scale_snapshot／scale_fresh／snapshot_age_s
jq -c 'select(.kind=="cycle") | {ts, snapshot_age_s, scale_snapshot: (.scale_snapshot|if .==null then null else tonumber end), scale_fresh: (.scale_fresh|if .==null then null else tonumber end), skipped_reason}' "$F"
#    (b) 有 decision 的輪次與各幣 branch／size（decision 行只在 B 段有動作或任一幣 t0≠t1 時才寫）
jq -c 'select(.kind=="decision") | {ts, acts: [.decisions[] | {coin, branch, size: (.size|tonumber), t0, t1, m}]}' "$F"
#    (c) 驗收指紋：B 段減倉決策的 hi 必須等於新鮮目標 t1
jq -c 'select(.kind=="decision") | .decisions[] | select(.branch=="decrease") | {coin, t1, hi, ok: (.hi==.t1)}' "$F"
```

⚠️ `historicalOrders` 只回最近 2000 筆；事件過去越久越可能被截掉，**盡快拉**。

**B. 要回答的問題與對照**

| 問題 | 這次（修法前） | 怎麼看 |
|---|---|---|
| 安全網用的快照多舊？ | 約 28 秒 | `fit`（無 trace 時）；有 trace：cycle 行 `snapshot_age_s`、`snapshots.leader_start` vs `leader_fresh` |
| 校正方向有沒有錯？ | 第一輪賣 0.72 ETH，新鮮輸入應為 −0.35 到 +0.08 | decision 行逐幣：`t0`／`t1`／`lo`／`hi`／`m`、`branch`、`size`（`decrease` 的 `hi` 應等於 `t1`） |
| 有沒有重掛已成交的單？ | 8 筆 `badAloPxRejected` | `rejects`；cycle 行 `reconcile.deferred_alo_cross`、`reconcile.consumed_skipped` 計數 |
| scale 為什麼降？ | 未知 | cycle 行：`snapshots.*.account_value`／`total_ntl_pos`、`capital_utilization`、`weight`、`vol`（`z`、`today_abs_pnl`、`mu`、`sigma`） |
| 來回洗單的量 | 第一輪賣 0.98（買回 0.26）vs 最小需要 0.57 | `cost`、`fit` |
| 讀取被跳過幾次？警告有沒有發？ | 不存在 | cycle 行 `skipped_reason`、`fresh_read.consecutive_skips`；TG（`fresh_read_skip_warn`／`_crit`） |
| 引擎有沒有因 500 退出？ | 5 連敗退出 | journal：`同步錯誤 (n/5)` 與 SystemExit |

**C. 修法生效的判準**
1. 連環成交期間不再出現「同價同量、相隔數秒、crossed False→True」以外的過賣：
   安全網的第一筆動作方向與新鮮目標一致。
2. `badAloPxRejected` 不再因「重掛已成交的單」出現；modify 被拒後沒有緊接著的 place。
3. HL 500 期間：單張 modify 例外不再中止整輪；若仍連續失敗，CRIT 文案為「已自動重啟」。
4. 跳過計數、警告、critical 與 trace 一致。

**D. 容易踩的坑**
- `startPosition` 是成交**前**部位。
- 「對不動作」不是正確反事實，只能當量級上限。
- 過度賣出的量取決於 scale；用固定 scale 會高估。
- 兩個 follower 同機同 IP，無法用「同時出錯」區分 HL 全域或本地路徑。
- 同一次調查的第一個結論常被後續資料推翻（本次就有兩處：固定 scale 反事實；波動權重數值）；
  結論前先找反例。
- 不要讀 `.env*`；進程 environ 也讀不到（權限），要看設定改看簽章 ledger、leaders.json
  與公式驗算。
