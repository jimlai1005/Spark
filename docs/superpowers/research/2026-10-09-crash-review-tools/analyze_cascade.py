"""離線分析 fetch_hl_window.py 的輸出（純本機、不連網）。

    python3 analyze_cascade.py window.json timeline   --me f6b3
    python3 analyze_cascade.py window.json rejects    --me f6b3
    python3 analyze_cascade.py window.json fit        --me f6b3
    python3 analyze_cascade.py window.json cost       --me f6b3 --marks 02:10,02:15

timeline  me 與 leader 的成交逐筆（時間、幣、方向、量、價、maker/taker、成交前部位）。
rejects   me 的 historicalOrders 中被拒的單（badAloPxRejected 等）。
fit       把 me 的 taker 校正輪（相鄰 taker 成交間隔 ≤10s 為一輪）逐輪拿去對 leader 部位
          時間線：搜尋「校正前 40 秒內」哪個時間點，能讓各幣『校正後部位 ÷ leader 部位』
          比例一致（變異係數最小）。比例一致＝該輪用的是單一時間點的 leader 快照，
          最佳時間點到校正的差＝快照年齡。比例對不齊的幣＝異常，值得細查。
cost      taker 成交（多為安全網校正）對「不動作」基準的損益：Σ賣量×(賣價−標記價)
          ＋Σ買量×(標記價−買價)，標記價取各 --marks 時刻 1 分 K 收盤。
          ⚠️ 「不動作」不是正確反事實（follower 本來可能就過重，部分校正是該做的），
          只能當量級上限；手續費另列。
"""
import argparse
import collections
import datetime as dt
import json
import statistics as st

UTC = dt.timezone.utc


def hms(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M:%S.%f")[:-3]


def sgn(f: dict) -> float:
    return float(f["sz"]) * (1 if f["side"] == "B" else -1)


def timeline_of(fills: list[dict]) -> dict[str, list[tuple[float, float, float]]]:
    """coin -> [(time_ms, 成交前部位, 成交後部位)]，依時間排序。"""
    tl: dict[str, list] = {}
    for f in sorted(fills, key=lambda f: (f["time"], f.get("tid", 0))):
        start = float(f["startPosition"])
        tl.setdefault(f["coin"], []).append((f["time"], start, start + sgn(f)))
    return tl


def pos_at(tl: dict, coin: str, t: float) -> float:
    p = None
    for ts, before, after in tl.get(coin, []):
        if ts <= t:
            p = after
        else:
            return before if p is None else p
    return p if p is not None else 0.0


def cmd_timeline(d: dict, me: str) -> None:
    for name, key in (("ME " + me, f"{me}_fills"), ("LEADER", "ld_fills")):
        print("==", name)
        for f in sorted(d[key], key=lambda f: f["time"]):
            print(hms(f["time"]), f["coin"], f["side"], f["sz"], "@", f["px"], f["dir"],
                  "taker" if f["crossed"] else "maker", "前部位", f["startPosition"])


def cmd_rejects(d: dict, me: str) -> None:
    hist = d.get(f"{me}_hist", [])
    print("historicalOrders 筆數", len(hist), "（上限 2000，過舊事件可能已被截掉）")
    print(collections.Counter(x["status"] for x in hist))
    for x in sorted(hist, key=lambda x: x["statusTimestamp"]):
        if "ejected" in x["status"]:
            o = x["order"]
            print(hms(x["statusTimestamp"]), x["status"], o["coin"], o["side"], o["sz"],
                  "@", o["limitPx"], o["tif"], "ro" if o["reduceOnly"] else "")


def clusters(fills: list[dict], gap_ms: int = 10_000) -> list[list[dict]]:
    out: list[list[dict]] = []
    for f in sorted((f for f in fills if f["crossed"]), key=lambda f: f["time"]):
        if out and f["time"] - out[-1][-1]["time"] <= gap_ms:
            out[-1].append(f)
        else:
            out.append([f])
    return out


def cmd_fit(d: dict, me: str) -> None:
    ltl = timeline_of(d["ld_fills"])
    for cl in clusters(d[f"{me}_fills"]):
        t_start = cl[0]["time"]
        target: dict[str, float] = {}
        for f in cl:
            target[f["coin"]] = float(f["startPosition"]) + sgn(f)
        scan: list[tuple[float, float, list[float]]] = []  # (cv, t, ratios)
        t = t_start - 40_000
        while t <= t_start:
            lp = {c: pos_at(ltl, c, t) for c in target}
            if len(target) >= 2 and all(v > 0 for v in lp.values()):
                r = [target[c] / lp[c] for c in target]
                scan.append((st.pstdev(r) / st.mean(r), t, r))
            t += 100
        label = f"{hms(t_start)}（{len(cl)} 筆 taker）目標 " + \
            ", ".join(f"{c}={v:.5f}" for c, v in target.items())
        if not scan:
            print(label, "→ 無法對齊（幣數<2 或 leader 部位為 0）")
            continue
        cv, tb, r = min(scan, key=lambda x: x[0])
        plateau = [x for x in scan if x[0] <= max(cv * 1.5, 0.002)]
        lo, hi = min(x[1] for x in plateau), max(x[1] for x in plateau)
        flag = "" if cv < 0.01 else "  ⚠️ 比例對不齊，單一快照解釋不了"
        print(label)
        print(f"   快照落在 {hms(lo)} ~ {hms(hi)}（校正前 {(t_start - hi) / 1000:.0f}~"
              f"{(t_start - lo) / 1000:.0f}s；此區間內 leader 部位不變，無法再縮小）"
              f" CV={cv:.4f} 比例={[round(x, 3) for x in r]}{flag}")
        if cv >= 0.01:
            for dt_s in (25, 20, 15, 10, 5):
                tt = t_start - dt_s * 1000
                print(f"   校正前 {dt_s:>2}s:", {c: round(target[c] / pos_at(ltl, c, tt), 3)
                                                  if pos_at(ltl, c, tt) else None
                                                  for c in target})


def close_at(d: dict, coin: str, hhmm: str) -> float:
    for c in d[f"c_{coin}"]:
        if dt.datetime.fromtimestamp(c["t"] / 1000, UTC).strftime("%H:%M") == hhmm:
            return float(c["c"])
    raise KeyError(f"{coin} 找不到 {hhmm} 的 K 線")


def cmd_cost(d: dict, me: str, marks: list[str]) -> None:
    taker = [f for f in d[f"{me}_fills"] if f["crossed"]]
    agg = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    fees = 0.0
    for f in taker:
        q, p = float(f["sz"]), float(f["px"])
        fees += float(f["fee"])
        a = agg[f["coin"]]
        if f["side"] == "A":
            a[0] += q
            a[1] += q * p
        else:
            a[2] += q
            a[3] += q * p
    print(f"taker 成交 {len(taker)} 筆，手續費 ${fees:.2f}")
    for m in marks:
        tot = 0.0
        for c, (sq, sn, bq, bn) in agg.items():
            mk = close_at(d, c, m)
            pnl = (sn - sq * mk) + (bq * mk - bn)
            tot += pnl
            print(f"  [{m}] {c}: 賣 {sq:.5f} 均價 {sn / sq if sq else 0:.3f}；"
                  f"買 {bq:.5f} 均價 {bn / bq if bq else 0:.3f}；標記 {mk}；"
                  f"淨部位變化 {bq - sq:+.5f}；對不動作 ${pnl:.2f}")
        print(f"  [{m}] 合計 ${tot:.2f}（扣手續費 ${tot - fees:.2f}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("window")
    ap.add_argument("cmd", choices=["timeline", "rejects", "fit", "cost"])
    ap.add_argument("--me", default="me")
    ap.add_argument("--marks", default="")
    a = ap.parse_args()
    with open(a.window) as fh:
        d = json.load(fh)
    if a.cmd == "timeline":
        cmd_timeline(d, a.me)
    elif a.cmd == "rejects":
        cmd_rejects(d, a.me)
    elif a.cmd == "fit":
        cmd_fit(d, a.me)
    else:
        cmd_cost(d, a.me, [m for m in a.marks.split(",") if m])


if __name__ == "__main__":
    main()
