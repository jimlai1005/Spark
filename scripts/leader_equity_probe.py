"""scripts/leader_equity_probe.py
leader 權益（跟單倍率分母）的**主網唯讀探針**（plan 2026-11-22 T3）。

動機：引擎把倍率分母改成「交易所顯示的帳戶總值」——`HyperliquidAdapter.get_account_total`
（`portfolio` 最新一點）經 `spark.copytrade.leader_equity.resolve_leader_equity`。離線測試的
假資料是照同一個假設寫的，測不出欄位名或語意假設錯（工程原則 1 的「欄位名是假設」教訓），
所以上線前用**正式程式路徑**對真實帳戶取值，並與一個**獨立重建**的數字對帳。

唯讀聲明：只打 Hyperliquid 主網公開 `/info`；不需要也不讀任何金鑰、`.env*`、設定檔；
不建立 Exchange／Signer 等任何會簽章的物件（`HyperliquidAdapter(..., exchange=None)`）。
`--help` 與參數檢查階段零網路（Info 物件在參數驗證通過之後才建立）。

用法：
  uv run python -m scripts.leader_equity_probe [--out PATH.jsonl] \\
      [--tolerance-abs 1.0] [--tolerance-pct 0.001] ADDRESS [ADDRESS ...]

兩側（每個位址）：
  - 正式路徑：`adapter.get_account_total(addr)` → `resolve_leader_equity(total, now_ms, leader_has_exposure)`；
    曝險＝`adapter.get_positions`（clearinghouseState 預設 dex）有倉位，或無倉位時
    `adapter.get_open_orders`（frontendOpenOrders）非空。**這一側不得有第二份等價邏輯。**
  - 獨立重建（直接打 `info` 原始端點；**不呼叫 `get_account_total`、不讀 `portfolio`**）：
      userAbstraction 模式；
      spotClearinghouseState 各代幣 total × 該代幣對 USDC 的 mark
        （spotMetaAndAssetCtxs：meta.tokens index→name；meta.universe 的 pair tokens=[base, quote] 與 name；
          ctxs 以 coin == pair name 對應取 markPx；USDC 本身以 1 計；
          找不到報價的非零代幣列入 unpriced，該位址標記 UNPRICED）；
      ＋ 質押 delegatorSummary.delegated × HYPE mark；
      ＋ userVaultEquities 各筆 equity 加總；
      ＋ 僅當模式為 "disabled"（傳統模式）：預設 dex 與 perpDexs 列出的每個 dex 的
        clearinghouseState.marginSummary.accountValue。
        unifiedAccount／portfolioMargin／default／dexAbstraction **不加** perp accountValue。
    規則出處：docs/superpowers/research/2026-11-22-leader-equity-basis-recon.md §2
    （unified／portfolioMargin：T = S，perp 未實現損益已記在現貨 USDC total，perp accountValue
      不得再加；disabled：T = S + A）。

判定：PASS＝|重建 − 正式| ≤ max(tolerance_abs, tolerance_pct × |正式值|)（tolerance_pct 是小數，
0.001＝0.1%）；否則 FAIL；有 unpriced 且未通過 → UNPRICED（視同未通過）；讀取例外 → ERROR。
兩側讀取之間行情會動：先讀正式路徑、緊接著讀重建端點；第一次未通過自動重試整個位址一次
（最多 2 次），以第二次為準並標註 retried。

Exit code：全部 PASS → 0；任何位址未通過或讀取例外 → 1；位址格式錯 → 2。

預期請求數（每位址一次嘗試）：正式 portfolio＋clearinghouseState（＋無倉位時 frontendOpenOrders）；
重建 userAbstraction、spotClearinghouseState、spotMetaAndAssetCtxs、delegatorSummary、
userVaultEquities、clearinghouseState（預設 dex）；disabled 另加 perpDexs 與每個非預設 dex 一次
clearinghouseState。另有建立 Info 時 SDK 的 spot_meta／meta 初始化（整個程序一次）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from spark.config import API_URLS
from spark.copytrade.leader_equity import LeaderEquityUnavailable, resolve_leader_equity
from spark.exchange.hyperliquid import HyperliquidAdapter

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ZERO = Decimal("0")
_MAX_ATTEMPTS = 2
# 只有傳統模式把 perp accountValue 另外加總（recon §2）；其餘模式一律不加。
_PERP_ADDED_MODES = frozenset({"disabled"})


# ── 純函式層 ────────────────────────────────────────────────────────────────

def is_valid_address(s: str) -> bool:
    return bool(_ADDR_RE.match(s))


def _dec(x: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(x))
    except Exception:
        raise ValueError(f"{what} 無法解析為數字") from None
    if not d.is_finite():
        raise ValueError(f"{what} 不是有限數")
    return d


@dataclass(frozen=True)
class Rebuild:
    mode: str
    usdc: Decimal
    other_spot: Decimal
    other_spot_detail: dict[str, Decimal]
    staking: Decimal
    vault: Decimal
    perp_default: Decimal           # 預設 dex accountValue（僅供顯示；非 disabled 不計入）
    perp_added: Decimal             # 實際計入 total 的 perp accountValue 合計
    perp_by_dex: dict[str, Decimal]
    unpriced: tuple[str, ...]
    total: Decimal


def build_price_table(meta_and_ctxs: Any) -> dict[int, Decimal]:
    """token index → 對 USDC 的 markPx。

    `meta.universe[i].tokens=[base, quote]` 且 quote 是 USDC 的 pair，以 `ctxs` 中
    `coin == pair name` 的 markPx 報價。同一 base 有多個 pair 時優先 isCanonical。
    USDC 本身不在表內（呼叫端以 1 計）。"""
    if not (isinstance(meta_and_ctxs, list) and len(meta_and_ctxs) == 2):
        raise ValueError("spotMetaAndAssetCtxs 回應形狀無法解讀（預期 [meta, ctxs]）")
    meta, ctxs = meta_and_ctxs
    tokens = meta["tokens"]
    usdc_idx = next((t["index"] for t in tokens if t["name"] == "USDC"), None)
    if usdc_idx is None:
        raise ValueError("spotMeta 沒有 USDC token")
    mark_by_coin: dict[str, Decimal] = {}
    for c in ctxs:
        px = c.get("markPx")
        if px is None:
            continue
        d = Decimal(str(px))
        if d.is_finite() and d > 0:
            mark_by_coin[c["coin"]] = d
    table: dict[int, Decimal] = {}
    canonical: set[int] = set()
    for pair in meta["universe"]:
        base, quote = pair["tokens"]
        if quote != usdc_idx or pair["name"] not in mark_by_coin:
            continue
        is_canon = bool(pair.get("isCanonical", False))
        if base in table and (base in canonical or not is_canon):
            continue
        table[base] = mark_by_coin[pair["name"]]
        if is_canon:
            canonical.add(base)
    return table


def compute_rebuild(*, mode: str, spot_state: Any, meta_and_ctxs: Any, staking: Any,
                    vault_equities: Any, perp_by_dex: dict[str, Decimal]) -> Rebuild:
    """獨立重建的純計算層（無 IO）。`perp_by_dex` 的 key "" 是預設 dex。"""
    if not isinstance(spot_state, dict):
        raise ValueError("spotClearinghouseState 回應形狀無法解讀")
    if not isinstance(staking, dict):
        raise ValueError("delegatorSummary 回應形狀無法解讀")
    if not isinstance(vault_equities, list):
        raise ValueError("userVaultEquities 回應形狀無法解讀（預期 list）")
    table = build_price_table(meta_and_ctxs)
    tokens = meta_and_ctxs[0]["tokens"]
    name_to_idx: dict[str, list[int]] = {}
    for t in tokens:
        name_to_idx.setdefault(t["name"], []).append(t["index"])

    usdc = _ZERO
    other_spot = _ZERO
    detail: dict[str, Decimal] = {}
    unpriced: list[str] = []
    for b in spot_state.get("balances", []):
        total = _dec(b["total"], f"spot {b.get('coin')} total")
        if total == 0:
            continue
        coin = b["coin"]
        if coin == "USDC":
            usdc += total
            continue
        idx = b.get("token")
        px = table.get(idx) if idx is not None else None
        if px is None:
            unpriced.append(coin)
            continue
        value = total * px
        detail[coin] = detail.get(coin, _ZERO) + value
        other_spot += value

    delegated = _dec(staking.get("delegated", "0"), "delegatorSummary.delegated")
    staking_value = _ZERO
    if delegated != 0:
        px = next((table[i] for i in name_to_idx.get("HYPE", []) if i in table), None)
        if px is None:
            unpriced.append("HYPE(staking)")
        else:
            staking_value = delegated * px

    vault = _ZERO
    for v in vault_equities:
        vault += _dec(v["equity"], "userVaultEquities.equity")

    perp_default = perp_by_dex.get("", _ZERO)
    perp_added = sum(perp_by_dex.values(), _ZERO) if mode in _PERP_ADDED_MODES else _ZERO
    total = usdc + other_spot + staking_value + vault + perp_added
    return Rebuild(mode=mode, usdc=usdc, other_spot=other_spot, other_spot_detail=detail,
                   staking=staking_value, vault=vault, perp_default=perp_default,
                   perp_added=perp_added, perp_by_dex=dict(perp_by_dex),
                   unpriced=tuple(unpriced), total=total)


def judge(*, formal: Decimal | None, rebuilt: Decimal | None, unpriced: tuple[str, ...],
          tol_abs: Decimal, tol_pct: Decimal) -> tuple[str, Decimal | None, Decimal | None]:
    """回 (verdict, diff, diff_pct)。diff＝重建 − 正式；diff_pct 以百分比表示（正式值為 0 → None）。"""
    if formal is None or rebuilt is None:
        return "FAIL", None, None
    diff = rebuilt - formal
    pct = (diff / formal * 100) if formal != 0 else None
    limit = max(tol_abs, tol_pct * abs(formal))
    if abs(diff) <= limit:
        return "PASS", diff, pct
    return ("UNPRICED" if unpriced else "FAIL"), diff, pct


# ── IO 層（只讀 info；全部在 main 內才會被呼叫）──────────────────────────────────

def _post(info, payload: dict) -> Any:
    return info.post("/info", payload)


def fetch_rebuild(info, address: str) -> Rebuild:
    """獨立重建：直接打 info 原始端點。不得呼叫 get_account_total、不得讀 portfolio。"""
    mode = _post(info, {"type": "userAbstraction", "user": address})
    if not isinstance(mode, str):
        raise ValueError(f"userAbstraction 回應不是字串（{type(mode).__name__}）")
    spot_state = _post(info, {"type": "spotClearinghouseState", "user": address})
    meta_and_ctxs = _post(info, {"type": "spotMetaAndAssetCtxs"})
    staking = _post(info, {"type": "delegatorSummary", "user": address})
    vaults = _post(info, {"type": "userVaultEquities", "user": address})

    def perp_av(dex: str) -> Decimal:
        st = _post(info, {"type": "clearinghouseState", "user": address, "dex": dex})
        return _dec(st["marginSummary"]["accountValue"], f"clearinghouseState[{dex!r}].accountValue")

    perp_by_dex: dict[str, Decimal] = {"": perp_av("")}
    if mode in _PERP_ADDED_MODES:
        dexs = _post(info, {"type": "perpDexs"})
        if not isinstance(dexs, list):
            raise ValueError("perpDexs 回應形狀無法解讀（預期 list）")
        for d in dexs:
            if isinstance(d, dict) and d.get("name"):
                perp_by_dex[d["name"]] = perp_av(d["name"])
    return compute_rebuild(mode=mode, spot_state=spot_state, meta_and_ctxs=meta_and_ctxs,
                           staking=staking, vault_equities=vaults, perp_by_dex=perp_by_dex)


@dataclass
class ProbeResult:
    address: str
    verdict: str = "ERROR"
    retried: bool = False
    attempts: int = 0
    formal_value: Decimal | None = None
    formal_as_of_ms: int | None = None
    formal_age_s: Decimal | None = None
    has_exposure: bool | None = None
    formal_error: str | None = None
    rebuild: Rebuild | None = None
    diff: Decimal | None = None
    diff_pct: Decimal | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        rb = self.rebuild
        return {
            "address": self.address, "verdict": self.verdict, "retried": self.retried,
            "attempts": self.attempts,
            "mode": rb.mode if rb else None,
            "perp_account_value_default": rb.perp_default if rb else None,
            "formal": {"value": self.formal_value, "as_of_ms": self.formal_as_of_ms,
                       "age_s": self.formal_age_s, "has_exposure": self.has_exposure,
                       "error": self.formal_error},
            "rebuild": None if rb is None else {
                "total": rb.total, "usdc": rb.usdc, "other_spot": rb.other_spot,
                "other_spot_detail": rb.other_spot_detail, "staking": rb.staking,
                "vault": rb.vault, "perp_added": rb.perp_added, "perp_by_dex": rb.perp_by_dex,
                "unpriced": list(rb.unpriced)},
            "diff": self.diff, "diff_pct": self.diff_pct, "error": self.error,
        }

    def summary(self) -> str:
        rb = self.rebuild
        parts = [f"{self.verdict:<8} {self.address}"]
        if self.retried:
            parts.append("retried")
        if rb is not None:
            parts.append(f"mode={rb.mode} perp_av={rb.perp_default:.2f}")
        if self.formal_value is not None:
            parts.append(f"formal={self.formal_value:.2f} age={self.formal_age_s:.1f}s "
                         f"exposure={self.has_exposure}")
        elif self.formal_error:
            parts.append(f"formal=UNAVAILABLE({self.formal_error})")
        if rb is not None:
            parts.append(f"rebuilt={rb.total:.2f} [usdc={rb.usdc:.2f} other_spot={rb.other_spot:.2f} "
                         f"staking={rb.staking:.2f} vault={rb.vault:.2f} perp_added={rb.perp_added:.2f}]")
            if rb.unpriced:
                parts.append(f"unpriced={','.join(rb.unpriced)}")
        if self.diff is not None:
            pct = f"{self.diff_pct:.4f}%" if self.diff_pct is not None else "n/a"
            parts.append(f"diff={self.diff:+.2f} ({pct})")
        if self.error:
            parts.append(f"error={self.error}")
        return " ".join(parts)


def probe_once(adapter: HyperliquidAdapter, info, address: str, *, now_ms_fn: Callable[[], int],
               tol_abs: Decimal, tol_pct: Decimal) -> ProbeResult:
    res = ProbeResult(address=address)
    # 正式路徑：先讀（行情會動，讀取順序固定為正式 → 重建）。
    try:
        has_exposure = bool(adapter.get_positions(address)) or bool(adapter.get_open_orders(address))
        total = adapter.get_account_total(address)
        now_ms = now_ms_fn()
        res.has_exposure = has_exposure
        res.formal_as_of_ms = total.as_of_ms
        res.formal_age_s = Decimal(now_ms - total.as_of_ms) / 1000
        equity = resolve_leader_equity(total, now_ms=now_ms, leader_has_exposure=has_exposure)
        res.formal_value = equity.value
    except LeaderEquityUnavailable as e:
        res.formal_error = e.reason
    except ValueError as e:      # get_account_total 的回應形狀錯誤；也記為該位址 FAIL
        res.formal_error = str(e)
    # 重建（讀取例外往外拋，由呼叫端記為 ERROR）。
    res.rebuild = fetch_rebuild(info, address)
    res.verdict, res.diff, res.diff_pct = judge(
        formal=res.formal_value, rebuilt=res.rebuild.total, unpriced=res.rebuild.unpriced,
        tol_abs=tol_abs, tol_pct=tol_pct)
    return res


def probe_address(adapter, info, address: str, *, now_ms_fn: Callable[[], int],
                  tol_abs: Decimal, tol_pct: Decimal) -> ProbeResult:
    """最多兩次嘗試，以最後一次為準；第一次未通過（含例外）才重試。"""
    result: ProbeResult | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = probe_once(adapter, info, address, now_ms_fn=now_ms_fn,
                                tol_abs=tol_abs, tol_pct=tol_pct)
        except Exception as e:   # noqa: BLE001 - 讀取例外要印型別與訊息，不吞
            result = ProbeResult(address=address, verdict="ERROR",
                                 error=f"{type(e).__name__}: {e}")
        result.attempts = attempt
        result.retried = attempt > 1
        if result.verdict == "PASS":
            break
    assert result is not None
    return result


# ── CLI ───────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_info():
    """唯一建立網路物件的地方（唯讀：不帶任何金鑰）。Info 初始化會打 spot_meta／meta。"""
    from hyperliquid.info import Info
    return Info(API_URLS["mainnet"], skip_ws=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leader_equity_probe",
        description="leader 權益主網唯讀探針：正式路徑（get_account_total＋resolve_leader_equity）"
                    "對獨立重建值對帳。只讀主網 info，不需金鑰。")
    p.add_argument("addresses", nargs="+", metavar="ADDRESS", help="0x 開頭 42 字元位址（可多個）")
    p.add_argument("--out", metavar="PATH.jsonl", help="每位址一行 JSONL；未給則不寫檔")
    p.add_argument("--tolerance-abs", type=Decimal, default=Decimal("1.0"),
                   help="絕對容差（美元，預設 1.0）")
    p.add_argument("--tolerance-pct", type=Decimal, default=Decimal("0.001"),
                   help="相對容差（小數，預設 0.001＝0.1%%）")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)          # --help 在此 SystemExit(0)，尚未建立任何網路物件
    bad = [a for a in args.addresses if not is_valid_address(a)]
    if bad:
        print(f"位址格式錯誤（需 0x＋40 位十六進位）：{', '.join(bad)}", file=sys.stderr)
        return 2
    if args.tolerance_abs < 0 or args.tolerance_pct < 0:
        print("容差不得為負", file=sys.stderr)
        return 2

    info = _build_info()
    adapter = HyperliquidAdapter("mainnet", info=info, exchange=None)
    out_fh = open(args.out, "w", encoding="utf-8") if args.out else None
    ok = True
    try:
        for addr in args.addresses:
            res = probe_address(adapter, info, addr, now_ms_fn=_now_ms,
                                tol_abs=args.tolerance_abs, tol_pct=args.tolerance_pct)
            print(res.summary())
            if out_fh is not None:
                out_fh.write(json.dumps(res.to_json(), default=str, ensure_ascii=False) + "\n")
                out_fh.flush()
            ok = ok and res.verdict == "PASS"
    finally:
        if out_fh is not None:
            out_fh.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
