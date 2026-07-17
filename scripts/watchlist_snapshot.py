"""scripts/watchlist_snapshot.py
每日 leader watchlist 快照 CLI —— 為 M3 leader 選人累積逐錢包日序列（≥2 個月）。
與 scripts/leaderboard_snapshot.py（全站 top-N，stats-data）互補；本腳本走官方
/info clearinghouseState（經 HLGateway 唯讀 resilience 邊界，transient 自動重試）。

用法（systemd timer 每日執行，見 deploy/filet-leaderboard.timer）：
  FILET_DATA_DIR=/var/lib/filet-api [FILET_LEADER_WATCHLIST=0x..,0x..] \\
  [SPARK_NETWORK=mainnet] uv run python -m scripts.watchlist_snapshot

環境變數:
  FILET_LEADER_WATCHLIST  逗號分隔 leader 地址；未設時用內建預設（M1 leader）
  FILET_DATA_DIR          資料根目錄（預設 var/filet）；落檔 <root>/leaderboard/watchlist/<day>.json
  SPARK_NETWORK           mainnet | testnet（預設 mainnet）

行為:
  - import 階段零網路（HLGateway 只在 main() 內建）；測試注入 state_fn。
  - 同日重跑覆寫同檔（冪等）；原子寫（tmp + os.replace）。
  - 逐地址失敗隔離：error 條目寫進快照；error_count > 0 → exit 1
    （systemd unit 顯示 failed = 大聲告警；快照檔仍已寫出，不丟資料）。
"""
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from spark.filet.leaderboard import DEFAULT_WATCHLIST, snapshot_watchlist, write_snapshot
from spark.publicapi.config import normalize_address


def parse_watchlist(raw: str | None) -> list[str]:
    """逗號分隔地址 → normalize（小寫）、去空白、去重保序。空/未設 → 預設清單。
    格式錯 → ValueError 整批失敗（工程原則 3：寧可 cron 告警也不靜默漏 leader）。"""
    if not raw or not raw.strip():
        return list(DEFAULT_WATCHLIST)
    seen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        addr = normalize_address(part)  # 壞地址在此大聲 ValueError
        if addr not in seen:
            seen.append(addr)
    if not seen:
        return list(DEFAULT_WATCHLIST)
    return seen


def main(state_fn=None, out_dir=None, today: date | None = None, env=None) -> None:
    """CLI 入口。state_fn/out_dir/today/env 皆可注入（測試不觸網）。"""
    env = os.environ if env is None else env
    addresses = parse_watchlist(env.get("FILET_LEADER_WATCHLIST"))
    day = today or datetime.now(timezone.utc).date()
    if out_dir is None:
        out_dir = Path(env.get("FILET_DATA_DIR", "var/filet")) / "leaderboard" / "watchlist"
    if state_fn is None:  # 延後 import + 延後建線：import 階段零網路
        from spark.config import API_URLS
        from spark.publicapi.hl import HLGateway
        network = env.get("SPARK_NETWORK", "mainnet")
        if network not in API_URLS:
            raise SystemExit(f"unknown SPARK_NETWORK: {network!r}")
        state_fn = HLGateway(API_URLS[network]).clearinghouse_state

    snapshot = snapshot_watchlist(state_fn, addresses, day)
    out_path = write_snapshot(out_dir, snapshot)
    print(f"[watchlist_snapshot] day={snapshot['day']} ok={snapshot['row_count']} "
          f"errors={snapshot['error_count']} -> {out_path}", file=sys.stderr)
    raise SystemExit(0 if snapshot["error_count"] == 0 else 1)


if __name__ == "__main__":
    main()
