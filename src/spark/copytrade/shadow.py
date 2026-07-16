"""Shadow diff 分類器（Task 16）：比對 spark `--shadow` JSONL 與 hl-copytrader 線上
log，把逐項差異分成三類——`match`（結構與價格一致）／`explainable`（純數值差，且所有
待歸類配對的 size 比值彼此相近，可歸因為單一 scale/weight 參數差）／`unexplained`
（結構差：動作種類/coin/方向對不上，或數值差但比值不一致、無法歸因單一原因）。

hl-copytrader log 行格式（`/Users/jim/projects/hl-copytrader` 為絕對唯讀來源，本檔
只讀取其原始碼格式字串與 `logs/copytrader.log` 實際輸出比對，不修改該 repo 任何檔案）：
  整行前綴：`main.py:43` `"%(asctime)s [%(levelname)s] %(name)s: %(message)s"`
  掛單 place：`src/trader.py:355`（dry）/`:366`（live）
    `[DRY RUN] 掛單 {coin} {買|賣} size={size} @ ${px:,.4f} {kind}{ro_tag}`
    `掛單 {coin} {買|賣} size={size} @ ${px:,.4f} {kind}{ro_tag}: {result}`
  改單 modify：`src/trader.py:404`（dry）/`:416`（live）
    `[DRY RUN] 改單 {coin} oid={oid} → {買|賣} size={size} @ ${px:,.4f}`
    `改單 {coin} oid={oid} → {買|賣} size={size} @ ${px:,.4f}: ok`
  取消 cancel：`src/trader.py:426`（dry）/`:430`（live）
    `[DRY RUN] 取消掛單 {coin} oid={oid}`
    `取消掛單 {coin} oid={oid}`
  開倉 market_open：`src/trader.py:158`（dry）/`:173`（live）；is_buy=True→多/long
    `[DRY RUN] 開倉 {coin} {多|空} size={size} lev={leverage}x`
    `開倉 {coin} {多|空} size={size}: {result}`
  平倉 close：`src/trader.py:219`（dry）/`:232`（live）；action="平多"|"平空"
    `[DRY RUN] 平倉 {coin} {平多|平空} size={size} pnl≈{unrealized_pnl:+.2f}`
    `平倉 {coin} {平多|平空} size={size}: {result}`
    is_buy 語意（`src/spark/copytrade/positions.py:206` 註解）：
    「平倉下單方向 = 持倉反向：平多賣（is_buy=False）、平空買（is_buy=True）」——
    hl 的 `action` 詞是「持倉方向」，實際下單方向相反，此處已轉換對齊 spark payload。
  設槓桿 update_leverage：`src/trader.py:129`（live）/`:114`（dry）
    `[DRY RUN] 設定 {coin} 槓桿 {leverage}x {cross|isolated}`
    `設定 {coin} 槓桿 {leverage}x {cross|isolated}: {result}`
以上均以 `logs/copytrader.log` 的實際輸出行核對過（該檔含真實帳戶資料，測試樣本
一律用合成座標值，格式照抄、數值不抄）。
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiffItem:
    """單一分類結果。kind 為 "match"|"explainable"|"unexplained"；detail 是人讀摘要，
    不含任何 key/signer 材料（與 ActionRecord.payload 同一紅線）。"""

    kind: str
    detail: str


# ═══════════════════════════════════════════════════════════════════════
# 1. spark shadow JSONL 讀取
# ═══════════════════════════════════════════════════════════════════════

def load_action_records(path: Path) -> list[dict]:
    """讀 `--shadow` 追加寫入的 JSONL（`scripts/run_copytrade.py:_append_shadow` 的
    schema：每行 `{"ts": float, "kind": str, "coin": str, "payload": dict}`）。

    Decimal 字串保留 str（不轉型，ActionRecord 紅線本就存 str；轉型交給呼叫端在
    真的需要算術時明確 `Decimal(...)`，避免這裡悄悄引入精度或型別漂移）。
    空白行跳過；壞行帶行號拋 ValueError（診斷工具，壞資料要大聲，不要吞掉再回傳
    看似正常的殘缺清單）。
    """
    records: list[dict] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON: {raw!r}") from e
    return records


# ═══════════════════════════════════════════════════════════════════════
# 2. hl-copytrader log 行解析
# ═══════════════════════════════════════════════════════════════════════

_LOG_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[\w+\] [\w.]+: (?P<msg>.*)$"
)

_PLACE_RE = re.compile(
    r"^(?:\[DRY RUN\] )?掛單 (?P<coin>\S+) (?P<side>買|賣) size=(?P<size>[0-9.]+) "
    r"@ \$(?P<px>[0-9,]+\.[0-9]+) (?P<order_kind>[A-Za-z]+)(?P<ro> \[reduceOnly\])?"
    r"(?::.*)?$"
)
_MODIFY_RE = re.compile(
    r"^(?:\[DRY RUN\] )?改單 (?P<coin>\S+) oid=(?P<oid>\d+) → (?P<side>買|賣) "
    r"size=(?P<size>[0-9.]+) @ \$(?P<px>[0-9,]+\.[0-9]+)(?::.*)?$"
)
_CANCEL_RE = re.compile(
    r"^(?:\[DRY RUN\] )?取消掛單 (?P<coin>\S+) oid=(?P<oid>\d+)$"
)
_OPEN_RE = re.compile(
    r"^(?:\[DRY RUN\] )?開倉 (?P<coin>\S+) (?P<side>多|空) size=(?P<size>[0-9.]+)"
    r"(?: lev=(?P<lev>\d+)x)?(?::.*)?$"
)
_CLOSE_RE = re.compile(
    r"^(?:\[DRY RUN\] )?平倉 (?P<coin>\S+) (?P<action>平多|平空) size=(?P<size>[0-9.]+)"
    r"(?: pnl≈[+-][0-9.]+)?(?::.*)?$"
)
_LEVERAGE_RE = re.compile(
    r"^(?:\[DRY RUN\] )?設定 (?P<coin>\S+) 槓桿 (?P<lev>\d+)x (?P<mode>cross|isolated)"
    r"(?::.*)?$"
)


def _px(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def parse_hl_log_line(line: str) -> dict | None:
    """hl-copytrader log 一行 → 標準化動作 dict；非動作行（例如彙總統計、啟動訊息）
    回 None。若整行帶 `asctime [LEVEL] logger: ` 前綴會先剝掉，沒有前綴時也接受
    裸 message（方便測試直接餵 message 片段）。

    回傳 dict 的 "kind" 對齊 `ActionRecord.kind` 詞彙（"place"/"modify"/"cancel"/
    "market_open"/"close"/"update_leverage"），"is_buy" 一律代表*實際下單方向*
    （close 已依 positions.py:206 的反向語意轉換），數值欄位為 Decimal。
    """
    stripped = line.rstrip("\n")
    m = _LOG_PREFIX_RE.match(stripped)
    msg = m.group("msg") if m else stripped

    if (mm := _PLACE_RE.match(msg)) is not None:
        return {
            "kind": "place", "coin": mm.group("coin"),
            "is_buy": mm.group("side") == "買",
            "sz": Decimal(mm.group("size")),
            "limit_px": _px(mm.group("px")),
            "order_type": mm.group("order_kind"),
            "reduce_only": mm.group("ro") is not None,
        }
    if (mm := _MODIFY_RE.match(msg)) is not None:
        return {
            "kind": "modify", "coin": mm.group("coin"),
            "oid": int(mm.group("oid")),
            "is_buy": mm.group("side") == "買",
            "sz": Decimal(mm.group("size")),
            "limit_px": _px(mm.group("px")),
        }
    if (mm := _CANCEL_RE.match(msg)) is not None:
        return {"kind": "cancel", "coin": mm.group("coin"), "oid": int(mm.group("oid"))}
    if (mm := _OPEN_RE.match(msg)) is not None:
        return {
            "kind": "market_open", "coin": mm.group("coin"),
            "is_buy": mm.group("side") == "多",
            "sz": Decimal(mm.group("size")),
        }
    if (mm := _CLOSE_RE.match(msg)) is not None:
        # 平多(持有多單)→賣出平倉 is_buy=False；平空→買入平倉 is_buy=True。
        return {
            "kind": "close", "coin": mm.group("coin"),
            "is_buy": mm.group("action") == "平空",
            "sz": Decimal(mm.group("size")),
        }
    if (mm := _LEVERAGE_RE.match(msg)) is not None:
        return {
            "kind": "update_leverage", "coin": mm.group("coin"),
            "leverage": int(mm.group("lev")), "is_cross": mm.group("mode") == "cross",
        }
    return None


# ═══════════════════════════════════════════════════════════════════════
# 3. 分類器
# ═══════════════════════════════════════════════════════════════════════

_PX_KINDS = frozenset({"place", "modify"})       # 有 limit_px 可比對
_SIZE_KINDS = frozenset({"market_open", "close"})  # 無價、只比 size
_IDENTITY_KINDS = frozenset({"cancel", "update_leverage"})  # 無數值可比對


def _normalize_spark(action: dict) -> dict:
    """spark JSONL 一筆（{"kind","coin","payload":{...}}）→ 與 parse_hl_log_line
    同形狀的標準化 dict（is_buy/sz/limit_px 為 Decimal，其餘型別依 kind）。"""
    kind = action["kind"]
    coin = action["coin"]
    p = action.get("payload", {})
    out: dict[str, Any] = {"kind": kind, "coin": coin}
    if kind in _PX_KINDS:
        out["is_buy"] = p["is_buy"]
        out["sz"] = Decimal(p["sz"])
        out["limit_px"] = Decimal(p["limit_px"])
        out["reduce_only"] = bool(p["reduce_only"])
    elif kind in _SIZE_KINDS:
        out["is_buy"] = p["is_buy"]
        out["sz"] = Decimal(p["sz"])
    elif kind == "cancel":
        if "oid" in p:
            out["oid"] = int(p["oid"])
    elif kind == "update_leverage":
        out["leverage"] = int(p["leverage"])
        out["is_cross"] = bool(p["is_cross"])
    else:
        # skip_trigger（或未知 kind）：is_buy/sz 照抄供 detail 顯示與排序，
        # 刻意不賦予 limit_px——與 hl 的 place/modify 結構性不同（kind 字串本身
        # 就不同），這正是我們要讓它落在 unexplained（結構差）的理由。
        if "is_buy" in p:
            out["is_buy"] = p["is_buy"]
        if "sz" in p:
            out["sz"] = Decimal(p["sz"])
    return out


def _key(a: dict) -> tuple:
    """分組鍵 = 結構維度：kind/coin/is_buy/reduce_only。

    reduce_only 是結構維度而非數值差——spark 若把 reduce-only 平倉單錯掛成
    非 reduce-only，下單語意就是錯的，不得因 coin/px/sz/is_buy 對得上而判 match。
    僅 place 可雙側對照：hl 的改單 log 行（trader.py:404/416）不含 reduceOnly
    資訊，modify 此維度單側缺料、無從比對，視為 wildcard（None）——硬塞進 key
    會把所有 modify 配對打成永久 unexplained 噪音（毒化 3 交易日 gate），
    比盲點更糟。此為已知限制，記錄於此。
    """
    ro = a.get("reduce_only") if a["kind"] == "place" else None
    return (a["kind"], a["coin"], a.get("is_buy"), ro)


def _sort_key(a: dict):
    if "limit_px" in a:
        return a["limit_px"]
    if "sz" in a:
        return a["sz"]
    return a.get("oid", 0)


def _fmt(a: dict) -> str:
    coin = a["coin"]
    kind = a["kind"]
    bits = [kind, coin]
    if "is_buy" in a:
        bits.append("buy" if a["is_buy"] else "sell")
    if a.get("reduce_only"):
        bits.append("[RO]")
    if "sz" in a:
        bits.append(f"sz={a['sz']}")
    if "limit_px" in a:
        bits.append(f"px={a['limit_px']}")
    return " ".join(bits)


def classify_diff(spark_actions: list[dict], hl_actions: list[dict], *,
                  px_rel_tol: Decimal, size_ratio_tol: Decimal) -> list[DiffItem]:
    """逐項分類 spark 意圖動作 vs hl-copytrader 實際動作。

    演算法：
    1. 依 (kind, coin, is_buy, reduce_only) 分組（cancel/update_leverage 的
       is_buy 恆為 None，故同 coin 同 kind 即歸一組；reduce_only 僅 place 進鍵，
       理由見 `_key` docstring）；兩邊都有的組內按 price（無 price 則 size）
       排序後逐一配對，多出來的（組內任一邊數量較多）直接記 "unexplained"
       （結構差：數量對不上）。只出現在單邊的整組（例如 hl 有 trigger 掛單但
       spark 記了 skip_trigger，kind 字串不同、天生不會配對）也記 "unexplained"。
    2. 每對配對：
       - 兩邊皆有 limit_px：px 相對差 = |spark_px-hl_px|/hl_px。≤ px_rel_tol
         → "match"；否則存入 pending（記錄 size 比值，若 hl size 為 0 記 None）。
       - 兩邊皆有 size 但無 px（market_open/close）：size 比值與 1 的相對差
         ≤ size_ratio_tol → "match"；否則存入 pending。
       - 兩邊皆無可比數值（cancel/update_leverage）：結構已對上即 "match"。
    3. pending 集合（跨 kind/coin，因為單一 scale/weight 參數差會讓*所有*配對的
       size 比值同向偏移）：比值全部彼此相近（每個比值與均值的相對差 ≤
       size_ratio_tol）→ 全部改判 "explainable"；否則全部維持 "unexplained"
       （數值差但無法歸因單一原因）。
    """
    spark_norm = [_normalize_spark(a) for a in spark_actions]
    hl_norm = list(hl_actions)

    spark_groups: dict[tuple, list[dict]] = {}
    hl_groups: dict[tuple, list[dict]] = {}
    for a in spark_norm:
        spark_groups.setdefault(_key(a), []).append(a)
    for a in hl_norm:
        hl_groups.setdefault(_key(a), []).append(a)

    items: list[DiffItem] = []
    pending: list[tuple[DiffItem, Decimal | None]] = []  # (預先建立的 item, size ratio)

    all_keys = set(spark_groups) | set(hl_groups)
    for key in sorted(all_keys, key=lambda k: (k[0], k[1], str(k[2]), str(k[3]))):
        s_list = sorted(spark_groups.get(key, []), key=_sort_key)
        h_list = sorted(hl_groups.get(key, []), key=_sort_key)
        n = min(len(s_list), len(h_list))

        for i in range(n):
            s, h = s_list[i], h_list[i]
            if "limit_px" in s and "limit_px" in h:
                hl_px = h["limit_px"]
                rel = (abs(s["limit_px"] - hl_px) / hl_px) if hl_px != 0 else None
                ratio = (s["sz"] / h["sz"]) if h.get("sz") else None
                if rel is not None and rel <= px_rel_tol:
                    items.append(DiffItem(
                        "match",
                        f"{_fmt(s)} vs {_fmt(h)}（px差 {rel:.4%}）"))
                else:
                    pend_item = DiffItem(
                        "pending",
                        f"{_fmt(s)} vs {_fmt(h)}（px差 "
                        f"{'n/a' if rel is None else f'{rel:.4%}'}, "
                        f"size比值 {'n/a' if ratio is None else f'{ratio:.4f}'}）")
                    pending.append((pend_item, ratio))
            elif "sz" in s and "sz" in h and "limit_px" not in s and "limit_px" not in h:
                hl_sz = h["sz"]
                ratio = (s["sz"] / hl_sz) if hl_sz != 0 else None
                if ratio is not None and abs(ratio - 1) <= size_ratio_tol:
                    items.append(DiffItem(
                        "match", f"{_fmt(s)} vs {_fmt(h)}（size比值 {ratio:.4f}）"))
                else:
                    pend_item = DiffItem(
                        "pending",
                        f"{_fmt(s)} vs {_fmt(h)}（size比值 "
                        f"{'n/a' if ratio is None else f'{ratio:.4f}'}）")
                    pending.append((pend_item, ratio))
            else:
                # cancel / update_leverage：無數值可比，結構已對齊即 match。
                items.append(DiffItem("match", f"{_fmt(s)} vs {_fmt(h)}（結構一致）"))

        # 組內數量不對等：多出來的視為結構差（unexplained）。
        for extra in s_list[n:]:
            items.append(DiffItem(
                "unexplained", f"spark 多出 {_fmt(extra)}，hl 無對應動作"))
        for extra in h_list[n:]:
            items.append(DiffItem(
                "unexplained", f"hl 多出 {_fmt(extra)}，spark 無對應動作"))

    # ── pending 的 size 比值一致性檢查（跨全部 pending，見演算法第 3 步）──
    ratios = [r for _item, r in pending if r is not None]
    consistent = False
    if ratios:
        mean_ratio = statistics.mean(ratios)
        consistent = mean_ratio != 0 and all(
            abs(r / mean_ratio - 1) <= size_ratio_tol for r in ratios)
    for pend_item, ratio in pending:
        if ratio is None:
            # 無法算比值（hl size=0 等退化狀況）→ 直接判定無法歸因。
            items.append(DiffItem("unexplained", pend_item.detail))
            continue
        kind = "explainable" if consistent else "unexplained"
        detail = pend_item.detail + (
            f"（與其他 pending 比值一致 ~{statistics.mean(ratios):.4f}，"
            f"疑為 scale/weight 參數差）" if consistent else
            "（比值與其他 pending 不一致，無法歸因單一原因）")
        items.append(DiffItem(kind, detail))

    return items
