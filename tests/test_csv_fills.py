from decimal import Decimal
from datetime import datetime
import lz4.frame
from pathlib import Path
from spark.exchange.csv_fills import parse_builder_fills

FIXTURE = Path(__file__).parent / "fixtures" / "builder_fills_sample.csv"


def test_parse_plain_csv_bytes():
    fills = parse_builder_fills(FIXTURE.read_bytes(), compressed=False)
    assert len(fills) == 2
    assert fills[0].coin == "ETH"
    assert fills[0].px == Decimal("4000.5")
    assert fills[0].sz == Decimal("0.01")
    assert fills[0].builder_fee == Decimal("0.008")
    assert fills[0].time == datetime.fromisoformat("2026-06-18T10:00:00")


def test_parse_lz4_roundtrip():
    raw = lz4.frame.compress(FIXTURE.read_bytes())
    fills = parse_builder_fills(raw, compressed=True)
    assert len(fills) == 2
    assert fills[1].sz == Decimal("0.02")


def test_total_builder_fee_helper():
    from spark.exchange.csv_fills import total_builder_fee
    fills = parse_builder_fills(FIXTURE.read_bytes(), compressed=False)
    assert total_builder_fee(fills) == Decimal("0.024")
