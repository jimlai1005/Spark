"""testnet 探針：Hyperliquid 觸發單（止盈／止損／條件進場）的真實行為（plan T1，P1–P13）。

背景：docs/superpowers/plans/2026-11-21-trigger-order-mirroring.md §1、§6 T1。
官方文件沒寫清楚的行為（`positionTpsl` 數量欄、成功回應形狀、builder 能否併用、
`normalTpsl` 子單的呈現……）是假設不是事實；本探針把**原始回應原封不動記錄下來**，
不替我們下結論。結果寫進 docs/superpowers/research/2026-11-21-trigger-order-probe.md，
結論由主線程寫回 plan §4 的 D-P*。

⚠️ 僅限 testnet：base url 不含 "testnet" 直接 raise（`_assert_testnet`，建構 Exchange 前
   與每次寫入前各檢一次）。單筆名目上限 $200（`MAX_NOTIONAL_USD`）。
⚠️ 測試工具，非生產路徑：不進 systemd、不被 src/ 任何模組 import。
⚠️ 私鑰、簽章不進任何輸出：簽章者只傳給 SDK `Exchange`；記錄檔寫入前一律過 `scrub`
   （移除 signature／private key 欄位、遮蔽 ≥64 hex 字元的字串）。

直接呼叫 SDK（`Exchange.order`／`bulk_orders`／`cancel`／`market_open`／`market_close`），
因為 adapter 還不支援 trigger；**每筆下單都帶 builder**。

流程：
  1. 前置檢查（唯讀）：探測幣種在該錢包上必須「無倉位、無掛單」，否則零寫入退出
     （這樣最後的「無倉位、無掛單」驗證才有意義）。
  2. 依序執行 P1–P13（`--only P3,P5` 可只跑指定項目）。每項自己確保所需倉位姿態
     （long／short／flat），項目結束撤掉該幣所有掛單。
  3. `try/finally`：不論任何一步拋例外，最後一定撤單、市價平倉並印出驗證結果
     （VERIFIED CLEAN／NOT CLEAN；NOT CLEAN 時 exit code 1）。
  交易所拒絕是有效結果：記錄拒因後繼續下一項。

輸出：JSON lines，路徑由 `--out` 指定（必填；不得落在 repo 內）。每個下單嘗試一行
（kind="placement"）：送出的 order request、grouping、去簽章的 action、原始回應、
之後 `frontendOpenOrders` 中該幣的完整原始 dict（含巢狀 children）、P13 另記下單前後的
withdrawable／保證金。另有 cancel／stance／poll／note／item_error 等行。

環境變數（沿用 scripts/testnet_leader_drive.py 慣例，預設值＝dev testnet 的 Builder3662 錢包）：
  FILET_KEYS_DIR            envfile keystore 根目錄（預設 ~/filet-dev/keys；<root>/<account_id>/agent.key，權限 600）
  SPARK_LEADER_ADDR         探測用錢包位址（agent key 必須已授權到該位址）
  SPARK_LEADER_ACCOUNT_ID   keystore account_id（預設 'f' + 去 0x 小寫位址）
  SPARK_BUILDER_ADDR        builder 位址（預設＝錢包位址自己）
  FILET_KEYSTORE            缺省／envfile → EnvFileKeyStore；keychain → MacKeychainBackend

用法：
  uv run python -m scripts.testnet_trigger_probe --out ~/Documents/trigger-probe-run1.jsonl
  uv run python -m scripts.testnet_trigger_probe --out ... --only P3,P5
"""
import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Callable

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import order_request_to_order_wire, order_wires_to_order_action

from scripts.testnet_leader_drive import (
    DEFAULT_KEYS_DIR,
    DEFAULT_LEADER_ADDR,
    MAX_NOTIONAL_USD,
    MIN_NOTIONAL_USD,
    _account_id_for,
    _assert_testnet,
)
from spark.config import Settings
from spark.exchange.base import BuilderCode

NETWORK = "testnet"
REPO_ROOT = Path(__file__).resolve().parents[1]
ITEM_IDS = tuple(f"P{i}" for i in range(1, 14))

DEFAULT_NOTIONAL_USD = Decimal("70")   # 探測倉位名目（plan：約 $50–100）
MAX_PROBE_NOTIONAL_USD = Decimal("100")  # P6 要掛 2× 倉位量的單，2×100 才不超過 $200 上限
PRICE_SIG_FIGS = 5
MARKET_SLIPPAGE = Decimal("0.10")      # 觸發市價單的滑點保護價：觸發價 ±10%（文件：slippage tolerance 10%）
FAR_DOWN = Decimal("0.8")              # 遠離市價的觸發價倍率（不會在探測期間被碰到）
FAR_UP = Decimal("1.2")
SETTLE_S = 1.0                         # 下單／撤單後等多久再讀 frontendOpenOrders
P7_POLL_SECONDS = 10                   # P7：平倉後輪詢觸發單是否自動消失的秒數

_HEX_SECRET = re.compile(r"0x[0-9a-fA-F]{64,}")
_SECRET_KEYS = frozenset({"signature", "privatekey", "private_key", "agent_key", "agentkey"})


class ProbeError(RuntimeError):
    """探針自身前置條件／姿態轉換失敗（不是交易所對某個觸發單的拒絕）。"""


# --------------------------------------------------------------------------- pure helpers


def scrub(obj: Any) -> Any:
    """寫檔／印出前的遮蔽：signature／private key 類欄位整個換掉，≥64 hex 字元的字串遮蔽。"""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if str(k).lower() in _SECRET_KEYS else scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return _HEX_SECRET.sub("<redacted-hex>", obj)
    return obj


def round_px(px: Decimal, sz_decimals: int) -> Decimal:
    """送單價捨入：5 位有效數字，再夾到 perp 小數位上限（6 − szDecimals）。
    與 HyperliquidAdapter._round_px 同規則（本探針直接打 SDK，不經 adapter，故自備一份）。"""
    d = Decimal(px)
    if d == 0:
        return d
    d = d.quantize(Decimal(1).scaleb(d.adjusted() - (PRICE_SIG_FIGS - 1)), rounding=ROUND_HALF_EVEN)
    return d.quantize(Decimal(1).scaleb(-(6 - sz_decimals)), rounding=ROUND_HALF_EVEN)


def round_sz(sz: Decimal, sz_decimals: int) -> Decimal:
    return Decimal(sz).quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN)


def find_oids(obj: Any) -> list[int]:
    """遞迴找出回應中所有 "oid" 整數（resting／filled／未知形狀都吃，順序＝出現順序）。"""
    out: list[int] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "oid" and isinstance(v, int) and not isinstance(v, bool):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return out


def statuses_of(raw: Any) -> list | None:
    try:
        st = raw["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return None
    return st if isinstance(st, list) else None


def no_error_status(raw: Any) -> bool:
    """便利旗標（不是結論）：頂層 status=="ok" 且 statuses 非空、其中沒有 {"error":…}。
    只用來決定探針後續步驟要用哪個數量欄，判讀一律以記錄的原始回應為準。"""
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        return False
    sts = statuses_of(raw)
    if not sts:
        return False
    return not any(isinstance(s, dict) and "error" in s for s in sts)


def parse_only(spec: str | None) -> list[str]:
    """`--only P3,P5` → ['P3','P5']（依 P 編號排序、去重）；未知編號 SystemExit(2)。"""
    if not spec:
        return list(ITEM_IDS)
    wanted = {s.strip().upper() for s in spec.split(",") if s.strip()}
    unknown = sorted(wanted - set(ITEM_IDS))
    if unknown or not wanted:
        raise SystemExit(f"--only 含未知項目 {unknown or spec!r}；可用：{', '.join(ITEM_IDS)}")
    return [i for i in ITEM_IDS if i in wanted]


def resolve_out(path: str) -> Path:
    """--out 不得落在 repo 內（記錄檔含帳戶資料，不進版控）；已存在的目錄也拒絕。"""
    p = Path(path).expanduser().resolve()
    if p == REPO_ROOT or REPO_ROOT in p.parents:
        raise SystemExit(f"拒絕：--out 不得在 repo 內（收到 {p}）")
    if p.is_dir():
        raise SystemExit(f"拒絕：--out 是目錄（收到 {p}）")
    return p


def select_keystore():
    """keystore 後端依 env FILET_KEYSTORE：缺省／envfile → EnvFileKeyStore（同 testnet_leader_drive）；
    keychain → MacKeychainBackend（延後 import，--help 不碰 Keychain）。"""
    backend = os.environ.get("FILET_KEYSTORE", "envfile")
    if backend == "keychain":
        from spark.keystore.keychain import MacKeychainBackend
        return MacKeychainBackend()
    from spark.keystore.envfile import EnvFileKeyStore
    return EnvFileKeyStore(Path(os.environ.get("FILET_KEYS_DIR", DEFAULT_KEYS_DIR)).expanduser())


# --------------------------------------------------------------------------- recorder


class Recorder:
    """JSON lines：每行一筆記錄，寫檔並印出；寫入前一律過 scrub。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self.path = path

    def write(self, kind: str, **data: Any) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
        line = json.dumps(scrub(rec), ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        print(line, flush=True)
        return rec

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- probe


@dataclass
class ProbeConfig:
    coin: str
    notional_usd: Decimal
    diagnostic_no_builder: bool = False


class Probe:
    def __init__(self, exchange: Exchange, info: Info, address: str, builder: BuilderCode,
                 cfg: ProbeConfig, rec: Recorder):
        self.exchange = exchange
        self.info = info
        self.address = address
        self.builder = builder
        self.cfg = cfg
        self.coin = cfg.coin
        self.rec = rec
        self.sz_dec = self._sz_decimals()
        self.size = self._size_for_notional(cfg.notional_usd)
        # P3 實測出「哪種數量欄被接受」，後面 P4／P5／P7 沿用；未跑 P3 時 pos_sz_candidates 兩種都試。
        self.accepted_pos_sz: list[Decimal] = []

    # ---- setup reads
    def _sz_decimals(self) -> int:
        for a in self.info.meta()["universe"]:
            if a["name"] == self.coin:
                return int(a["szDecimals"])
        raise ProbeError(f"{self.coin} 不在 testnet perp universe")

    def mid(self) -> Decimal:
        return Decimal(str(self.info.all_mids()[self.coin]))

    def _size_for_notional(self, notional: Decimal) -> Decimal:
        if notional > MAX_PROBE_NOTIONAL_USD:
            raise ProbeError(f"名目 ${notional} 超過探針上限 ${MAX_PROBE_NOTIONAL_USD}")
        if notional < MIN_NOTIONAL_USD:
            raise ProbeError(f"名目 ${notional} 低於 HL 最小下單名目 ${MIN_NOTIONAL_USD}")
        size = round_sz(notional / self.mid(), self.sz_dec)
        if size <= 0:
            raise ProbeError(f"${notional} 換算 {self.coin} 數量後捨入為 0")
        return size

    # ---- state reads
    def coin_open_orders(self) -> list[dict]:
        return [o for o in self.info.frontend_open_orders(self.address) if o.get("coin") == self.coin]

    def position_szi(self) -> Decimal:
        for p in self.info.user_state(self.address).get("assetPositions", []):
            item = p.get("position", {})
            if item.get("coin") == self.coin:
                return Decimal(str(item.get("szi", "0")))
        return Decimal("0")

    def margin(self) -> dict:
        """P13 用：下單前後的 withdrawable／保證金。unifiedAccount 的保證金從 spot hold 扣，
        所以 spot USDC 的 total／hold 一併記（見 engineering-principles 事故 #6）。"""
        out: dict[str, Any] = {}
        try:
            st = self.info.user_state(self.address)
            out["withdrawable"] = st.get("withdrawable")
            out["margin_summary"] = st.get("marginSummary")
        except Exception as e:  # noqa: BLE001 — 記錄用，失敗不中止探測
            out["perp_error"] = f"{type(e).__name__}: {e}"
        try:
            for b in self.info.spot_user_state(self.address).get("balances", []):
                if b.get("coin") == "USDC":
                    out["spot_usdc_total"] = b.get("total")
                    out["spot_usdc_hold"] = b.get("hold")
        except Exception as e:  # noqa: BLE001
            out["spot_error"] = f"{type(e).__name__}: {e}"
        return out

    # ---- request builders
    def px(self, v: Decimal) -> Decimal:
        return round_px(v, self.sz_dec)

    def limit_req(self, *, is_buy: bool, sz: Decimal, px: Decimal, reduce_only: bool = False,
                  tif: str = "Gtc") -> dict:
        return {"coin": self.coin, "is_buy": is_buy, "sz": float(sz),
                "limit_px": float(self.px(px)), "order_type": {"limit": {"tif": tif}},
                "reduce_only": reduce_only}

    def trigger_req(self, *, is_buy: bool, sz: Decimal, trigger: Decimal, tpsl: str,
                    is_market: bool, reduce_only: bool, limit_px: Decimal | None = None) -> dict:
        """觸發單 request。市價型 `limit_px` 缺省＝觸發價 ±10%（買單加、賣單減）；限價型缺省＝觸發價。"""
        trig = self.px(trigger)
        if limit_px is None:
            if is_market:
                limit_px = trig * (1 + MARKET_SLIPPAGE if is_buy else 1 - MARKET_SLIPPAGE)
            else:
                limit_px = trig
        return {"coin": self.coin, "is_buy": is_buy, "sz": float(sz),
                "limit_px": float(self.px(limit_px)),
                "order_type": {"trigger": {"triggerPx": float(trig), "isMarket": is_market,
                                           "tpsl": tpsl}},
                "reduce_only": reduce_only}

    def pos_sz_candidates(self) -> list[Decimal]:
        """倉位止盈止損的數量欄候選：P3 已測出被接受的優先；否則 "0" 與倉位量都試。"""
        return list(self.accepted_pos_sz) or [Decimal("0"), self.size]

    # ---- writes
    def _builder_wire(self) -> dict:
        return {"b": self.builder.b, "f": self.builder.f}   # 每次新建：SDK 會就地改 builder["b"]

    def _guard_notional(self, reqs: list[dict]) -> None:
        mid = self.mid()
        for r in reqs:
            ref = max(Decimal(str(r["limit_px"])), mid)
            trig = r["order_type"].get("trigger")
            if trig:
                ref = max(ref, Decimal(str(trig["triggerPx"])))
            notional = Decimal(str(r["sz"])) * ref
            if notional > MAX_NOTIONAL_USD:
                raise ProbeError(f"單筆名目 ${notional} 超過測試上限 ${MAX_NOTIONAL_USD}（拒送）")

    def place(self, item: str, variant: str, reqs: list[dict], grouping: str = "na", *,
              with_builder: bool = True, with_margin: bool = False, extra: dict | None = None) -> dict:
        """送一個下單嘗試並完整記錄；回傳記錄 dict（含 oids／accepted_flag／response／error）。
        `with_builder=False` 只供 `--diagnostic-no-builder` 的 P4 對照變體使用。"""
        _assert_testnet(self.exchange.base_url)
        self._guard_notional(reqs)
        builder_wire = self._builder_wire() if with_builder else None
        wires = [order_request_to_order_wire(r, self.exchange.info.name_to_asset(r["coin"]))
                 for r in reqs]
        action = order_wires_to_order_action(
            wires,
            {"b": builder_wire["b"].lower(), "f": builder_wire["f"]} if builder_wire else None,
            grouping)
        margin_before = self.margin() if with_margin else None
        rec: dict[str, Any] = {
            "item": item, "variant": variant, "ref_mid": str(self.mid()),
            "request": reqs, "grouping": grouping, "with_builder": with_builder,
            "action": action,
        }
        if extra:
            rec["extra"] = extra
        try:
            if grouping == "na" and len(reqs) == 1 and with_builder:
                r = reqs[0]
                raw = self.exchange.order(r["coin"], r["is_buy"], r["sz"], r["limit_px"],
                                          r["order_type"], reduce_only=r["reduce_only"],
                                          builder=builder_wire)
            else:
                raw = self.exchange.bulk_orders(reqs, builder_wire, grouping)
            rec["response"] = raw
        except Exception as e:  # noqa: BLE001 — 拒絕／傳輸錯誤都是要記錄的結果
            rec["error"] = f"{type(e).__name__}: {e}"
        time.sleep(SETTLE_S)
        oids = find_oids(rec.get("response"))
        orders = self.coin_open_orders()
        rec["oids"] = oids
        rec["no_error_status"] = no_error_status(rec.get("response"))
        rec["open_orders_after"] = orders
        rec["target_orders"] = [o for o in orders if o.get("oid") in oids]
        if with_margin:
            rec["margin_before"] = margin_before
            rec["margin_after"] = self.margin()
        return self.rec.write("placement", **rec)

    def cancel_oid(self, item: str, label: str, oid: int) -> dict:
        _assert_testnet(self.exchange.base_url)
        rec: dict[str, Any] = {"item": item, "label": label, "oid": oid}
        try:
            rec["response"] = self.exchange.cancel(self.coin, oid)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
        time.sleep(SETTLE_S)
        rec["open_orders_after"] = self.coin_open_orders()
        return self.rec.write("cancel", **rec)

    def cancel_coin_orders(self, item: str, why: str) -> list[dict]:
        """撤掉該幣全部掛單（前置檢查保證起始為空，所以現存的都是探針自己掛的）；
        最多 3 輪，回傳最後仍殘留的掛單。"""
        for _ in range(3):
            orders = self.coin_open_orders()
            if not orders:
                break
            for o in orders:
                rec: dict[str, Any] = {"item": item, "why": why, "oid": o.get("oid"),
                                       "orderType": o.get("orderType")}
                try:
                    rec["response"] = self.exchange.cancel(self.coin, o["oid"])
                except Exception as e:  # noqa: BLE001
                    rec["error"] = f"{type(e).__name__}: {e}"
                self.rec.write("cancel", **rec)
            time.sleep(SETTLE_S)
        leftover = self.coin_open_orders()
        self.rec.write("cleanup_check", item=item, why=why, leftover_orders=leftover)
        return leftover

    # ---- position management
    def _wait_position(self, pred: Callable[[Decimal], bool], timeout_s: float = 15.0) -> Decimal:
        deadline = time.monotonic() + timeout_s
        szi = self.position_szi()
        while not pred(szi) and time.monotonic() < deadline:
            time.sleep(1.0)
            szi = self.position_szi()
        return szi

    def open_position(self, item: str, is_buy: bool) -> None:
        _assert_testnet(self.exchange.base_url)
        self._guard_notional([{"sz": float(self.size), "limit_px": float(self.mid()),
                               "order_type": {"limit": {}}}])
        rec: dict[str, Any] = {"item": item, "action": "market_open", "is_buy": is_buy,
                               "sz": str(self.size)}
        try:
            rec["response"] = self.exchange.market_open(
                self.coin, is_buy, float(self.size), builder=self._builder_wire())
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
        szi = self._wait_position(lambda s: s != 0)
        rec["position_szi_after"] = str(szi)
        self.rec.write("stance", **rec)
        if szi == 0:
            raise ProbeError(f"開 {'多' if is_buy else '空'}倉失敗（{self.coin} 仍無倉位）")

    def close_position(self, item: str) -> bool:
        """市價平倉（reduce-only IOC，帶 builder），最多 3 次；回傳是否已無倉位。"""
        _assert_testnet(self.exchange.base_url)
        for attempt in range(3):
            szi = self.position_szi()
            if szi == 0:
                return True
            rec: dict[str, Any] = {"item": item, "action": "market_close", "attempt": attempt,
                                   "position_szi_before": str(szi)}
            try:
                rec["response"] = self.exchange.market_close(
                    self.coin, builder=self._builder_wire())
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"{type(e).__name__}: {e}"
            szi_after = self._wait_position(lambda s: s == 0)
            rec["position_szi_after"] = str(szi_after)
            self.rec.write("stance", **rec)
        return self.position_szi() == 0

    def ensure(self, item: str, stance: str) -> None:
        """確保倉位姿態：flat／long／short（long/short 要求倉位量 == 探針的 size）。
        不符就先撤該幣全部掛單、平倉、再依需要開倉。"""
        szi = self.position_szi()
        ok = {"flat": szi == 0, "long": szi == self.size, "short": szi == -self.size}[stance]
        if ok:
            return
        self.cancel_coin_orders(item, f"before stance change to {stance}")
        if szi != 0 and not self.close_position(item):
            raise ProbeError("平倉後仍有倉位，無法轉換姿態")
        if stance != "flat":
            self.open_position(item, is_buy=(stance == "long"))
        final = self.position_szi()
        self.rec.write("note", item=item, text=f"stance={stance}", position_szi=str(final))

    def variant_cleanup(self, item: str, variant: str) -> None:
        self.cancel_coin_orders(item, f"variant cleanup {variant}")


# --------------------------------------------------------------------------- P1–P13


def item_p1(p: Probe) -> None:
    """P1 reduce-only 止損限價單（grouping=na、帶 builder）是否被接受？statuses[0] 形狀？
    （另測 reduce-only 止盈限價，兩種 tpsl 各留一份形狀。）"""
    p.ensure("P1", "long")
    mid = p.mid()
    for variant, tpsl, trig in (("sl_limit", "sl", mid * FAR_DOWN), ("tp_limit", "tp", mid * FAR_UP)):
        p.place("P1", variant, [p.trigger_req(is_buy=False, sz=p.size, trigger=trig, tpsl=tpsl,
                                              is_market=False, reduce_only=True)])
        p.variant_cleanup("P1", variant)


def item_p2(p: Probe) -> None:
    """P2 reduce-only 止損市價單（isMarket=true，p＝觸發價 −10% 的滑點價）是否被接受？回讀的 limitPx？"""
    p.ensure("P2", "long")
    mid = p.mid()
    for variant, tpsl, trig in (("sl_market", "sl", mid * FAR_DOWN), ("tp_market", "tp", mid * FAR_UP)):
        p.place("P2", variant, [p.trigger_req(is_buy=False, sz=p.size, trigger=trig, tpsl=tpsl,
                                              is_market=True, reduce_only=True)])
        p.variant_cleanup("P2", variant)


def item_p3(p: Probe) -> None:
    """P3 grouping=positionTpsl：(a) s="0"、(b) s＝目前倉位量，各自是否被接受？回讀 sz／origSz／isPositionTpsl？"""
    p.ensure("P3", "long")
    p.accepted_pos_sz.clear()
    mid = p.mid()
    for variant, sz in (("s_zero", Decimal("0")), ("s_position", p.size)):
        rec = p.place("P3", variant,
                      [p.trigger_req(is_buy=False, sz=sz, trigger=mid * FAR_DOWN, tpsl="sl",
                                     is_market=True, reduce_only=True)],
                      grouping="positionTpsl")
        if rec["no_error_status"]:
            p.accepted_pos_sz.append(sz)
        p.variant_cleanup("P3", variant)
    p.rec.write("note", item="P3", text="accepted position-tpsl size fields",
                accepted=[str(s) for s in p.accepted_pos_sz])


def item_p4(p: Probe) -> None:
    """P4 positionTpsl 併帶 builder 是否被接受？
    P3 已全程帶 builder；P4 另測 (a) 止盈型、(b) 止盈＋止損成對送出（交易所介面的實際形狀）。
    `--diagnostic-no-builder` 才多一個 (c) 不帶 builder 的對照（預設關：本探針規範每筆都帶 builder）。"""
    p.ensure("P4", "long")
    mid = p.mid()
    sz = p.pos_sz_candidates()[0]
    tp = p.trigger_req(is_buy=False, sz=sz, trigger=mid * FAR_UP, tpsl="tp",
                       is_market=True, reduce_only=True)
    sl = p.trigger_req(is_buy=False, sz=sz, trigger=mid * FAR_DOWN, tpsl="sl",
                       is_market=True, reduce_only=True)
    p.place("P4", "tp_single", [tp], grouping="positionTpsl", extra={"s": str(sz)})
    p.variant_cleanup("P4", "tp_single")
    p.place("P4", "tp_sl_pair", [tp, sl], grouping="positionTpsl", extra={"s": str(sz)})
    p.variant_cleanup("P4", "tp_sl_pair")
    if p.cfg.diagnostic_no_builder:
        p.place("P4", "diagnostic_no_builder", [sl], grouping="positionTpsl", with_builder=False,
                extra={"s": str(sz)})
        p.variant_cleanup("P4", "diagnostic_no_builder")


def item_p5(p: Probe) -> None:
    """P5 已有一張倉位止損時再掛第二張（不同觸發價）：並存、取代、還是被拒？"""
    p.ensure("P5", "long")
    mid = p.mid()
    sz = p.pos_sz_candidates()[0]
    first = p.place("P5", "first_sl", [p.trigger_req(
        is_buy=False, sz=sz, trigger=mid * FAR_DOWN, tpsl="sl", is_market=True, reduce_only=True)],
        grouping="positionTpsl", extra={"s": str(sz)})
    second = p.place("P5", "second_sl_other_trigger", [p.trigger_req(
        is_buy=False, sz=sz, trigger=mid * Decimal("0.75"), tpsl="sl", is_market=True,
        reduce_only=True)],
        grouping="positionTpsl", extra={"s": str(sz)})
    p.rec.write("note", item="P5", text="first vs second oids（並存＝兩者皆在 open_orders_after）",
                first_oids=first["oids"], second_oids=second["oids"])
    p.variant_cleanup("P5", "end")


def item_p6(p: Probe) -> None:
    """P6 固定數量 reduce-only 觸發單的數量 > 倉位量（2×）：是否被接受？會不會被修剪？"""
    p.ensure("P6", "long")
    mid = p.mid()
    big = round_sz(p.size * 2, p.sz_dec)
    p.place("P6", "sl_limit_2x_position", [p.trigger_req(
        is_buy=False, sz=big, trigger=mid * FAR_DOWN, tpsl="sl", is_market=False, reduce_only=True)],
        extra={"position_size": str(p.size), "order_size": str(big)})
    p.variant_cleanup("P6", "sl_limit_2x_position")


def item_p7(p: Probe) -> None:
    """P7 市價平倉後，倉位止盈止損與固定數量 reduce-only 觸發單是否自動消失（輪詢 10 秒）？"""
    p.ensure("P7", "long")
    mid = p.mid()
    pos_sz = p.pos_sz_candidates()[0]
    p.place("P7", "position_tpsl_sl", [p.trigger_req(
        is_buy=False, sz=pos_sz, trigger=mid * FAR_DOWN, tpsl="sl", is_market=True,
        reduce_only=True)], grouping="positionTpsl", extra={"s": str(pos_sz)})
    p.place("P7", "fixed_size_ro_sl_limit", [p.trigger_req(
        is_buy=False, sz=p.size, trigger=mid * Decimal("0.85"), tpsl="sl", is_market=False,
        reduce_only=True)])
    p.rec.write("note", item="P7", text="before close", open_orders=p.coin_open_orders())
    closed = p.close_position("P7")
    p.rec.write("note", item="P7", text="position closed", closed=closed)
    for t in range(1, P7_POLL_SECONDS + 1):
        time.sleep(1.0)
        orders = p.coin_open_orders()
        p.rec.write("poll", item="P7", t_seconds=t, position_szi=str(p.position_szi()),
                    open_orders=orders)
        if not orders:
            break
    p.variant_cleanup("P7", "end")


def item_p8(p: Probe) -> None:
    """P8 方向不符的 reduce-only 觸發單：接受或拒絕？拒因字串？
    三種姿態：(a) 無倉位掛 reduce-only 賣、(b) 多倉掛 reduce-only 買、(c) 空倉掛 reduce-only 賣。"""
    mid_of = p.mid
    p.ensure("P8", "flat")
    p.place("P8", "a_flat_ro_sell", [p.trigger_req(
        is_buy=False, sz=p.size, trigger=mid_of() * FAR_DOWN, tpsl="sl", is_market=False,
        reduce_only=True)])
    p.variant_cleanup("P8", "a_flat_ro_sell")
    p.ensure("P8", "long")
    p.place("P8", "b_long_ro_buy", [p.trigger_req(
        is_buy=True, sz=p.size, trigger=mid_of() * FAR_UP, tpsl="sl", is_market=False,
        reduce_only=True)])
    p.variant_cleanup("P8", "b_long_ro_buy")
    p.ensure("P8", "short")
    p.place("P8", "c_short_ro_sell", [p.trigger_req(
        is_buy=False, sz=p.size, trigger=mid_of() * FAR_DOWN, tpsl="sl", is_market=False,
        reduce_only=True)])
    p.variant_cleanup("P8", "c_short_ro_sell")


def item_p9(p: Probe) -> None:
    """P9 以 oid 撤觸發單（Exchange.cancel）的回應形狀是否與限價單相同？
    同一次各掛一張普通限價單與一張觸發單，各自 cancel 並記錄原始回應。"""
    p.ensure("P9", "long")
    mid = p.mid()
    plain = p.place("P9", "plain_limit", [p.limit_req(is_buy=True, sz=p.size, px=mid * Decimal("0.7"))])
    trig = p.place("P9", "trigger_sl_limit", [p.trigger_req(
        is_buy=False, sz=p.size, trigger=mid * FAR_DOWN, tpsl="sl", is_market=False,
        reduce_only=True)])
    for label, rec in (("plain_limit", plain), ("trigger_sl_limit", trig)):
        for oid in rec["oids"]:
            p.cancel_oid("P9", label, oid)
    p.variant_cleanup("P9", "end")


def item_p10(p: Probe) -> None:
    """P10 觸發價已被越過（空倉的止損買單觸發價低於現價）：拒因字串？限價型與市價型各一。"""
    p.ensure("P10", "short")
    for variant, is_market in (("sl_buy_limit_crossed", False), ("sl_buy_market_crossed", True)):
        p.place("P10", variant, [p.trigger_req(
            is_buy=True, sz=p.size, trigger=p.mid() * Decimal("0.9"), tpsl="sl",
            is_market=is_market, reduce_only=True)])
        p.variant_cleanup("P10", variant)


def item_p11(p: Probe) -> None:
    """P11 名目 < $10 的固定數量 reduce-only 觸發單：接受或拒絕？（限價型與市價型各一，目標約 $5）"""
    p.ensure("P11", "long")
    mid = p.mid()
    small = round_sz(Decimal("5") / mid, p.sz_dec)
    if small <= 0:
        p.rec.write("note", item="P11", text="$5 換算數量捨入為 0，無法測試", mid=str(mid))
        return
    for variant, is_market in (("sl_limit_small", False), ("sl_market_small", True)):
        p.place("P11", variant, [p.trigger_req(
            is_buy=False, sz=small, trigger=mid * FAR_DOWN, tpsl="sl", is_market=is_market,
            reduce_only=True)],
            extra={"order_size": str(small), "notional_at_mid": str(small * mid)})
        p.variant_cleanup("P11", variant)


def item_p12(p: Probe) -> None:
    """P12 遠離市價的限價母單附帶止盈止損（grouping=normalTpsl）：母單未成交時，子單在
    frontendOpenOrders 是巢狀在母單 children、還是也出現在最上層？有無欄位可與「已生效觸發單」區分？
    測完只撤母單，確認子單一併消失。"""
    mid = p.mid()
    parent = p.limit_req(is_buy=True, sz=p.size, px=mid * Decimal("0.7"))
    tp = p.trigger_req(is_buy=False, sz=p.size, trigger=mid * Decimal("1.1"), tpsl="tp",
                       is_market=True, reduce_only=True)
    sl = p.trigger_req(is_buy=False, sz=p.size, trigger=mid * Decimal("0.6"), tpsl="sl",
                       is_market=True, reduce_only=True)
    rec = p.place("P12", "normal_tpsl_parent_unfilled", [parent, tp, sl], grouping="normalTpsl")
    try:
        p.rec.write("note", item="P12", text="open_orders (non-frontend) for comparison",
                    open_orders_basic=[o for o in p.info.open_orders(p.address)
                                       if o.get("coin") == p.coin])
    except Exception as e:  # noqa: BLE001
        p.rec.write("note", item="P12", text="open_orders read failed", error=f"{type(e).__name__}: {e}")
    if rec["oids"]:
        parent_oid = rec["oids"][0]   # statuses 順序＝request 順序，母單在最前
        p.cancel_oid("P12", "parent_only", parent_oid)
        time.sleep(3.0)
        p.rec.write("note", item="P12", text="after cancelling parent only (+3s)",
                    open_orders=p.coin_open_orders())
    p.variant_cleanup("P12", "end")


def item_p13(p: Probe) -> None:
    """P13 非 reduce-only 的條件進場觸發單（grouping=na、帶 builder、觸發價遠離現價、無倉位）：
    是否被接受？statuses[0] 形狀？未觸發時是否佔用保證金（下單前後 withdrawable／保證金）？
    止損市價／止損限價（plan 指定）＋止盈市價／止盈限價（買進逢低型，D3 也要跟）。"""
    p.ensure("P13", "flat")
    mid_of = p.mid
    variants = (
        ("sl_market_buy", "sl", True, FAR_UP),
        ("sl_limit_buy", "sl", False, FAR_UP),
        ("tp_market_buy", "tp", True, FAR_DOWN),
        ("tp_limit_buy", "tp", False, FAR_DOWN),
    )
    for variant, tpsl, is_market, mult in variants:
        p.place("P13", variant, [p.trigger_req(
            is_buy=True, sz=p.size, trigger=mid_of() * mult, tpsl=tpsl, is_market=is_market,
            reduce_only=False)], with_margin=True)
        p.variant_cleanup("P13", variant)
        p.rec.write("note", item="P13", text=f"margin after cancel ({variant})", margin=p.margin())


ITEMS: dict[str, tuple[Callable[[Probe], None], str]] = {
    "P1": (item_p1, "reduce-only 止損/止盈限價（na＋builder）"),
    "P2": (item_p2, "reduce-only 止損/止盈市價（isMarket、p±10%）"),
    "P3": (item_p3, "positionTpsl：s=0 vs s=倉位量"),
    "P4": (item_p4, "positionTpsl 併帶 builder（止盈型、成對）"),
    "P5": (item_p5, "第二張倉位止損：並存/取代/被拒"),
    "P6": (item_p6, "固定數量 > 倉位量（2×）"),
    "P7": (item_p7, "平倉後觸發單是否自動消失（輪詢 10 秒）"),
    "P8": (item_p8, "方向不符的 reduce-only 觸發單（無倉/多倉買/空倉賣）"),
    "P9": (item_p9, "以 oid 撤觸發單的回應形狀"),
    "P10": (item_p10, "觸發價已被越過（空倉止損買單）"),
    "P11": (item_p11, "名目 < $10 的固定數量觸發單"),
    "P12": (item_p12, "normalTpsl 子單的呈現（母單未成交）"),
    "P13": (item_p13, "非 reduce-only 條件進場觸發單＋保證金佔用"),
}


# --------------------------------------------------------------------------- orchestration


def run_probe(p: Probe, selected: list[str]) -> bool:
    """跑選定項目並在 finally 清理；回傳最終是否 VERIFIED CLEAN。"""
    clean = False
    try:
        for pid in selected:
            fn, desc = ITEMS[pid]
            p.rec.write("item_start", item=pid, desc=desc)
            try:
                fn(p)
            except Exception as e:  # noqa: BLE001 — 單項失敗（含 ProbeError）記錄後繼續下一項
                p.rec.write("item_error", item=pid, error=f"{type(e).__name__}: {e}")
            finally:
                p.cancel_coin_orders(pid, "item cleanup")
            p.rec.write("item_end", item=pid)
    finally:
        clean = _final_cleanup_and_verify(p)
    return clean


def _final_cleanup_and_verify(p: Probe) -> bool:
    """最終清理：撤單 → 市價平倉 → 再撤單 → 驗證「無倉位、無掛單」。自己的失敗只記錄不再拋，
    否則會蓋掉原本例外；驗證結果決定 exit code。"""
    verdict = "NOT CLEAN"
    try:
        p.cancel_coin_orders("final", "final cleanup")
        p.close_position("final")
        p.cancel_coin_orders("final", "final cleanup after close")
        szi = p.position_szi()
        orders = p.coin_open_orders()
        if szi == 0 and not orders:
            verdict = "VERIFIED CLEAN"
        p.rec.write("final_verification", verdict=verdict, coin=p.coin,
                    position_szi=str(szi), open_orders=orders,
                    margin=p.margin())
    except Exception as e:  # noqa: BLE001
        p.rec.write("final_verification", verdict="NOT CLEAN",
                    error=f"cleanup failed: {type(e).__name__}: {e}")
    print(f"\n=== 最終驗證：{verdict}（{p.coin} 無倉位、無掛單才算乾淨）===", flush=True)
    return verdict == "VERIFIED CLEAN"


def _epilog() -> str:
    lines = ["項目："] + [f"  {pid:<4} {desc}" for pid, (_, desc) in ITEMS.items()]
    lines.append("\n環境變數與流程見檔頭 docstring。僅限 testnet；單筆名目上限 $200。")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scripts.testnet_trigger_probe",
        description="testnet 探針：Hyperliquid 觸發單（止盈/止損/條件進場）真實行為 P1–P13。"
                    "只記錄原始回應，不下結論。僅限 testnet。",
        epilog=_epilog(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True,
                   help="JSON lines 輸出檔（必填；不得在 repo 內）")
    p.add_argument("--only", help="只跑指定項目，逗號分隔，如 P3,P5（預設 P1–P13 全跑）")
    p.add_argument("--coin", default="BTC", help="探測幣種（預設 BTC）")
    p.add_argument("--notional", type=Decimal, default=DEFAULT_NOTIONAL_USD,
                   help=f"探測倉位名目 USD（預設 {DEFAULT_NOTIONAL_USD}；範圍 "
                        f"{MIN_NOTIONAL_USD}–{MAX_PROBE_NOTIONAL_USD}）")
    p.add_argument("--diagnostic-no-builder", action="store_true",
                   help="P4 多測一個不帶 builder 的 positionTpsl 對照變體（預設關）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)           # --help 在這裡結束：零網路、零 Keychain
    selected = parse_only(args.only)
    out = resolve_out(args.out)

    address = os.environ.get("SPARK_LEADER_ADDR", DEFAULT_LEADER_ADDR)
    account_id = os.environ.get("SPARK_LEADER_ACCOUNT_ID") or _account_id_for(address)
    builder_addr = os.environ.get("SPARK_BUILDER_ADDR", address.lower())
    settings = Settings(builder_address=builder_addr, account_id=account_id, network=NETWORK)
    _assert_testnet(settings.api_url)  # 建構任何 Exchange／簽章者之前

    signer = select_keystore().get_agent_signer(account_id)
    info = Info(settings.api_url, skip_ws=True)
    exchange = Exchange(signer, settings.api_url, account_address=address)
    _assert_testnet(exchange.base_url)

    rec = Recorder(out)
    cfg = ProbeConfig(coin=args.coin, notional_usd=args.notional,
                      diagnostic_no_builder=args.diagnostic_no_builder)
    try:
        probe = Probe(exchange, info, address, BuilderCode(b=settings.builder_address, f=settings.f),
                      cfg, rec)
        # 前置檢查（唯讀）：探測幣種必須無倉位、無掛單，最後的清潔驗證才有意義。
        szi, orders = probe.position_szi(), probe.coin_open_orders()
        try:
            abstraction = info.query_user_abstraction_state(address)
        except Exception as e:  # noqa: BLE001
            abstraction = f"<查詢失敗: {type(e).__name__}: {e}>"
        rec.write("start", address=address, builder=settings.builder_address, f=settings.f,
                  api_url=settings.api_url, coin=probe.coin, sz_decimals=probe.sz_dec,
                  probe_size=str(probe.size), selected=selected, account_abstraction=abstraction,
                  margin=probe.margin(), position_szi=str(szi), open_orders=orders)
        if szi != 0 or orders:
            print(f"拒絕：{probe.coin} 在 {address} 上已有倉位或掛單，零寫入退出"
                  "（請先手動清掉，否則最後的清潔驗證無意義）。", file=sys.stderr)
            return 2
        clean = run_probe(probe, selected)
    finally:
        rec.close()
    print(f"記錄檔：{out}")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
