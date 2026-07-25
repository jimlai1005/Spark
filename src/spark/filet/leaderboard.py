"""src/spark/filet/leaderboard.py
Leader watchlist 每日快照（M3 leader 選人資料）。與 scripts/leaderboard_snapshot.py
的**全站 top-N** 快照互補：那份出自 stats-data 排行榜端點（未進官方文件），
這份是關注清單逐錢包的官方 /info clearinghouseState。落檔目錄也分開
（<data_dir>/leaderboard/ vs <data_dir>/leaderboard/watchlist/，定案 6）。

純函式 + 注入 state_fn（HLGateway.clearinghouse_state；transient 重試在 gateway
的 resilience 邊界，這裡不再重試）。不觸網；落檔只在 write_snapshot。

PnL 極限註記（工程原則 1）：日 PnL 由 account_value 日序列差分近似——出入金會混入
差分。快照存原始欄位（accountValue/totalMarginUsed/totalNtlPos/withdrawable/
unrealizedPnl 合計），衍生計算留給 M3 分析端做並自行對帳。"""
import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from spark.filet.leader_perf import compute_perp_performance, jsonable_performance

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ("0xfb9c52f56f03d786ad5d435aa70fe45d80569760",)  # Filet 自營 leader（mainnet 訊號源）

# 績效資料的來源標記（落進快照，讓讀端知道這些數字的 basis 是什麼）。
# 字串裡明講 perp——快照檔會被人直接打開來看，basis 不該只存在於程式碼裡。
PERF_SOURCE = "portfolio(perp windows)"


def snapshot_watchlist(state_fn: Callable[[str], dict], addresses: list[str] | tuple,
                       day: date, *,
                       portfolio_fn: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """逐地址查 clearinghouseState（＋選配 portfolio 績效）→ 正規化快照 dict。

    單一地址失敗：大聲隔離（logger.error + error 條目 + error_count），不弄丟整批
    （工程原則 3——「大聲」= 快照內可見 + log + CLI exit code，見 watchlist_snapshot）。

    `portfolio_fn`（選配，`HLGateway.portfolio`）：另外抓 **perp 窗**的績效序列並
    以 `perf` 欄位落地。⭐ 兩件事刻意分開計數：
    - `error_count`：clearinghouseState 失敗 → 這一列**完全沒有**資料。
    - `perf_error_count`：規模資料拿到了但績效沒拿到 → 這一列**部分降級**。
    合成一個計數會讓「整列沒了」與「少了績效」在告警上看起來一樣嚴重，
    營運端因此分不出該不該緊急處理。兩者都會讓 CLI exit 1（都要有人看一眼）。

    ⚠️ 績效**只取 perp 窗**（leader_perf.PERP_PERIODS）：預設窗是 spot+perp 總和
    且含 vault 餘額，而 copytrade 只鏡像 perp——用預設窗等於把客戶根本複製不到的
    報酬存進快照，之後每一個讀這份檔的人都會繼承這個錯誤。理由全文見
    `spark/filet/leader_perf.py` 檔頭閘門 1。
    """
    rows: list[dict[str, Any]] = []
    errors = 0
    perf_errors = 0
    for addr in addresses:
        try:
            state = state_fn(addr)
            ms = state["marginSummary"]
            positions = state.get("assetPositions", [])
            upnl = sum((Decimal(str(p["position"]["unrealizedPnl"])) for p in positions),
                       Decimal("0"))
            row = {
                "address": addr,
                "account_value": str(Decimal(str(ms["accountValue"]))),
                "total_margin_used": str(Decimal(str(ms["totalMarginUsed"]))),
                "total_ntl_pos": str(Decimal(str(ms["totalNtlPos"]))),
                "withdrawable": str(Decimal(str(state["withdrawable"]))),
                "unrealized_pnl": str(upnl),
                "position_count": len(positions),
            }
        except Exception as e:  # noqa: BLE001 — 逐地址隔離；計數上報，絕不靜默
            errors += 1
            logger.error("watchlist 快照 %s 失敗: %s", addr, e)
            rows.append({"address": addr, "error": f"{type(e).__name__}: {e}"})
            continue
        if portfolio_fn is not None:
            try:
                row["perf"] = {
                    "source": PERF_SOURCE,
                    "windows": {p: jsonable_performance(r) for p, r
                                in compute_perp_performance(portfolio_fn(addr)).items()},
                }
            except Exception as e:  # noqa: BLE001 — 績效失敗不連坐規模資料
                perf_errors += 1
                logger.error("watchlist 績效 %s 失敗: %s", addr, e)
                row["perf_error"] = f"{type(e).__name__}: {e}"
        rows.append(row)
    return {
        "day": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "clearinghouseState",
        # None ＝ 本次根本沒抓績效（cron 未啟用）。與「抓了但全失敗」是兩件事：
        # 前者不該讓讀端顯示「績效暫時不可用」，後者該。
        "perf_source": PERF_SOURCE if portfolio_fn is not None else None,
        "row_count": len(rows) - errors,
        "error_count": errors,
        "perf_error_count": perf_errors,
        "rows": rows,
    }


def load_latest_snapshot(out_dir: str | Path) -> dict[str, Any] | None:
    """讀 <out_dir> 下**最新一天**的快照（檔名＝ISO 日期，字典序即時序）。

    唯讀消費端用（目錄頁 /api/leaders）：cron 每日 00:10 UTC 產出，讀的一方永遠是
    「拿得到就用、拿不到就沒有統計」，**不得**因為統計缺席而讓上層功能停擺。
    故目錄不存在／無快照檔／JSON 壞／結構不符 → 回 None（不 raise），
    但一律 logger.error 留痕（工程原則 3 的「大聲」：呼叫端另需把「統計不可用」
    這件事顯式回給使用者，不可假裝統計是 0 或最新）。

    刻意**不**回退到更舊的快照：舊資料被當成今天的資料讀，比沒有資料危險。
    """
    d = Path(out_dir)
    files = sorted(p for p in d.glob("*.json") if not p.name.startswith("."))
    if not files:
        logger.error("watchlist 快照目錄無可用快照: %s", d)
        return None
    latest = files[-1]
    try:
        data = json.loads(latest.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.error("watchlist 快照讀取失敗 %s: %s", latest, e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        logger.error("watchlist 快照結構不符（缺 rows 陣列）: %s", latest)
        return None
    return data


def snapshot_rows_by_address(snapshot: dict | None) -> dict[str, dict[str, Any]]:
    """快照 → {address: row}，**只收成功列**（含 "error" 鍵的失敗列一律剔除）。

    失敗列只有 address ＋ error 兩個鍵；若讓它留在索引裡，呼叫端的
    `row.get("account_value")` 會拿到 None 卻分不出「這個 leader 沒被快照到」與
    「快照到但欄位缺」——兩者對使用者的意義相同（沒有統計），在此統一成「查無此列」。
    """
    if not snapshot:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("rows", []):
        if not isinstance(row, dict) or "error" in row:
            continue
        addr = row.get("address")
        if isinstance(addr, str):
            out[addr.lower()] = row
    return out


def write_snapshot(out_dir: str | Path, snapshot: dict) -> Path:
    """原子寫 <out_dir>/<day>.json：同目錄 tmp + os.replace（同檔系統原子）；
    同日重跑覆寫同檔＝冪等（檔名 = day）。cron 中途被殺不留半寫檔。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snapshot['day']}.json"
    tmp_path = out_dir / f".{snapshot['day']}.json.tmp"
    tmp_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)
    return out_path
