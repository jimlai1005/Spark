"""scripts/perf_series_snapshot.py
每 12 小時的 leader 績效時間序列採集 CLI —— **append-only，資料無法回填**。

與 scripts/watchlist_snapshot.py（每日一次的資產負債切面快照）互補：那份是可覆寫的
「今天的樣子」，這份是只增不改的**歷史**。兩者都走官方 /info，但目的相反——
快照要的是最新值，序列要的是**不能有洞**。

⭐ 為什麼是 12 小時而不是每日（改這個數字之前先讀 filet/perf_series.py 檔頭）
------------------------------------------------------------------------
抓的是 `perpDay` 窗（涵蓋 24 小時、15 分鐘一點）。12 小時抓一次 ⇒ 相鄰兩次必然
重疊約 12 小時，而**重疊正是拼接時把窗內累積 PnL 重定基準的依據**。改成 24 小時
會讓重疊消失，每次拼接都斷段；改成更短只是多花幾次唯讀請求，永遠安全。
本腳本的節奏與 deploy/filet-perf-series.timer 是同一個決定的兩半。

⭐ 抓取對象＝**leader 白名單 ∪ FILET_LEADER_WATCHLIST**
------------------------------------------------------
直接沿用 `scripts.watchlist_snapshot.resolve_targets`（**import 既有函式，不複製
一份**）：兩支腳本對「該抓誰」若有兩份定義，白名單新增一個 leader 時就會出現
「快照有他、序列沒有他」的錯位，而錯位的那幾天**永遠補不回來**。

用法（systemd timer 每 12 小時執行，見 deploy/filet-perf-series.timer）：
  FILET_DATA_DIR=/var/lib/filet-api [FILET_LEADERS_PATH=...] \\
  [SPARK_NETWORK=mainnet] uv run python -m scripts.perf_series_snapshot

環境變數:
  FILET_LEADERS_PATH      leader 白名單路徑（與引擎／API／activate CLI 同一個變數）
  FILET_LEADER_WATCHLIST  逗號分隔的**額外**地址（白名單之外的研究對象）
  FILET_DATA_DIR          資料根目錄（預設 var/filet）；
                          落檔 <root>/leaderboard/perf_series/<address>.jsonl
  SPARK_NETWORK           mainnet | testnet（預設 mainnet）

行為:
  - import 階段零網路（HLGateway 只在 main() 內建）；測試注入 portfolio_fn。
  - 同一個 12 小時取樣窗重跑 → 不產生重複點（冪等鍵見 perf_series.sample_bucket）。
  - 逐地址失敗隔離：一個 leader 抓不到不連坐其他人；error_count > 0 → exit 1
    （systemd unit 顯示 failed ＝ 大聲告警）。
  - 白名單壞掉 → **不中止**（仍以 env 清單抓完），但 stderr ＋ exit 1：中止會讓
    一個手滑的編輯連帶弄丟當次所有 leader 的資料點，而序列無法回填。
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.watchlist_snapshot import resolve_targets
from spark.filet.perf_series import (SAMPLE_INTERVAL_HOURS, append_samples,
                                     build_sample, series_path_for)


def main(portfolio_fn=None, data_dir=None, now: datetime | None = None, env=None) -> None:
    """CLI 入口。portfolio_fn/data_dir/now/env 皆可注入（測試不觸網）。"""
    env = os.environ if env is None else env
    addresses, leaders_error = resolve_targets(env)
    if leaders_error:
        # 工程原則 3：擷取仍然繼續，但這件事必須同時出現在 stderr 與 exit code。
        print(f"[perf_series] {leaders_error}", file=sys.stderr)
    sampled_at = now or datetime.now(timezone.utc)
    root = Path(data_dir if data_dir is not None
                else env.get("FILET_DATA_DIR", "var/filet"))
    if portfolio_fn is None:  # 延後 import + 延後建線：import 階段零網路
        from spark.config import API_URLS
        from spark.publicapi.hl import HLGateway
        network = env.get("SPARK_NETWORK", "mainnet")
        if network not in API_URLS:
            raise SystemExit(f"unknown SPARK_NETWORK: {network!r}")
        # 唯讀 gateway＝既有的 resilience 邊界（transient 重試在那裡，這裡不再重試）。
        portfolio_fn = HLGateway(API_URLS[network]).portfolio

    errors = 0
    appended = 0
    skipped = 0
    for addr in addresses:
        try:
            sample = build_sample(addr, portfolio_fn(addr), sampled_at)
        except Exception as e:  # noqa: BLE001 — 逐地址隔離；計數上報，絕不靜默
            errors += 1
            print(f"[perf_series] {addr} 取樣失敗: {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        try:
            n = append_samples(series_path_for(root, addr), [sample])
        except OSError as e:
            # 落檔失敗是 transient（磁碟／權限），但**這一次的資料點就此永久消失**
            # ——序列無法回填，所以它與取樣失敗一樣要進 exit code，不得只 log。
            errors += 1
            print(f"[perf_series] {addr} 落檔失敗: {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        appended += n
        skipped += (1 - n)      # n ∈ {0, 1}：0 ＝ 該取樣窗已存在（冪等跳過）

    print(f"[perf_series] targets={len(addresses)} appended={appended} "
          f"idempotent_skips={skipped} errors={errors} "
          f"interval={SAMPLE_INTERVAL_HOURS}h -> {series_path_for(root, '<addr>')}",
          file=sys.stderr)
    raise SystemExit(1 if (errors or leaders_error is not None) else 0)


if __name__ == "__main__":
    main()
