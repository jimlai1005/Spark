# Spec: 用戶自訂 leader（任意 wallet address 輸入）

日期：2026-07-27｜狀態：已與使用者完成 grilling 共識，seam 已確認｜Branch: feat/m2-frontend

## Problem Statement

Filet 的跟單用戶目前只能從平台精選的 leader 卡片清單中選擇跟單對象。想跟 HL 官方
leaderboard 上其他優秀交易者的用戶沒有任何管道——清單外的位址在前端無處輸入，
在後端會被白名單閘門與引擎每輪驗證拒絕。用戶的選擇自由被平台策展範圍完全限死。

## Solution

leaders 頁保留精選卡片清單，另新增「自訂 leader」區塊：用戶可輸入任意 wallet
address（並附 HL 官方 leaderboard 外部連結供研究）。輸入後即時格式驗證、後端查鏈
確認該帳戶存在且有 perp 活動並回傳預覽（帳戶權益、持倉數、最近活動），用戶勾選
「未審核 leader」風險聲明後走既有的簽章選擇流程。通過的位址自動寫入獨立的
user-sourced 白名單 registry——僅該用戶可用、不進公開目錄，operator 保留 kill-switch。
跟單邏輯與風險槓桿完全不變。

## User Stories

1. As a 跟單用戶, I want 在 leaders 頁輸入任意 leader wallet address, so that 我可以跟精選清單以外我自己研究過的交易者。
2. As a 跟單用戶, I want 頁面上有 HL 官方 leaderboard 的外部連結, so that 我可以自己去找喜歡的 leader 再回來輸入。
3. As a 跟單用戶, I want 輸入位址時得到即時格式驗證回饋, so that 貼錯格式時立刻知道，不用等送出才失敗。
4. As a 跟單用戶, I want 送出前看到該位址的鏈上預覽（帳戶權益、持倉數、最近活動）, so that 我能確認自己沒有貼錯位址、跟錯人。
5. As a 跟單用戶, I want 輸入不存在或沒有 perp 活動的位址時被明確拒絕並說明原因, so that 我不會跟單一個永遠不會有動靜的死地址。
6. As a 跟單用戶, I want 輸入自己的登入位址時被擋下, so that 我不會誤設出無意義的自我跟單。
7. As a 跟單用戶, I want 自訂 leader 必須勾選「我知道此為未審核 leader」專屬聲明才能送出, so that 我清楚知道這個 leader 沒有經過平台審核、風險自負。
8. As a 跟單用戶, I want 自訂 leader 沿用與精選 leader 相同的簽章確認流程, so that 換 leader 的授權安全性不因來源不同而打折。
9. As a 跟單用戶, I want 輸入的位址若已在精選清單中則直接視同選擇該精選 leader, so that 同一位址不會出現兩種身分。
10. As a 跟單用戶, I want 輸入已被平台停用的 leader 位址時被拒絕並告知, so that 我不會繞過平台的安全撤銷去跟一個已知出事的 leader。
11. As a 跟單用戶, I want 自訂 leader 沒有績效快照時看到「無績效資料」而非錯誤, so that 頁面不會因為缺資料而壞掉或誤導。
12. As a 跟單用戶, I want 選定自訂 leader 後在「我目前跟誰」看到該位址, so that 我隨時能確認目前的跟單對象。
13. As a 平台 operator, I want 用戶自訂的 leader 不出現在其他用戶的精選清單, so that 策展門面不被任意位址稀釋、平台不為未審核 leader 背書。
14. As a 平台 operator, I want user-sourced leader 落在獨立的 registry 檔（非手編的精選白名單檔）, so that 服務自動寫入與人工編輯不會互相蓋寫。
15. As a 平台 operator, I want 對 user-sourced leader 保留與精選 leader 相同的 kill-switch（enabled=false → 引擎受控收尾）, so that 自訂 leader 出事時我有同等的緊急處置能力。
16. As a 平台 operator, I want 精選白名單對同一位址的判斷優先於 user registry, so that 已被我停用的 leader 不能被用戶用自訂路徑重新准入。
17. As a 平台 operator, I want user registry 記錄每筆位址由哪個帳戶加入, so that 出事時有稽核線索。
18. As a 跟單引擎, I want 白名單驗證涵蓋精選與 user-sourced 兩個來源, so that 合法選定的自訂 leader 不會在引擎層被拒絕跟單。
19. As a 跟單引擎, I want 每輪持續驗證的語義不變（只看 enabled）, so that 撤銷觸發的受控收尾行為與現行完全一致。

## Implementation Decisions

**白名單政策——自動准入，引擎結構不動**
- 保留白名單機制作為結構承重與 kill-switch；新增自動准入路徑，跟單引擎的
  每輪驗證邏輯零改動。
- user-sourced leader 落在獨立的 registry 檔（`user_leaders.json`），只由 public API
  服務寫入；operator 手編的精選白名單檔維持人工所有權。兩檔在讀取時合併。
- registry 檔沿用精選白名單的頂層形狀與欄位（address/name/description/enabled/
  accepting_new），另加 `source: "user"` 與 `added_by`（帳戶稽核欄位）。載入沿用既有
  loader 的 fail-fast 型別驗證慣例與「述詞函式而非裸旗標」的呼叫模式。
- 合併優先序：**精選白名單條目優先**。同一位址若已在精選檔中，user registry 的條目
  一律忽略；精選條目 enabled=false 時，自訂路徑必須拒絕准入（不得繞過安全撤銷）。
- 權限語義：user-sourced 條目是「全域 permitted、不 listed」——引擎驗證通過，
  但不進公開目錄。「僅本人可用」由 API/前端層的可見性實現，不是引擎層的帳戶綁定
  （任何用戶本就可自行輸入同一位址走准入，帳戶級封鎖無安全增量）。

**准入門檻**
- 三道檢查：(1) 格式——0x + 40 hex，正規化為小寫（與後端既有慣例一致）；
  (2) 鏈上存在——透過既有 HL gateway 的 clearinghouse 查詢，帳戶須有 perp 活動痕跡
  （權益 > 0 或有持倉；精確判準以測試錨定）；(3) 禁止自跟——輸入位址不得等於
  session 登入位址（小寫比對）。
- 不審查績效——leader 品質判斷歸用戶。

**API 契約**
- 新增預覽端點：`GET /api/leaders/preview?leader_address=...`（session 驗證）。
  執行三道准入檢查並回傳預覽資料：`{ address, exists, account_value, position_count,
  already_listed }`（already_listed = 該位址已在精選清單且可選）。檢查不過回 4xx
  並附機器可判的 reason code（`invalid_format` / `self_follow` / `not_found` /
  `leader_disabled`）。
- 既有換 leader 訊息端點與提交端點放行自訂位址：訊息端點對非精選位址改為執行准入
  前置檢查（取代原本的 is_selectable 拒絕）；提交端點在驗簽通過後**重新執行全部
  准入檢查**（不信任客戶端曾呼叫 preview，防 TOCTOU），通過即冪等寫入 user registry
  （條目已存在則跳過寫入），再記錄簽章換 leader 記錄。
- registry 寫入失敗必須大聲失敗（回 5xx，不記錄換 leader）；寫入採原子寫
  （temp file + rename）。寫入成功但換 leader 記錄失敗時，用戶重送同一 POST 可安全
  重試（registry 寫入冪等）。
- HL 鏈上查詢走既有的單一 resilience boundary（唯讀、冪等、transient 重試與 502
  轉譯自動繼承），不另開 HTTP 呼叫路徑。

**前端**
- leaders 頁新增「自訂 leader」區塊：位址輸入框（viem `isAddress` 即時驗證＋小寫
  正規化）→「查詢」觸發 preview → 預覽卡（帳戶權益、持倉數；無績效快照顯示
  「無績效資料」）→ 專屬「我知道此為未審核 leader」checkbox（未勾選不得送出，
  仿既有 AML attestation 的純前端閘門模式）→ 既有確認框與簽章流程。
- already_listed 的位址：預覽卡標示「此位址已在精選清單」，後續流程視同選擇精選
  leader（不寫 registry）。
- HL 官方 leaderboard 以外部連結呈現（`https://app.hyperliquid.xyz/leaderboard`，
  新分頁開啟）。
- 表單與流程模組仿既有「伺服器產生原文 → 錢包簽 → 本地 recover 預驗 → 送整包
  payload」的 flow 模組慣例（依賴注入、可離線測試）。

## Testing Decisions

- 好測試的定義：只測公開介面的外部行為，不測實作細節；測試名讀起來像規格。
- Seam 已與使用者確認，全部沿用既有 seam、不開新 seam 類型：
  - 後端 HTTP seam：FastAPI test client 打 preview／訊息／提交端點（prior art：
    既有的 leaders 目錄與 leader select API 測試）。HL gateway 一律注入 mock。
  - 後端 loader seam：user registry 的載入、合併、優先序、fail-fast 驗證
    （prior art：既有的白名單載入測試）。
  - 前端 flow seam：自訂 leader 的驗證＋預覽＋送出編排，依賴注入離線測
    （prior art：既有的換 leader 授權編排測試）。
  - 前端 page seam：自訂區塊的渲染與互動閘門（輸入驗證回饋、checkbox 未勾不可
    送出、預覽卡呈現）（prior art：既有的 leaders 頁測試）。
- 測試全離線：沿用 autouse socket-ban；所有 HL 外呼在測試中 mock。
- 關鍵行為錨定測試（必須存在）：精選優先於 user registry、operator 停用位址不可
  經自訂路徑准入、自跟拒絕、提交端點獨立重跑准入檢查、registry 寫入冪等。

## Out of Scope

- 跟單引擎的每輪迴圈、下單、風險與槓桿邏輯——完全不動（使用者明確要求維持原樣）。
- 自訂 leader 的績效快照／回測資料生成（顯示「無績效資料」即可）。
- attestation 勾選狀態的簽章綁定或後端記錄（既有 AML checkbox 亦為純前端閘門；
  兩者一起升級為簽章內聲明是未來工作）。
- 移除或改動精選清單本身的呈現與資料來源。
- user registry 檔的部署與營運配置（服務啟動參數、檔案路徑佈署）——晨間檢查點。
- 帳戶級的 leader 可見性隔離之外的多租戶功能。

## Further Notes

- 精選白名單的兩旗標語義維持不變：`enabled`＝安全撤銷（False → 新客選不到＋
  跟單中的 follower 受控收尾）；`accepting_new`＝例行下架（只擋新客）。user-sourced
  條目繼承同語義，operator 可直接編輯 registry 檔執行撤銷。
- 用戶輸入位址在 HL 上「存在」的精確判準（權益閾值、是否計入歷史成交）以實作時的
  測試錨例為準；原則是擋死地址與 typo，不是審查品質。
- 本 spec 由 grilling 共識產出（2026-07-27 夜間 session），決策鏈：白名單自動准入 →
  格式＋鏈上存在門檻 → hybrid UI → 驗證即預覽 → 僅本人可見 → 專屬風險 checkbox。
