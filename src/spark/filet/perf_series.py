"""src/spark/filet/perf_series.py
Leader 績效**時間序列**的自建採集與拼接——append-only，跨窗指標的唯一可信來源。

⭐ 為什麼非做不可，而且非**現在**做不可（本模組唯一的存在理由）
--------------------------------------------------------------
`portfolio()` 的 `perpAllTime` 窗是**降採樣**的（對長帳戶可能是雙週一點）。用它算
長期 MDD／Sharpe 會得到一個看起來精確、實際上把絕大多數回撤走勢直接跳過的數字
——而那個數字會被印在客戶選 leader 的畫面上。唯一的解是自己按固定節奏抓
**高解析度的短窗**（`perpDay`，15 分鐘一點）並拼接成長序列。

而這件事**無法回填**：`perpDay` 只保留 24 小時，今天沒抓的點明天就永遠不存在了。
「90 天後才開始採集」的意思是「90 天後仍然沒有 90 天的資料」。這是本專案唯一一項
「延後就永久損失」的工作，所以它的失效模式一律偏向**多留一點資料**：
- 白名單載入壞掉 → 仍以既有清單抓完（沿 watchlist_snapshot 的既有決定）。
- 單一 leader 失敗 → 隔離計數，不連坐其他 leader。
- 檔案裡有一行壞掉 → **跳過那一行**，其餘數月的資料照常可用（見 `load_records`）。

⭐ append-only：檔案只增不改
----------------------------
本模組**沒有**任何重寫既有內容的路徑（沒有 tmp+replace、沒有 truncate）。理由與
快照檔不同：快照是「今天的切面」，重跑覆寫同一天是正確的；序列是**歷史**，任何
「重寫整檔」的操作都有機會用一次壞掉的執行把數月的採集成果換掉，而那個損失無法
回填。代價是壞行不能被清掉——所以讀端必須容忍壞行，這是刻意的取捨。

⭐ 每筆帶取樣時刻（`sampled_at` ＋ `bucket`）
--------------------------------------------
沒有時刻的序列無法對齊：拼接要靠時間戳判斷「這兩個窗有沒有重疊」，冪等要靠
「這個取樣窗是不是抓過了」。兩者都不能靠檔案的行序或 mtime 推測。
- `sampled_at`：實際發出請求的 UTC 時刻（稽核與診斷用）。
- `bucket`：`sampled_at` 向下取整到 12 小時窗（`sample_bucket`）＝**冪等鍵**。
  同一個 12 小時窗重跑幾次都只會留一筆（timer 補跑、手動重跑、部署重啟皆然）。

⭐⭐ 拼接時 `pnlHistory` 必須**重定基準**（工程原則 1：同源、同基準）
------------------------------------------------------------------
`accountValueHistory` 是絕對量，跨窗直接接得起來；`pnlHistory` 卻是**窗內累積**
——每個 `perpDay` 窗的 PnL 從該窗起點重新起算。把兩個窗的 pnl 值直接串起來，
接縫處會憑空出現一個等於「前一窗全部 PnL」的假跳空，而 `leader_perf` 會把它當成
一段真實的分段報酬算進複利裡。

修法是用**重疊區的同一個時間戳**求偏移量：12 小時抓一次、每個窗涵蓋 24 小時，
相鄰兩次取樣必然重疊約 12 小時。在共同時間戳 `t*` 上，前段報 `p_seg(t*)`、
新窗報 `p_new(t*)`，偏移量 `= p_seg(t*) − p_new(t*)`——兩側取自**同一時刻**，
所以這個偏移量是精確的，不是估計。同一個 `t*` 上兩側的 `accountValue` 也必須相等
（同源不變量），不相等代表兩個窗根本不是同一個帳戶的同一段歷史，此時**斷開分段**
而不是硬接（見 `splice_segments`）。

⚠️ 沒有重疊時**不接**（誠實標註的極限）
--------------------------------------
漏抓一次（服務停機、timer 沒補跑）會讓兩個窗之間出現無重疊的缺口。缺口期間的
`ΔAV` 已知，但「其中多少是交易損益、多少是出入金」**無法從資料還原**——這是真正
的資訊損失，不是可以靠假設補起來的東西。所以 `splice_segments` 在此**開一個新分段**
（回傳多個 `Segment`），讓呼叫端結構上不可能把跨缺口的兩段複利乘在一起。
「分段數 > 1」本身就是「這裡漏抓過」的可見證據。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from spark.filet.leader_perf import extract_window

logger = logging.getLogger(__name__)

# 落檔 schema 版本（寫進每一筆）。append-only 的檔案會同時存在新舊格式的行，
# 讀端要能分辨——這正是「不能重寫整檔」換來的義務。
SCHEMA = "filet.perf_series.v1"

# ⭐ 採集的是**高解析度短窗**。perpAllTime 是降採樣的，抓它等於把本模組要解決的
# 問題原樣存進檔案裡。`extract_window` 只收 PERP_PERIODS，非 perp 窗會直接 ValueError
# （leader_perf 檔頭閘門 1）——所以「不小心抓成含 spot 與 vault 的預設窗」寫不出來。
SAMPLE_PERIOD = "perpDay"

# 取樣節奏（小時）。`perpDay` 涵蓋 24 小時，用 12 小時的節奏抓 ⇒ 相鄰兩次必然重疊
# 約 12 小時，而重疊正是 pnl 重定基準的依據（見檔頭）。改小仍安全（重疊更多）；
# **改大到 ≥ 24 就會讓重疊消失**，拼接從此每次都斷段——這個常數與 deploy 的
# filet-perf-series.timer 是同一個決定的兩半。
SAMPLE_INTERVAL_HOURS = 12

# 拼接後餵給 leader_perf 的期別。拼出來的序列涵蓋數月，語意上就是「全部歷史」，
# 且 leader_perf 只接受 PERP_PERIODS 內的字串。
SPLICE_PERIOD = "perpAllTime"


def series_dir_for(data_dir: str | Path) -> Path:
    """序列落點目錄（**寫端與讀端的單一定義**，沿 leader_changes_path_for 的慣例）。

    與每日快照分開目錄（`leaderboard/watchlist/` vs `leaderboard/perf_series/`）：
    快照是「可覆寫的當日切面」、序列是「只增不改的歷史」，兩種生命週期的檔案混在
    同一個目錄裡，遲早有人寫一支清理腳本按 mtime 掃全目錄。
    """
    return Path(data_dir) / "leaderboard" / "perf_series"


def series_path_for(data_dir: str | Path, address: str) -> Path:
    """**逐 leader 一個檔**。單一大檔的兩個缺點在這裡都是真的損失：
    (1) 一個 leader 的壞行會拖慢所有 leader 的讀取；(2) 每次 append 的冪等檢查都要
    掃全部歷史。位址已由呼叫端 normalize（0x + 40 hex 小寫），不含路徑分隔符。
    """
    return series_dir_for(data_dir) / f"{address.lower()}.jsonl"


def sample_bucket(dt: datetime) -> str:
    """取樣窗識別碼＝UTC 日期 ＋ 12 小時整點（**向下取整**）。例：`2026-07-19T12`。

    ⭐ 這是冪等的**唯一**載體：同一個窗內跑幾次（timer 補跑、手動重跑、部署後重啟）
    都得到同一個鍵，`append_samples` 據此丟掉重複。刻意用「向下取整的時間窗」而不是
    「距上一筆超過 N 小時」——後者依賴讀出上一筆的時刻，而讀失敗時它會靜默退化成
    「每次都寫一筆」。時間窗只依賴當下的時鐘，算得出來就一定正確。

    naive datetime 一律拒絕：把它當 UTC 是一個看不見的假設，機器時區一換，
    bucket 就會整體位移，同一個窗被寫成兩筆（沿 leader_change.parse_issued_at）。
    """
    if not isinstance(dt, datetime):
        raise TypeError(f"sample_bucket 需要 datetime: {dt!r}")
    if dt.tzinfo is None:
        raise ValueError("sample_bucket 需要帶時區的時間（UTC）——naive 時間會讓 "
                         "bucket 隨機器時區位移，同一個取樣窗被寫成兩筆")
    d = dt.astimezone(timezone.utc)
    hour = (d.hour // SAMPLE_INTERVAL_HOURS) * SAMPLE_INTERVAL_HOURS
    return f"{d.date().isoformat()}T{hour:02d}"


def build_sample(address: str, portfolio_rows: Any, sampled_at: datetime) -> dict:
    """一次 `portfolio()` 回應 → 一筆序列記錄（**純函式，不落檔、不觸網**）。

    ⭐ `points` 是 **(ts_ms, accountValue, pnl) 三元組**，不是兩個平行陣列。
    `leader_perf.compute_window_performance` 對「兩序列必須同一組時間戳」有一道
    執行期檢查，理由是配錯了會讓每個數字看起來都正常（見該模組行 197-202）。
    存成三元組讓那個錯誤在**落檔格式這一層**就寫不出來：沒有兩個可以各自錯位的陣列。

    形狀不符／窗缺席 → `ValueError`（semantic：重試同一份回應必定再次失敗）。
    呼叫端逐 leader 隔離，不連坐其他 leader。
    """
    window = extract_window(portfolio_rows, SAMPLE_PERIOD)
    if window is None:
        raise ValueError(f"portfolio 回應缺少 {SAMPLE_PERIOD} 窗或形狀不符")
    av, pnl = window
    if len(av) != len(pnl) or any(a[0] != p[0] for a, p in zip(av, pnl)):
        raise ValueError(
            f"{SAMPLE_PERIOD} 窗的 accountValueHistory 與 pnlHistory 時間戳不同步"
            f"（len={len(av)}/{len(pnl)}）——兩序列非同源，拒絕落檔")
    if not av:
        raise ValueError(f"{SAMPLE_PERIOD} 窗沒有任何取樣點")
    d = sampled_at.astimezone(timezone.utc)
    return {
        "schema": SCHEMA,
        "address": address.lower(),
        "period": SAMPLE_PERIOD,
        "bucket": sample_bucket(d),
        "sampled_at": d.isoformat(),
        "points": [[a[0], str(a[1]), str(p[1])] for a, p in zip(av, pnl)],
    }


def _valid_record(rec: Any) -> bool:
    """一筆記錄是否可用。**不修復、不猜測**：形狀不對就整筆跳過。

    容忍壞行是為了「一行壞掉不該讓數月資料不可用」，不是為了讓半殘的資料混進
    拼接——半殘的點會靜默算出錯誤的報酬率，比少一筆資料危險得多。
    """
    if not isinstance(rec, dict):
        return False
    for k in ("address", "bucket", "sampled_at"):
        if not isinstance(rec.get(k), str) or not rec[k]:
            return False
    pts = rec.get("points")
    if not isinstance(pts, list) or not pts:
        return False
    for pt in pts:
        if not (isinstance(pt, (list, tuple)) and len(pt) == 3):
            return False
        ts, a, p = pt
        if isinstance(ts, bool) or not isinstance(ts, int):
            return False
        try:
            if not (Decimal(str(a)).is_finite() and Decimal(str(p)).is_finite()):
                return False
        except (InvalidOperation, ValueError, TypeError):
            return False
    return True


def load_records(path: str | Path) -> tuple[list[dict], int]:
    """讀序列檔 → `(可用記錄, 被跳過的行數)`。檔案不存在 → `([], 0)`。

    ⭐⭐ **壞行跳過、不 raise**（本函式最重要的性質）。append-only 檔案沒有「修檔」
    這條路：磁碟寫到一半斷電、一行被外部工具截斷、schema 漂移出一種讀不懂的行——
    任一種都可能發生，而讓整檔失效的代價是**數月不可回填的資料一起不可用**，
    遠大於少算一個點。這與 `leader_change_apply.load_ledger` 對壞檔 fail-closed 的
    決定方向相反，是刻意的：帳本壞掉會導致**無授權的曝險變更**（安全性質），
    序列壞掉只會讓一個統計數字少一個點（可用性性質）。

    被跳過的行**一律計數並回傳**（工程原則 3：不靜默吞掉）。呼叫端負責把它變成
    可見的訊號；靜靜跳過壞行等於讓一個持續寫壞資料的 bug 永遠沒有人發現。

    以 `errors="replace"` 開檔：損壞的位元組只會讓**那一行**解不出 JSON 而被跳過，
    不會讓整個檔案的迭代在中途拋 UnicodeDecodeError（那等於整檔失效）。
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    records: list[dict] = []
    skipped = 0
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                logger.error("績效序列 %s 第 %d 行不是合法 JSON —— 跳過該行"
                             "（append-only：不重寫檔案）", p, lineno)
                continue
            if not _valid_record(rec):
                skipped += 1
                logger.error("績效序列 %s 第 %d 行結構不符 —— 跳過該行", p, lineno)
                continue
            records.append(rec)
    return records, skipped


def append_samples(path: str | Path, samples: Iterable[dict]) -> int:
    """把記錄**附加**到序列檔尾；回傳實際寫入的筆數。已存在的取樣窗自動跳過。

    ⭐ 冪等鍵＝`(address, bucket)`。同一個 12 小時窗重跑幾次都只會留一筆。
    這是「檢查後寫入」而非原子操作——採集只有一個 systemd timer 這一個寫入者，
    沒有並發寫的情境；真要並發，重複的代價也只是同一個窗多一筆（拼接時同時間戳
    會被去重），遠低於為此引入鎖檔的複雜度。

    ⭐ 寫入用 `O_APPEND` ＋ **單次 write** ＋ fsync，全程沒有任何重寫既有內容的路徑
    （沒有 tmp+replace、沒有 truncate）。快照檔用 tmp+replace 是對的（那是可覆寫的
    當日切面），序列用它就錯了：一次壞掉的執行會用半截內容換掉數月的歷史，而歷史
    **無法回填**。O_APPEND 保證每次 write 落在當下的檔尾，不會覆蓋任何既有位元組。
    """
    p = Path(path)
    existing, _ = load_records(p)
    seen = {(r["address"], r["bucket"]) for r in existing}
    fresh: list[dict] = []
    for s in samples:
        key = (s["address"], s["bucket"])
        if key in seen:
            logger.info("績效序列：%s 的取樣窗 %s 已存在，跳過（冪等）", *key)
            continue
        seen.add(key)
        fresh.append(s)
    if not fresh:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(json.dumps(s, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for s in fresh)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, blob.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(fresh)


@dataclass(frozen=True)
class Segment:
    """一段**連續**（重疊可考證）的拼接序列。`points` 為 (ts_ms, accountValue, pnl)。

    `pnl` 已重定基準成整段連續累積（見檔頭）；`accountValue` 原樣（本來就是絕對量）。
    """

    points: tuple[tuple[int, Decimal, Decimal], ...]

    @property
    def first_ts_ms(self) -> int:
        return self.points[0][0]

    @property
    def last_ts_ms(self) -> int:
        return self.points[-1][0]

    @property
    def covered_days(self) -> Decimal:
        return Decimal(self.last_ts_ms - self.first_ts_ms) / Decimal("86400000")

    def portfolio_rows(self, period: str = SPLICE_PERIOD) -> list:
        """拼接結果 → `leader_perf` 吃得下的 `portfolio()` 版型。

        ⭐ 這是本模組與 `leader_perf` 的**唯一**接點：拼接不自己算任何指標
        （TWR／MDD／年化全部留在 leader_perf）。兩套計算是兩套會漂移的定義，而
        leader_perf 那套已經釘著分級揭露、權益指數基準、年化硬門檻等一整組保證。
        """
        return [[period, {
            "accountValueHistory": [[ts, str(av)] for ts, av, _ in self.points],
            "pnlHistory": [[ts, str(pnl)] for ts, _, pnl in self.points],
        }]]


def _record_points(rec: dict) -> list[tuple[int, Decimal, Decimal]]:
    """一筆記錄的點，依 ts 遞增排序、同 ts 去重（保留最後一個）。"""
    by_ts: dict[int, tuple[Decimal, Decimal]] = {}
    for ts, av, pnl in rec["points"]:
        by_ts[int(ts)] = (Decimal(str(av)), Decimal(str(pnl)))
    return [(ts, *by_ts[ts]) for ts in sorted(by_ts)]


def splice_segments(records: Sequence[dict]) -> list[Segment]:
    """多筆 `perpDay` 記錄 → 連續分段清單（依時間遞增）。

    演算法（完整理由見檔頭「pnl 必須重定基準」與「沒有重疊時不接」）：
    1. 記錄依 `sampled_at` 排序。
    2. 相鄰兩窗找**最晚的共同時間戳** `t*`：
       - 有 `t*` 且兩側 `accountValue` 相等 → 以 `p_seg(t*) − p_new(t*)` 為偏移量，
         把新窗尾巴（`ts > 目前段尾`）接上去。
       - 無 `t*`（漏抓）或 `t*` 上 AV 不一致（非同源）→ **開新分段**。
    3. 回傳所有分段。分段數 > 1 本身就是「這段期間漏抓過」的可見證據。

    ⚠️ 刻意**不**回傳「最長的那一段」之類的單一結果：那會讓呼叫端拿到一個看不出
    有缺口的序列。缺口是這份資料的真實性質，必須留在型別裡讓呼叫端顯式處理。
    """
    ordered = sorted(records, key=lambda r: (str(r.get("sampled_at", "")),
                                             str(r.get("bucket", ""))))
    segments: list[Segment] = []
    cur: list[tuple[int, Decimal, Decimal]] = []
    idx: dict[int, tuple[Decimal, Decimal]] = {}   # ts → (av, pnl_rebased)

    def _flush() -> None:
        if cur:
            segments.append(Segment(tuple(cur)))

    for rec in ordered:
        pts = _record_points(rec)
        if not pts:
            continue
        if not cur:
            cur = list(pts)
            idx = {ts: (av, pnl) for ts, av, pnl in cur}
            continue
        common = [ts for ts, _, _ in pts if ts in idx]
        if not common:
            # 無重疊＝漏抓。缺口期間的 ΔAV 已知，但「多少是交易損益、多少是出入金」
            # 無法從資料還原——這是真正的資訊損失，不是可以靠假設補起來的東西。
            logger.warning("績效序列 %s：取樣窗 %s 與前段無重疊（疑似漏抓），開新分段",
                           rec.get("address"), rec.get("bucket"))
            _flush()
            cur = list(pts)
            idx = {ts: (av, pnl) for ts, av, pnl in cur}
            continue
        star = max(common)
        seg_av, seg_pnl = idx[star]
        new_av, new_pnl = next((av, pnl) for ts, av, pnl in pts if ts == star)
        if seg_av != new_av:
            # 同一時刻、同一帳戶，accountValue 必須相同。不同 ⇒ 兩個窗不是同一段
            # 歷史（位址搞混、上游 schema 漂移）。硬接會得到一條每個數字都正常、
            # 但整體是假的序列——寧可斷段（工程原則 1）。
            logger.error("績效序列 %s：取樣窗 %s 在 ts=%d 的 accountValue 與前段不符"
                         "（%s vs %s）——非同源，開新分段",
                         rec.get("address"), rec.get("bucket"), star, seg_av, new_av)
            _flush()
            cur = list(pts)
            idx = {ts: (av, pnl) for ts, av, pnl in cur}
            continue
        offset = seg_pnl - new_pnl
        last_ts = cur[-1][0]
        for ts, av, pnl in pts:
            if ts <= last_ts:
                continue
            rebased = (ts, av, pnl + offset)
            cur.append(rebased)
            idx[ts] = (av, pnl + offset)
    _flush()
    return segments
