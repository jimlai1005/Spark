"""唯讀：拉 Hyperliquid 公開 info API 的一個時間窗，輸出 JSON 到 stdout。

不需要任何金鑰，只打 https://api.hyperliquid.xyz/info。本機若連不上 HL，就把本檔
經 ssh stdin 丟到正式機執行（正式機在東京）：

    ssh -i <pem> ubuntu@<prod-ip> 'python3 - \
        --acct f6b3=0x6b3e7dbd9e168d9d23ca153bd22bb476e59c232c \
        --acct f438=0x438b3bbc83b44c483f6c6c8d2d5664453791dce9 \
        --leader 0xfb9c52f56f03d786ad5d435aa70fe45d80569760 \
        --start 2026-10-07T01:40 --end 2026-10-07T02:20 --coins ETH,BTC,HYPE' \
        < fetch_hl_window.py > window.json

輸出鍵：<label>_fills / <label>_hist / <label>_state（label 含 ld＝leader）、c_<COIN>（1 分 K）。
權重：每個地址 userFillsByTime(≥20)+historicalOrders(20)+clearinghouseState(2)，
每個幣 candleSnapshot(20)；呼叫間隔 0.4s，與正式機引擎共用同一個 IP 的 1200/分預算，
請別在短時間內重複大量執行。
注意：historicalOrders 只回最近 2000 筆——事件過去越久越可能被截掉，要盡快拉。
"""
import argparse
import calendar
import json
import sys
import time
import urllib.request

URL = "https://api.hyperliquid.xyz/info"


def post(body: dict):
    req = urllib.request.Request(
        URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def parse_ts(s: str) -> int:
    """'YYYY-MM-DDTHH:MM'（UTC）→ epoch ms。"""
    date, hm = s.split("T")
    y, mo, d = (int(x) for x in date.split("-"))
    h, mi = (int(x) for x in hm.split(":"))
    return calendar.timegm((y, mo, d, h, mi, 0)) * 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--acct", action="append", default=[], help="label=0xaddr，可重複")
    ap.add_argument("--leader", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--coins", default="ETH,BTC,HYPE")
    a = ap.parse_args()
    t0, t1 = parse_ts(a.start), parse_ts(a.end)
    accts = [("ld", a.leader)] + [tuple(x.split("=", 1)) for x in a.acct]
    out: dict = {"window": {"start_ms": t0, "end_ms": t1}}
    for label, addr in accts:
        out[f"{label}_fills"] = post({"type": "userFillsByTime", "user": addr,
                                      "startTime": t0, "endTime": t1,
                                      "aggregateByTime": False})
        time.sleep(0.4)
        out[f"{label}_hist"] = post({"type": "historicalOrders", "user": addr})
        time.sleep(0.4)
        s = post({"type": "clearinghouseState", "user": addr})
        out[f"{label}_state"] = {
            "accountValue": s["marginSummary"]["accountValue"],
            "totalNtlPos": s["marginSummary"]["totalNtlPos"],
            "positions": {p["position"]["coin"]: p["position"]["szi"]
                          for p in s["assetPositions"]},
        }
        time.sleep(0.4)
    for c in a.coins.split(","):
        out[f"c_{c}"] = post({"type": "candleSnapshot", "req": {
            "coin": c, "interval": "1m", "startTime": t0, "endTime": t1}})
        time.sleep(0.4)
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
