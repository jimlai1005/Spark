"""scripts/watchlist_snapshot.py
每日 leader watchlist 快照 CLI —— 為 M3 leader 選人累積逐錢包日序列（≥2 個月）。
與 scripts/leaderboard_snapshot.py（全站 top-N，stats-data）互補；本腳本走官方
/info clearinghouseState（經 HLGateway 唯讀 resilience 邊界，transient 自動重試）。

用法（systemd timer 每日執行，見 deploy/filet-leaderboard.timer）：
  FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json \\
  FILET_DATA_DIR=/var/lib/filet-api [FILET_LEADER_WATCHLIST=0x..,0x..] \\
  [SPARK_NETWORK=mainnet] uv run python -m scripts.watchlist_snapshot

⭐ 抓取對象的**單一來源＝leader 白名單**（2026-07-19 缺口修補）
--------------------------------------------------------------
原本抓取對象只來自 `FILET_LEADER_WATCHLIST`，與 `leaders.json` 白名單**沒有任何
同步機制**：管理端在白名單新增一個 leader、卻忘了一併加進那個 env，目錄頁就會
**永遠**顯示 null 統計，而且**沒有任何告警**——沒有人會發現，因為每個元件都在
正常運作。修法是讓本腳本直接讀白名單當抓取對象（單一來源優於事後對帳：對帳表
本身還要有人記得看）。env 保留為**額外**候選（尚未進白名單的研究對象），
兩者取聯集：白名單保證不漏，env 保證研究彈性。

環境變數:
  FILET_LEADERS_PATH      leader 白名單路徑（與引擎／API／activate CLI 同一個變數）。
                          ⭐ **必填、無預設、必須絕對路徑**（2026-07-20）——見下方
                          「設定錯誤與內容錯誤是兩件事」。
  FILET_LEADER_WATCHLIST  逗號分隔的**額外**地址（白名單之外的研究對象）
  FILET_DATA_DIR          資料根目錄（預設 var/filet）；落檔 <root>/leaderboard/watchlist/<day>.json
  SPARK_NETWORK           mainnet | testnet（預設 mainnet）

行為:
  - import 階段零網路（HLGateway 只在 main() 內建）；測試注入 state_fn/portfolio_fn。
  - 同日重跑覆寫同檔（冪等）；原子寫（tmp + os.replace）。
  - 逐地址失敗隔離：error 條目寫進快照；error_count > 0 → exit 1
    （systemd unit 顯示 failed = 大聲告警；快照檔仍已寫出，不丟資料）。
  - 白名單壞掉 → **不中止**（仍以 env 清單抓完並落檔），但寫進快照的
    `leaders_error` ＋ logger.error ＋ exit 1：中止會讓一個手滑的編輯連帶弄丟
    當天所有 leader 的資料點，而序列**無法回填**（今天沒抓就是永久的洞）。

⚠️ 設定錯誤與內容錯誤是兩件事，處理方式刻意相反（2026-07-20）
------------------------------------------------------------
上面那條「白名單壞掉不中止」講的是**內容**錯誤（JSON 壞了、路徑指到的檔讀不出來）
——那時我們仍知道自己該讀哪一份檔，只是那一份當下壞了，於是降級抓 env 清單。
`FILET_LEADERS_PATH` 未設是**設定**錯誤：我們根本不知道該讀哪一份檔。此時降級會靜默
抓錯對象（甚至抓成內建的 DEFAULT_WATCHLIST）而 unit 回報 SUCCESS，序列從此對著錯的
一組 leader 累積，且無法回填。所以它 raise、unit 進 failed —— 大聲失敗優於安靜地錯。
"""
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from spark.filet.leaderboard import DEFAULT_WATCHLIST, snapshot_watchlist, write_snapshot
from spark.filet.leader_resolve import require_leaders_path
from spark.filet.leaders import load_leaders
from spark.publicapi.config import normalize_address


def parse_watchlist(raw: str | None, default=DEFAULT_WATCHLIST) -> list[str]:
    """逗號分隔地址 → normalize（小寫）、去空白、去重保序。空/未設 → `default`。
    格式錯 → ValueError 整批失敗（工程原則 3：寧可 cron 告警也不靜默漏 leader）。

    `default` 可覆寫成空：`resolve_targets` 把本函式當成「**額外**候選」的解析器，
    此時「env 未設」的正確語意是「沒有額外候選」，不是「回退到內建預設」——
    後者會讓內建預設地址永遠混進抓取對象，即使白名單早已取代它。
    """
    if not raw or not raw.strip():
        return list(default)
    seen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        addr = normalize_address(part)  # 壞地址在此大聲 ValueError
        if addr not in seen:
            seen.append(addr)
    if not seen:
        return list(default)
    return seen


def resolve_targets(env) -> tuple[list[str], str | None]:
    """抓取對象 = **leader 白名單** ∪ `FILET_LEADER_WATCHLIST`。回 `(addresses, error)`。

    ⭐ 白名單擺在前面且**全收**（含 `enabled=False` 與 `accepting_new=False`）：
    治理旗標決定的是「客戶能不能選他」，不是「我們要不要繼續記錄他」。已撤銷的
    leader 之後可能被重新啟用，而序列一旦斷了就**永遠補不回來**（HL 的 day 窗只
    保留 24 小時）——為了省一次 API 呼叫而在資料上開一個不可回填的洞不划算。

    白名單載入失敗 → 回 `(env 清單, 錯誤訊息)`：呼叫端負責大聲告警並 exit 1，
    但**不中止當天的擷取**（見檔頭「行為」最後一條）。

    ⚠️ 但 `FILET_LEADERS_PATH` **未設**不走那條軟失敗路徑，直接 raise（檔頭「設定錯誤
    與內容錯誤是兩件事」）：讀不到指定的那份檔可以降級，不知道該讀哪份檔不行。
    """
    error = None
    whitelisted: list[str] = []
    path = require_leaders_path(env)
    try:
        # 檔案不存在 → load_leaders 回 []（尚未策劃任何 leader 是合法狀態）
        whitelisted = [ref.address for ref in load_leaders(path)]
    except (ValueError, OSError) as e:
        error = f"leader 白名單載入失敗 {path}: {e}"
    extra = parse_watchlist(env.get("FILET_LEADER_WATCHLIST"), default=())
    out: list[str] = []
    for addr in [*whitelisted, *extra]:      # 白名單優先、去重保序
        if addr not in out:
            out.append(addr)
    # 兩邊都空（白名單未建立且 env 未設）→ 沿用內建預設，行為與修補前一致。
    return (out or list(DEFAULT_WATCHLIST)), error


def main(state_fn=None, out_dir=None, today: date | None = None, env=None,
         portfolio_fn=None) -> None:
    """CLI 入口。state_fn/portfolio_fn/out_dir/today/env 皆可注入（測試不觸網）。"""
    env = os.environ if env is None else env
    addresses, leaders_error = resolve_targets(env)
    if leaders_error:
        # 工程原則 3：擷取仍然繼續，但這件事必須同時出現在 log、快照檔與 exit code。
        print(f"[watchlist_snapshot] {leaders_error}", file=sys.stderr)
    day = today or datetime.now(timezone.utc).date()
    if out_dir is None:
        out_dir = Path(env.get("FILET_DATA_DIR", "var/filet")) / "leaderboard" / "watchlist"
    if state_fn is None:  # 延後 import + 延後建線：import 階段零網路
        from spark.config import API_URLS
        from spark.publicapi.hl import HLGateway
        network = env.get("SPARK_NETWORK", "mainnet")
        if network not in API_URLS:
            raise SystemExit(f"unknown SPARK_NETWORK: {network!r}")
        gw = HLGateway(API_URLS[network])
        state_fn = gw.clearinghouse_state
        # 同一個 gateway 兩個唯讀方法：規模與績效走同一條 resilience 邊界
        # （工程原則 5），不另開第二條連線路徑。
        if portfolio_fn is None:
            portfolio_fn = gw.portfolio

    snapshot = snapshot_watchlist(state_fn, addresses, day, portfolio_fn=portfolio_fn)
    if leaders_error:
        snapshot["leaders_error"] = leaders_error
    out_path = write_snapshot(out_dir, snapshot)
    print(f"[watchlist_snapshot] day={snapshot['day']} targets={len(addresses)} "
          f"ok={snapshot['row_count']} errors={snapshot['error_count']} "
          f"perf_errors={snapshot['perf_error_count']} -> {out_path}", file=sys.stderr)
    failed = (snapshot["error_count"] or snapshot["perf_error_count"]
              or leaders_error is not None)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
