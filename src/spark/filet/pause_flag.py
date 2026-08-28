"""src/spark/filet/pause_flag.py
owner 暫停旗標（Task 15 kill switch 第一級）——路徑慣例、寫入、雙側讀取。

路徑：`FILET_EXCHANGE_DIR/<user_address>/pause.json`（**不是** account_id，
與換 leader／資金／風控設定的三個檔不同——那三個錨在交換目錄根、以 account_id
區分記錄；本旗標刻意每個地址一個子目錄，因為它不是一筆需要驗章的「意圖記錄」，
只是一個由已登入 session 直接寫入的布林狀態，per-address 子目錄讓檔案系統權限
與稽核都更直覺）。**路徑推導只有這一個函式**（`pause_flag_path_for`）：
`src/spark/publicapi/app.py` 的寫端（`POST /api/me/pause`）與引擎的讀端
（`scripts/run_copytrade.py` 經本檔 `read_pause_flag_for_engine`）都呼叫它，
兩端不可能各拼一份路徑而漂移（工程原則 1，同 leader_changes_path_for 的既有模式）。

⭐⭐ 兩側的失敗方向刻意不同（都寫在各自函式的 docstring）：
- 引擎側（`read_pause_flag_for_engine`）：IO/格式失敗 → **視為 paused** ＋ critical
  （fail-safe 朝「少動作」，plan 不變量 7：「讀不到 ≠ 進入危險態」的鏡像——這裡反過來，
  讀不到暫停旗標時危險的方向是**繼續開倉**，所以 fail 的方向是暫停，不是放行）。
- API 顯示側（`publicapi/app.py::_read_pause_flag`，Task 13 既有）：IO/格式失敗 →
  回 `unknown`，由呼叫端另外判斷 `state`，不在這裡假裝「未暫停」——這是純顯示層，
  沒有 fail-safe 動作可做，硬套引擎那套會讓面板在讀檔異常時謊報「暫停中」。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def pause_flag_path_for(exchange_dir: str, user_address: str) -> str:
    """暫停旗標的路徑（寫端與讀端的單一定義）。"""
    return str(Path(exchange_dir) / user_address / "pause.json")


def write_pause_flag(path: str, *, paused: bool, by: str = "owner",
                     now_s: float | None = None) -> None:
    """原子落檔 `{"paused": bool, "ts": epoch 秒, "by": "owner"}`。

    `by` 恆為 `"owner"`（本旗標目前只有一條寫入路徑：登入使用者自己按按鈕）；
    保留欄位是為了與換 leader／風控設定等記錄格式一致（稽核時一眼看出誰動的），
    也讓未來若加入營運端緊急暫停時不必改格式。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"paused": paused, "ts": now_s if now_s is not None else time.time(),
              "by": by}
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, p)


def read_pause_flag_for_engine(exchange_dir: str, user_address: str, notifier) -> bool:
    """引擎每輪呼叫：本輪是否該視為暫停。**絕不 raise**。

    - 檔案不存在 → `False`（正常狀態：客戶從未暫停過）。
    - 讀取或解析失敗（OSError／JSON 壞掉／頂層不是 dict）→ `True` ＋ critical
      （fail-safe：讀不到旗標時，繼續正常開倉的風險大於誤判成暫停——誤判的代價
      是客戶少開幾筆新倉，讀不到卻繼續開倉的代價是客戶明明按了暫停卻沒生效）。
    """
    p = Path(pause_flag_path_for(exchange_dir, user_address))
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        notifier.critical(
            "pause",
            f"**暫停旗標讀取失敗**（{p}: {e!r}）——本輪**視為已暫停**（fail-safe），"
            f"跳過新開倉與加倉直到旗標可讀；已放行的減倉/平倉與既有風控動作不受影響",
            dedup_key="pause_flag_unreadable")
        return True
    if not isinstance(data, dict):
        notifier.critical(
            "pause",
            f"**暫停旗標格式不合法**（{p} 頂層非物件）——本輪視為已暫停（fail-safe）",
            dedup_key="pause_flag_malformed")
        return True
    return bool(data.get("paused"))
