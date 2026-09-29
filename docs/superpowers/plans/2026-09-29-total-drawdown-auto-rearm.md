# 熔斷後一律冷靜期自動恢復（絕對底線／殘留暴險／冷靜期下限 2 小時）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 風控熔斷後的恢復語意統一成「要嘛不熔斷；熔斷了就等冷靜期（最少 2 小時）自動恢復跟單」——絕對底線與殘留暴險不再擋自動恢復，不再要求客戶簽章，冷靜期不可設 0。

**Architecture:** 引擎側只動 `killswitch.py` 的自動恢復函式（清單、殘留暴險、冷靜期地板）與 `config.py` 一個常數；設定側改 `risk_prefs.py` 的 spec 下限與說明；API 側拿掉「冷靜期 0」分支；前端只改文案。`trip()`、`manual_rearm()`、簽章驗證全部不動，「立即恢復跟單」按鈕保留為快速通道。

**Tech Stack:** Python 3.11 + uv、pytest（離線，autouse socket-ban）、ruff；前端 Next.js + vitest（`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test`）。

**狀態：✅ 已部署** 2026-09-29 13:15 UTC，commit `34b70e7`（Task 1–4b 六個 commit；reviewer 兩輪：第一輪 5 Warning 全修（Task 4b），第二輪 PASS＋2 Warning 已修於 `64aabf4`）。部署記錄見 `deploy/RUNBOOK.md` 部署日誌 2026-09-29 第三條。

**使用者裁決（2026-09-29）：**
1. 客戶是懶人投資法，放著不會再進來操作；絕對底線（`total_drawdown`）也比照冷靜期自動恢復，不做客戶簽章。
2. 平倉失敗或掛單未撤（殘留暴險）**也不擋**自動恢復，時間一到直接恢復。
3. 冷靜期 UI 下限改為 2 小時；語意變成「要嘛不熔斷，熔斷了冷靜期（最少 2 小時）後自動跟單」。
4. 既有的冷靜期 0 一律改 2。**主線程 2026-09-29 已查正式機**：四個 follower env 的 `COPY_RISK_COOLDOWN_HOURS` 全是 12，唯一簽章記錄（f6b3e7…）cooldown 12，**沒有 0 存在**；Task 5 部署前再驗一次即可。已在跟單的客戶由使用者自行溝通。

本 plan 推翻 killswitch.py 2026-07-30 審查 F1-B（殘留暴險擋自動恢復）與 F2（絕對底線不自動恢復）的拍板；相關「不得」字句一律改寫並留日期與理由。

**已接受的後果（plan 內明寫，實作不得再加擋）：**
- 全期高水位隨每次絕對底線熔斷重設＝棘輪：理論上每個冷靜期可再虧一次 40%。
- 殘留部位在冷靜期內無人管理；恢復後由下一輪 `sync_positions` 往 leader 目標收斂。
- 冷靜期地板 2 小時是**引擎端結構性保證**（`effective_cooldown_hours`）：不論 env 或舊記錄寫什麼，低於 2 都當 2 算；同時 spec 下限 2 讓 API／UI 不再收 0。

**仍不自動恢復（本 plan 不動）：** `leader_revoked`、`owner_close`、空 reason（營運端 panic）、ARM 時間戳讀不到、刪檔 OSError。

---

## 影響範圍盤點（2026-09-29 主線程與 scout 查過）

| 位置 | 現況 | 動作 |
|---|---|---|
| `src/spark/copytrade/config.py:115, 353-356` | `risk_cooldown_hours` 預設 12、驗證 `>= 0` | Task 1：加常數與地板函式；驗證**保持** `>= 0`（舊 env 寫 0 不能讓引擎起不來，地板在使用端） |
| `src/spark/copytrade/killswitch.py:172-181` | `_AUTO_REARM_REASONS` 不含 `total_drawdown` | Task 1 |
| `src/spark/copytrade/killswitch.py:446-516` | `auto_rearm_if_cooled_down`：`hours<=0` 不恢復、殘留暴險不恢復、不清全期高水位 | Task 1 三處都改 |
| `src/spark/copytrade/killswitch.py:1-15, 79-83, 155-160, 198-217` | docstring／註解寫舊語意 | Task 1 改字 |
| `src/spark/copytrade/equity.py:164-171`、`loop.py:175, 254-255` | 註解寫「只能簽章」 | Task 1 改字 |
| `tests/test_copy_killswitch.py:686-692, 748-765, 797-806, 809-827` | 斷言舊語意 | Task 1 改寫 |
| `src/spark/filet/risk_prefs.py:84-90` | spec `min="0"`、help 寫「設 0＝不自動恢復」 | Task 2 |
| `tests/test_risk_settings_apply.py:174-184` | 快樂路徑用 `cooldown_hours="0"` | Task 2 改 "2" |
| `src/spark/publicapi/app.py:3718-3735, 3747-3764` | `_resume_at` 對 0 回 None；`_halt_note` 有 `cooldown_h == "0"` 分支 | Task 2 拿掉、套地板 |
| `web/src/lib/copy.ts:1461-1465（zh）, 3105-3109（en）` | cooldown help「設 0＝…」 | Task 3 |
| `web/src/lib/copy.ts` `halted.noAutoResume`（zh 1477-1479；en 用 grep 找） | 「冷靜期設為 0，或目前算不出來」 | Task 3 |
| `web/src/app/settings/page.tsx:226-227` | slider min/max 來自 API specs | 不動（spec 改了就跟著變） |
| `halt_status.resumable`、`manual_rearm`、`trip`、簽章驗證 | — | 不動 |
| 正式機：三個 live follower＋`filet-api`＋`filet-dashboard` | 引擎、API、前端都有改 | Task 5，**動之前必問使用者** |

---

### Task 1: 引擎端——自動恢復清單、殘留暴險、冷靜期地板 `@inline`

**Files:**
- Modify: `src/spark/copytrade/config.py`（`risk_cooldown_hours` 欄位附近，約 108-116 行；檔尾加函式）
- Modify: `src/spark/copytrade/killswitch.py:1-15, 79-83, 155-181, 198-217, 446-516`
- Modify: `src/spark/copytrade/equity.py:164-171`、`src/spark/copytrade/loop.py:175, 254-255`
- Test: `tests/test_copy_killswitch.py`

- [ ] **Step 1: 改寫既有測試（先讓它紅）**

(a) `tests/test_copy_killswitch.py:686-692` 的 `test_cooldown_zero_means_manual_only` **整段替換**為：

```python
def test_cooldown_below_floor_behaves_as_two_hours(tmp_path):
    """⭐ 2026-09-29 使用者裁決：冷靜期不再有 0（「只有簽章才恢復」的語意作廢）。
    引擎端地板 2 小時：env／舊記錄寫 0 或 1 都當 2 算，這是結構性保證，不靠 spec 擋。
    觸發情境：舊 env `COPY_RISK_COOLDOWN_HOURS=0` 的引擎熔斷後 3 小時。"""
    at, now = _hours_ago(3)
    arm = _arm(tmp_path, tripped_at=at)
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("0")),
                                     RecordingNotifier(), now_s=now) is True
    assert not arm.exists()


def test_cooldown_below_floor_still_waits_the_floor(tmp_path):
    """對照：地板 2 小時內仍鎖著（設 0 不等於立刻恢復）。"""
    at, now = _hours_ago(1)
    arm = _arm(tmp_path, tripped_at=at)
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("0")),
                                     RecordingNotifier(), now_s=now) is False
    assert arm.exists()
```

(b) `tests/test_copy_killswitch.py:748-765` 的 `test_halt_with_unflattened_positions_never_auto_resumes` **整段替換**為：

```python
def test_halt_with_unflattened_positions_auto_resumes_and_discloses(tmp_path):
    """⭐⭐ 2026-09-29 使用者裁決（推翻 2026-07-30 F1-B）：殘留暴險**不擋**自動恢復。
    理由與 2026-07-31 自助解除同一條：恢復本身就是收拾殘局的手段，下一輪 sync_positions
    會把殘留部位往 leader 目標收斂；鎖著只會讓它無人管理。但告警必須揭露殘留。
    觸發情境：回撤熔斷 → ETH 平倉失敗 → 放著滿 12 小時，沒人進來按任何東西。"""
    at, now = _hours_ago(20)
    p = tmp_path / ARM_FILE_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tripped_at": at, "reason": "drawdown",
                             "failures": ["ETH"], "breached": True}))
    n = RecordingNotifier()
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("12")),
                                     n, now_s=now) is True
    assert not p.exists()
    assert any(r[0] == "critical" and "已自動恢復跟單" in r[2] and "殘留暴險" in r[2]
               for r in n.records)
```

(c) `tests/test_copy_killswitch.py:797-806` 的 `test_orders_not_cancelled_also_blocks_resume` **整段替換**為：

```python
def test_orders_not_cancelled_also_auto_resumes(tmp_path):
    """掛單清單沒讀到（orders_not_cancelled）同理：不擋自動恢復，告警揭露。"""
    at, now = _hours_ago(20)
    p = tmp_path / ARM_FILE_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tripped_at": at, "reason": "cost_breach",
                             "orders_not_cancelled": True}))
    n = RecordingNotifier()
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("12")),
                                     n, now_s=now) is True
    assert not p.exists()
    assert any("殘留暴險" in r[2] for r in n.records)
```

(d) `tests/test_copy_killswitch.py:809-827` 的 `test_total_drawdown_never_auto_resumes_but_owner_can_rebase` **整段替換**為：

```python
def test_total_drawdown_auto_resumes_after_cooldown_and_rebases(tmp_path):
    """⭐⭐ 2026-09-29 使用者裁決（推翻 2026-07-30 F2）：絕對底線也比照冷靜期自動恢復；
    解鎖同時清掉全期高水位＝以恢復當下的權益為新基準（不清會下一輪立刻再 trip）。
    觸發情境：客戶累虧達 40% 上限熔斷，12 小時後沒人進來按任何東西。"""
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, update_lifetime_peak
    at, now = _hours_ago(13)
    arm = _arm(tmp_path, tripped_at=at, reason="total_drawdown")
    update_lifetime_peak(tmp_path, Decimal("10000"))
    n = RecordingNotifier()

    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("12")),
                                     n, now_s=now) is True
    assert not arm.exists()
    assert not (tmp_path / LIFETIME_PEAK_RELPATH).exists(), "自動解鎖＝以目前權益為新基準"
    assert any(r[0] == "critical" and "已自動恢復跟單" in r[2] and "新的高水位基準" in r[2]
               for r in n.records)


def test_total_drawdown_within_cooldown_keeps_lock_and_peak(tmp_path):
    """冷靜期未滿：鎖與全期高水位都留著（清高水位只能發生在真的解鎖那一刻）。"""
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, update_lifetime_peak
    at, now = _hours_ago(3)
    arm = _arm(tmp_path, tripped_at=at, reason="total_drawdown")
    update_lifetime_peak(tmp_path, Decimal("10000"))
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("12")),
                                     RecordingNotifier(), now_s=now) is False
    assert arm.exists()
    assert (tmp_path / LIFETIME_PEAK_RELPATH).exists()


def test_total_drawdown_manual_rearm_still_works_and_rebases(tmp_path):
    """對照組：客戶簽章的快速通道保留、行為不變（解鎖＋清全期高水位）。"""
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, update_lifetime_peak
    at, _ = _hours_ago(1)
    arm = _arm(tmp_path, tripped_at=at, reason="total_drawdown")
    update_lifetime_peak(tmp_path, Decimal("10000"))
    n = RecordingNotifier()
    assert manual_rearm(tmp_path, n, requested_at_iso=_hours_ago(0)[0]) is True
    assert not arm.exists()
    assert not (tmp_path / LIFETIME_PEAK_RELPATH).exists()
    assert any("新的高水位基準" in r[2] for r in n.records)
```

`test_rolling_drawdown_resume_keeps_the_lifetime_peak`、`test_leader_revoked_never_auto_rearms`、`test_operator_panic_halt_never_auto_resumes`、`test_owner_can_self_resume_even_with_unflattened_positions`、`test_halt_status_discloses_residual_exposure_without_blocking` **全部保留不動**。

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest tests/test_copy_killswitch.py -k "cooldown_below_floor or unflattened_positions_auto or orders_not_cancelled_also or total_drawdown" -v`
Expected: `..._behaves_as_two_hours`、`..._auto_resumes_and_discloses`、`..._also_auto_resumes`、`..._auto_resumes_after_cooldown_and_rebases` FAIL；其餘 PASS。

- [ ] **Step 3: `config.py` 加常數與地板函式**

在 `src/spark/copytrade/config.py` 的 `risk_cooldown_hours: Decimal = Decimal("12")` 欄位（約 115 行）上方註解區**追加**：

```python
    # ⭐ 2026-09-29 使用者裁決：冷靜期**不再有 0**（「只有簽章才恢復」語意作廢）。
    # 下限由 RISK_COOLDOWN_MIN_HOURS 給，且在**使用端**（killswitch.auto_rearm_if_cooled_down）
    # 以 effective_cooldown_hours() 取地板——不在這裡把驗證改成 >= 2，因為舊 env 若還
    # 寫著 0，引擎起不來的代價（follower 整顆停掉）遠大於多等 2 小時。
```

在檔案**模組層級**（`CopySettings` class 之前、import 之後）加：

```python
# 冷靜期地板（小時）。spec 下限（filet/risk_prefs.RISK_PARAM_SPECS）與引擎地板共用這一個值。
RISK_COOLDOWN_MIN_HOURS = Decimal("2")
```

在檔案**尾端**加：

```python
def effective_cooldown_hours(settings: "CopySettings") -> Decimal:
    """引擎實際採用的冷靜期＝max(設定值, RISK_COOLDOWN_MIN_HOURS)。

    2026-09-29 使用者裁決的結構性保證：不論 env、簽章記錄或人工改檔寫了什麼，
    冷靜期最少 2 小時、且**一定會**自動恢復。所有讀 `risk_cooldown_hours` 來決定
    「何時解鎖」的地方都必須經本函式，不得直接讀欄位。
    """
    return max(settings.risk_cooldown_hours, RISK_COOLDOWN_MIN_HOURS)
```

- [ ] **Step 4: `killswitch.py` 改清單與 `auto_rearm_if_cooled_down`**

(a) import 區把 `from spark.copytrade.config import CopySettings` 改為：

```python
from spark.copytrade.config import CopySettings, effective_cooldown_hours
```

(b) `killswitch.py:172-181` 替換為：

```python
# 冷靜期屆滿可**自動**恢復的原因。
# ⚠️ 2026-09-29 使用者裁決（推翻 2026-07-30 審查 F2）：`total_drawdown` 也進本清單。
# 理由：客戶是懶人投資法，放著不會再進來簽任何東西；絕對底線若只能簽章解鎖，等於
# 把客戶永久鎖在門外。代價（已知情接受）：全期高水位隨每次絕對底線熔斷重設，
# 理論上每個冷靜期可再虧一次 40%。清高水位的動作在 `auto_rearm_if_cooled_down` 內。
_AUTO_REARM_REASONS = (REASON_ROLLING_DRAWDOWN, REASON_COST_BREACH, REASON_TOTAL_DRAWDOWN)

# 客戶**親自簽章**可以恢復的原因：自 2026-09-29 起與自動清單相同（簽章只是「不想等
# 冷靜期」的快速通道）。保留兩個名字與 `manual` 參數：呼叫端語意不同、日後若再分岔
# 只改這一行。⚠️ 兩張清單都不含 `leader_revoked`（治理動作）、`owner_close`（owner 主動
# 退出）與 `""`（營運端緊急停機）。
_MANUAL_REARM_REASONS = _AUTO_REARM_REASONS
```

(c) `auto_rearm_if_cooled_down` 函式本體（原 473-516 行）**整段替換**為：

```python
    arm_path = root / ARM_FILE_RELPATH
    if not arm_path.exists():
        return False
    # ⭐ 2026-09-29：地板 2 小時、永不為 0（見 config.effective_cooldown_hours）。
    hours = effective_cooldown_hours(settings)
    now_s = time.time() if now_s is None else now_s

    def _stay(reason_text: str, key: str) -> bool:
        notifier.warn("killswitch", f"維持鎖定：{reason_text}", dedup_key=key)
        return False

    parsed = _read_arm_payload(arm_path)
    if parsed is None:
        return _stay(
            f"ARM 檔的觸發時間無法解析（{arm_path}），不能證明冷靜期已過——"
            f"自動恢復不執行，需人工刪檔", "rearm_unparseable")
    tripped_at, reason, tripped_s, residual = parsed
    if not rearm_allowed_for(reason):
        return _stay(
            f"觸發原因為 `{reason or '未標示'}`，不屬於可自動恢復的風險事件"
            f"（leader 撤銷、營運端緊急停機等只能人工處理）", f"rearm_blocked:{reason}")
    # ⚠️ 殘留暴險（平倉失敗／掛單未撤）**不擋**自動恢復（2026-09-29 使用者裁決，
    # 推翻 2026-07-30 F1-B）：理由與 manual_rearm 的 2026-07-31 裁決同一條——恢復本身
    # 就是收拾殘局的手段，下一輪 sync_positions 會把殘留部位往 leader 目標收斂；
    # 鎖著只會讓它無人管理。差別在於現在沒有「知情的人按了按鈕」，所以告警必須揭露。

    elapsed_h = (now_s - tripped_s) / 3600
    if elapsed_h < float(hours):
        return False        # 還在冷靜期內：安靜等待（tripped 的提醒由呼叫端負責）
    try:
        arm_path.unlink()
    except OSError as e:
        notifier.critical("killswitch",
                          f"冷靜期已滿但 ARM 檔刪除失敗 {arm_path}: {e!r}"
                          f"——維持鎖定，需人工處理")
        _append_alert(root, f"自動恢復失敗（刪檔）: {e!r}")
        return False
    # ⭐ 2026-09-29：絕對底線的熔斷被冷靜期自動解除 ⇒ 以恢復當下的權益為新基準。
    # 不清的話下一輪 evaluate() 仍拿舊高水位判 total_dd > 上限 → 立刻再 trip，
    # 變成每個冷靜期平倉一次的死循環。棘輪後果見 _AUTO_REARM_REASONS 註解。
    # 只在 unlink 成功之後清：刪檔失敗＝仍鎖著，高水位必須留著。
    rebased = reason == REASON_TOTAL_DRAWDOWN
    if rebased:
        reset_lifetime_peak(root)
    msg = (f"**已自動恢復跟單**：冷靜期 {hours} 小時已滿"
           f"（觸發於 {tripped_at}，實際經過 {elapsed_h:.1f} 小時）。"
           f"權益基準已於觸發當下重置，下一輪起恢復交易動作。"
           + ("｜⚠️ 這是**絕對底線**的熔斷，已以目前權益作為新的高水位基準。"
              if rebased else "")
           + ("｜⚠️ 熔斷當下有**殘留暴險**（部位平倉失敗或掛單未撤），"
              "引擎下一輪起會把它們往 leader 目標收斂，請留意帳戶。"
              if residual else ""))
    notifier.critical("killswitch", msg)
    _append_alert(root, msg)
    return True
```

(d) `auto_rearm_if_cooled_down` 的 docstring（原 448-471 行）**整段替換**為：

```python
    """冷靜期屆滿 → 自動解除鎖定（刪 ARM 檔）。回傳是否真的解除了。

    ⭐⭐ 語意沿革：
    - 2026-07-30 使用者裁決：從「re-arm 一律人工」放寬成冷靜期自動恢復（保留客戶該有的
      權力——保護要提供，但不該把客戶鎖在門外）。
    - 2026-09-29 使用者裁決：客戶是懶人投資法，放著不會再進來操作。於是
      (1) `total_drawdown` 也自動恢復並清全期高水位；(2) 殘留暴險不擋；
      (3) 冷靜期地板 2 小時、不再有「設 0＝只有簽章才恢復」。
      語意收斂成一句：**要嘛不熔斷，熔斷了就等冷靜期（最少 2 小時）自動跟回去。**
    `trip()` 與 `manual_rearm()` 的行為不變。

    **仍不會自動恢復的情形（每一條都是刻意的 fail-closed）**：
    - `reason` 不在 `_AUTO_REARM_REASONS`（`leader_revoked`／`owner_close`／空字串）：
      治理動作或 owner 主動退出，不是等一段時間就作廢的風險事件。
    - ARM payload 讀不到、或 `tripped_at` 解析不出來：**無法證明冷靜期已過**就不恢復。
    - 刪檔失敗（OSError）：維持鎖定並 critical——鎖不掉就不該宣稱已解除。

    恢復時的基準處理：7 天滾動樣本已在 `trip()` 觸發當下清掉，恢復後不會被崩跌前的
    舊 peak 立刻再熔斷。全期高水位**只在 `reason == total_drawdown` 時清**——滾動窗與
    成本熔斷的解鎖不清，否則絕對底線會被滾動熔斷逐次重設而失去意義。
    """
```

(e) 模組 docstring `killswitch.py:6-15`（兩段「⚠️ 2026-07-30 …放寬」之後）**追加**：

```
  ⚠️ **2026-09-29 第三次放寬**（使用者裁決：客戶是懶人投資法，不會回來簽章）：
  `total_drawdown` 進 `_AUTO_REARM_REASONS` 並於解鎖時清全期高水位；殘留暴險不再擋
  自動恢復；冷靜期地板 2 小時（`config.effective_cooldown_hours`），不再有 0。
  語意收斂為「要嘛不熔斷，熔斷了就等冷靜期自動跟回去」。`manual_rearm()` 保留為
  快速通道，`trip()` 不變。
```

同一段裡原句「且它對三種情形仍然 fail-closed（leader 撤銷、時間戳讀不到、冷靜期設為 0）」改為「且它對兩種情形仍然 fail-closed（leader 撤銷、時間戳讀不到）」。

(f) `DrawdownStatus.basis` 註解（`killswitch.py:79-83`）替換為：

```python
    # ⭐ 觸發的是哪一道閘（2026-07-30）：`"rolling"`＝7 天滾動窗、`"lifetime"`＝
    # 自開始跟單以來的絕對底線。兩者解鎖時的差別（2026-09-29 起）只剩「要不要清
    # 全期高水位」：絕對底線解鎖＝以當下權益為新基準；滾動窗解鎖不動高水位。
    basis: str = "rolling"
```

(g) `_read_arm_payload` 內的殘留暴險註解（`killswitch.py:210-212`）替換為：

```python
        # ⭐ 殘留暴險（審查 F1-B）：平倉失敗的 coin，或掛單清單根本沒讀到。
        # 2026-09-29 起它**不擋**任何恢復路徑（自動／簽章都放行），只用於揭露：
        # halt_status 給前端、auto_rearm 的恢復告警都要帶上它。
```

(h) `REASON_*` 上方註解（`killswitch.py:155-160`）中「同理，回撤熔斷但**平倉失敗**（ARM payload 帶 failures、告警明寫「需人工處置」）也會照樣恢復」這一句改為「（殘留暴險自 2026-09-29 起刻意允許自動恢復，見 auto_rearm_if_cooled_down）」。

- [ ] **Step 5: 同步 `equity.py` 與 `loop.py` 註解**

`src/spark/copytrade/equity.py:165-169`（`reset_lifetime_peak` docstring）替換為：

```python
    """清除全期高水位＝**接受一個新的絕對底線基準**。

    合法呼叫點只有兩個，都在 `killswitch` 解除一次 `total_drawdown` 熔斷的當下：
    客戶簽章的 `manual_rearm()`，以及 2026-09-29 起的冷靜期自動路徑
    `auto_rearm_if_cooled_down()`。其他任何路徑（滾動回撤解鎖、營運端、trip 本身）
    **不得**呼叫——那會把絕對底線變成每次熔斷重設的棘輪。
    """
```

`src/spark/copytrade/loop.py:175`「（幻影破線；total_drawdown 一旦 trip 還要客戶簽章解鎖）」改為「（幻影破線；total_drawdown 一旦 trip 會在恢復時清全期高水位，幻影 trip 等於白白重設基準）」。

`src/spark/copytrade/loop.py:254-255` 替換為：

```python
                # ⭐ reason 必須明講是哪一道閘（審查 F1）：兩者解鎖時的差別是要不要清
                # 全期高水位（絕對底線清、滾動窗不清；2026-09-29 起兩者都走冷靜期自動恢復）。
```

`src/spark/copytrade/loop.py:89-90`（step 0 呼叫 `auto_rearm_if_cooled_down` 上方的兩行註解）替換為：<!-- 2026-09-29 主線程裁決：builder 回報此行寫「三種情形（含冷靜期設 0）」已過時，納入 Task 1 -->

```python
    # 排在 is_tripped 之前：本輪就恢復交易，而不是白等一輪。函式本身對
    # 「reason 不可恢復（leader 撤銷／owner_close／空）」「時間戳讀不到」「刪檔失敗」
    # fail-closed；殘留暴險與冷靜期 0 自 2026-09-29 起**不再**擋（見其 docstring）。
```

- [ ] **Step 6: 跑測試確認綠**

Run: `uv run pytest tests/test_copy_killswitch.py tests/test_copy_loop.py tests/test_risk_settings_apply.py tests/test_run_copytrade_wiring.py -q`
Expected: 全部 PASS，0 failed。

- [ ] **Step 7: 殘留舊說法檢查＋lint**

Run: `rg -n "冷靜期設為 0|設 0|hours <= 0|不屬於可自動恢復|絕對底線不|只能由客戶簽章|需人工確認帳戶已收乾淨" src/spark/copytrade/killswitch.py src/spark/copytrade/equity.py src/spark/copytrade/loop.py src/spark/copytrade/config.py`
Expected（<!-- 2026-09-29 主線程裁決：原「只剩一句」寫得太寬，改成白名單 -->）：**只允許**下列四類命中，其餘零命中：
1. `killswitch.py` 的 `rearm_blocked` 那句「不屬於可自動恢復的風險事件」（仍正確）。
2. `config.py` 的 `flow_decay_hours <= 0`（無關的既有程式，pattern 誤中）。
3. `killswitch.py` 模組 docstring 的「預設 0.30」（flatten_slippage，pattern 誤中）。
4. `killswitch.py` docstring 內「不再有「設 0＝只有簽章才恢復」」（否定句，本 plan 要求寫的）。
判準：任何**肯定**「冷靜期 0＝不自動恢復」「絕對底線不自動恢復」「殘留暴險擋自動恢復」的句子都不得存在。

Run: `uv run ruff check src tests scripts`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add src/spark/copytrade/config.py src/spark/copytrade/killswitch.py src/spark/copytrade/equity.py src/spark/copytrade/loop.py tests/test_copy_killswitch.py
git commit -m "feat: 熔斷後一律冷靜期自動恢復——絕對底線清高水位、殘留暴險不擋、冷靜期地板 2 小時（2026-09-29 使用者裁決）"
```

---

### Task 2: 設定側與 API——冷靜期 spec 下限 2、拿掉「冷靜期 0」分支 `@inline`

**Files:**
- Modify: `src/spark/filet/risk_prefs.py:84-90`（＋import）
- Modify: `src/spark/publicapi/app.py:3718-3735, 3747-3764`
- Test: `tests/test_risk_settings_apply.py:174-184`、`tests/test_risk_prefs.py`（新增一個測試）

- [ ] **Step 1: 寫失敗測試**

`tests/test_risk_prefs.py` 檔尾**追加**：

```python
def test_cooldown_hours_floor_is_two_and_zero_is_rejected():
    """2026-09-29 使用者裁決：冷靜期不再有 0；spec 下限與引擎地板共用同一個常數。"""
    from spark.copytrade.config import RISK_COOLDOWN_MIN_HOURS
    from spark.filet.risk_prefs import RISK_PARAM_SPECS, RiskPrefsError, canonical_prefs
    spec = next(s for s in RISK_PARAM_SPECS if s["name"] == "cooldown_hours")
    assert Decimal(spec["min"]) == RISK_COOLDOWN_MIN_HOURS == Decimal("2")
    assert canonical_prefs({"enabled": True, "cooldown_hours": "2"})["cooldown_hours"] == "2"
    with pytest.raises(RiskPrefsError) as ei:
        canonical_prefs({"enabled": True, "cooldown_hours": "0"})
    assert ei.value.reason == "cooldown_hours_out_of_range"
```

（若檔頭沒有 `from decimal import Decimal` 或 `import pytest`，補上。）

`tests/test_risk_settings_apply.py:174-184` 的 `test_applies_valid_signed_settings` 內兩處 `"0"`／`Decimal("0")` 改為 `"2"`／`Decimal("2")`（其餘不動）。

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest tests/test_risk_prefs.py::test_cooldown_hours_floor_is_two_and_zero_is_rejected tests/test_risk_settings_apply.py::test_applies_valid_signed_settings -v`
Expected: 前者 FAIL（`spec["min"]` 是 "0"，且 `"0"` 沒被拒）；後者 PASS。

- [ ] **Step 3: `risk_prefs.py` spec**

import 區加：

```python
from spark.copytrade.config import RISK_COOLDOWN_MIN_HOURS
```

（`spark.filet` 已有多處 import `spark.copytrade`，方向合法；`spark.copytrade.config` 不 import `spark.filet`，無循環。）

`risk_prefs.py:84-90` 的 `cooldown_hours` spec 替換為：

```python
    {"name": "cooldown_hours", "env": "COPY_RISK_COOLDOWN_HOURS", "type": "decimal",
     "group": "risk", "unit": "hours", "recommended": "12",
     # ⭐ 2026-09-29 使用者裁決：下限 2 小時、不再有 0（「只有簽章才恢復」語意作廢）。
     # 與引擎地板 config.RISK_COOLDOWN_MIN_HOURS 同源；引擎端另有 effective_cooldown_hours
     # 兜底，舊記錄寫 0 也當 2 算。
     "default": "12", "min": str(RISK_COOLDOWN_MIN_HOURS), "max": "168",
     "label": "熔斷後的冷靜期（小時）",
     "help": "熔斷後經過這段時間就自動恢復跟單（最少 2 小時），權益基準已在熔斷當下重置。"
             "建議 12 小時：短到不會把你鎖在門外，長到足以讓觸發熔斷的那段行情過去。"},
```

- [ ] **Step 4: `app.py` 拿掉 0 分支、套地板**

import 區加（放在 `from spark.copytrade.equity import sample_coverage` 旁）：

```python
from spark.copytrade.config import RISK_COOLDOWN_MIN_HOURS
```

`_resume_at`（`app.py:3718-3735`）替換為：

```python
    def _resume_at(tripped_at: str | None, cooldown_hours: str | None) -> str | None:
        """自動恢復的時刻＝熔斷時刻 ＋ 冷靜期；算不出來一律 None。

        `None` 的兩種來源刻意不分開（對客戶都是「沒有可顯示的自動恢復時刻」）：
        讀不到熔斷時刻、或讀不到冷靜期。⭐ 2026-09-29 起冷靜期沒有 0：引擎端以
        RISK_COOLDOWN_MIN_HOURS 為地板，這裡用同一個地板算，顯示的時刻才是真的會發生的。
        """
        if not tripped_at or cooldown_hours is None:
            return None
        try:
            hours = max(Decimal(str(cooldown_hours)), RISK_COOLDOWN_MIN_HOURS)
            base = datetime.fromisoformat(tripped_at)
        except (ValueError, TypeError, ArithmeticError):
            return None
        return (base + timedelta(hours=float(hours))).isoformat()
```

`_halt_note` 內 `if halt.get("resumable"):` 區塊（`app.py:3747-3764`）替換為：

```python
        if halt.get("resumable"):
            # ⭐ 2026-09-29 起冷靜期沒有 0（引擎地板 2 小時），不再有「不自動恢復」分支。
            # ⚠️ 兩態：None 是我們不知道他設了多久——仍然會自動恢復，只是說不出時間。
            if cooldown_h is None:
                auto = ("冷靜期屆滿後會自動恢復（目前讀不到你設定的時數）；"
                        "要立即恢復請在本頁簽署一次「立即恢復跟單」。")
            else:
                shown = max(Decimal(str(cooldown_h)), RISK_COOLDOWN_MIN_HOURS)
                auto = (f"冷靜期（{shown} 小時）屆滿後會自動恢復；"
                        f"要立即恢復請在本頁簽署一次「立即恢復跟單」。")
            base = ("你的跟單目前因**累計虧損達到你設定的絕對底線**而停止交易。"
                    if reason == "total_drawdown"
                    else "你的跟單目前因風控熔斷而停止交易。")
            return base + auto + residual
```

（`_halt_note` 的 `cooldown_h` 型別註記維持 `str | None`；`Decimal(str(...))` 若拋 `InvalidOperation` 屬 `ArithmeticError`，此處外層沒有 try——請把 `shown = ...` 那行包成 `try/except (ValueError, TypeError, ArithmeticError): shown = None`，`shown is None` 時退回 `cooldown_h is None` 那句文案。）

- [ ] **Step 5: 跑測試確認綠**

Run: `uv run pytest tests/test_risk_prefs.py tests/test_risk_settings_apply.py tests/test_api_risk_settings.py tests/test_filet_auto_activate.py -q`
Expected: 全部 PASS。

- [ ] **Step 6: 全量測試＋lint**

Run: `uv run pytest -q && uv run ruff check src tests scripts`
Expected: 全綠、`All checks passed!`。

- [ ] **Step 7: Commit**

```bash
git add src/spark/filet/risk_prefs.py src/spark/publicapi/app.py tests/test_risk_prefs.py tests/test_risk_settings_apply.py
git commit -m "feat: 冷靜期 spec 下限 2 小時、API 拿掉冷靜期 0 分支（2026-09-29 使用者裁決）"
```

---

### Task 3: 前端文案（zh/en 對稱） `@inline`

**Files:**
- Modify: `web/src/lib/copy.ts`（zh `cooldown_hours.help` 約 1461-1465；en 約 3105-3109；`halted.noAutoResume` zh 約 1477-1479 與 en 對應行，用 `rg -n "noAutoResume" web/src/lib/copy.ts` 找）

- [ ] **Step 1: zh `cooldown_hours.help`** 替換為：

```ts
        cooldown_hours: {
          label: "熔斷後的冷靜期（小時）",
          help: "熔斷後經過這段時間就自動恢復跟單（最少 2 小時），權益基準已在熔斷當下重置。"
                + "建議 12 小時：短到不會把你鎖在門外，長到足以讓觸發熔斷的那段行情過去。",
        },
```

- [ ] **Step 2: en `cooldown_hours.help`** 替換為：

```ts
        cooldown_hours: {
          label: "Cooldown after a trip (hours)",
          help: "Following resumes automatically once this much time has passed after a trip (minimum "
                + "2 hours); the equity baseline is reset at the moment it trips. Recommended 12 hours — "
                + "short enough not to lock you out, long enough for the move that triggered it to pass.",
        },
```

- [ ] **Step 3: `halted.noAutoResume`** zh 改為：

```ts
        noAutoResume:
          "目前算不出預計的自動恢復時間——引擎會在冷靜期屆滿後自動恢復；"
          + "要立即恢復請按下方按鈕。",
```

en 對應鍵改為：

```ts
        noAutoResume:
          "No estimated auto-resume time is available right now — the engine will resume on its own "
          + "once the cooldown has passed; press the button below to resume immediately.",
```

- [ ] **Step 4: 檢查零殘留＋跑前端測試**

Run: `rg -n -i "設 0|set to 0|cooldown.*= 0|冷靜期設為 0" web/src/lib/copy.ts`
Expected: 零命中。

Run: `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test -- --run 2>&1 | tail -15`
Expected: 全部 PASS（含 zh/en 結構對稱測試）。

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/copy.ts
git commit -m "docs: 冷靜期文案改為最少 2 小時、移除「設 0 不自動恢復」（zh/en）"
```

---

### Task 4: 審查（`reviewer`，opus）

輸入：`git diff <起點>..HEAD`、本 plan 檔、Task 2 Step 6 與 Task 3 Step 4 的測試輸出。
特別要盯：
1. `auto_rearm_if_cooled_down` 內 `reset_lifetime_peak` 只在 `unlink()` 成功後、且 `reason == total_drawdown` 時呼叫。
2. `manual_rearm`、`trip`、簽章驗證零改動；`halt_status.resumable` 對各 reason 的輸出與改前相同。
3. 不得有任何路徑讓 `leader_revoked`／`owner_close`／`""` 自動恢復。
4. `effective_cooldown_hours` 是唯一決定解鎖時刻的讀法（grep `risk_cooldown_hours` 在 killswitch.py 應零直接讀取）。
5. `risk_prefs` ↔ `copytrade.config` 無循環 import（`uv run python -c "import spark.filet.risk_prefs, spark.publicapi.app"` 成功）。
6. 舊簽章記錄若 cooldown 寫 0：`verify_risk_settings` 會拒（超界）→ 引擎沿用 env → env 再經地板；確認這條鏈上沒有例外未接。

---

### Task 4b: 審查修正（2026-09-29 reviewer W1–W5、S1、S3；主線程已逐條複驗屬實） `@inline`

**Files:**
- Modify: `src/spark/copytrade/killswitch.py`（`auto_rearm_if_cooled_down`、`manual_rearm`、`halt_status` 註解 ~249、模組 docstring ~14）
- Modify: `src/spark/copytrade/config.py:362`
- Modify: `scripts/run_copytrade.py:_risk_prefs_snapshot`
- Modify: `src/spark/publicapi/app.py`（`cooldown_h` 取值處 ~3878；不可自助恢復文案 ~3773）
- Modify: `web/src/lib/copy.ts`（`halted.noAutoResume` zh/en）
- Test: `tests/test_copy_killswitch.py`、`tests/test_run_copytrade_wiring.py`（若該檔已有 `_risk_prefs_snapshot` 測試就加在旁邊；沒有就加在 `tests/test_copy_killswitch.py` 檔尾）

**主線程裁決摘要：**
- W1（rebase 非原子＋清檔失敗被吞）：改成**先清高水位、驗證真的不在了、再刪 ARM**。清不掉 → critical＋維持鎖定（fail-closed）。ARM 刪不掉但高水位已清 → 仍鎖著（`is_tripped` 短路、不會 evaluate），之後解鎖時高水位從當下權益重建，結果與預期相同，可接受。`manual_rearm` 同型問題一起收斂到同一個 helper（同一種錯 ≥2 call site → 一個邊界）。
- W2：`noAutoResume` 文案改為中性，不斷言會自動恢復、不指向按鈕。
- W3：心跳快照的 `cooldown_hours` 改報引擎**實際執行**的地板值（`effective_cooldown_hours`），API 端同一個值再地板一次後供 `cooldown_hours`／`resume_at`／`note` 三處同源。
- W4：兩處過時字串改掉。W5：補 `owner_close` 與 `rearm_allowed_for` 成員測試。S1：文案去掉「熔斷時有部位未收乾淨」。S3：docstring「兩種」改「三種」。S2（API 巢狀函式測試）不做，記錄為已知缺口。

- [ ] **Step 1: 寫失敗測試（killswitch）**

`tests/test_copy_killswitch.py` 檔尾追加：

```python
# ── 2026-09-29 審查修正回歸釘（W1／W5）──────────────────────────────────
def test_owner_close_never_auto_rearms(tmp_path):
    """W5：owner 主動退出的鎖不得因冷靜期自動解開——step 0 排在 owner_close_terminal 之前，
    一旦放行就會對已收尾的帳戶重新建倉。"""
    at, now = _hours_ago(999)
    arm = _arm(tmp_path, tripped_at=at, reason="owner_close")
    n = RecordingNotifier()
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("12")),
                                     n, now_s=now) is False
    assert arm.exists()
    assert manual_rearm(tmp_path, n, requested_at_iso=_hours_ago(0)[0]) is False
    assert arm.exists()


def test_rearm_allowed_for_membership_is_pinned():
    """W5：兩張清單的成員釘死——改動任何一邊都必須有人來改這個測試。"""
    from spark.copytrade.killswitch import rearm_allowed_for
    for manual in (False, True):
        assert rearm_allowed_for("drawdown", manual=manual)
        assert rearm_allowed_for("cost_breach", manual=manual)
        assert rearm_allowed_for("total_drawdown", manual=manual)
        assert not rearm_allowed_for("leader_revoked", manual=manual)
        assert not rearm_allowed_for("owner_close", manual=manual)
        assert not rearm_allowed_for("", manual=manual)
        assert not rearm_allowed_for(None, manual=manual)
        assert not rearm_allowed_for(123, manual=manual)


def test_total_drawdown_auto_rearm_stays_locked_when_peak_cannot_be_cleared(tmp_path):
    """W1：高水位清不掉（這裡用「該路徑是目錄」讓 unlink 拋 OSError）→ 不得刪 ARM、
    不得宣稱已 rebase；否則下一輪拿舊高水位立刻再 trip，變成每個冷靜期平倉一次。"""
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH
    at, now = _hours_ago(13)
    arm = _arm(tmp_path, tripped_at=at, reason="total_drawdown")
    (tmp_path / LIFETIME_PEAK_RELPATH).mkdir(parents=True)
    n = RecordingNotifier()
    assert auto_rearm_if_cooled_down(tmp_path, _settings(risk_cooldown_hours=Decimal("12")),
                                     n, now_s=now) is False
    assert arm.exists(), "高水位清不掉就不能解鎖"
    assert any(r[0] == "critical" and "高水位" in r[2] for r in n.records)
    assert not any("已自動恢復跟單" in r[2] for r in n.records)


def test_total_drawdown_manual_rearm_stays_locked_when_peak_cannot_be_cleared(tmp_path):
    """W1 同型：簽章路徑也先清高水位再刪 ARM。"""
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH
    at, _ = _hours_ago(1)
    arm = _arm(tmp_path, tripped_at=at, reason="total_drawdown")
    (tmp_path / LIFETIME_PEAK_RELPATH).mkdir(parents=True)
    n = RecordingNotifier()
    assert manual_rearm(tmp_path, n, requested_at_iso=_hours_ago(0)[0]) is False
    assert arm.exists()
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest tests/test_copy_killswitch.py -k "owner_close_never_auto or membership_is_pinned or cannot_be_cleared" -v`
Expected: `owner_close_never_auto`、`membership_is_pinned` PASS（它們是回歸釘，現況已正確）；兩個 `cannot_be_cleared` FAIL（現況會先刪 ARM）。

- [ ] **Step 3: `killswitch.py` 收斂 rebase 成一個 helper**

在 `auto_rearm_if_cooled_down` 之前加：

```python
def _rebase_lifetime_peak_or_stay(root: Path, notifier: Notifier, *, who: str) -> bool:
    """絕對底線解鎖前的 rebase：清全期高水位並**驗證真的不在了**。回 True 才可以刪 ARM。

    ⭐ 順序是紅線（2026-09-29 審查 W1）：先清高水位、再刪 ARM。反過來（先刪 ARM）
    的兩種失敗都會變成「下一輪拿舊高水位立刻再 trip」：(a) `_unlink_quietly` 吞掉
    OSError、高水位還在；(b) 刪完 ARM 行程被殺（部署 restart 正是這個時刻）。
    先清高水位的失敗模式只剩「高水位已清、ARM 還在」：`is_tripped` 短路不會 evaluate，
    之後真的解鎖時高水位從當下權益重建——結果與預期相同。
    `reset_lifetime_peak` 絕不拋例外（見其 docstring），所以這裡用 exists() 驗證結果。
    """
    reset_lifetime_peak(root)
    if (root / LIFETIME_PEAK_RELPATH).exists():
        msg = (f"{who}：絕對底線熔斷的全期高水位檔清不掉（{root / LIFETIME_PEAK_RELPATH}）"
               f"——維持鎖定，否則下一輪會拿舊高水位立刻再熔斷；需人工處理")
        notifier.critical("killswitch", msg)
        _append_alert(root, msg)
        return False
    return True
```

import 區 `from spark.copytrade.equity import (...)` 補上 `LIFETIME_PEAK_RELPATH`。

`auto_rearm_if_cooled_down`：把「`arm_path.unlink()` 的 try 區塊」與「`rebased = ...; if rebased: reset_lifetime_peak(root)`」改成下面順序（其餘不動）：

```python
    rebased = reason == REASON_TOTAL_DRAWDOWN
    if rebased and not _rebase_lifetime_peak_or_stay(root, notifier, who="冷靜期自動恢復"):
        return False
    try:
        arm_path.unlink()
    except OSError as e:
        notifier.critical("killswitch",
                          f"冷靜期已滿但 ARM 檔刪除失敗 {arm_path}: {e!r}"
                          f"——維持鎖定，需人工處理")
        _append_alert(root, f"自動恢復失敗（刪檔）: {e!r}")
        return False
```

並把該處註解「只在 unlink 成功之後清：刪檔失敗＝仍鎖著，高水位必須留著」改為「先清高水位再刪 ARM，理由見 `_rebase_lifetime_peak_or_stay`」。

`manual_rearm`：同樣把 `rebased = reason == REASON_TOTAL_DRAWDOWN` / `if rebased: reset_lifetime_peak(root)` 移到 `arm_path.unlink()` 的 try 之前，改為：

```python
    rebased = reason == REASON_TOTAL_DRAWDOWN
    if rebased and not _rebase_lifetime_peak_or_stay(root, notifier, who="客戶簽章解除"):
        return False
```

（`manual_rearm` 其餘判定與訊息不變。）

- [ ] **Step 4: W4／S3 字串**

`src/spark/copytrade/config.py:362` 的 `"risk_cooldown_hours must be >= 0 (0=不自動恢復), "` 改為 `"risk_cooldown_hours must be >= 0 (引擎端以 RISK_COOLDOWN_MIN_HOURS 為地板), "`。

`src/spark/copytrade/killswitch.py` `halt_status` 內（~249）「# 語意：絕對底線客戶簽章可解、冷靜期不自動解。」改為「# 語意：自 2026-09-29 起 manual 與 auto 清單相同，這裡用 manual 只是語意上對應那顆按鈕。」

模組 docstring（~14）「且它對兩種情形仍然 fail-closed（leader 撤銷、時間戳讀不到）」改為「且它對三種情形仍然 fail-closed（reason 不可恢復、時間戳讀不到、刪檔／清高水位失敗）」。

- [ ] **Step 5: W3 心跳快照報地板值**

`scripts/run_copytrade.py` 的 `_risk_prefs_snapshot`：在 `for spec in RISK_PARAM_SPECS:` 迴圈結束後、`return out` 之前加：

```python
        # ⭐ 2026-09-29 審查 W3：cooldown 報**引擎實際執行**的值（地板後），
        # 否則頁面上 cooldown_hours 與由它推出的 resume_at／note 不同源（工程原則 1）。
        from spark.copytrade.config import effective_cooldown_hours
        out["cooldown_hours"] = f"{effective_cooldown_hours(settings):f}"
```

（若該函式內 `out` 的組裝方式與上面描述不同，以「迴圈後覆寫 `cooldown_hours` 鍵」為準。）測試：在 `tests/test_run_copytrade_wiring.py` 找既有 `_risk_prefs_snapshot` 測試旁追加：

```python
def test_risk_prefs_snapshot_reports_floored_cooldown():
    from decimal import Decimal
    from scripts.run_copytrade import _risk_prefs_snapshot
    from spark.copytrade.config import CopySettings
    snap = _risk_prefs_snapshot(CopySettings(risk_cooldown_hours=Decimal("0")))
    assert snap["cooldown_hours"] == "2"
    snap = _risk_prefs_snapshot(CopySettings(risk_cooldown_hours=Decimal("12")))
    assert snap["cooldown_hours"] == "12"
```

（`CopySettings(...)` 若還需要其他必填參數，照該測試檔既有的建構方式來。）

- [ ] **Step 6: W3 API 同源＋S1 文案**

`src/spark/publicapi/app.py` ~3878 `cooldown_h = (applied_prefs or {}).get("cooldown_hours")` 改為：

```python
        raw_cooldown = (applied_prefs or {}).get("cooldown_hours")
        # ⭐ 2026-09-29 審查 W3：地板一次、三處（cooldown_hours／resume_at／note）同源。
        # 舊版引擎心跳可能還帶未地板的值；讀不出數字 → None（未知），不猜。
        try:
            cooldown_h = (None if raw_cooldown is None else
                          f"{max(Decimal(str(raw_cooldown)), RISK_COOLDOWN_MIN_HOURS):f}")
        except (ValueError, TypeError, ArithmeticError):
            cooldown_h = None
```

`_resume_at`／`_halt_note` 內既有的 `max(..., RISK_COOLDOWN_MIN_HOURS)` 保留（冪等，且它們也被別處呼叫時仍安全）。

~3773 的不可自助恢復文案「（例如營運端的緊急處置，或熔斷時有部位未收乾淨）」改為「（例如營運端的緊急處置，或你已簽章平倉並撤銷）」。

- [ ] **Step 7: W2 前端文案**

`web/src/lib/copy.ts` `halted.noAutoResume` zh 改為：

```ts
        noAutoResume:
          "目前算不出預計的自動恢復時間（引擎回報的熔斷時間或冷靜期讀不到）。",
```

en 改為：

```ts
        noAutoResume:
          "No estimated auto-resume time is available right now (the engine did not report a readable "
          + "trip time or cooldown).",
```

- [ ] **Step 8: 全量驗證**

Run: `uv run pytest tests/test_copy_killswitch.py tests/test_run_copytrade_wiring.py tests/test_api_risk_settings.py tests/test_engine_health.py -q`
Expected: 全綠。

Run: `uv run pytest -q && uv run ruff check src tests scripts`
Expected: 全綠、`All checks passed!`。

Run: `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test -- --run 2>&1 | tail -6`
Expected: 全綠。

Run: `rg -n "0=不自動恢復|冷靜期不自動解|對兩種情形|部位未收乾淨" src scripts`
Expected: 零命中。

- [ ] **Step 9: Commit**

```bash
git add src/spark/copytrade/killswitch.py src/spark/copytrade/config.py scripts/run_copytrade.py src/spark/publicapi/app.py web/src/lib/copy.ts tests/test_copy_killswitch.py tests/test_run_copytrade_wiring.py
git commit -m "fix: 審查修正——rebase 先清高水位再刪 ARM、心跳冷靜期報地板值、補 owner_close 回歸釘、文案中性化"
```

---

### Task 5: 部署（**碰實盤，動之前必問使用者**）

引擎、API、前端都有改動，三個 unit 家族都要動：

0. 部署前再驗一次「沒有 0」：
   ```
   sudo sh -c 'grep -H "^COPY_RISK_COOLDOWN_HOURS=" /etc/filet/followers/*.env'
   sudo python3 -c 'import json;[print(r["account_id"][:10],r["prefs"].get("cooldown_hours")) for r in json.load(open("/var/lib/filet-exchange/risk_settings.json"))["settings"]]'
   sudo find /opt/filet/state -name killswitch.tripped
   ```
   任一顯示 0 → 先把 env 改 2（`sed -i`）再 restart；有鎖檔且冷靜期已過 → restart 後第一輪就會自動解鎖並重新建倉，先向使用者報告。
1. `rsync` 依 `deploy/RUNBOOK.md` §3 同步 `src/`（含 §3.2 的 chown 版本，勿 `chown -R`）。
2. `sudo systemctl restart filet-api`；前端依 RUNBOOK §4 rebuild + restart `filet-dashboard`。
3. 逐一 `sudo systemctl restart filet-follower@<id>`，清單用 `systemctl list-units "filet-follower@*"` 動態取（目前：`f438b3…`、`fb8c35…`、`f6b3e7…`）。
4. 驗收：每個 unit `active (running)`；`systemctl --failed` 為空；restart 後 5 分鐘內各 follower 的 `equity_samples.json` 有新樣本；`/api/me/risk` 的 specs 顯示 `cooldown_hours.min == "2"`。

---

## 自我檢查

- 需求覆蓋：裁決 1 → Task 1 Step 4(b)(c)；裁決 2 → Task 1 Step 4(c) 殘留暴險段＋測試 (b)(c)；裁決 3 → Task 1 Step 3（地板）＋Task 2 Step 3（spec min）＋Task 3（文案）；裁決 4 → 正式機已查無 0，Task 5 Step 0 再驗。
- 符號一致：`RISK_COOLDOWN_MIN_HOURS`、`effective_cooldown_hours`、`REASON_TOTAL_DRAWDOWN`、`reset_lifetime_peak`、`_AUTO_REARM_REASONS`、`_MANUAL_REARM_REASONS`、`rearm_allowed_for(reason, manual=...)`。
- 無 placeholder。
