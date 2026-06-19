"""解析 builder_fills CSV（LZ4）。header-driven + alias map 容錯。"""
import csv
import io
from datetime import datetime
from decimal import Decimal

import lz4.frame

from spark.exchange.base import Fill

# 真實表頭以 Task 14（真實 builder 地址）確認為準；此 map 容納可能的命名差異。
ALIASES = {
    "time": ["time", "timestamp", "ts"],
    "coin": ["coin", "asset"],
    "side": ["side", "dir"],
    "px": ["px", "price"],
    "sz": ["sz", "size"],
    "builder_fee": ["builderFee", "builder_fee", "fee"],
}


def _pick(row: dict, names: list[str]) -> str:
    for n in names:
        if n in row and row[n] != "":
            return row[n]
    raise KeyError(f"none of {names} in CSV header {list(row)}")


def _parse_time(v: str) -> datetime:
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return datetime.fromtimestamp(int(v) / 1000)  # epoch ms 後備


def parse_builder_fills(data: bytes, compressed: bool = True) -> list[Fill]:
    text = (
        lz4.frame.decompress(data).decode()
        if compressed
        else data.decode()
    )
    reader = csv.DictReader(io.StringIO(text))
    out: list[Fill] = []
    for row in reader:
        out.append(
            Fill(
                time=_parse_time(_pick(row, ALIASES["time"])),
                coin=_pick(row, ALIASES["coin"]),
                side=_pick(row, ALIASES["side"]),
                px=Decimal(_pick(row, ALIASES["px"])),
                sz=Decimal(_pick(row, ALIASES["sz"])),
                builder_fee=Decimal(_pick(row, ALIASES["builder_fee"])),
            )
        )
    return out


def total_builder_fee(fills: list[Fill]) -> Decimal:
    return sum((f.builder_fee for f in fills), Decimal("0"))
