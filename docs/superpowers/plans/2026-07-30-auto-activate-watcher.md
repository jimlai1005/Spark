# 2026-07-30 全自動啟用 watcher（移除人工審核）

## 使用者裁決（AskUserQuestion，2026-07-30）
1. 形態：**全自動 watcher**（特權 systemd timer 輪詢 pending，API 權限拓撲不變）。
2. 紅線 5 例外：**內部用戶自動啟用直接 COPY_LIVE_TRADING=true**——使用者明確確認解除。

## 流程
用戶：綁定（兩授權＋入金）→ verify 寫 pending → 跟單頁選 leader（簽章，API 落
leader_changes 記錄；自訂 leader 同時進 user_leaders.json registry）。
watcher（root，每分鐘）：pending ∩ 有 leader 記錄 → 建 state dir → 套範本寫
/etc/filet/followers/<id>.env（LIVE=true）→ activate()（builder/account 核對＋
白名單∪registry 准入）→ systemctl start。沒選 leader 的條目跳過（下輪再看）。

## 改動
- `scripts/filet_activate.py`：`_resolve_leader` 加 `user_leaders_path` 選參——
  給了才把 registry 併入准入集合（與引擎共用 load_user_leaders/merge_leaders，
  單一定義）；預設 None＝行為不變（人工 CLI 照舊只認白名單）。
- 新 `scripts/filet_auto_activate.py`：一次性掃描（timer 驅動）。冪等：已在 manifest
  → 補清 pending＋確保 unit 已啟（處理 crash 窗口，工程原則 2 對帳語意）。
  逐條目失敗隔離：單條 CRIT 不擋其他條目，任何失敗 exit 非零（systemd 可見）。
- 新 deploy：`filet-auto-activate.service`/`.timer`、`follower.env.autoactivate.example`
  （無 REPLACE 佔位殘留檢查——env 範本沒填好時 watcher 拒跑，fail-closed）。
- RUNBOOK 新節：安裝、驗收、回退（disable timer 即回到人工 CLI）。
- 前端：wizard step 4 文案「送出審核」→「完成綁定」；完成後導向選 leader；
  admin 頁保留（唯讀觀察用）。
- CLAUDE.md 紅線 5 追加例外條款（日期＋裁決出處）。

## 不改
- API 端點零改動（pending 寫入照舊；select 照舊）——無新攻擊面。
- followers.json 權限拓撲不變（filet-api 仍無寫權）。
- 既有 follower、停機留倉的引擎（2026-07-27 事故）不受影響——watcher 只處理新啟用。

## 驗收
1. `uv run pytest`（新測試含：正常啟用／未選 leader 跳過／registry 准入／
   非法 leader 拒絕／冪等重跑／builder 不符拒絕／範本佔位殘留拒跑）。
2. `uv run ruff check`。
3. 前端 `npm test`＋build。
4. fresh-context opus 審查 watcher＋filet_activate diff（實盤紅線審查）。
