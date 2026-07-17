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

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ("0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1",)  # M1 leader


def snapshot_watchlist(state_fn: Callable[[str], dict], addresses: list[str] | tuple,
                       day: date) -> dict[str, Any]:
    """逐地址查 clearinghouseState → 正規化快照 dict（Decimal 過手後以 str 落地）。
    單一地址失敗：大聲隔離（logger.error + error 條目 + error_count），不弄丟整批
    （工程原則 3——「大聲」= 快照內可見 + log + CLI exit code，見 watchlist_snapshot）。"""
    rows: list[dict[str, Any]] = []
    errors = 0
    for addr in addresses:
        try:
            state = state_fn(addr)
            ms = state["marginSummary"]
            positions = state.get("assetPositions", [])
            upnl = sum((Decimal(str(p["position"]["unrealizedPnl"])) for p in positions),
                       Decimal("0"))
            rows.append({
                "address": addr,
                "account_value": str(Decimal(str(ms["accountValue"]))),
                "total_margin_used": str(Decimal(str(ms["totalMarginUsed"]))),
                "total_ntl_pos": str(Decimal(str(ms["totalNtlPos"]))),
                "withdrawable": str(Decimal(str(state["withdrawable"]))),
                "unrealized_pnl": str(upnl),
                "position_count": len(positions),
            })
        except Exception as e:  # noqa: BLE001 — 逐地址隔離；計數上報，絕不靜默
            errors += 1
            logger.error("watchlist 快照 %s 失敗: %s", addr, e)
            rows.append({"address": addr, "error": f"{type(e).__name__}: {e}"})
    return {
        "day": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "clearinghouseState",
        "row_count": len(rows) - errors,
        "error_count": errors,
        "rows": rows,
    }


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
