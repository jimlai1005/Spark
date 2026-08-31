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
  → ⭐ builder 資格合規：對同一批 mainnet builder 查 perp 淨值 ≥ 100 USDC 與
    account abstraction mode == standard（HL 官方生效條件，主詞是 builder）。
    任一不符或查不到 → 報表頂部 🚨 區塊 ＋ stderr [ALERT]。見
    src/spark/filet/builder_compliance.py 檔頭。
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

from scripts.copytrade_daily_report import append_accrued_history
from spark.config import API_URLS
from spark.copytrade.notifier import Notifier, NullNotifier, TelegramNotifier
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.filet.aggregate import aggregate, builder_fee_delta, collect_follower_summary, render_aggregate
from spark.filet.builder_compliance import check_builders, compliance_alert_items
from spark.filet.followers import load_followers_tolerant
from spark.filet.leader_change_apply import (LEADER_CHANGE_STALE_S, LEDGER_RELPATH,
                                             scan_unapplied_leader_changes)
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


def leader_change_items(refs, now_s, changes_path=None, ledger_for=None) -> list[tuple[str, str]]:
    """對帳告警的結構化形式：`(dedup_key, text)` 清單（空清單＝一切正常）。

    ⭐ `dedup_key` 帶 **account_id + reason**：同一個帳號的同一種對帳失敗持續存在時，
    推播端（Notifier）靠 TTL 去重不洗版，而不同帳號／不同 reason 各自獨立。掃描層級的
    錯誤（沒有 account 歸屬，例如交換目錄解不出來）用 `leader_change_scan_error_<i>` 做鍵。

    `leader_change_warnings()`（純字串清單，報表本文與 stderr 用）是本函式 `text` 的視圖——
    單一定義，兩者不會漂移。判定本身仍住在 `scan_unapplied_leader_changes`（見下）。
    """
    ledger_for = ledger_for or ledger_path_for
    findings, errors = scan_unapplied_leader_changes(
        refs, now_s, changes_path=changes_path, ledger_for=ledger_for,
        stale_s=LEADER_CHANGE_STALE_S)

    items: list[tuple[str, str]] = []
    for i, e in enumerate(errors):
        items.append((f"leader_change_scan_error_{i}", e))
    for f in findings:
        key = f"leader_change_{f.account_id}_{f.reason}"
        if f.reason == "bad_issued_at":
            text = (f"換 leader 記錄的 issued_at 不合法（account={f.account_id}）"
                    f"——引擎會拒絕它，客戶的變更不會生效")
        elif f.reason == "ledger_unreadable":
            text = (f"換 leader 對帳失敗：帳本讀不到（{f.ledger_path}）："
                    f"{f.detail}——無法確認 account={f.account_id} 的變更"
                    f"是否已套用")
        else:
            text = (
                f"**換 leader 已記錄但未被套用**：account={f.account_id} 的變更已落地 "
                f"{f.age_s / 60:.0f} 分鐘（> {LEADER_CHANGE_STALE_S // 60} 分），引擎帳本"
                f"（{f.ledger_path}）卻沒有對應的已兌現 nonce。客戶以為換好了，實際仍跟"
                f"舊 leader。請依序檢查：交換目錄權限（RUNBOOK §5.5.1 的四條驗收）、"
                f"兩個 unit 的 FILET_EXCHANGE_DIR 是否同值、該 follower 引擎是否在跑")
        items.append((key, text))
    return items


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

    ⭐ 判定本身住在 `leader_change_apply.scan_unapplied_leader_changes`（**單一定義**），
    本函式只負責渲染成人看的字串。營運健康面板（/api/ops/health）是同一個判定的
    即時視圖，兩邊各寫一次判定式會漂移，而一個對帳工具最糟的失效是狼來了。

    渲染細節（含 dedup_key）住在 `leader_change_items`；本函式是其 text 的字串視圖。
    """
    return [text for _key, text in leader_change_items(
        refs, now_s, changes_path=changes_path, ledger_for=ledger_for)]


def generate_report(refs, load_errors, adapter_for, now, notifier: Notifier | None = None):
    """核心 wiring（adapter_for 注入以便離線測試）。回 AggregateReport。

    adapter_for: Callable[[str], adapter]——依 network 回一個 exchange adapter。
    北極星：對每個相異 mainnet builder 查一次 accrued（testnet 不計入），減快照 →
    builder_fee_delta；per-follower：collect_follower_summary（不查 accrued）。
    寫報表與快照為副作用。查詢失敗的 builder 大聲告警、其當日增量不計入（低估方向）。

    notifier: 推播管道（注入，預設 NullNotifier）。營收關鍵告警（builder 資格、換 leader
    對帳）除了既有的 stderr `[ALERT]`／報表檔（稽核軌跡）之外，**另外**推到 Notifier
    ——那兩者埋在 journal 與檔案裡，營收靜默中斷時沒人會即時看到。這是新增管道，不取代
    既有輸出；無 Telegram 憑證時注入 NullNotifier，行為與過去完全一致（只 stderr/檔案）。
    """
    notifier = notifier or NullNotifier()
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

    # --- I-24（2026-09-01）：accrued 歷史序列每日落檔 ---
    # 首頁「累計路由交易量」（public_stats）由 accrued_history.jsonl 末筆反推；
    # 過去只有 copytrade 版日報會寫這檔，filet 主機從未產生 → 數字停在人工播種值。
    # 重用 copytrade_daily_report.append_accrued_history（同日冪等覆蓋、captured_at
    # 與值同源）。⭐ 只在**全部** builder 查詢成功時落檔：部分失敗的加總是低估值，
    # 寫進去會讓下游路由量無聲下修（工程原則 1 混源）；保留上一筆完好值更誠實。
    if today_accrued and failed_builders == 0:
        append_accrued_history(day.isoformat(), sum(today_accrued.values()),
                               now_fn=lambda: now)
    elif failed_builders:
        print("[WARN] accrued 歷史序列本日不落檔（部分 builder 查詢失敗，"
              "避免低估值進入路由量推導）", file=sys.stderr)

    # --- ⭐ builder 資格合規（營收關鍵；見 filet/builder_compliance.py 檔頭）---
    # 這裡查的是「builder fee 到底還會不會產生」，與上面查的「產生了多少」是兩件事：
    # 資格斷掉時北極星增量會是 0，而 0 與「今天沒人交易」在報表上長得一模一樣。
    # 放在北極星之後、per-follower 之前，用的是同一批 mainnet builder 位址（同源）。
    _compliance, compliance_alerts_ = check_builders(
        lambda: adapter_for("mainnet"), mainnet_builders)

    # --- per-follower summary（fills 衍生，不查 accrued）---
    summaries = []
    for ref in refs:
        adapter = adapter_for(ref.network)
        summaries.append(collect_follower_summary(ref, adapter, start, now))

    report = aggregate(day, summaries, north_star_fee_delta=north_star_delta)
    text = render_aggregate(report)

    # ⭐ 合規告警排在最前面（在低估註記與 manifest 錯誤之前）：它是唯一一種「一切
    # 看起來正常但錢已經不再進來」的失效，讀報表的人必須先看到它（工程原則 3）。
    if compliance_alerts_:
        text += "\n\n## 🚨 builder 資格異常（builder fee 可能已停止累積）\n" + \
                "\n".join(f"- {a}" for a in compliance_alerts_)
        for a in compliance_alerts_:
            print(f"[ALERT] {a}", file=sys.stderr)
        # ⭐ 新增推播管道：每一條合規告警 → notifier.critical（帶 builder+條件的 dedup_key）。
        # stderr/報表是稽核軌跡；推播是唯一能在營收靜默中斷時即時觸達的路徑（工程原則 3）。
        for r in _compliance:
            for item in compliance_alert_items(r):
                notifier.critical("builder合規", item.text, dedup_key=item.dedup_key)

    if failed_builders:
        text += (f"\n\n> 注意：北極星不含 {failed_builders} 個查詢失敗的 builder，"
                 "當日增量為低估值。")
    if load_errors:
        text += "\n\n## Manifest 錯誤\n" + "\n".join(f"- {e}" for e in load_errors)

    # ⭐ 換 leader 鏈路對帳：把「客戶簽了、API 收了、引擎從沒套用」這類**可用性**
    # 失敗變大聲（工程原則 3）。它不影響北極星，但它是唯一會在日報裡浮出來的訊號
    # ——這條鏈路壞掉時，其他所有地方看起來都完全正常。
    lc_items = leader_change_items(refs, now.timestamp())
    if lc_items:
        lc_warnings = [w for _key, w in lc_items]
        text += "\n\n## ⚠️ 換 leader 未生效\n" + "\n".join(f"- {w}" for w in lc_warnings)
        for w in lc_warnings:
            print(f"[WARN] {w}", file=sys.stderr)
        # ⭐ 新增推播管道：每一條對帳告警 → notifier.critical（帶 account_id 的 dedup_key）。
        for key, w in lc_items:
            notifier.critical("換leader對帳", w, dedup_key=key)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{day.isoformat()}.md"
    out_path.write_text(text + "\n")

    print(text)
    print(f"\n[written] {out_path}", file=sys.stderr)

    if today_accrued:
        merged = {**prev_snapshot, **today_accrued}
        save_builder_snapshot(day.isoformat(), merged)

    return report


def build_notifier(env: dict[str, str] | None = None) -> Notifier:
    """依環境變數建構推播管道（沿 copytrade 引擎慣例）。

    有 `COPY_TG_BOT_TOKEN`＋`COPY_TG_CHAT_ID` → `TelegramNotifier.from_env()`（實測可送）；
    否則 → `NullNotifier`（行為與過去完全一致，只 stderr/報表檔）。憑證缺失不是錯誤，
    是降級：日報照跑，只是少了即時推播（systemd unit 的 `-EnvironmentFile=` 讓缺檔不擋啟動）。
    """
    if env is None:
        env = dict(os.environ)
    if env.get("COPY_TG_BOT_TOKEN") and env.get("COPY_TG_CHAT_ID"):
        return TelegramNotifier.from_env(env)
    return NullNotifier()


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

    generate_report(refs, load_errors, adapter_for, now, build_notifier())
    raise SystemExit(0)


if __name__ == "__main__":
    main()
