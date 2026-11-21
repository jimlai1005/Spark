"""testnet 端到端編排：follower 引擎鏡射 leader 觸發單（plan T5，情境 E1–E9）。

背景：docs/superpowers/plans/2026-11-21-trigger-order-mirroring.md §6 T5。離線測試證明邏輯，
不證明真交易所照預期反應；本腳本在 Hyperliquid testnet 以兩個拋棄式錢包（leader、follower）
跑完九個情境，逐條判定 PASS／FAIL，結果決定這個功能能不能上主網實盤。
判定原則：**寧可判 FAIL 並記下原始資料，也不寬鬆判 PASS；沒觀察到的事不當成通過**。

⚠️ 僅限 testnet：`_assert_testnet`（沿用 testnet_leader_drive）在建構任何簽章者之前與每次
   leader／清理寫入之前各檢一次。leader 單筆名目上限沿用 `MAX_NOTIONAL_USD`；
   follower 的下單量由引擎依倍率決定，故啟動前置檢查會估算 follower 名目並拒絕過大者。
⚠️ 測試工具，非生產路徑：不進 systemd、不被 src/ 任何模組 import。
⚠️ 私鑰不進任何輸出：寫檔／印出前一律過 scrub，並額外遮蔽兩把私鑰的字面值與裸 64-hex。
   私鑰檔路徑必須含 `scratchpad`（由 `tests/integration/harness.py mint-wallet` 產生）；
   follower 私鑰會複製成引擎 keystore 內的 agent.key（拋棄式錢包的主鑰直接當簽章者），
   結束時（finally）一律刪除並驗證，刪不掉＝非 0 結束。

流程：
  1. 前置檢查（零寫入）：兩錢包該幣無倉位、無掛單；對 --builder 的 maxBuilderFee ≥ f；
     餘額足夠。任一項不過 → 印原因、exit 2。
  2. 每個情境先 reset（兩邊撤單＋市價平倉＋驗證乾淨）→ leader 動作（一律走產品程式
     `HyperliquidAdapter.place_order`／`cancel_order`／`market_open`）→ 跑 N 輪 follower 引擎
     → 讀兩邊 `frontendOpenOrders` 原始 dict 與倉位 → 逐條判定。情境失敗不中止後續情境。
  3. try/finally：最後一定兩邊撤單、平倉、驗證「無倉位、無掛單」，並刪除 keys 目錄。

follower 引擎：以子行程跑 `scripts.run_copytrade --once`（見下），環境強制 SPARK_NETWORK=testnet、
COPY_LIVE_TRADING=true、COPY_MIRROR_TRIGGERS 依情境；繼承的 COPY_*／SPARK_*／FILET_* 先清掉；
不設任何 Telegram 變數。其他引擎設定取產品預設，只有兩項刻意對齊「新錢包上線預設」：
COPY_RISK_CONTROLS_ENABLED=false（watcher 對新錢包的預設；風控熔斷不在本次受測範圍）、
COPY_CAPITAL_UTILIZATION=0.9（deploy/follower.env.autoactivate.example）。

引擎可觀測性（告警／動作）：引擎的 notifier 在無 TG 變數時是 NullNotifier（全吞），告警既不
印出也不落檔，唯一出口是 ActionExecutor.records（記憶體）。本腳本因此不直接 exec
`scripts.run_copytrade`，而是以自身的 `_engine_child` 模式（同一個 `python -m` 子行程）載入
`scripts.run_copytrade`，只做兩處替換後呼叫它原本的 `main(["--once"])`：
  (a) `NullNotifier` → `RecordingNotifier`（spark.copytrade.notifier，測試用、不發送）；
  (b) `run_cycle` → 薄包裝，轉呼叫原函式並把本輪 `ex.records`（place／cancel／market_open…）
      與 CycleReport 的數字寫進 JSON。
引擎邏輯一行不改；`TelegramNotifier` 同時被換成會 raise 的替身（保證不會真的送出）。
子行程沒寫出 JSON（引擎在 run_cycle 之前就失敗）→ 該輪的動作／告警判為「無法觀測」。

E6／E9（讓觸發單真的觸發）：觸發價從現價 --chase-frac（預設 0.1%）起，等 follower 鏡射後輪詢，
最多等 10 分鐘，每 2 分鐘把距離乘 0.6。「follower 已鏡射」的證據＝該步引擎 action 記錄有成功的
`place`（觸發價與型態相符）或快照有對應掛單——觸發後才拍的快照可能看不到已成交的單（run1 教訓）。
leader 先觸發而兩種證據都沒有＝無效嘗試，reset 後重來（起始距離乘 1.5，最多 3 次）；
三次皆無效 → verdict INCONCLUSIVE（非 0）。有效嘗試另查 follower 的 historicalOrders／
userFillsByTime 作「由交易所端觸發」的直接證據（triggered 紀錄、成交 oid 等於該單 oid、
引擎 action 沒有平倉／market_open）。

用法：
  uv run python -m scripts.testnet_trigger_e2e \\
      --leader-pk-file <scratchpad>/leader.pk --follower-pk-file <scratchpad>/follower.pk \\
      --builder 0x... --out <repo 外>/trigger-e2e-run1.jsonl [--only E1,E3] [--coin BTC] [--notional 60]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from scripts.testnet_leader_drive import (
    MAX_NOTIONAL_USD,
    MIN_NOTIONAL_USD,
    SLIPPAGE,
    _account_id_for,
    _assert_testnet,
)
from scripts.testnet_trigger_probe import REPO_ROOT, Recorder, resolve_out, round_px, scrub
from spark.config import MIN_BUILDER_BALANCE, Settings
from spark.copytrade.instrument import _round_size
from spark.exchange.base import BuilderCode, Order
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.filet.followers import normalize_hex_address

NETWORK = "testnet"
SCENARIO_IDS = tuple(f"E{i}" for i in range(1, 10))

DEFAULT_NOTIONAL_USD = Decimal("60")
MIN_E2E_NOTIONAL_USD = Decimal("20")
MAX_E2E_NOTIONAL_USD = Decimal("100")
MARKET_SLIPPAGE = Decimal("0.10")        # 市價型觸發單的滑點保護價：觸發價 ±10%（同 T1 探針）
FAR_DOWN = Decimal("0.90")               # 遠離市價、不會在測試期間被碰到的觸發價倍率
FAR_DOWN_2 = Decimal("0.85")             # E2 改價後的止損
FAR_UP = Decimal("1.10")
ENTRY_UP = Decimal("1.05")               # E8 條件進場觸發價（高於現價 5%）
CAPITAL_UTILIZATION = "0.9"              # 對齊 deploy/follower.env.autoactivate.example
SIZE_TOLERANCE = Decimal("0.08")         # CopySettings.size_tolerance 預設
SCALE_CROSSCHECK_TOL = Decimal("0.10")   # 引擎回報的倍率 vs 兩邊權益比（同 test_e2e_noncustodial 做法）
MIN_EQUITY_USD = Decimal("50")           # 錢包權益絕對下限
MIN_EQUITY_NOTIONAL_FACTOR = Decimal("0.5")  # 權益 ≥ 名目 × 0.5（假設預設槓桿 ≥ 2x；估計值，見回報）

ENGINE_TIMEOUT_S = 240
CHASE_TOTAL_S = 600                      # E6／E9：最長等待觸發 10 分鐘
CHASE_STEP_S = 120                       # 每 2 分鐘把觸發價往現價再靠近一次
CHASE_POLL_S = 10
CHASE_FRAC_DEFAULT = Decimal("0.001")    # 追價初始距離（--chase-frac 預設）：0.1%
CHASE_FRAC_MIN = Decimal("0.0002")
CHASE_FRAC_MAX = Decimal("0.005")
CHASE_FACTOR = Decimal("0.6")            # 每 2 分鐘距離乘 0.6（run1：0.03% 起始太近，leader 先觸發）
CHASE_ATTEMPTS = 3                       # E6／E9 無效嘗試（leader 先觸發）時最多重試到 3 次
CHASE_RETRY_GROWTH = Decimal("1.5")      # 每次重試起始距離乘 1.5
HISTORY_POLL_S = 25                      # 觸發後等 historicalOrders／fills 出現的最長秒數
SLACK_MS = 5000                          # 本機時鐘與交易所時間戳的容許誤差
POST_TRIGGER_ROUNDS = 3

WRITE_KINDS = frozenset({"place", "modify", "cancel", "market_open", "close"})
POS_SL = "Stop Market"
POS_TP = "Take Profit Market"

_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")


def say(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- pure helpers


def parse_only(spec: str | None) -> list[str]:
    """`--only E1,E3` → ['E1','E3']（依編號排序、去重）；未知編號 SystemExit。"""
    if not spec:
        return list(SCENARIO_IDS)
    wanted = {s.strip().upper() for s in spec.split(",") if s.strip()}
    unknown = sorted(wanted - set(SCENARIO_IDS))
    if unknown or not wanted:
        raise SystemExit(f"--only 含未知情境 {unknown or spec!r}；可用：{', '.join(SCENARIO_IDS)}")
    return [i for i in SCENARIO_IDS if i in wanted]


def require_scratchpad_path(path: str, flag: str) -> Path:
    """私鑰檔路徑限制：解析後的路徑任一段含 "scratchpad"，否則拒絕（避免拋棄式錢包私鑰
    落進 repo 工作樹或家目錄常駐位置）。只檢查路徑字串，不讀檔。"""
    p = Path(path).expanduser().resolve()
    if not any("scratchpad" in part for part in p.parts):
        raise SystemExit(f"拒絕：{flag} 路徑必須落在 scratchpad 底下（收到 {p}）")
    return p


def check_notional(n: Decimal) -> Decimal:
    if not (MIN_E2E_NOTIONAL_USD <= n <= MAX_E2E_NOTIONAL_USD):
        raise SystemExit(
            f"拒絕：--notional 必須落在 [{MIN_E2E_NOTIONAL_USD}, {MAX_E2E_NOTIONAL_USD}]（收到 {n}）")
    return n


def make_redactor(secrets: Iterable[str]) -> Callable[[Any], Any]:
    """回傳遞迴遮蔽函式：probe 的 scrub（signature／private key 欄位、0x+≥64hex）之外，
    再遮蔽傳入私鑰的字面值（含去 0x、小寫變體）與任何裸 64-hex 字串。"""
    needles: set[str] = set()
    for s in secrets:
        s = s.strip()
        for v in (s, s.lower(), s.removeprefix("0x"), s.lower().removeprefix("0x")):
            if len(v) >= 16:
                needles.add(v)
    ordered = sorted(needles, key=len, reverse=True)

    def _walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_walk(v) for v in o]
        if isinstance(o, str):
            for n in ordered:
                o = o.replace(n, "<redacted-key>")
            return _HEX64.sub("<redacted-hex>", o)
        return o

    return lambda obj: _walk(scrub(obj))


def szi_from_state(state: dict, coin: str) -> Decimal:
    for p in state.get("assetPositions", []):
        item = p.get("position", {})
        if item.get("coin") == coin:
            return Decimal(str(item.get("szi", "0")))
    return Decimal("0")


def chase_trigger(mid: Decimal, frac: Decimal, direction: int, tick: Decimal) -> Decimal:
    """E6／E9 的「靠近現價」觸發價：距離 mid×frac，但至少一個 tick，且絕不越過 mid。
    direction=-1 ＝ 低於現價（多倉止損）；+1 ＝ 高於現價（止損市價買的條件進場）。"""
    delta = max(abs(mid * frac), tick)
    return mid + direction * delta


def price_tick(mid: Decimal, sz_decimals: int) -> Decimal:
    """價格最小跳動估計：5 位有效數字與小數位上限（6 − szDecimals）兩者取粗者。"""
    sig = Decimal(1).scaleb(mid.adjusted() - 4)
    dec = Decimal(1).scaleb(-(6 - sz_decimals))
    return max(sig, dec)


def verdict_of(checks: list[dict], inconclusive: bool = False) -> str:
    """PASS＝至少一條且全部 pass==True；任何 False → FAIL；
    沒有 False 時：inconclusive（E6／E9 三次嘗試皆無效，實驗沒有真的測到東西）→ INCONCLUSIVE；
    有 null → UNVERIFIED。只有 PASS 算通過。"""
    if not checks or any(c["pass"] is False for c in checks):
        return "FAIL"
    if inconclusive:
        return "INCONCLUSIVE"
    if any(c["pass"] is None for c in checks):
        return "UNVERIFIED"
    return "PASS"


def check_chase_frac(v: Decimal) -> Decimal:
    if not (CHASE_FRAC_MIN <= v <= CHASE_FRAC_MAX):
        raise SystemExit(
            f"拒絕：--chase-frac 必須落在 [{CHASE_FRAC_MIN}, {CHASE_FRAC_MAX}]（收到 {v}）")
    return v


def overall_exit_code(results: list[dict], clean: bool, keys_deleted: bool) -> int:
    """0 ＝ 所有被選情境皆 PASS、每個情境至少一筆成功的 leader 下單、清理後乾淨、私鑰副本已刪。"""
    if not results or not clean or not keys_deleted:
        return 1
    ok = all(r["verdict"] == "PASS" and r["leader_writes_ok"] >= 1 for r in results)
    return 0 if ok else 1


def brief(o: dict) -> dict:
    """掛單的判讀欄位摘要（原始 dict 另行完整保存）。"""
    return {k: o.get(k) for k in
            ("oid", "side", "orderType", "isPositionTpsl", "reduceOnly", "triggerPx",
             "limitPx", "sz", "origSz")}


def triggers(snap: dict) -> list[dict]:
    return [o for o in snap["open_orders"] if o.get("isTrigger")]


def pos_tpsl(snap: dict, order_type: str | None = None) -> list[dict]:
    return [o for o in triggers(snap) if o.get("isPositionTpsl")
            and (order_type is None or o.get("orderType") == order_type)]


def fixed_triggers(snap: dict) -> list[dict]:
    return [o for o in triggers(snap) if not o.get("isPositionTpsl")]


def oid_set(snap: dict) -> set:
    return {o["oid"] for o in snap["open_orders"]}


def dec(x: Any) -> Decimal:
    return Decimal(str(x))


def child_write_actions(child: dict | None) -> list[dict] | None:
    """本輪引擎的「下單／撤單／改單／開平倉」ActionRecord；無法觀測回 None。"""
    if child is None:
        return None
    return [a for a in child.get("actions", []) if a.get("kind") in WRITE_KINDS]


def child_notes(child: dict | None, level: str) -> list[dict] | None:
    if child is None:
        return None
    return [n for n in child.get("notifications", []) if n.get("level") == level]


def child_scale(child: dict | None) -> Decimal | None:
    rep = (child or {}).get("report") or {}
    try:
        return Decimal(str(rep["scale"]))
    except (KeyError, ArithmeticError, ValueError):
        return None


# --------------------------------------------------------------------------- leader 訂單建構


def position_tpsl_order(coin: str, tpsl: str, trigger: Decimal) -> Order:
    """leader 倉位止盈／止損（多倉，平倉方向＝賣）：reduce-only、市價型、數量欄由 adapter 送 0。"""
    return Order(coin=coin, is_buy=False, size=Decimal("0"),
                 limit_px=trigger * (1 - MARKET_SLIPPAGE), tif="Gtc", reduce_only=True,
                 trigger_px=trigger, tpsl=tpsl, is_market=True, position_tpsl=True)


def fixed_sl_limit_order(coin: str, size: Decimal, trigger: Decimal) -> Order:
    """leader 固定數量止損限價（多倉平倉方向＝賣）：limitPx＝triggerPx（T1 P1 實測形狀）。"""
    return Order(coin=coin, is_buy=False, size=size, limit_px=trigger, tif="Gtc",
                 reduce_only=True, trigger_px=trigger, tpsl="sl", is_market=False)


def conditional_entry_order(coin: str, size: Decimal, trigger: Decimal) -> Order:
    """leader 條件進場單：非 reduce-only 的止損市價買（觸發價高於現價）。"""
    return Order(coin=coin, is_buy=True, size=size, limit_px=trigger * (1 + MARKET_SLIPPAGE),
                 tif="Gtc", reduce_only=False, trigger_px=trigger, tpsl="sl", is_market=True)


# --------------------------------------------------------------------------- recorder / 檢查


class E2ERecorder(Recorder):
    """JSON lines：完整記錄寫檔（先過 redactor），stdout 只印一行摘要。"""

    def __init__(self, path: Path, redact: Callable[[Any], Any]):
        super().__init__(path)
        self._redact = redact

    def write(self, kind: str, **data: Any) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
        line = json.dumps(self._redact(rec), ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        tag = data.get("scenario") or data.get("label") or ""
        extra = f" {data['verdict']}" if "verdict" in data else ""
        say(f"[record] {kind} {tag}{extra}")
        return rec


class ScenarioLog:
    """單一情境的累積記錄：leader 動作、引擎每輪、兩邊快照、逐條判定。"""

    def __init__(self, sid: str, title: str):
        self.sid = sid
        self.title = title
        self.leader_actions: list[dict] = []
        self.engine_rounds: list[dict] = []
        self.snapshots: list[dict] = []
        self.notes: list[dict] = []
        self.checks: list[dict] = []
        self.leader_writes_ok = 0
        self.attempts: list[dict] = []        # E6／E9：每次嘗試的追價過程（含無效嘗試的完整記錄）
        self.inconclusive = False
        self.started = datetime.now(timezone.utc)

    def merge(self, other: "ScenarioLog") -> None:
        """把一次有效嘗試的暫存記錄併進情境記錄（checks 也併入）。"""
        self.leader_actions += other.leader_actions
        self.engine_rounds += other.engine_rounds
        self.snapshots += other.snapshots
        self.notes += other.notes
        self.checks += other.checks
        self.leader_writes_ok += other.leader_writes_ok

    def check(self, name: str, expected: Any, actual: Any, passed: bool | None) -> None:
        self.checks.append({"check": name, "expected": expected, "actual": actual,
                            "pass": passed})
        flag = {True: "PASS", False: "FAIL", None: "N/A "}[passed]
        say(f"  [{self.sid}] {flag} {name}")

    def note(self, text: str, **data: Any) -> None:
        self.notes.append({"ts": datetime.now(timezone.utc).isoformat(), "text": text, **data})
        say(f"  [{self.sid}] note: {text}")

    def to_record(self) -> dict:
        return {
            "scenario": self.sid, "title": self.title,
            "started": self.started.isoformat(),
            "leader_writes_ok": self.leader_writes_ok,
            "leader_actions": self.leader_actions, "engine_rounds": self.engine_rounds,
            "snapshots": self.snapshots, "notes": self.notes, "checks": self.checks,
            "attempts": self.attempts,
            "verdict": verdict_of(self.checks, self.inconclusive),
        }


@dataclass
class EngineRun:
    label: str
    mirror: bool
    rc: int | None
    duration_s: float
    stdout_tail: str
    stderr_tail: str
    child: dict | None

    @property
    def actions(self) -> list[dict] | None:
        return child_write_actions(self.child)

    @property
    def scale(self) -> Decimal | None:
        return child_scale(self.child)

    def to_record(self) -> dict:
        return {"label": self.label, "mirror_triggers": self.mirror, "returncode": self.rc,
                "duration_s": round(self.duration_s, 1), "stdout_tail": self.stdout_tail,
                "stderr_tail": self.stderr_tail, "child_observed": self.child is not None,
                "actions": (self.child or {}).get("actions"),
                "report": (self.child or {}).get("report"),
                "notifications": (self.child or {}).get("notifications")}


def level_count(runs: list[EngineRun], level: str) -> tuple[int | None, list[str]]:
    """這些輪的 notifier 記錄中某級別的則數與文字；任一輪無法觀測 → (None, [])。"""
    total, texts = 0, []
    for r in runs:
        notes = child_notes(r.child, level)
        if notes is None:
            return None, []
        total += len(notes)
        texts += [n.get("text", "") for n in notes]
    return total, texts


def no_crit_check(log: ScenarioLog, runs: list[EngineRun], name: str = "無 CRIT") -> None:
    n, texts = level_count(runs, "critical")
    if n is None:
        log.check(name, 0, "無法觀測（引擎子行程未寫出記錄）", None)
    else:
        log.check(name, 0, {"critical_count": n, "texts": texts}, n == 0)


def rc_check(log: ScenarioLog, run: EngineRun) -> None:
    log.check(f"引擎結束碼 0（{run.label}）", 0, run.rc, run.rc == 0)


def zero_action_check(log: ScenarioLog, run: EngineRun, prev: dict, cur: dict) -> None:
    """不 churn：本輪引擎零下單／零撤單／零改單（actions 與 report 兩個來源都要 0），
    且 follower 掛單 oid 集合與上一輪相同。"""
    acts = run.actions
    rep = (run.child or {}).get("report") or {}
    rec = rep.get("reconcile") or {}
    if acts is None:
        log.check(f"零下單、零撤單（{run.label}）", 0, "無法觀測", None)
    else:
        counts = {k: rec.get(k) for k in ("placed", "cancelled", "modified")}
        ok = (not acts) and all(v == 0 for v in counts.values())
        log.check(f"零下單、零撤單（{run.label}）", 0,
                  {"write_actions": [a.get("kind") for a in acts], "report": counts}, ok)
    log.check(f"follower 掛單 oid 集合不變（{run.label}）", sorted(oid_set(prev)),
              sorted(oid_set(cur)), oid_set(prev) == oid_set(cur))


# --------------------------------------------------------------------------- 錢包／交易所存取


def _read_retry(fn: Callable[[], Any], tries: int = 4, base: float = 1.0) -> Any:
    """只用於唯讀查詢（冪等）：暫時性失敗指數退避重試。寫入一律不經此處。"""
    for i in range(tries):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — 唯讀重試，最後一次照常上拋
            if i == tries - 1:
                raise
            time.sleep(base * 2 ** i)
    raise AssertionError("unreachable")


@dataclass
class Party:
    name: str
    address: str
    account: Any = field(repr=False)
    exchange: Exchange = field(repr=False)
    adapter: HyperliquidAdapter = field(repr=False)


def wait_until(pred: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


class E2E:
    """跑情境所需的一切：兩個錢包、Info、引擎子行程組態、reset／清理。"""

    def __init__(self, *, info: Info, coin: str, sz_dec: int, builder: BuilderCode,
                 leader: Party, follower: Party, notional: Decimal, work: Path,
                 account_id: str, rec: E2ERecorder,
                 chase_frac: Decimal = CHASE_FRAC_DEFAULT):
        self.info = info
        self.chase_frac = chase_frac
        self.coin = coin
        self.sz_dec = sz_dec
        self.builder = builder
        self.leader = leader
        self.follower = follower
        self.notional = notional
        self.work = work
        self.account_id = account_id
        self.rec = rec
        self.round_no = 0

    # ---- reads
    def mid(self) -> Decimal:
        return dec(_read_retry(lambda: self.info.all_mids())[self.coin])

    def size_for(self, notional: Decimal) -> Decimal:
        if notional > MAX_NOTIONAL_USD:
            raise RuntimeError(f"名目 ${notional} 超過測試上限 ${MAX_NOTIONAL_USD}")
        size = _round_size(notional / self.mid(), self.sz_dec)
        if size <= 0:
            raise RuntimeError(f"${notional} 換算 {self.coin} 數量後捨入為 0")
        return size

    def state(self, p: Party) -> dict:
        return _read_retry(lambda: self.info.user_state(p.address))

    def szi(self, p: Party) -> Decimal:
        return szi_from_state(self.state(p), self.coin)

    def equity(self, p: Party) -> Decimal:
        return dec(self.state(p)["marginSummary"]["accountValue"])

    def coin_orders(self, p: Party) -> list[dict]:
        raw = _read_retry(lambda: self.info.frontend_open_orders(p.address))
        return [o for o in raw if o.get("coin") == self.coin]

    def snap(self, p: Party) -> dict:
        st = self.state(p)
        return {"party": p.name, "open_orders": self.coin_orders(p),
                "position_szi": str(szi_from_state(st, self.coin)),
                "account_value": str(st["marginSummary"]["accountValue"])}

    def take(self, log: ScenarioLog, label: str) -> tuple[dict, dict]:
        """兩邊快照（掛單原始 dict＋倉位＋權益），記進情境記錄；回傳 (leader, follower)。"""
        l_snap, f_snap = self.snap(self.leader), self.snap(self.follower)
        log.snapshots.append({"label": label, "ts": datetime.now(timezone.utc).isoformat(),
                              "leader": l_snap, "follower": f_snap})
        return l_snap, f_snap

    # ---- leader 寫入（一律走產品程式 HyperliquidAdapter）
    def _guard(self, size: Decimal, *prices: Decimal) -> None:
        ref = max([self.mid(), *prices])
        if size * ref > MAX_NOTIONAL_USD:
            raise RuntimeError(f"單筆名目 ${size * ref} 超過測試上限 ${MAX_NOTIONAL_USD}（拒送）")

    def _log_action(self, log: ScenarioLog, label: str, request: dict, ok: bool, raw: Any,
                    error: str | None = None, counts_as_order: bool = True) -> None:
        log.leader_actions.append({"ts": datetime.now(timezone.utc).isoformat(), "label": label,
                                   "request": request, "ok": ok, "raw": raw, "error": error})
        if ok and counts_as_order:
            log.leader_writes_ok += 1
        say(f"  [{log.sid}] leader {label}: {'ok' if ok else 'FAILED'}")

    def leader_market_open(self, log: ScenarioLog, label: str, size: Decimal) -> bool:
        req = {"coin": self.coin, "is_buy": True, "size": str(size), "slippage": str(SLIPPAGE)}
        try:
            self._guard(size)
            _assert_testnet(self.leader.exchange.base_url)
            res = self.leader.adapter.market_open(self.leader.account, self.coin, True, size,
                                                  SLIPPAGE, self.builder)
        except Exception as e:  # noqa: BLE001 — 失敗是要記錄的結果，由後續判定把情境判 FAIL
            self._log_action(log, label, req, False, None, f"{type(e).__name__}: {e}")
            return False
        self._log_action(log, label, req, res.ok, res.raw)
        return res.ok

    def leader_place(self, log: ScenarioLog, label: str, order: Order, ref_size: Decimal) -> bool:
        req = {"coin": order.coin, "is_buy": order.is_buy, "size": str(order.size),
               "limit_px": str(order.limit_px), "reduce_only": order.reduce_only,
               "trigger_px": str(order.trigger_px), "tpsl": order.tpsl,
               "is_market": order.is_market, "position_tpsl": order.position_tpsl}
        try:
            self._guard(ref_size, order.limit_px, order.trigger_px or Decimal("0"))
            _assert_testnet(self.leader.exchange.base_url)
            res = self.leader.adapter.place_order(self.leader.account, order, self.builder)
        except Exception as e:  # noqa: BLE001
            self._log_action(log, label, req, False, None, f"{type(e).__name__}: {e}")
            return False
        self._log_action(log, label, req, res.ok, res.raw)
        return res.ok

    def leader_cancel(self, log: ScenarioLog, label: str, oid: int) -> bool:
        req = {"coin": self.coin, "oid": oid}
        try:
            _assert_testnet(self.leader.exchange.base_url)
            ok = self.leader.adapter.cancel_order(self.leader.account, self.coin, oid)
        except Exception as e:  # noqa: BLE001
            self._log_action(log, label, req, False, None, f"{type(e).__name__}: {e}",
                             counts_as_order=False)
            return False
        self._log_action(log, label, req, ok, None, counts_as_order=False)
        return ok

    def leader_cancel_all(self, log: ScenarioLog, label: str) -> None:
        for o in self.coin_orders(self.leader):
            self.leader_cancel(log, f"{label} oid={o['oid']}", o["oid"])
        time.sleep(1.0)

    # ---- follower 引擎（子行程）
    def engine_env(self, mirror: bool, child_out: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COPY_", "SPARK_", "FILET_", "E2E_"))}
        env.update({
            "SPARK_NETWORK": NETWORK,
            "SPARK_ACCOUNT_ID": self.account_id,
            "SPARK_USER_ADDR": self.follower.address,
            "SPARK_BUILDER_ADDR": self.builder.b,
            "COPY_LIVE_TRADING": "true",
            "COPY_MIRROR_TRIGGERS": "true" if mirror else "false",
            "COPY_LEADER_ADDRESS": self.leader.address.lower(),
            "COPY_RISK_CONTROLS_ENABLED": "false",
            "COPY_CAPITAL_UTILIZATION": CAPITAL_UTILIZATION,
            "FILET_KEYSTORE": "envfile",
            "FILET_KEYS_DIR": str(self.work / "keys"),
            "FILET_STATE_DIR": str(self.work / "state"),
            "FILET_FOLLOWERS": str(self.work / "followers.json"),
            "FILET_LEADERS_PATH": str(self.work / "leaders.json"),
            "FILET_EXCHANGE_DIR": str(self.work / "exchange"),
            "E2E_CHILD_OUT": str(child_out),
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def engine_round(self, log: ScenarioLog, label: str, mirror: bool) -> EngineRun:
        self.round_no += 1
        child_out = self.work / "engine" / f"round-{self.round_no:03d}.json"
        env = self.engine_env(mirror, child_out)
        say(f"  [{log.sid}] 引擎第 {self.round_no} 輪（{label}；mirror_triggers={mirror}）…")
        t0 = time.monotonic()
        rc: int | None
        try:
            cp = subprocess.run(
                [sys.executable, "-m", "scripts.testnet_trigger_e2e", "_engine_child"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                timeout=ENGINE_TIMEOUT_S)
            rc, so, se = cp.returncode, cp.stdout, cp.stderr
        except subprocess.TimeoutExpired as e:
            rc = None
            so = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            se = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        child = None
        if child_out.exists():
            try:
                child = json.loads(child_out.read_text())
            except (OSError, ValueError):
                child = None
        run = EngineRun(label, mirror, rc, time.monotonic() - t0, so[-4000:], se[-4000:], child)
        log.engine_rounds.append(run.to_record())
        time.sleep(2.0)   # 讓交易所端狀態穩定再讀
        return run

    # ---- reset／清理
    def _raw_flatten(self, p: Party, why: str, steps: list[dict]) -> None:
        for _ in range(3):
            for o in self.coin_orders(p):
                step: dict[str, Any] = {"party": p.name, "why": why, "action": "cancel",
                                        "oid": o.get("oid"), "orderType": o.get("orderType")}
                try:
                    _assert_testnet(p.exchange.base_url)
                    step["response"] = p.exchange.cancel(self.coin, o["oid"])
                except Exception as e:  # noqa: BLE001
                    step["error"] = f"{type(e).__name__}: {e}"
                steps.append(step)
            szi = self.szi(p)
            if szi != 0:
                step = {"party": p.name, "why": why, "action": "market_close",
                        "szi_before": str(szi)}
                try:
                    _assert_testnet(p.exchange.base_url)
                    step["response"] = p.exchange.market_close(
                        self.coin, builder={"b": self.builder.b.lower(), "f": self.builder.f})
                except Exception as e:  # noqa: BLE001
                    step["error"] = f"{type(e).__name__}: {e}"
                steps.append(step)
            time.sleep(1.5)
            if self.szi(p) == 0 and not self.coin_orders(p):
                return

    def reset_flat(self, label: str) -> bool:
        """兩邊撤該幣全部掛單、市價平倉並驗證乾淨；寫一行 reset 記錄。"""
        steps: list[dict] = []
        for p in (self.leader, self.follower):
            try:
                self._raw_flatten(p, label, steps)
            except Exception as e:  # noqa: BLE001
                steps.append({"party": p.name, "why": label, "error": f"{type(e).__name__}: {e}"})
        verdict: dict[str, Any] = {}
        clean = True
        for p in (self.leader, self.follower):
            try:
                szi, orders = self.szi(p), self.coin_orders(p)
            except Exception as e:  # noqa: BLE001
                verdict[p.name] = {"error": f"{type(e).__name__}: {e}"}
                clean = False
                continue
            verdict[p.name] = {"position_szi": str(szi), "open_orders": orders}
            clean = clean and szi == 0 and not orders
        self.rec.write("reset", label=label, clean=clean, steps=steps, state=verdict)
        return clean


# --------------------------------------------------------------------------- 情境共用步驟


def setup_synced_position(ctx: E2E, log: ScenarioLog, mirror: bool) -> bool:
    """leader 開多倉 → 引擎最多兩輪 → follower 倉位出現（前置條件，記為一條 check）。"""
    size = ctx.size_for(ctx.notional)
    opened = ctx.leader_market_open(log, "開多倉（前置）", size)
    wait_until(lambda: ctx.szi(ctx.leader) != 0, 15)
    ok = False
    runs: list[EngineRun] = []
    if opened and ctx.szi(ctx.leader) > 0:
        for k in (1, 2):
            runs.append(ctx.engine_round(log, f"前置同步倉位 #{k}", mirror))
            wait_until(lambda: ctx.szi(ctx.follower) != 0, 15)
            if ctx.szi(ctx.follower) > 0:
                ok = True
                break
    ctx.take(log, "前置：倉位同步後")
    log.check("(前置) leader 開多倉成功且 follower 倉位已鏡射（同向、非零）",
              "leader szi>0 且 follower szi>0",
              {"leader_szi": str(ctx.szi(ctx.leader)), "follower_szi": str(ctx.szi(ctx.follower)),
               "engine_rounds": len(runs)}, ok)
    return ok


@dataclass
class E1State:
    run: EngineRun
    leader: dict
    follower: dict
    sl_trigger: Decimal
    tp_trigger: Decimal


def build_e1_state(ctx: E2E, log: ScenarioLog, mirror: bool = True) -> E1State | None:
    """E1 動作：開倉並同步後，leader 掛倉位止損＋止盈，跑一輪引擎（判定輪）。
    前置（倉位同步）失敗回 None。mirror=False 即 E7 的設定。"""
    if not setup_synced_position(ctx, log, mirror):
        return None
    mid = ctx.mid()
    sl_t, tp_t = mid * FAR_DOWN, mid * FAR_UP
    ref = abs(ctx.szi(ctx.leader))
    ctx.leader_place(log, "倉位止損", position_tpsl_order(ctx.coin, "sl", sl_t), ref)
    ctx.leader_place(log, "倉位止盈", position_tpsl_order(ctx.coin, "tp", tp_t), ref)
    time.sleep(1.0)
    run = ctx.engine_round(log, "E1 判定輪", mirror)
    l_snap, f_snap = ctx.take(log, "E1 判定輪後")
    return E1State(run, l_snap, f_snap, sl_t, tp_t)


def judge_e1_state(log: ScenarioLog, st: E1State, *, prefix: str = "") -> bool:
    """follower 兩張倉位型觸發單（Stop Market＋Take Profit Market、isPositionTpsl:true）。
    回傳是否全部成立（供 E2–E5 當前置）。"""
    l_sl, l_tp = pos_tpsl(st.leader, POS_SL), pos_tpsl(st.leader, POS_TP)
    f_sl, f_tp = pos_tpsl(st.follower, POS_SL), pos_tpsl(st.follower, POS_TP)
    ok_leader = len(l_sl) == 1 and len(l_tp) == 1
    log.check(f"{prefix}(前置) leader 端已掛上倉位止損＋止盈各 1 張", "1+1",
              [brief(o) for o in pos_tpsl(st.leader)], ok_leader)
    ok_sl = len(f_sl) == 1 and f_sl[0].get("reduceOnly") is True
    ok_tp = len(f_tp) == 1 and f_tp[0].get("reduceOnly") is True
    log.check(f"{prefix}follower 倉位止損：Stop Market、isPositionTpsl:true、reduceOnly:true，1 張",
              1, [brief(o) for o in f_sl], ok_sl)
    log.check(f"{prefix}follower 倉位止盈：Take Profit Market、isPositionTpsl:true、"
              "reduceOnly:true，1 張", 1, [brief(o) for o in f_tp], ok_tp)
    total = len(triggers(st.follower))
    log.check(f"{prefix}follower 觸發單總數 == 2", 2, total, total == 2)
    same = (ok_sl and ok_tp and ok_leader
            and dec(f_sl[0]["triggerPx"]) == dec(l_sl[0]["triggerPx"])
            and dec(f_tp[0]["triggerPx"]) == dec(l_tp[0]["triggerPx"]))
    log.check(f"{prefix}(補強) follower 觸發價與 leader 相同（D8）",
              {"sl": l_sl[0]["triggerPx"] if l_sl else None,
               "tp": l_tp[0]["triggerPx"] if l_tp else None},
              {"sl": f_sl[0]["triggerPx"] if f_sl else None,
               "tp": f_tp[0]["triggerPx"] if f_tp else None}, same)
    return ok_leader and ok_sl and ok_tp and total == 2


def scale_checks(log: ScenarioLog, run: EngineRun, ctx: E2E, l_eq: Decimal, f_eq: Decimal) -> None:
    """(補強) 引擎回報的倍率與「follower 權益×使用率 / leader 權益」相差不超過 10%。"""
    scale = run.scale
    if scale is None or l_eq <= 0:
        log.check("(補強) 引擎回報的倍率可觀測", "report.scale", "無法觀測", None)
        return
    expected = f_eq * Decimal(CAPITAL_UTILIZATION) / l_eq
    ok = expected > 0 and abs(scale - expected) / expected <= SCALE_CROSSCHECK_TOL
    log.check("(補強) 引擎倍率 ≈ follower 權益×0.9÷leader 權益（容差 10%）",
              str(expected), str(scale), ok)


def qty_check(log: ScenarioLog, ctx: E2E, run: EngineRun, leader_order: dict | None,
              follower_order: dict | None, name: str) -> None:
    """數量≈leader×倍率：以引擎本輪回報的倍率重算期望值，容差一個 szDecimals 步長。"""
    scale = run.scale
    if scale is None or leader_order is None or follower_order is None:
        log.check(name, "leader sz × report.scale（捨入）",
                  {"scale": None if scale is None else str(scale),
                   "leader": None if leader_order is None else leader_order.get("sz"),
                   "follower": None if follower_order is None else follower_order.get("sz")},
                  None if scale is None else False)
        return
    expected = _round_size(dec(leader_order["sz"]) * scale, ctx.sz_dec)
    step = Decimal(1).scaleb(-ctx.sz_dec)
    actual = dec(follower_order["sz"])
    log.check(name, f"{expected}（±{step}）", str(actual), abs(actual - expected) <= step)


# --------------------------------------------------------------------------- E1–E9


def scenario_e1(ctx: E2E, log: ScenarioLog) -> None:
    st = build_e1_state(ctx, log)
    if st is None:
        return
    rc_check(log, st.run)
    judge_e1_state(log, st)
    no_crit_check(log, [st.run], "(補強) 無 CRIT")


def scenario_e2(ctx: E2E, log: ScenarioLog) -> None:
    st = build_e1_state(ctx, log)
    if st is None:
        return
    if not judge_e1_state(log, st, prefix="(前置) E1 狀態："):
        log.note("E1 狀態未成立，E2 不繼續")
        return
    old_f_sl = pos_tpsl(st.follower, POS_SL)[0]
    old_l_sl = pos_tpsl(st.leader, POS_SL)[0]
    ctx.leader_cancel(log, f"撤舊止損 oid={old_l_sl['oid']}", old_l_sl["oid"])
    new_t = ctx.mid() * FAR_DOWN_2
    ctx.leader_place(log, "改掛新止損", position_tpsl_order(ctx.coin, "sl", new_t),
                     abs(ctx.szi(ctx.leader)))
    time.sleep(1.0)
    run = ctx.engine_round(log, "E2 判定輪", True)
    l_snap, f_snap = ctx.take(log, "E2 判定輪後")
    rc_check(log, run)
    new_l_sl = pos_tpsl(l_snap, POS_SL)
    log.check("(前置) leader 端新止損已掛上（1 張、觸發價不同於舊單）", "1 張且不同",
              [brief(o) for o in new_l_sl],
              len(new_l_sl) == 1 and dec(new_l_sl[0]["triggerPx"]) != dec(old_l_sl["triggerPx"]))
    log.check("舊單消失：follower 舊止損 oid 不在掛單中", f"{old_f_sl['oid']} 不在",
              sorted(oid_set(f_snap)), old_f_sl["oid"] not in oid_set(f_snap))
    f_sl = pos_tpsl(f_snap, POS_SL)
    ok_new = (len(f_sl) == 1 and bool(new_l_sl)
              and dec(f_sl[0]["triggerPx"]) == dec(new_l_sl[0]["triggerPx"])
              and f_sl[0]["oid"] != old_f_sl["oid"])
    log.check("新單出現：follower 恰 1 張倉位止損、觸發價＝leader 新觸發價、oid 與舊單不同",
              {"triggerPx": new_l_sl[0]["triggerPx"] if new_l_sl else None,
               "oid_not": old_f_sl["oid"]}, [brief(o) for o in f_sl], ok_new)
    n_tp = len(pos_tpsl(f_snap, POS_TP))
    log.check("(補強) follower 止盈仍在、觸發單總數 == 2", "止盈 1 張、總數 2",
              {"tp": n_tp, "total": len(triggers(f_snap))}, n_tp == 1 and len(triggers(f_snap)) == 2)
    no_crit_check(log, [run])


def scenario_e3(ctx: E2E, log: ScenarioLog) -> None:
    st = build_e1_state(ctx, log)
    if st is None:
        return
    if not judge_e1_state(log, st, prefix="(前置) E1 狀態："):
        log.note("E1 狀態未成立，E3 不繼續")
        return
    for o in pos_tpsl(st.leader):
        ctx.leader_cancel(log, f"撤倉位型 oid={o['oid']}", o["oid"])
    size = abs(ctx.szi(ctx.leader))
    ctx.leader_place(log, "改掛固定數量止損限價",
                     fixed_sl_limit_order(ctx.coin, size, ctx.mid() * FAR_DOWN), size)
    time.sleep(1.0)
    l_eq, f_eq = ctx.equity(ctx.leader), ctx.equity(ctx.follower)
    run = ctx.engine_round(log, "E3 判定輪", True)
    l_snap, f_snap = ctx.take(log, "E3 判定輪後")
    rc_check(log, run)
    l_fixed = fixed_triggers(l_snap)
    log.check("(前置) leader 端只剩 1 張固定數量止損限價（Stop Limit、非倉位型）", "1 張",
              [brief(o) for o in triggers(l_snap)],
              len(l_fixed) == 1 and l_fixed[0].get("orderType") == "Stop Limit"
              and not pos_tpsl(l_snap))
    log.check("倉位型消失：follower 無任何 isPositionTpsl 觸發單", 0,
              [brief(o) for o in pos_tpsl(f_snap)], not pos_tpsl(f_snap))
    f_fixed = fixed_triggers(f_snap)
    ok_shape = (len(f_fixed) == 1 and f_fixed[0].get("orderType") == "Stop Limit"
                and f_fixed[0].get("reduceOnly") is True and f_fixed[0].get("side") == "A"
                and len(triggers(f_snap)) == 1)
    log.check("固定數量單出現：follower 恰 1 張 Stop Limit、isPositionTpsl:false、reduceOnly:true、賣",
              1, [brief(o) for o in triggers(f_snap)], ok_shape)
    qty_check(log, ctx, run, l_fixed[0] if l_fixed else None, f_fixed[0] if f_fixed else None,
              "數量≈leader×倍率")
    log.check("(補強) follower 觸發價與 leader 相同（D8）",
              l_fixed[0]["triggerPx"] if l_fixed else None,
              f_fixed[0]["triggerPx"] if f_fixed else None,
              bool(l_fixed and f_fixed)
              and dec(l_fixed[0]["triggerPx"]) == dec(f_fixed[0]["triggerPx"]))
    scale_checks(log, run, ctx, l_eq, f_eq)
    no_crit_check(log, [run], "(補強) 無 CRIT")


def scenario_e4(ctx: E2E, log: ScenarioLog) -> None:
    st = build_e1_state(ctx, log)
    if st is None:
        return
    if not judge_e1_state(log, st, prefix="(前置) E1 狀態："):
        log.note("E1 狀態未成立，E4 不繼續")
        return
    prev = st.follower
    runs: list[EngineRun] = []
    for k in (1, 2, 3):
        run = ctx.engine_round(log, f"E4 不動第 {k} 輪", True)
        runs.append(run)
        _, cur = ctx.take(log, f"E4 第 {k} 輪後")
        rc_check(log, run)
        zero_action_check(log, run, prev, cur)
        prev = cur
    no_crit_check(log, runs, "(補強) 三輪皆無 CRIT")


def scenario_e5(ctx: E2E, log: ScenarioLog) -> None:
    st = build_e1_state(ctx, log)
    if st is None:
        return
    if not judge_e1_state(log, st, prefix="(前置) E1 狀態："):
        log.note("E1 狀態未成立，E5 不繼續")
        return
    ctx.leader_cancel_all(log, "撤全部觸發單")
    run = ctx.engine_round(log, "E5 判定輪", True)
    l_snap, f_snap = ctx.take(log, "E5 判定輪後")
    rc_check(log, run)
    log.check("(前置) leader 端掛單已清空", 0, [brief(o) for o in l_snap["open_orders"]],
              not l_snap["open_orders"])
    log.check("follower 觸發單全撤（觸發單數 0）", 0, [brief(o) for o in triggers(f_snap)],
              not triggers(f_snap))
    log.check("(補強) follower 該幣掛單全空", 0, [brief(o) for o in f_snap["open_orders"]],
              not f_snap["open_orders"])
    no_crit_check(log, [run], "(補強) 無 CRIT")


# ---- E6／E9 的證據判定（純函式，離線可驗）

# E6＝多倉倉位止損（賣、倉位型、reduce-only）；E9＝條件進場（止損市價買、非 reduce-only、非倉位型）
KIND_SPEC = {
    "e6": {"is_buy": False, "reduce_only": True, "is_position_tpsl": True, "fill_dir": "Close",
           "bad_actions": frozenset({"close", "market_close"})},
    "e9": {"is_buy": True, "reduce_only": False, "is_position_tpsl": False, "fill_dir": "Open",
           "bad_actions": frozenset({"market_open"})},
}


def action_mirror_evidence(run: EngineRun | None, *, trigger: Decimal, kind: str) -> dict | None:
    """證據 A：該步引擎子行程的 action 記錄裡有一筆成功（payload.ok 為真）的 `place`，
    trigger_px 等於 leader 觸發價（已過價格捨入）且型態欄位相符。"""
    spec = KIND_SPEC[kind]
    for a in (run.child or {}).get("actions", []) if run is not None else []:
        pl = a.get("payload") or {}
        if a.get("kind") != "place" or pl.get("ok") is not True or "trigger_px" not in pl:
            continue
        try:
            same_px = dec(pl["trigger_px"]) == trigger
        except ArithmeticError:
            continue
        if (same_px and pl.get("is_position_tpsl") is spec["is_position_tpsl"]
                and pl.get("reduce_only") is spec["reduce_only"]
                and pl.get("is_buy") is spec["is_buy"]):
            return {"source": "engine_action", "action": {
                k: pl.get(k) for k in ("is_buy", "sz", "limit_px", "reduce_only", "trigger_px",
                                       "tpsl", "is_market", "is_position_tpsl", "ok", "status")}}
    return None


def snapshot_mirror_evidence(f_snap: dict, *, trigger: Decimal, kind: str) -> dict | None:
    """證據 B：follower 快照中有對應掛單（觸發價相等、型態相符）。"""
    spec = KIND_SPEC[kind]
    for o in triggers(f_snap):
        if (dec(o["triggerPx"]) == trigger and o.get("orderType") == POS_SL
                and bool(o.get("isPositionTpsl")) is spec["is_position_tpsl"]
                and o.get("reduceOnly") is spec["reduce_only"]
                and (o.get("side") == "B") is spec["is_buy"]):
            return {"source": "snapshot", "order": brief(o)}
    return None


def mirror_evidence(run: EngineRun | None, f_snap: dict, *, trigger: Decimal,
                    kind: str) -> dict | None:
    """「follower 已鏡射」＝證據 A 或 B 任一成立（A 優先）；都沒有回 None。
    快照拍在觸發之後可能看不到已成交的單，所以引擎 action 記錄也算數。"""
    return (action_mirror_evidence(run, trigger=trigger, kind=kind)
            or snapshot_mirror_evidence(f_snap, trigger=trigger, kind=kind))


def find_triggered_order(hist: list[dict], *, coin: str, trigger: Decimal, kind: str,
                         since_ms: int) -> dict | None:
    """historicalOrders（{status, statusTimestamp, order{…}} 逐狀態列）中，status=="triggered"、
    觸發價相等、型態相符、下單時間 ≥ since_ms 的那張單（取最新）；回傳 order dict。
    倉位型單的 place 回應沒有 oid，所以 oid 只能這樣從歷史找。"""
    spec = KIND_SPEC[kind]
    found = []
    for row in hist:
        o = row.get("order") or {}
        if (row.get("status") == "triggered" and o.get("coin") == coin
                and o.get("isTrigger") is True and dec(o.get("triggerPx", "0")) == trigger
                and o.get("orderType") == POS_SL
                and bool(o.get("isPositionTpsl")) is spec["is_position_tpsl"]
                and o.get("reduceOnly") is spec["reduce_only"]
                and (o.get("side") == "B") is spec["is_buy"]
                and int(o.get("timestamp", 0)) >= since_ms):
            found.append(o)
    return max(found, key=lambda o: int(o["timestamp"])) if found else None


def find_fill_for_oid(fills: list[dict], *, coin: str, oid: Any, kind: str) -> dict | None:
    """成交的 oid 等於觸發單 oid，且 dir 含 Close（E6）／Open（E9）。"""
    word = KIND_SPEC[kind]["fill_dir"]
    for f in fills:
        if f.get("coin") == coin and str(f.get("oid")) == str(oid) and word in str(f.get("dir", "")):
            return f
    return None


def exchange_side_checks(hist: list[dict] | None, fills: list[dict] | None, *, coin: str,
                         trigger: Decimal, kind: str, since_ms: int, runs: list[EngineRun],
                         error: str | None = None) -> list[tuple[str, Any, Any, bool | None]]:
    """「由交易所端觸發」的直接證據（回傳 check 四元組）：
    (1) follower 鏡射單在 historicalOrders 有 triggered；(2) 開／平倉成交 oid＝該單 oid；
    (3) 各輪引擎 action 沒有平倉類（E6）／market_open（E9）動作。"""
    label = "止損" if kind == "e6" else "條件進場單"
    out: list[tuple[str, Any, Any, bool | None]] = []
    order = None
    if hist is None:
        out.append((f"follower 鏡射{label}在 historicalOrders 有 status==triggered",
                    "triggered", f"無法觀測（{error}）", None))
    else:
        order = find_triggered_order(hist, coin=coin, trigger=trigger, kind=kind, since_ms=since_ms)
        out.append((f"follower 鏡射{label}在 historicalOrders 有 status==triggered",
                    {"triggerPx": str(trigger), "orderType": POS_SL},
                    ({k: order.get(k) for k in ("oid", "triggerPx", "orderType", "timestamp")}
                     if order is not None else "查無 triggered 紀錄"), order is not None))
    word = KIND_SPEC[kind]["fill_dir"]
    if fills is None:
        out.append((f"follower {'平' if kind == 'e6' else '開'}倉成交的 oid 等於該{label} oid",
                    "oid 相等", f"無法觀測（{error}）", None))
    elif order is None:
        out.append((f"follower {'平' if kind == 'e6' else '開'}倉成交的 oid 等於該{label} oid",
                    "oid 相等", "無法比對：沒有找到 triggered 的單", False))
    else:
        fill = find_fill_for_oid(fills, coin=coin, oid=order["oid"], kind=kind)
        out.append((f"follower {'平' if kind == 'e6' else '開'}倉成交的 oid 等於該{label} oid",
                    {"oid": order["oid"], "dir 含": word},
                    ({k: fill.get(k) for k in ("oid", "dir", "sz", "px", "time")}
                     if fill is not None else
                     f"查無 dir 含 {word} 且 oid 相等的成交（成交 oid："
                     f"{sorted({f.get('oid') for f in fills if f.get('coin') == coin})}）"),
                    fill is not None))
    bad = KIND_SPEC[kind]["bad_actions"]
    seen: list[str] = []
    observable = True
    for r in runs:
        if r.child is None:
            observable = False
            continue
        seen += [f"{r.label}:{a.get('kind')}" for a in r.child.get("actions", [])
                 if a.get("kind") in bad]
    name = ("各輪引擎 action 沒有平倉類動作（close）" if kind == "e6"
            else "各輪引擎 action 沒有 market_open")
    if seen:
        out.append((name, [], seen, False))
    elif not observable:
        out.append((name, [], "無法觀測（部分輪次引擎子行程未寫出記錄）", None))
    else:
        out.append((name, [], [], True))
    return out


def fetch_follower_history(ctx: E2E, *, kind: str, trigger: Decimal, since_ms: int,
                           end_ms: int | None = None) -> tuple[list[dict] | None,
                                                               list[dict] | None, str | None]:
    """唯讀查 follower 的 historicalOrders 與 userFillsByTime；觸發後歷史可能延遲出現，
    最多輪詢 HISTORY_POLL_S 秒直到找到 triggered 單與對應成交。查詢失敗 → (None, None, 錯誤)。"""
    deadline = time.monotonic() + HISTORY_POLL_S
    hist: list[dict] | None = None
    fills: list[dict] | None = None
    while True:
        try:
            hist = _read_retry(lambda: ctx.info.historical_orders(ctx.follower.address))
            fills = _read_retry(lambda: ctx.info.user_fills_by_time(
                ctx.follower.address, since_ms, end_ms or int(time.time() * 1000) + SLACK_MS))
        except Exception as e:  # noqa: BLE001 — 查詢失敗要明說，由呼叫端把 check 記成 null
            return None, None, f"{type(e).__name__}: {e}"
        order = find_triggered_order(hist, coin=ctx.coin, trigger=trigger, kind=kind,
                                     since_ms=since_ms)
        if order is not None and find_fill_for_oid(fills, coin=ctx.coin, oid=order["oid"],
                                                   kind=kind) is not None:
            return hist, fills, None
        if time.monotonic() >= deadline:
            return hist, fills, None
        time.sleep(3.0)


def add_exchange_side_checks(ctx: E2E, log: ScenarioLog, *, kind: str, trigger: Decimal,
                             since_ms: int, runs: list[EngineRun]) -> None:
    hist, fills, err = fetch_follower_history(ctx, kind=kind, trigger=trigger, since_ms=since_ms)
    for name, expected, actual, passed in exchange_side_checks(
            hist, fills, coin=ctx.coin, trigger=trigger, kind=kind, since_ms=since_ms, runs=runs,
            error=err):
        log.check(name, expected, actual, passed)


@dataclass
class ChaseResult:
    triggered: bool
    runs: list[EngineRun]
    steps: list[dict]
    evidence: dict | None          # 最後一步（＝觸發那一步）「follower 已鏡射」的證據；None＝沒有
    last_trigger: Decimal | None   # 最後一步的觸發價（已過價格捨入）
    last_step_start_ms: int


def chase(ctx: E2E, log: ScenarioLog, *, direction: int, frac0: Decimal,
          place: Callable[[Decimal], bool],
          evidence: Callable[[EngineRun, dict, Decimal], dict | None],
          triggered: Callable[[], bool]) -> ChaseResult:
    """E6／E9 共用：讓 leader 的觸發單真的被觸發。觸發價從現價 frac0 起，等兩邊鏡射後輪詢，
    最多等 10 分鐘，每 2 分鐘把觸發價往現價再靠近一次（距離乘 0.6：leader 撤單重掛→跑一輪引擎）。
    逾時未觸發就如實回報 triggered=False（呼叫端必須判 FAIL）。
    每步記錄「follower 已鏡射」的證據（引擎 action 記錄或快照）；觸發後才拍的快照看不到已成交的單，
    所以證據不能只靠快照。"""
    frac = frac0
    waited = 0.0
    runs: list[EngineRun] = []
    steps: list[dict] = []
    ev: dict | None = None
    last_trig: Decimal | None = None
    step_start_ms = int(time.time() * 1000)
    tick = price_tick(ctx.mid(), ctx.sz_dec)
    step = 0
    while True:
        step_start_ms = int(time.time() * 1000)
        if step > 0:
            ctx.leader_cancel_all(log, f"第 {step} 次靠近：撤舊觸發單")
        mid = ctx.mid()
        trig = chase_trigger(mid, frac, direction, tick)
        trig_r = round_px(trig, ctx.sz_dec)
        last_trig = trig_r
        placed = place(trig)
        time.sleep(1.0)
        run = ctx.engine_round(log, f"追價第 {step} 步", True)
        runs.append(run)
        _, f_snap = ctx.take(log, f"追價第 {step} 步後")
        ev = evidence(run, f_snap, trig_r) if placed else None
        steps.append({"step": step, "mid": str(mid), "frac": str(frac), "trigger": str(trig),
                      "trigger_rounded": str(trig_r), "leader_placed": placed,
                      "follower_mirror_evidence": ev, "step_start_ms": step_start_ms,
                      "waited_total_s": round(waited, 1)})
        window = min(CHASE_STEP_S, CHASE_TOTAL_S - waited)
        t0 = time.monotonic()
        hit = wait_until(triggered, window, CHASE_POLL_S)
        waited += time.monotonic() - t0
        if hit:
            return ChaseResult(True, runs, steps, ev, last_trig, step_start_ms)
        if waited >= CHASE_TOTAL_S - 1:
            return ChaseResult(False, runs, steps, ev, last_trig, step_start_ms)
        step += 1
        frac = frac * CHASE_FACTOR


def run_chase_attempts(ctx: E2E, log: ScenarioLog,
                       attempt: Callable[[ScenarioLog, Decimal], tuple[bool, list[dict]]]) -> None:
    """E6／E9：一次嘗試＝reset 兩邊後從頭來。attempt(alog, frac) 回傳 (有效?, 追價步驟)。
    無效嘗試（leader 先觸發、follower 尚未鏡射——動作記錄與快照皆無證據）自動重試，最多
    CHASE_ATTEMPTS 次，每次起始距離乘 CHASE_RETRY_GROWTH；有效嘗試的記錄併入情境並照常判定，
    無效嘗試的完整記錄留在 `attempts`；全部無效 → INCONCLUSIVE（結束碼非 0，不算通過）。"""
    frac = ctx.chase_frac
    for k in range(1, CHASE_ATTEMPTS + 1):
        alog = ScenarioLog(log.sid, log.title)
        if k > 1:
            clean = ctx.reset_flat(f"{log.sid} attempt {k}")
            if not clean:
                alog.check("(前置) 重試前兩邊該幣已清空", "clean", "not clean", False)
                log.merge(alog)
                return
        say(f"  [{log.sid}] 第 {k}/{CHASE_ATTEMPTS} 次嘗試（起始距離 {frac}）")
        valid, steps = attempt(alog, frac)
        entry: dict[str, Any] = {"attempt": k, "start_frac": str(frac), "valid": valid,
                                 "steps": steps}
        if valid:
            log.attempts.append(entry)
            log.merge(alog)
            return
        entry.update({"leader_actions": alog.leader_actions, "engine_rounds": alog.engine_rounds,
                      "snapshots": alog.snapshots, "notes": alog.notes})
        log.attempts.append(entry)
        log.leader_writes_ok += alog.leader_writes_ok
        say(f"  [{log.sid}] 嘗試 {k} 無效（leader 先觸發、follower 尚未鏡射）")
        frac = frac * CHASE_RETRY_GROWTH
    log.inconclusive = True
    log.check(f"(無效) {CHASE_ATTEMPTS} 次嘗試皆是 leader 先觸發、follower 尚未鏡射，實驗沒有測到東西",
              "至少 1 次有效嘗試", [{"attempt": a["attempt"], "start_frac": a["start_frac"]}
                                    for a in log.attempts], None)


def scenario_e6(ctx: E2E, log: ScenarioLog) -> None:
    def attempt(alog: ScenarioLog, frac: Decimal) -> tuple[bool, list[dict]]:
        if not setup_synced_position(ctx, alog, True):
            return True, []          # 前置失敗是真失敗（check 已記為 False），不重試

        def place(trig: Decimal) -> bool:
            return ctx.leader_place(alog, f"倉位止損 @{trig}",
                                    position_tpsl_order(ctx.coin, "sl", trig),
                                    abs(ctx.szi(ctx.leader)))

        res = chase(ctx, alog, direction=-1, frac0=frac, place=place,
                    evidence=lambda run, f_snap, trig: mirror_evidence(
                        run, f_snap, trigger=trig, kind="e6"),
                    triggered=lambda: ctx.szi(ctx.leader) == 0)
        alog.note("追價過程", steps=res.steps)
        if res.triggered and res.evidence is None:
            alog.note("無效嘗試：leader 先觸發，follower 尚未鏡射（動作記錄與快照皆無證據）")
            return False, res.steps
        alog.check("(前置) 觸發前最後一次放置時，follower 已鏡射同價倉位止損"
                   "（引擎 place 動作或快照其一）", "有證據", res.evidence or "無證據",
                   res.evidence is not None)
        alog.check("止損在 10 分鐘內被觸發（leader 倉位歸零）", "觸發",
                   "觸發" if res.triggered else "未觸發（逾時 10 分鐘）", res.triggered)
        runs = list(res.runs)
        if not res.triggered:
            no_crit_check(alog, runs, "(補強) 追價過程無 CRIT")
            return True, res.steps
        f_closed_by_own = ctx.szi(ctx.follower) == 0
        alog.note("leader 觸發當下 follower 倉位", follower_szi=str(ctx.szi(ctx.follower)),
                  follower_closed_before_any_engine_round=f_closed_by_own)
        for k in range(1, POST_TRIGGER_ROUNDS + 1):
            if ctx.szi(ctx.follower) == 0 and not ctx.coin_orders(ctx.follower):
                break
            runs.append(ctx.engine_round(alog, f"E6 觸發後第 {k} 輪", True))
        time.sleep(2.0)
        l_snap, f_snap = ctx.take(alog, "E6 結束")
        alog.check("兩邊皆平倉", "leader szi=0 且 follower szi=0",
                   {"leader": l_snap["position_szi"], "follower": f_snap["position_szi"]},
                   dec(l_snap["position_szi"]) == 0 and dec(f_snap["position_szi"]) == 0)
        alog.check("之後無殘留觸發單（兩邊該幣掛單皆空）", 0,
                   {"leader": [brief(o) for o in l_snap["open_orders"]],
                    "follower": [brief(o) for o in f_snap["open_orders"]]},
                   not l_snap["open_orders"] and not f_snap["open_orders"])
        no_crit_check(alog, runs, "無 CRIT（含追價與觸發後各輪）")
        add_exchange_side_checks(ctx, alog, kind="e6", trigger=res.last_trigger or Decimal(0),
                                 since_ms=res.last_step_start_ms - SLACK_MS, runs=runs)
        return True, res.steps

    run_chase_attempts(ctx, log, attempt)


def scenario_e7(ctx: E2E, log: ScenarioLog) -> None:
    st = build_e1_state(ctx, log, mirror=False)
    if st is None:
        return
    rc_check(log, st.run)
    l_ok = len(pos_tpsl(st.leader)) == 2
    log.check("(前置) leader 端已掛上倉位止損＋止盈 2 張", 2, [brief(o) for o in pos_tpsl(st.leader)],
              l_ok)
    log.check("不掛單：follower 該幣無任何掛單", 0, [brief(o) for o in st.follower["open_orders"]],
              not st.follower["open_orders"])
    acts = st.run.actions
    log.check("(補強) 引擎本輪無任何下單動作（place）", 0,
              "無法觀測" if acts is None else [a.get("kind") for a in acts if a.get("kind") == "place"],
              None if acts is None else not any(a.get("kind") == "place" for a in acts))
    warns = child_notes(st.run.child, "warn")
    if warns is None:
        log.check("category 為 orders 且含「[未鏡射]」的 warn 恰一則", 1,
                  "無法觀測（引擎子行程未寫出記錄）", None)
        log.check("沒有其他 category 為 orders 的 warn", 0, "無法觀測", None)
    else:
        all_w = [{"category": w.get("category"), "text": w.get("text")} for w in warns]
        orders_w = [w for w in warns if w.get("category") == "orders"]
        unmirrored = [w for w in orders_w if "[未鏡射]" in w.get("text", "")]
        others = [w for w in orders_w if w not in unmirrored]
        log.check("category 為 orders 且含「[未鏡射]」的 warn 恰一則", 1,
                  {"matched": len(unmirrored), "all_warns": all_w}, len(unmirrored) == 1)
        log.check("沒有其他 category 為 orders 的 warn（其他 category 的 warn 不影響判定，"
                  "全部列在 actual）", 0,
                  {"other_orders_warns": [w.get("text") for w in others], "all_warns": all_w},
                  not others)
    no_crit_check(log, [st.run])


def entry_mirrored_checks(ctx: E2E, log: ScenarioLog, run: EngineRun, l_snap: dict,
                          f_snap: dict, name_prefix: str) -> None:
    """follower 出現同型條件進場單：Stop Market、非 reduce-only、非倉位型、買、數量≈leader×倍率。"""
    l_t, f_t = triggers(l_snap), triggers(f_snap)
    log.check(f"{name_prefix}(前置) leader 端恰 1 張條件進場單（非 reduce-only）", 1,
              [brief(o) for o in l_t],
              len(l_t) == 1 and l_t[0].get("reduceOnly") is False
              and l_t[0].get("orderType") == POS_SL)
    ok_shape = (len(f_t) == 1 and f_t[0].get("orderType") == POS_SL
                and f_t[0].get("reduceOnly") is False and not f_t[0].get("isPositionTpsl")
                and f_t[0].get("side") == "B")
    log.check(f"{name_prefix}follower 出現同型單：恰 1 張 Stop Market、reduceOnly:false、"
              "isPositionTpsl:false、買", 1, [brief(o) for o in f_t], ok_shape)
    qty_check(log, ctx, run, l_t[0] if l_t else None, f_t[0] if f_t else None,
              f"{name_prefix}數量≈leader×倍率")
    log.check(f"{name_prefix}(補強) follower 觸發價與 leader 相同（D8）",
              l_t[0]["triggerPx"] if l_t else None, f_t[0]["triggerPx"] if f_t else None,
              bool(l_t and f_t) and dec(l_t[0]["triggerPx"]) == dec(f_t[0]["triggerPx"]))


def scenario_e8(ctx: E2E, log: ScenarioLog) -> None:
    size = ctx.size_for(ctx.notional)
    trig = ctx.mid() * ENTRY_UP
    ctx.leader_place(log, f"條件進場（止損市價買）@{trig}",
                     conditional_entry_order(ctx.coin, size, trig), size)
    time.sleep(1.0)
    l_eq, f_eq = ctx.equity(ctx.leader), ctx.equity(ctx.follower)
    run = ctx.engine_round(log, "E8 判定輪", True)
    l_snap, f_snap = ctx.take(log, "E8 判定輪後")
    rc_check(log, run)
    entry_mirrored_checks(ctx, log, run, l_snap, f_snap, "")
    scale_checks(log, run, ctx, l_eq, f_eq)
    runs = [run]
    prev = f_snap
    for k in (1, 2, 3):
        r = ctx.engine_round(log, f"E8 不動第 {k} 輪", True)
        runs.append(r)
        _, cur = ctx.take(log, f"E8 不動第 {k} 輪後")
        rc_check(log, r)
        zero_action_check(log, r, prev, cur)
        prev = cur
    ctx.leader_cancel_all(log, "撤條件進場單")
    r = ctx.engine_round(log, "E8 撤單後判定輪", True)
    runs.append(r)
    l_snap, f_snap = ctx.take(log, "E8 撤單後")
    rc_check(log, r)
    log.check("(前置) leader 端掛單已清空", 0, [brief(o) for o in l_snap["open_orders"]],
              not l_snap["open_orders"])
    log.check("leader 撤後 follower 撤：follower 該幣掛單全空", 0,
              [brief(o) for o in f_snap["open_orders"]], not f_snap["open_orders"])
    no_crit_check(log, runs, "(補強) 全程無 CRIT")


def follower_buy_total(ctx: E2E, since_ms: int) -> Decimal | None:
    """follower 自 since_ms 起的該幣買入成交總量（判定「沒有重複開倉」的證據）；讀不到回 None。"""
    try:
        fills = _read_retry(lambda: ctx.info.user_fills_by_time(ctx.follower.address, since_ms))
    except Exception:  # noqa: BLE001
        return None
    return sum((dec(f["sz"]) for f in fills if f.get("coin") == ctx.coin and f.get("side") == "B"),
               Decimal("0"))


def scenario_e9(ctx: E2E, log: ScenarioLog) -> None:
    def attempt(alog: ScenarioLog, frac: Decimal) -> tuple[bool, list[dict]]:
        size = ctx.size_for(ctx.notional)
        since_ms = int(time.time() * 1000)

        def place(trig: Decimal) -> bool:
            return ctx.leader_place(alog, f"條件進場（止損市價買）@{trig}",
                                    conditional_entry_order(ctx.coin, size, trig), size)

        res = chase(ctx, alog, direction=+1, frac0=frac, place=place,
                    evidence=lambda run, f_snap, trig: mirror_evidence(
                        run, f_snap, trigger=trig, kind="e9"),
                    triggered=lambda: ctx.szi(ctx.leader) != 0)
        alog.note("追價過程", steps=res.steps)
        if res.triggered and res.evidence is None:
            alog.note("無效嘗試：leader 先觸發，follower 尚未鏡射條件進場單（動作記錄與快照皆無證據）")
            return False, res.steps
        alog.check("(前置) 觸發前最後一次放置時，follower 已鏡射同價條件進場單"
                   "（引擎 place 動作或快照其一）", "有證據", res.evidence or "無證據",
                   res.evidence is not None)
        alog.check("條件進場單在 10 分鐘內被觸發（leader 出現倉位）", "觸發",
                   "觸發" if res.triggered else "未觸發（逾時 10 分鐘）", res.triggered)
        runs = list(res.runs)
        if not res.triggered:
            no_crit_check(alog, runs, "(補強) 追價過程無 CRIT")
            return True, res.steps
        alog.note("leader 觸發當下 follower 倉位", follower_szi=str(ctx.szi(ctx.follower)))
        for k in range(1, POST_TRIGGER_ROUNDS + 1):
            runs.append(ctx.engine_round(alog, f"E9 觸發後第 {k} 輪", True))
        time.sleep(2.0)
        l_snap, f_snap = ctx.take(alog, "E9 結束")
        scale = next((r.scale for r in reversed(runs) if r.scale is not None), None)
        l_szi, f_szi = dec(l_snap["position_szi"]), dec(f_snap["position_szi"])
        alog.check("兩邊都開倉（同向、非零）", "leader szi>0 且 follower szi>0",
                   {"leader": str(l_szi), "follower": str(f_szi)}, l_szi > 0 and f_szi > 0)
        step = Decimal(1).scaleb(-ctx.sz_dec)
        if scale is None:
            alog.check("follower 倉位≈leader×倍率（容忍帶內）", "leader szi × report.scale",
                       "無法取得引擎倍率", None)
            alog.check("沒有重複開倉：follower 買入成交總量未超出容忍帶上界", "≤ 期望×(1+8%)",
                       "無法取得引擎倍率", None)
        else:
            expected = l_szi * scale
            band = expected * SIZE_TOLERANCE + step
            alog.check("follower 倉位≈leader×倍率（容忍帶 8%＋1 步長；超出者須已由安全網收斂）",
                       f"{expected}（±{band}）", str(f_szi), abs(f_szi - expected) <= band)
            bought = follower_buy_total(ctx, since_ms)
            alog.check("沒有重複開倉：follower 買入成交總量 ≤ 期望×(1+8%)＋1 步長",
                       f"≤ {expected + band}", None if bought is None else str(bought),
                       None if bought is None else bought <= expected + band)
        alog.check("(補強) follower 無殘留條件進場單（兩邊該幣掛單皆空）", 0,
                   {"leader": [brief(o) for o in l_snap["open_orders"]],
                    "follower": [brief(o) for o in f_snap["open_orders"]]},
                   not l_snap["open_orders"] and not f_snap["open_orders"])
        no_crit_check(alog, runs, "無 CRIT（含追價與觸發後各輪）")
        add_exchange_side_checks(ctx, alog, kind="e9", trigger=res.last_trigger or Decimal(0),
                                 since_ms=res.last_step_start_ms - SLACK_MS, runs=runs)
        return True, res.steps

    run_chase_attempts(ctx, log, attempt)


SCENARIOS: dict[str, tuple[Callable[[E2E, ScenarioLog], None], str]] = {
    "E1": (scenario_e1, "開倉後掛倉位止損＋止盈"),
    "E2": (scenario_e2, "改止損觸發價"),
    "E3": (scenario_e3, "撤倉位型、改掛固定數量止損限價"),
    "E4": (scenario_e4, "連續 3 輪不動（不 churn）"),
    "E5": (scenario_e5, "撤掉全部觸發單"),
    "E6": (scenario_e6, "止損觸發價設到現價附近讓它觸發"),
    "E7": (scenario_e7, "COPY_MIRROR_TRIGGERS=false 重跑 E1"),
    "E8": (scenario_e8, "無倉位時掛條件進場單；3 輪後撤單"),
    "E9": (scenario_e9, "條件進場單觸發價設到現價附近讓它觸發"),
}


def run_scenario(ctx: E2E, sid: str) -> dict:
    fn, title = SCENARIOS[sid]
    log = ScenarioLog(sid, title)
    say(f"\n=== {sid} {title} ===")
    try:
        clean = ctx.reset_flat(f"before {sid}")
        log.check("(前置) 兩邊該幣已清空（無倉位、無掛單）", "clean", "clean" if clean else "not clean",
                  clean)
        if clean:
            fn(ctx, log)
    except Exception as e:  # noqa: BLE001 — 單一情境失敗不中止後續情境
        log.check("情境執行無例外", "無例外", f"{type(e).__name__}: {e}", False)
    finally:
        log.check("(補強) 至少一筆成功的 leader 下單", ">=1", log.leader_writes_ok,
                  log.leader_writes_ok >= 1)
        try:
            ctx.take(log, "情境結束")
        except Exception as e:  # noqa: BLE001
            log.note("結束快照失敗", error=f"{type(e).__name__}: {e}")
        rec = log.to_record()
        ctx.rec.write("scenario", **rec)
    return rec


# --------------------------------------------------------------------------- 前置檢查


def preflight(info: Info, adapter: HyperliquidAdapter, *, coin: str, leader: str, follower: str,
              builder: str, f: int, notional: Decimal) -> tuple[list[str], dict]:
    """零寫入的前置檢查。回傳 (失敗原因清單, 事實 dict)；清單為空才可繼續。"""
    reasons: list[str] = []
    facts: dict[str, Any] = {"coin": coin, "notional": str(notional), "f": f}
    meta = _read_retry(lambda: info.meta())
    names = {a["name"] for a in meta["universe"]}
    if coin not in names:
        reasons.append(f"{coin} 不在 testnet perp universe")
        return reasons, facts
    mids = _read_retry(lambda: info.all_mids())
    facts["mid"] = str(mids.get(coin))
    equities: dict[str, Decimal] = {}
    for role, addr in (("leader", leader), ("follower", follower)):
        st = _read_retry(lambda a=addr: info.user_state(a))
        orders = [o for o in _read_retry(lambda a=addr: info.frontend_open_orders(a))
                  if o.get("coin") == coin]
        szi = szi_from_state(st, coin)
        eq = dec(st["marginSummary"]["accountValue"])
        equities[role] = eq
        try:
            abstraction = _read_retry(lambda a=addr: info.query_user_abstraction_state(a))
        except Exception as e:  # noqa: BLE001
            abstraction = f"<查詢失敗: {type(e).__name__}>"
        try:
            max_fee = adapter.query_max_builder_fee(addr, builder)
        except Exception as e:  # noqa: BLE001
            max_fee = None
            reasons.append(f"{role} 查詢 maxBuilderFee 失敗：{type(e).__name__}")
        facts[role] = {"address": addr, "position_szi": str(szi), "open_orders": len(orders),
                       "account_value": str(eq), "max_builder_fee": max_fee,
                       "abstraction": abstraction}
        if szi != 0:
            reasons.append(f"{role} 在 {coin} 已有倉位 {szi}")
        if orders:
            reasons.append(f"{role} 在 {coin} 已有 {len(orders)} 張掛單")
        if max_fee is not None and max_fee < f:
            reasons.append(f"{role} 對 builder 的 maxBuilderFee={max_fee} < 需要的 f={f}"
                           "（請先核准 builder fee）")
    try:
        facts["builder_account_value"] = str(adapter.get_account_value(builder))
        if dec(facts["builder_account_value"]) < MIN_BUILDER_BALANCE:
            say(f"⚠️ builder 帳戶權益 {facts['builder_account_value']} < {MIN_BUILDER_BALANCE}："
                "官方文件要求 builder 帳戶有 100 USDC 才會累積 builder fee，下單可能被拒（僅警告）")
    except Exception as e:  # noqa: BLE001
        facts["builder_account_value"] = f"<查詢失敗: {type(e).__name__}>"
    mid = dec(mids[coin])
    l_eq, f_eq = equities["leader"], equities["follower"]
    if l_eq < max(MIN_EQUITY_USD, notional * MIN_EQUITY_NOTIONAL_FACTOR):
        reasons.append(f"leader 權益 {l_eq} 不足（需 ≥ {max(MIN_EQUITY_USD, notional * MIN_EQUITY_NOTIONAL_FACTOR)}）")
    if l_eq > 0:
        est = notional * f_eq * Decimal(CAPITAL_UTILIZATION) / l_eq
        facts["estimated_follower_notional"] = str(est)
        if est > MAX_NOTIONAL_USD:
            reasons.append(f"估計 follower 名目 ${est:.1f} 超過上限 ${MAX_NOTIONAL_USD}"
                           "（兩錢包權益差距過大）")
        if est < MIN_NOTIONAL_USD + 2:
            reasons.append(f"估計 follower 名目 ${est:.1f} 太小，鏡射單會被最小名目過濾")
        if f_eq < max(MIN_EQUITY_USD, est * MIN_EQUITY_NOTIONAL_FACTOR):
            reasons.append(f"follower 權益 {f_eq} 不足（需 ≥ "
                           f"{max(MIN_EQUITY_USD, est * MIN_EQUITY_NOTIONAL_FACTOR):.1f}）")
    else:
        reasons.append("leader 權益 ≤ 0")
    facts["size_at_mid"] = str(_round_size(notional / mid, _sz_decimals(meta, coin)))
    return reasons, facts


def _sz_decimals(meta: dict, coin: str) -> int:
    return next(int(a["szDecimals"]) for a in meta["universe"] if a["name"] == coin)


# --------------------------------------------------------------------------- 引擎子行程模式


def _engine_child_main() -> int:
    """`_engine_child`：載入 scripts.run_copytrade，替換 notifier 與 run_cycle（薄包裝）後
    呼叫它原本的 main(["--once"])。輸出 JSON 到 env E2E_CHILD_OUT。見檔頭「引擎可觀測性」。"""
    if os.environ.get("SPARK_NETWORK") != NETWORK:
        print("拒絕：_engine_child 要求 SPARK_NETWORK=testnet", file=sys.stderr)
        return 2
    out_path = os.environ.get("E2E_CHILD_OUT")
    if not out_path:
        print("拒絕：缺 E2E_CHILD_OUT", file=sys.stderr)
        return 2

    import scripts.run_copytrade as rc
    from spark.copytrade.notifier import RecordingNotifier

    notifier = RecordingNotifier()
    captured: dict[str, Any] = {"actions": [], "report": None}

    class _NoTelegram:
        @classmethod
        def from_env(cls, *a: Any, **k: Any) -> Any:
            raise RuntimeError("E2E：不得建立 TelegramNotifier")

    orig_run_cycle = rc.run_cycle

    def wrapped_run_cycle(adapter: Any, ex: Any, *a: Any, **k: Any) -> Any:
        start = len(ex.records)
        try:
            report = orig_run_cycle(adapter, ex, *a, **k)
            rec = report.reconcile
            captured["report"] = {
                "scale": str(report.scale), "tripped": report.tripped,
                "reconcile": {"placed": rec.placed, "cancelled": rec.cancelled,
                              "modified": rec.modified, "matched": rec.matched,
                              "sync_failed": rec.sync_failed},
                "safety_net": report.safety_net,
            }
            return report
        finally:
            captured["actions"] = [{"kind": r.kind, "coin": r.coin, "payload": r.payload}
                                   for r in ex.records[start:]]

    rc.NullNotifier = lambda: notifier        # type: ignore[assignment]
    rc.TelegramNotifier = _NoTelegram         # type: ignore[assignment]
    rc.run_cycle = wrapped_run_cycle          # type: ignore[assignment]

    exit_code = 0
    try:
        rc.main(["--once"])
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        payload = {
            "exit_code": exit_code, "actions": captured["actions"], "report": captured["report"],
            "notifications": [{"level": lv, "category": cat, "text": txt, "dedup_key": dk}
                              for lv, cat, txt, dk in notifier.records],
        }
        Path(out_path).write_text(json.dumps(payload, default=str, ensure_ascii=False))
    return exit_code


# --------------------------------------------------------------------------- main


def _epilog() -> str:
    lines = ["情境："] + [f"  {sid:<3} {title}" for sid, (_, title) in SCENARIOS.items()]
    lines.append(
        "\nverdict：PASS／FAIL／UNVERIFIED（有 check 無法觀測）／INCONCLUSIVE（E6／E9 三次嘗試"
        "皆是 leader 先觸發、follower 尚未鏡射，實驗沒測到東西）。只有 PASS 算通過。\n"
        "E6／E9 追價：觸發價從現價 --chase-frac 起，每 2 分鐘距離乘 0.6、最多等 10 分鐘；"
        "無效嘗試自動重來（reset 後起始距離乘 1.5，最多 3 次）。\n"
        "僅限 testnet。啟動前置檢查失敗 exit 2（零寫入）；任何 FAIL／UNVERIFIED／INCONCLUSIVE／"
        "leader 寫入全失敗／清理後不乾淨／私鑰副本未刪 → exit 1。\n"
        "流程、引擎可觀測性做法與環境見檔頭 docstring。")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scripts.testnet_trigger_e2e",
        description="testnet 端到端：follower 引擎鏡射 leader 觸發單（E1–E9）。僅限 testnet；"
                    "只用兩個拋棄式錢包。",
        epilog=_epilog(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--leader-pk-file", required=True,
                   help="leader 私鑰檔（路徑須含 scratchpad；權限不得開放給 group/other）")
    p.add_argument("--follower-pk-file", required=True,
                   help="follower 私鑰檔（同上）")
    p.add_argument("--builder", required=True,
                   help="builder 地址（兩錢包都必須已核准對它的 builder fee）")
    p.add_argument("--out", required=True, help="JSON lines 輸出檔（必填；不得在 repo 內）")
    p.add_argument("--only", help="只跑指定情境，逗號分隔，如 E1,E3（預設 E1–E9 全跑）")
    p.add_argument("--coin", default="BTC", help="測試幣種（預設 BTC）")
    p.add_argument("--notional", type=Decimal, default=DEFAULT_NOTIONAL_USD,
                   help=f"leader 倉位名目 USD（預設 {DEFAULT_NOTIONAL_USD}；範圍 "
                        f"{MIN_E2E_NOTIONAL_USD}–{MAX_E2E_NOTIONAL_USD}）")
    p.add_argument("--chase-frac", type=Decimal, default=CHASE_FRAC_DEFAULT,
                   help=f"E6／E9 追價的起始距離（占現價比例，預設 {CHASE_FRAC_DEFAULT}＝0.1%%；"
                        f"範圍 {CHASE_FRAC_MIN}–{CHASE_FRAC_MAX}）。每 2 分鐘距離乘 0.6；"
                        "無效嘗試（leader 先觸發、follower 尚未鏡射）重試時再乘 1.5，最多 3 次")
    return p.parse_args(argv)


def read_pk(path: Path, flag: str) -> str:
    """讀私鑰檔：必須是檔案且權限不開放 group/other；內容必須是合法私鑰。
    任何錯誤訊息都不含私鑰內容或底層例外文字。"""
    if not path.is_file():
        raise SystemExit(f"拒絕：{flag} 不是存在的檔案（{path}）")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"拒絕：{flag} 權限過寬（{oct(mode)}），請 chmod 600（{path}）")
    pk = path.read_text().strip()
    try:
        Account.from_key(pk)
    except Exception:  # noqa: BLE001 — 不回顯底層訊息，避免私鑰外洩
        raise SystemExit(f"拒絕：{flag} 內容不是合法私鑰（{path}）") from None
    return pk


def write_engine_files(work: Path, *, account_id: str, follower_pk: str, follower_addr: str,
                       leader_addr: str, builder: str) -> None:
    """引擎需要的檔案：keystore（agent.key＝follower 主鑰副本，拋棄式錢包專用）、manifest、
    leader 白名單、交換目錄、狀態目錄。keys 目錄結束時必須刪除。"""
    for d in ("state", "exchange", "engine"):
        (work / d).mkdir(parents=True, exist_ok=True)
    key_dir = work / "keys" / account_id
    key_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(work / "keys", 0o700)
    os.chmod(key_dir, 0o700)
    fd = os.open(key_dir / "agent.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, follower_pk.encode())
    finally:
        os.close(fd)
    (work / "followers.json").write_text(json.dumps({"followers": [{
        "account_id": account_id, "user_address": follower_addr.lower(),
        "builder_address": builder.lower(), "network": NETWORK,
        "leader_address": leader_addr.lower(), "label": "e2e-trigger"}]}))
    (work / "leaders.json").write_text(json.dumps({"leaders": [{
        "address": leader_addr.lower(), "name": "e2e-leader",
        "description": "testnet 觸發單 E2E 拋棄式 leader", "enabled": True,
        "accepting_new": True}]}))


def remove_keys(work: Path | None) -> bool:
    """刪除 keys 目錄（含 agent.key 私鑰副本）並驗證不存在。"""
    if work is None:
        return True
    keys = work / "keys"
    try:
        if keys.exists():
            shutil.rmtree(keys)
    except OSError as e:
        say(f"❌ 刪除 keys 目錄失敗（私鑰副本可能還在 {keys}）：{type(e).__name__}")
    gone = not keys.exists()
    say(f"keys 目錄已刪除：{gone}（{keys}）")
    return gone


def print_summary(results: list[dict], clean: bool, keys_deleted: bool) -> None:
    say("\n=== 情境總表 ===")
    say(f"{'#':<4}{'結果':<14}{'checks(P/F/N)':<16}{'leader 下單成功':<16}標題")
    for r in results:
        cs = r["checks"]
        p = sum(1 for c in cs if c["pass"] is True)
        f = sum(1 for c in cs if c["pass"] is False)
        n = sum(1 for c in cs if c["pass"] is None)
        say(f"{r['scenario']:<4}{r['verdict']:<14}{f'{p}/{f}/{n}':<16}"
            f"{r['leader_writes_ok']:<16}{r['title']}")
    say(f"清理後驗證（兩邊無倉位、無掛單）：{'VERIFIED CLEAN' if clean else 'NOT CLEAN'}")
    say(f"私鑰副本（keys 目錄）已刪除：{keys_deleted}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_engine_child"]:
        return _engine_child_main()
    args = _parse_args(argv)              # --help 在這裡結束：零網路、零私鑰檔讀取
    selected = parse_only(args.only)
    notional = check_notional(args.notional)
    chase_frac = check_chase_frac(args.chase_frac)
    leader_pk_path = require_scratchpad_path(args.leader_pk_file, "--leader-pk-file")
    follower_pk_path = require_scratchpad_path(args.follower_pk_file, "--follower-pk-file")
    if leader_pk_path == follower_pk_path:
        raise SystemExit("拒絕：leader 與 follower 的私鑰檔不得是同一個檔")
    out = resolve_out(args.out)
    try:
        builder = normalize_hex_address("--builder", args.builder)
    except ValueError as e:
        raise SystemExit(f"拒絕：{e}") from None

    leader_pk = read_pk(leader_pk_path, "--leader-pk-file")
    follower_pk = read_pk(follower_pk_path, "--follower-pk-file")
    leader_acct = Account.from_key(leader_pk)
    follower_acct = Account.from_key(follower_pk)
    if leader_acct.address == follower_acct.address:
        raise SystemExit("拒絕：leader 與 follower 是同一個錢包")

    account_id = _account_id_for(follower_acct.address)
    settings = Settings(builder_address=builder, account_id=account_id, network=NETWORK)
    _assert_testnet(settings.api_url)      # 建構任何 Exchange／簽章者之前
    info = Info(settings.api_url, skip_ws=True)
    read_adapter = HyperliquidAdapter(NETWORK, info=info, exchange=None)

    rec = E2ERecorder(out, make_redactor([leader_pk, follower_pk]))
    work: Path | None = None
    ctx: E2E | None = None
    results: list[dict] = []
    clean = False
    try:
        reasons, facts = preflight(info, read_adapter, coin=args.coin, leader=leader_acct.address,
                                   follower=follower_acct.address, builder=builder, f=settings.f,
                                   notional=notional)
        rec.write("start", label="preflight", selected=selected, passed=not reasons,
                  chase_frac=str(chase_frac),
                  reasons=reasons, facts=facts, api_url=settings.api_url, account_id=account_id,
                  engine_overrides={"COPY_RISK_CONTROLS_ENABLED": "false",
                                    "COPY_CAPITAL_UTILIZATION": CAPITAL_UTILIZATION})
        if reasons:
            for r in reasons:
                print(f"拒絕：{r}", file=sys.stderr)
            print("前置檢查未通過，零寫入退出。", file=sys.stderr)
            return 2

        work = Path(tempfile.mkdtemp(prefix=f"{out.stem}-work-", dir=str(out.parent)))
        os.chmod(work, 0o700)
        write_engine_files(work, account_id=account_id, follower_pk=follower_pk,
                           follower_addr=follower_acct.address, leader_addr=leader_acct.address,
                           builder=builder)

        def party(name: str, acct: Any) -> Party:
            ex = Exchange(acct, settings.api_url, account_address=acct.address)
            _assert_testnet(ex.base_url)
            return Party(name, acct.address, acct, ex,
                         HyperliquidAdapter(NETWORK, info=info, exchange=ex))

        leader, follower = party("leader", leader_acct), party("follower", follower_acct)
        sz_dec = read_adapter.get_size_decimals(args.coin)
        ctx = E2E(info=info, coin=args.coin, sz_dec=sz_dec,
                  builder=BuilderCode(b=builder, f=settings.f), leader=leader, follower=follower,
                  notional=notional, work=work, account_id=account_id, rec=rec,
                  chase_frac=chase_frac)
        for sid in selected:
            results.append(run_scenario(ctx, sid))
    finally:
        if ctx is not None:
            try:
                clean = ctx.reset_flat("final cleanup")
            except Exception as e:  # noqa: BLE001
                rec.write("reset", label="final cleanup", clean=False,
                          error=f"{type(e).__name__}: {e}")
            say(f"\n=== 最終驗證：{'VERIFIED CLEAN' if clean else 'NOT CLEAN'}"
                f"（{ctx.coin} 兩邊無倉位、無掛單才算乾淨）===")
        keys_deleted = remove_keys(work)
        if results or ctx is not None:
            print_summary(results, clean, keys_deleted)
            rec.write("summary", label="summary", clean=clean, keys_deleted=keys_deleted,
                      verdicts={r["scenario"]: r["verdict"] for r in results})
        rec.close()
    say(f"記錄檔：{out}")
    return overall_exit_code(results, clean, keys_deleted)


if __name__ == "__main__":
    raise SystemExit(main())
