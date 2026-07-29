# 2026-07-30 M2 前端減法：單一入口

## 目標
前端縮成單一使命：**綁定錢包（跟單授權＋builder code 收款授權）→ 貼上 leader 地址跟單**。

- 移除頁面：`/capital`、`/performance`、`/pricing`、`/billing`（連同各自測試）
- `/leaders` 重塑為「貼地址」單一入口：移除精選目錄卡、績效區塊、付費閘門
- Header 公開 tab 縮為 3 個（開始／綁定錢包／跟單）；admin tabs（/ops、/admin）不動
- `/onboarding` 精靈功能不動（兩張授權卡就是「綁定錢包的兩項」）
- **後端 API 與 deploy env 全部不動**：資金配置 90% 已由生產 env `COPY_CAPITAL_UTILIZATION=0.9`
  控制（deploy/follower.env.example:37 註明生產現值），前端只是不再提供設定介面。
  引擎 config 預設值（config.py:112 = 1.0）屬實盤引擎行為，不在本次範圍，動之前必問使用者。

## 設計方向（frontend-design skill）
- 保留既有 tokens（深海軍藍 #07111F ＋ #19D3AE；沿 2026-07-17 design doc，與 HL 生態一致）。
  重設計的力氣花在資訊架構與招牌互動，不換膚。
- Signature：`/leaders` 的**地址 dock**——超大 mono 輸入框＋准入預覽卡，全站唯一的視覺重拳。
- Landing `/` = 三步旅程（連結錢包 → 完成兩項授權 → 貼地址跟單），步驟 2 明列兩項授權名稱。
- 文案禁詞照舊（固定收益／保證／存款／代操；copy.test.ts + redline.test.ts 把關）。

## 刪除清單（git rm）
- web/src/app/{capital,performance,pricing,billing}/（各含 page.tsx + page.test.tsx）
- web/src/lib/capitalSettingsFlow.ts(.test.ts)、capitalValues.ts(.test.ts)、leaderPerf.ts(.test.ts)

## 改動分工（兩個 agent 檔案不相交）
**Agent A（設計實作，sonnet）**：leaders/page.tsx(+test)、app/page.tsx(+test)、
Header.tsx(+test)、copy.ts(+copy.test.ts)、globals.css。
**Agent B（機械修剪，haiku）**：api.ts（刪 7 個函式：getBillingPlans/getBillingStatus/
postBillingCheckout/postBillingPortal/getMyCapital/getCapitalSettingsMessage/postCapitalSettings）、
hooks.ts（刪 useBillingStatus）、api.test.ts（契約表同步）、pageStates.test.tsx（ROUTES 刪 4 條）。

## 已探明的雷（派工 prompt 內已標）
1. `/leaders` 送出鈕的付費閘門吃 `getBillingPlans()`——閘門必須連根拔，否則按鈕永久 disabled。
2. `.capital-field/.capital-input` 被 leaders 自訂輸入框借用——改名 `.addr-*` 遷入 leaders 區塊。
3. `pageStates.test.tsx:280`（導覽涵蓋率）與 `api.test.ts:321`（反射掃描）是刻意護欄，同步修。
4. `copy.test.ts:35` 斷言 `COPY.perf.fundsWarning`——警語若屬安全揭露需搬家而非默刪。
5. redline.test.ts 自包含（不 import leaderPerf），績效碼全刪後兩條結構測試空集合通過。

## 驗收（主對話親跑，不採信 agent 回報）
1. `cd web && npm test` 全綠。
2. `npm run build` 成功（抓孤兒 import）。
3. `grep -rn "capital\|billing\|pricing\|performance"` 前端 src 無殘留消費者（api 型別除外）。
4. Header 只剩 3 公開 tab ＋ admin tabs；四條路由 404。
