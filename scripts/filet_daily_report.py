"""跨 follower 日報：正確北極星（builder fee 查一次，不重複計）。

用法:
  [FILET_FOLLOWERS=var/filet/followers.json] uv run python -m scripts.filet_daily_report

環境變數:
  FILET_FOLLOWERS    follower manifest 路徑（預設 var/filet/followers.json）
  FILET_EXCHANGE_DIR 換 leader 記錄的交換目錄（對帳用；未設 → 對帳項自己報錯）
  FILET_STATE_BASE   per-follower 狀態根的基底（預設 /opt/filet/state，同 panic_all）

流程:
  load_followers_tolerant(manifest) → refs, errors（壞條目跳過，不擋整份日報）
  → 北極星：對 refs 中出現的每個相異 mainnet builder 位址各查一次
    query_builder_accrued，減去 builder 層級快照
    （var/filet/builder_accrued_snapshot.json，無檔視為 0）→ builder_fee_delta；
    加總（跨「相異 builder」相加合法——M2 通常只有一個 builder，跨「follower」
    才是紅線禁止的重複計）；更新快照。
  → per-follower：collect_follower_summary(ref, adapter, start, end)
    （當日 UTC 0 點～now；函式內建錯誤隔離，單一 follower 失敗不中止其他）。
  → aggregate → render_aggregate 印 stdout ＋寫 var/filet/reports/YYYY-MM-DD.md。

注意:
  - testnet follower 不計入北極星（builder fee 對 testnet 無真實經濟意義），
    但仍在明細列出（FollowerSummary 標 network）。
  - manifest 壞條目與 per-follower 查詢失敗一律大聲印出（工程原則 4：失敗不得靜默），
    但不中止其他 follower 的匯總。
  - 換 leader 鏈路對帳（leader_change_warnings）：記錄落地超過 LEADER_CHANGE_STALE_S
    卻沒有對應的已兌現 nonce → 告警。這捕捉「客戶簽了、API 收了、引擎從沒套用」
    整類**靜默**失敗（路徑不通、權限錯、引擎沒跑），它們在其他地方全都看起來正常。
  - import 階段不觸網：所有網路呼叫都在 main() 內。
"""
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from hyperliquid.info import Info

from spark.config import API_URLS
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.filet.aggregate import aggregate, builder_fee_delta, collect_follower_summary, render_aggregate
from spark.filet.followers import load_followers_tolerant
from spark.filet.leader_change import (LeaderChangeError, load_leader_changes,
                                       parse_issued_at)
from spark.filet.leader_change_apply import (LEDGER_RELPATH, load_ledger,
                                             resolve_changes_path)
from spark.filet.leader_resolve import DEFAULT_MANIFEST_PATH

# ⭐ repo 根錨定，**不是** CWD 相對（沿 leader_resolve.DEFAULT_MANIFEST_PATH 的
# I4 修法）。日報由 cron/systemd 跑，CWD 未必等於 repo 根：manifest 讀不到會
# FileNotFoundError 大聲失敗（非 fail-open），但 SNAPSHOT_PATH 讀錯位置的方向更壞——
# 讀不到既有快照會被當成「無檔＝0」，於是 builder_fee_delta 把**全期累積量**當成
# 當日增量報出去（北極星數字暴增），而且會把錯的快照寫回錯的地方。
# 三個路徑同源於 VAR_DIR：只錨定 manifest 會讓同一份 var/filet 一半絕對一半相對。
VAR_DIR = Path(DEFAULT_MANIFEST_PATH).parent
DEFAULT_MANIFEST = VAR_DIR / "followers.json"
SNAPSHOT_PATH = VAR_DIR / "builder_accrued_snapshot.json"
REPORTS_DIR = VAR_DIR / "reports"

# ⭐ 「已寫入但未被套用」的判定門檻（秒）。一筆記錄落地超過這麼久，引擎的已兌現帳本
# 卻還沒有對應的 nonce ⇒ 這條鏈路壞了。30 分鐘遠大於引擎的 cycle 間隔（數十秒）與
# 任何合理的重啟窗，所以逾時不可能是「還沒輪到」；它是真的沒發生。
LEADER_CHANGE_STALE_S = 1800

# per-follower 狀態根的基底（對齊 systemd 的 FILET_STATE_DIR=/opt/filet/state/%i，
# 沿 panic_all.py 的同一個 env 與同一個預設值——同一件事兩個名字是誤設的溫床）。
DEFAULT_STATE_BASE = "/opt/filet/state"


def _usage() -> str:
    return ("用法: [FILET_FOLLOWERS=var/filet/followers.json] "
            "uv run python -m scripts.filet_daily_report")


def load_builder_snapshot() -> dict[str, Decimal]:
    """讀 builder 層級 accrued 快照（跨 follower 共用的全域量）；無檔視為空。"""
    if not SNAPSHOT_PATH.exists():
        return {}
    data = json.loads(SNAPSHOT_PATH.read_text())
    return {addr: Decimal(str(v)) for addr, v in data.get("builders", {}).items()}


def save_builder_snapshot(day_iso: str, accrued_by_builder: dict[str, Decimal]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps({
        "date": day_iso,
        "builders": {addr: str(v) for addr, v in accrued_by_builder.items()},
    }, indent=2))


def _mainnet_builders(refs) -> list[str]:
    """北極星要查的相異 mainnet builder 位址（小寫正規化後去重）。

    以太坊位址大小寫不敏感（checksum 只是顯示層），若不正規化，同一位址的兩種
    大小寫會被當成兩個 builder 各查一次 `query_builder_accrued`——但兩者回同一個
    **全域**累積值，加總即北極星重複計，正是本任務要根除的。（先例 c624d2e。）
    """
    return sorted({r.builder_address.lower() for r in refs if r.network == "mainnet"})


def ledger_path_for(account_id: str, state_base=None) -> Path:
    """某個 follower 的已兌現帳本落點。

    ⭐ 由 `LEDGER_RELPATH`（引擎寫入時用的同一個常數）推導，不另拼一次字串：
    兩份會漂移的定義會讓本檔「找不到帳本」而把**每一筆**記錄都報成未套用——
    一個對帳工具最糟的失效是狼來了，因為它會訓練操作者忽略它。
    """
    base = Path(state_base or os.environ.get("FILET_STATE_BASE", DEFAULT_STATE_BASE))
    return base / account_id / LEDGER_RELPATH


def leader_change_warnings(refs, now_s, changes_path=None, ledger_for=None) -> list[str]:
    """對帳「客戶簽了、API 收了，但引擎從沒套用」的整類失敗。

    ⭐ 為什麼需要這一項（2026-07-19 opus 審查 I2）
    ----------------------------------------------
    換 leader 這條鏈路橫跨兩個進程、兩套權限、兩個目錄。它的可用性失敗是**靜默**的：
    API 驗完章、寫檔、回客戶 200「於引擎的下一個 cycle 生效」，而引擎那邊可能根本
    讀不到那個檔（路徑不通、group 權限錯、引擎沒在跑）。三種原因症狀完全一樣——
    什麼都不會發生，兩邊的 log 都正常，客戶以為換好了，實際上還跟著舊 leader。

    判定用的兩個值刻意同源同單位（工程原則 1）：記錄的年齡由**記錄自己的**
    `issued_at` 換算成 epoch 秒，與傳入的 `now_s` 相減；帳本則用引擎寫入時的同一個
    `load_ledger`。跨進程比對「這顆 nonce 兌現了沒」只看 `redeemed`——那正是引擎
    冪等性的唯一述詞，用別的欄位推斷會與引擎的實際行為漂移。

    回傳的是給人看的告警字串清單（空清單＝一切正常）。不 raise：日報的其他部分
    （北極星）不該被這一項的失敗擋住。
    """
    ledger_for = ledger_for or ledger_path_for
    try:
        path = changes_path or resolve_changes_path()
    except ValueError as e:
        # 交換目錄本身沒設好 ⇒ 這條鏈路必定不通。這**就是**要報的事，不是略過的理由。
        return [f"換 leader 記錄無法定位（交換目錄設定有問題）：{e}"]
    try:
        records = load_leader_changes(path)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        return [f"換 leader 記錄檔讀取失敗（{path}）：{e!r}——無法確認是否有未套用的變更"]

    known = {r.account_id for r in refs}
    warnings: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        account_id, nonce = rec.get("account_id"), rec.get("nonce")
        if not isinstance(account_id, str) or not isinstance(nonce, str):
            continue
        if account_id not in known:
            # manifest 沒有這個帳號＝尚未 activate。引擎本來就不會套用它，不是鏈路故障。
            continue
        try:
            age_s = now_s - parse_issued_at(rec.get("issued_at")).timestamp()
        except (LeaderChangeError, AttributeError):
            warnings.append(f"換 leader 記錄的 issued_at 不合法（account={account_id}）"
                            f"——引擎會拒絕它，客戶的變更不會生效")
            continue
        if age_s <= LEADER_CHANGE_STALE_S:
            continue        # 還新，引擎可能只是還沒跑到下一輪
        ledger_path = ledger_for(account_id)
        try:
            ledger = load_ledger(ledger_path)
        except (OSError, ValueError) as e:
            warnings.append(f"換 leader 對帳失敗：帳本讀不到（{ledger_path}）：{e!r}"
                            f"——無法確認 account={account_id} 的變更是否已套用")
            continue
        if nonce not in ledger.redeemed:
            warnings.append(
                f"**換 leader 已記錄但未被套用**：account={account_id} 的變更已落地 "
                f"{age_s / 60:.0f} 分鐘（> {LEADER_CHANGE_STALE_S // 60} 分），引擎帳本"
                f"（{ledger_path}）卻沒有對應的已兌現 nonce。客戶以為換好了，實際仍跟"
                f"舊 leader。請依序檢查：交換目錄權限（RUNBOOK §5.5.1 的四條驗收）、"
                f"兩個 unit 的 FILET_EXCHANGE_DIR 是否同值、該 follower 引擎是否在跑")
    return warnings


def generate_report(refs, load_errors, adapter_for, now):
    """核心 wiring（adapter_for 注入以便離線測試）。回 AggregateReport。

    adapter_for: Callable[[str], adapter]——依 network 回一個 exchange adapter。
    北極星：對每個相異 mainnet builder 查一次 accrued（testnet 不計入），減快照 →
    builder_fee_delta；per-follower：collect_follower_summary（不查 accrued）。
    寫報表與快照為副作用。查詢失敗的 builder 大聲告警、其當日增量不計入（低估方向）。
    """
    day = now.date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    # --- 北極星：mainnet builder 層級查一次（絕不跨 follower 加總）---
    mainnet_builders = _mainnet_builders(refs)
    prev_snapshot = load_builder_snapshot()
    today_accrued: dict[str, Decimal] = {}
    failed_builders = 0
    north_star_delta = Decimal("0")
    for builder in mainnet_builders:
        adapter = adapter_for("mainnet")
        try:
            accrued_today = adapter.query_builder_accrued(builder)
        except Exception as e:  # noqa: BLE001 — 北極星查詢失敗大聲告警，不吞掉
            print(f"[WARN] builder accrued 查詢失敗 {builder}: {e}", file=sys.stderr)
            failed_builders += 1
            continue
        prev = prev_snapshot.get(builder, Decimal("0"))
        north_star_delta += builder_fee_delta(accrued_today, prev)
        today_accrued[builder] = accrued_today

    # --- per-follower summary（fills 衍生，不查 accrued）---
    summaries = []
    for ref in refs:
        adapter = adapter_for(ref.network)
        summaries.append(collect_follower_summary(ref, adapter, start, now))

    report = aggregate(day, summaries, north_star_fee_delta=north_star_delta)
    text = render_aggregate(report)
    if failed_builders:
        text += (f"\n\n> 注意：北極星不含 {failed_builders} 個查詢失敗的 builder，"
                 "當日增量為低估值。")
    if load_errors:
        text += "\n\n## Manifest 錯誤\n" + "\n".join(f"- {e}" for e in load_errors)

    # ⭐ 換 leader 鏈路對帳：把「客戶簽了、API 收了、引擎從沒套用」這類**可用性**
    # 失敗變大聲（工程原則 3）。它不影響北極星，但它是唯一會在日報裡浮出來的訊號
    # ——這條鏈路壞掉時，其他所有地方看起來都完全正常。
    lc_warnings = leader_change_warnings(refs, now.timestamp())
    if lc_warnings:
        text += "\n\n## ⚠️ 換 leader 未生效\n" + "\n".join(f"- {w}" for w in lc_warnings)
        for w in lc_warnings:
            print(f"[WARN] {w}", file=sys.stderr)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{day.isoformat()}.md"
    out_path.write_text(text + "\n")

    print(text)
    print(f"\n[written] {out_path}", file=sys.stderr)

    if today_accrued:
        merged = {**prev_snapshot, **today_accrued}
        save_builder_snapshot(day.isoformat(), merged)

    return report


def main():
    manifest_path = Path(os.environ.get("FILET_FOLLOWERS", str(DEFAULT_MANIFEST)))
    try:
        refs, load_errors = load_followers_tolerant(manifest_path)
    except FileNotFoundError:
        print(_usage())
        raise SystemExit(2)

    for e in load_errors:
        print(f"[WARN] follower manifest 條目跳過: {e}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    adapters: dict[str, HyperliquidAdapter] = {}

    def adapter_for(network: str) -> HyperliquidAdapter:
        """每網路一個 Info client（Info 綁定單一 API URL，mainnet/testnet 不可共用）。"""
        if network not in adapters:
            adapters[network] = HyperliquidAdapter(
                network, info=Info(API_URLS[network], skip_ws=True), exchange=None)
        return adapters[network]

    generate_report(refs, load_errors, adapter_for, now)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
