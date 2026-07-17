"""scripts/leaderboard_snapshot.py
每日 HL leaderboard 快照 —— 為 M3 leader 選人蒐集 ≥2 個月歷史資料。

資料來源查證（2026-07-17）：
  hyperliquid-python-sdk 的 `Info`（.venv/.../hyperliquid/info.py）沒有 leaderboard 方法，
  官方 info-endpoint 文件（hyperliquid.gitbook.io）亦未記載 leaderboard 端點——leaderboard
  不是標準 Info API 的一部分。實測（`curl https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`）
  確認存在一個獨立的唯讀統計端點，與 `spark.config.CSV_BASE_URLS` 同一個 stats-data host、
  同一種 `Mainnet`/`Testnet` 路徑慣例（大寫字首）。故本模組直接以 HTTP GET 呼叫該端點，
  不經 SDK 的 Info 物件。

已驗證回應形狀（GET .../Mainnet/leaderboard，2026-07-17 實測）：
    {
      "leaderboardRows": [
        {
          "ethAddress": "0x85ecf584f25db6f146718b86d493e33c5af72052",
          "accountValue": "61909829.8215270117",
          "windowPerformances": [
            ["day",     {"pnl": "459635.9623899998", "roi": "...", "vlm": "..."}],
            ["week",    {"pnl": "...", "roi": "...", "vlm": "..."}],
            ["month",   {"pnl": "...", "roi": "...", "vlm": "..."}],
            ["allTime", {"pnl": "...", "roi": "...", "vlm": "..."}]
          ],
          "prize": 0,
          "displayName": null
        },
        ...
      ]
    }

已知限制：此端點不在官方文件內、非 SDK 涵蓋範圍，欄位可能無預告變動——若未來 schema
漂移，`snapshot_from_rows` 對缺欄位 row 大聲跳過並計數（見 skipped_count），不靜默丟棄，
供操作者從輸出察覺異常，而非任由快照悄悄失真。

用法（crontab 每日執行範例，UTC 00:10 抓前一日排行榜）：
  10 0 * * * cd /path/to/spark && SPARK_NETWORK=mainnet \\
    uv run python -m scripts.leaderboard_snapshot >> var/filet/leaderboard/cron.log 2>&1

環境變數:
  SPARK_NETWORK   mainnet | testnet（預設 mainnet）

注意:
  - import 階段零網路：fetch_leaderboard 只在 main() 內被呼叫，測試以注入的 fetcher
    取代（不觸網，見 tests/test_leaderboard_snapshot.py）。
  - 同日重跑 append_snapshot 會覆寫當日檔案（冪等；非 append-log）。
"""
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 與 spark.config.CSV_BASE_URLS 同一 host、同一大寫 Mainnet/Testnet 路徑慣例
# （本模組刻意不 import config.CSV_BASE_URLS——那是 builder_fills 路徑，語意不同；
# 這裡是獨立、未進官方文件的 leaderboard 端點，查證紀錄見模組 docstring）。
LEADERBOARD_URLS = {
    "mainnet": "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
    "testnet": "https://stats-data.hyperliquid.xyz/Testnet/leaderboard",
}
DEFAULT_OUT_DIR = Path("var/filet/leaderboard")
DEFAULT_TOP_N = 200
DEFAULT_WINDOW = "day"  # 每日快照配對「當日」窗口 pnl，累積出 M3 選人用的日序列


def fetch_leaderboard(network: str = "mainnet", *, timeout: int = 30) -> list[dict]:
    """HTTP GET stats-data leaderboard 端點，回傳原始 leaderboardRows 清單。

    只在 main() 內被呼叫——import 階段不觸網。呼叫端（或測試）可注入其他 callable
    取代本函式，signature 需維持 `(network) -> list[dict]`。"""
    if network not in LEADERBOARD_URLS:
        raise ValueError(f"unknown network: {network!r}（須為 {set(LEADERBOARD_URLS)}）")
    url = LEADERBOARD_URLS[network]
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"leaderboard 抓取失敗: {url}: {e}") from e
    return payload.get("leaderboardRows", [])


def _to_finite_decimal(field: str, raw: Any) -> Decimal:
    """Decimal 化並拒收非 finite 值。`Decimal(str("NaN"))` / `Decimal(str("Infinity"))`
    是**合法**建構、不拋例外，卻會在後續排序比較時觸發 InvalidOperation 崩潰整批——
    故在來源處就把 NaN/Inf 當「格式錯誤」擋下（raise ValueError），走同一條「大聲跳過」路徑。"""
    value = Decimal(str(raw))
    if not value.is_finite():
        raise ValueError(f"{field} 非 finite（NaN/Inf）: {raw!r}")
    return value


def _extract_window_pnl(row: dict, window: str) -> Decimal:
    """從 windowPerformances（[[name, {pnl,...}], ...] 形狀）挖出指定窗口的 pnl。
    查無該窗口 → KeyError（由呼叫端統一當「缺欄位」處理，大聲跳過並計數）。
    pnl 非 finite（NaN/Inf）→ ValueError（同上，統一跳過）。"""
    for entry in row.get("windowPerformances", []):
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == window:
            return _to_finite_decimal(f"windowPerformances[{window}].pnl", entry[1]["pnl"])
    raise KeyError(f"windowPerformances 缺 {window!r} 區間")


def snapshot_from_rows(rows: list[dict], day: date, *, window: str = DEFAULT_WINDOW,
                       top_n: int = DEFAULT_TOP_N) -> dict[str, Any]:
    """純函式：raw leaderboardRows → 正規化每日快照。不觸網、不寫檔。

    每 row 取 HL leaderboard 實際回應欄位（見模組 docstring 查證紀錄）：
    ethAddress → address、accountValue → account_value、
    windowPerformances[window].pnl → pnl。全程 Decimal 化後依 pnl 降冪排序、截前 top_n。

    缺欄位或格式錯誤的 row：大聲跳過（logger.warning）並計入回傳的 skipped_count——
    不靜默丟棄（工程原則 3）。空 rows → 回傳空 "rows" 快照，不炸。
    """
    out_rows: list[dict[str, str]] = []
    skipped = 0
    for i, row in enumerate(rows):
        try:
            address = row["ethAddress"]
            if not isinstance(address, str) or not address:
                raise ValueError(f"ethAddress 不是合法字串: {address!r}")
            account_value = _to_finite_decimal("accountValue", row["accountValue"])
            pnl = _extract_window_pnl(row, window)
        except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError) as e:
            skipped += 1
            logger.warning("leaderboard row[%d] 缺欄位或格式錯誤，跳過: %s", i, e)
            continue
        out_rows.append({
            "address": address,
            "account_value": str(account_value),
            "pnl": str(pnl),
        })

    out_rows.sort(key=lambda r: Decimal(r["pnl"]), reverse=True)
    out_rows = out_rows[:top_n]

    return {
        "day": day.isoformat(),
        "window": window,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(out_rows),
        "skipped_count": skipped,
        "rows": out_rows,
    }


def append_snapshot(out_dir: str | Path, snapshot: dict) -> Path:
    """寫 <out_dir>/<day>.json（day 取自 snapshot["day"]）。同日重跑覆寫整檔——
    冪等由「檔名=day、直接覆寫」保證，非 append-log 語意。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snapshot['day']}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    return out_path


def main(
    fetch_fn: Callable[[str], list[dict]] = fetch_leaderboard,
    out_dir: str | Path | None = None,
    network: str | None = None,
    today: date | None = None,
) -> None:
    """CLI 入口。fetch_fn/out_dir/today 皆可注入（測試用；不觸網）。
    out_dir 未指定時：FILET_DATA_DIR 設定 → <FILET_DATA_DIR>/leaderboard（M3 systemd
    共用資料根）；未設 → DEFAULT_OUT_DIR（行為與 M2 完全相同）。"""
    network = network or os.environ.get("SPARK_NETWORK", "mainnet")
    day = today or datetime.now(timezone.utc).date()
    if out_dir is None:
        data_dir = os.environ.get("FILET_DATA_DIR")
        out_dir = (Path(data_dir) / "leaderboard") if data_dir else DEFAULT_OUT_DIR

    rows = fetch_fn(network)
    snapshot = snapshot_from_rows(rows, day)
    out_path = append_snapshot(out_dir, snapshot)

    print(f"[leaderboard_snapshot] network={network} day={snapshot['day']} "
         f"rows={snapshot['row_count']} skipped={snapshot['skipped_count']} -> {out_path}",
         file=sys.stderr)
    if snapshot["skipped_count"] > 0:
        logger.warning("leaderboard_snapshot: %d row(s) skipped（缺欄位／格式錯誤，見上方 log）",
                       snapshot["skipped_count"])
    raise SystemExit(0)


if __name__ == "__main__":
    main()
