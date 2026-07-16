"""copytrade/positions.py `sync_positions` 部位安全網測試。

對照移植來源 hl-copytrader：
  - src/sync.py:54-172（三分支＋min_notional 只 debug、抗單保護 gate）
  - src/trader.py:254-300（adjust_position 三情況）
FakeExecutor 實作 ExecutorPort、記錄所有呼叫、可注入失敗；
RecordingNotifier 取自 spark.copytrade.notifier。
"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ExecutorPort
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.positions import sync_positions
from spark.exchange.base import OrderResult, Position


class FakeExecutor:
    """記錄所有呼叫；fail_ops 內的操作回傳失敗（ok=False / False）。"""

    def __init__(self, fail_ops: frozenset[str] | set[str] = frozenset()) -> None:
        self.records: list[tuple] = []
        self.fail_ops = set(fail_ops)

    def place(self, spec) -> bool:
        self.records.append(("place", spec))
        return "place" not in self.fail_ops

    def modify(self, oid: int, spec) -> bool:
        self.records.append(("modify", oid, spec))
        return "modify" not in self.fail_ops

    def cancel(self, coin: str, oid: int) -> bool:
        self.records.append(("cancel", coin, oid))
        return "cancel" not in self.fail_ops

    def market_open(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        self.records.append(("market_open", coin, is_buy, size))
        ok = "market_open" not in self.fail_ops
        return OrderResult(ok=ok, filled_size=size if ok else Decimal("0"),
                           avg_px=Decimal("0"), raw={})

    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        self.records.append(("close_reduce_only", coin, is_buy, size))
        ok = "close_reduce_only" not in self.fail_ops
        return OrderResult(ok=ok, filled_size=size if ok else Decimal("0"),
                           avg_px=Decimal("0"), raw={})

    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool:
        self.records.append(("update_leverage", coin, leverage, is_cross))
        return "update_leverage" not in self.fail_ops

    def get_open_orders(self) -> list:
        return []

    def get_size_decimals(self, coin: str) -> int:
        return 4


def _pos(coin="ETH", szi="1", entry_px="2000", leverage=5, is_cross=True) -> Position:
    return Position(
        coin=coin, szi=Decimal(szi), entry_px=Decimal(entry_px),
        leverage=leverage, is_cross=is_cross,
        unrealized_pnl=Decimal("0"), margin_used=Decimal("0"),
    )


def _sync(leader, mine, *, scale="0.1", protected=frozenset(), fail_ops=frozenset(),
          mids=None, settings=None):
    ex = FakeExecutor(fail_ops)
    notifier = RecordingNotifier()
    res = sync_positions(
        ex, leader, mine, Decimal(scale),
        settings=settings or CopySettings(),
        notifier=notifier,
        protected=set(protected),
        size_decimals=lambda coin: 4,
        mids=mids if mids is not None else {"ETH": Decimal("2000"), "BTC": Decimal("60000")},
    )
    return ex, notifier, res


def _ops(ex: FakeExecutor) -> list[str]:
    return [r[0] for r in ex.records]


def _warns(notifier: RecordingNotifier) -> list[tuple]:
    return [r for r in notifier.records if r[0] == "warn"]


# ── 結構檢查：FakeExecutor 符合 ExecutorPort ─────────────────────────
def test_fake_executor_satisfies_executor_port():
    assert isinstance(FakeExecutor(), ExecutorPort)


# ── 1. 新開多倉：market_open 前先 update_leverage、方向/量正確 ───────
def test_open_new_long_sets_leverage_before_market_open():
    leader = {"ETH": _pos(szi="10", leverage=7, is_cross=True)}
    ex, notifier, res = _sync(leader, {})

    assert _ops(ex) == ["update_leverage", "market_open"]
    assert ex.records[0] == ("update_leverage", "ETH", 7, True)
    assert ex.records[1] == ("market_open", "ETH", True, Decimal("1.0000"))
    assert res["opened"] == [{"coin": "ETH", "side": "long", "size": Decimal("1.0000")}]
    assert res["failed"] == [] and res["skipped"] == []


def test_open_new_short_direction():
    leader = {"ETH": _pos(szi="-10")}
    ex, _, res = _sync(leader, {})
    assert ex.records[1] == ("market_open", "ETH", False, Decimal("1.0000"))
    assert res["opened"][0]["side"] == "short"


def test_open_new_size_rounded_down_to_size_decimals():
    # 10.12345678 × 0.1 = 1.012345678 → sz_dec=4 無條件捨去 → 1.0123
    leader = {"ETH": _pos(szi="10.12345678")}
    ex, _, _ = _sync(leader, {})
    assert ex.records[1][3] == Decimal("1.0123")


def test_open_new_protected_zero_calls_and_warn():
    leader = {"ETH": _pos(szi="10")}
    ex, notifier, res = _sync(leader, {}, protected={"ETH"})

    assert ex.records == []
    assert res["opened"] == []
    assert res["skipped"] == [{"coin": "ETH", "reason": "protected_open"}]
    warns = _warns(notifier)
    assert len(warns) == 1 and "ETH" in warns[0][2]


def test_missing_mid_falls_back_to_entry_px():
    # hl sync.py:97：get_mid_price(...) or entry_px。mid 缺 → 用 entry_px 算名目。
    leader = {"ETH": _pos(szi="10", entry_px="2000")}
    ex, _, res = _sync(leader, {}, mids={})
    assert _ops(ex) == ["update_leverage", "market_open"]
    assert res["opened"][0]["size"] == Decimal("1.0000")


# ── 2. 新開但名目過小 → 零呼叫、skipped、無告警（hl sync.py:100-102）──
def test_open_below_min_notional_skipped_silently():
    # 0.04 × 0.1 = 0.004 → ×2000 = 名目 8 < 預設 min_order_notional 10
    leader = {"ETH": _pos(szi="0.04")}
    ex, notifier, res = _sync(leader, {}, scale="0.1")

    assert ex.records == []
    assert res["skipped"] == [{"coin": "ETH", "reason": "min_notional"}]
    assert notifier.records == []  # debug 級語意：不告警


# ── 3. 同向加倉/減倉/容忍帶 ─────────────────────────────────────────
def test_same_side_increase_opens_diff():
    # 我 1.0 → 目標 1.5：diff/my = 50% > 2% → market_open 買入差額 0.5
    leader = {"ETH": _pos(szi="15", leverage=7)}
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, res = _sync(leader, mine)

    assert _ops(ex) == ["update_leverage", "market_open"]
    assert ex.records[1] == ("market_open", "ETH", True, Decimal("0.5000"))
    assert res["adjusted"] == [{
        "coin": "ETH", "kind": "increase",
        "from_size": Decimal("1.0"), "to_size": Decimal("1.5000"),
        "diff": Decimal("0.5000"),
    }]


def test_same_side_decrease_closes_diff_reduce_only():
    # 我 1.0 多 → 目標 0.8：減倉 0.2，平多 → is_buy=False
    leader = {"ETH": _pos(szi="8")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, res = _sync(leader, mine)

    assert _ops(ex) == ["close_reduce_only"]
    assert ex.records[0] == ("close_reduce_only", "ETH", False, Decimal("0.2000"))
    assert res["adjusted"][0]["kind"] == "decrease"


def test_within_tolerance_no_action():
    # 我 1.0 → 目標 1.01：1% ≤ 2% 容忍 → 零動作
    leader = {"ETH": _pos(szi="10.1")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, notifier, res = _sync(leader, mine)

    assert ex.records == []
    assert res["adjusted"] == [] and res["skipped"] == []


def test_relative_diff_denominator_is_my_size():
    # hl sync.py:125：分母是 my_size。目標 0.9803、我 1.0 → 1.97% < 2% → 零動作。
    # 若分母誤用 target_size：0.0197/0.9803 = 2.01% > 2% 會誤觸發。
    leader = {"ETH": _pos(szi="9.803")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, res = _sync(leader, mine)
    assert ex.records == []
    assert res["adjusted"] == []


# ── 4. 方向反轉 → 全平先於重開（trader.py:269-279）───────────────────
def test_direction_flip_closes_full_then_opens_target():
    # 我 1.0 空 → leader 12 多（×0.1 = 1.2 多）
    leader = {"ETH": _pos(szi="12", leverage=9, is_cross=False)}
    mine = {"ETH": _pos(szi="-1.0")}
    ex, _, res = _sync(leader, mine)

    assert _ops(ex) == ["close_reduce_only", "update_leverage", "market_open"]
    # 平空 → 買入，全量 1.0
    assert ex.records[0] == ("close_reduce_only", "ETH", True, Decimal("1.0"))
    # 重開新方向目標量（非差額）
    assert ex.records[1] == ("update_leverage", "ETH", 9, False)
    assert ex.records[2] == ("market_open", "ETH", True, Decimal("1.2000"))
    assert res["adjusted"][0]["kind"] == "flip"
    assert res["opened"] == []  # 反轉重開只記 adjusted，不重複記入 opened


def test_direction_flip_long_to_short():
    leader = {"ETH": _pos(szi="-12")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, _ = _sync(leader, mine)
    # 平多 → 賣出全量；重開空方向
    assert ex.records[0] == ("close_reduce_only", "ETH", False, Decimal("1.0"))
    assert ex.records[2] == ("market_open", "ETH", False, Decimal("1.2000"))


def test_flip_allowed_even_when_protected():
    # hl sync.py:128 gate 要求 target_side == my_side 才擋——反轉不受 protected 影響。
    leader = {"ETH": _pos(szi="-12")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, res = _sync(leader, mine, protected={"ETH"})
    assert _ops(ex) == ["close_reduce_only", "update_leverage", "market_open"]
    assert res["skipped"] == []


# ── 5. protected 擋加倉、不擋減倉/趨平（hl sync.py:128-130）──────────
def test_protected_blocks_increase_with_warn():
    leader = {"ETH": _pos(szi="15")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, notifier, res = _sync(leader, mine, protected={"ETH"})

    assert ex.records == []
    assert res["skipped"] == [{"coin": "ETH", "reason": "protected_increase"}]
    assert len(_warns(notifier)) == 1


def test_protected_allows_decrease():
    leader = {"ETH": _pos(szi="8")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, res = _sync(leader, mine, protected={"ETH"})

    assert _ops(ex) == ["close_reduce_only"]
    assert res["adjusted"][0]["kind"] == "decrease"
    assert res["skipped"] == []


def test_protected_allows_flatten():
    # leader 已平 → 即使 protected 也照平（hl sync.py:154-167 無 protected 檢查）
    mine = {"ETH": _pos(szi="1.0")}
    ex, _, res = _sync({}, mine, protected={"ETH"})

    assert _ops(ex) == ["close_reduce_only"]
    assert res["flattened"] == [{"coin": "ETH", "side": "long", "size": Decimal("1.0")}]


# ── 6. 趨平：方向與量正確 ────────────────────────────────────────────
def test_flatten_short_position_buys_full_size():
    # 空倉 szi=-2.5 → 平空買入（is_buy=True）全量 2.5
    mine = {"ETH": _pos(szi="-2.5")}
    ex, _, res = _sync({}, mine)

    assert ex.records == [("close_reduce_only", "ETH", True, Decimal("2.5"))]
    assert res["flattened"] == [{"coin": "ETH", "side": "short", "size": Decimal("2.5")}]


def test_flatten_long_position_sells_full_size():
    mine = {"ETH": _pos(szi="1.75")}
    ex, _, res = _sync({}, mine)
    assert ex.records == [("close_reduce_only", "ETH", False, Decimal("1.75"))]


# ── 7. ex.* 失敗 → failed 記錄＋warn、不拋例外 ──────────────────────
def test_market_open_failure_recorded_and_warned():
    leader = {"ETH": _pos(szi="10")}
    ex, notifier, res = _sync(leader, {}, fail_ops={"market_open"})

    assert res["failed"] == [{"coin": "ETH", "action": "market_open"}]
    assert res["opened"] == []
    warns = _warns(notifier)
    assert len(warns) == 1 and "ETH" in warns[0][2]


def test_update_leverage_failure_warns_but_open_proceeds():
    # hl trader.py:163 不檢查 set_leverage 回傳值、照樣下單——best-effort＋可見性
    leader = {"ETH": _pos(szi="10")}
    ex, notifier, res = _sync(leader, {}, fail_ops={"update_leverage"})

    assert _ops(ex) == ["update_leverage", "market_open"]
    assert res["failed"] == [{"coin": "ETH", "action": "update_leverage"}]
    assert res["opened"] == [{"coin": "ETH", "side": "long", "size": Decimal("1.0000")}]
    assert len(_warns(notifier)) == 1


def test_close_failure_on_flatten_recorded_and_warned():
    mine = {"ETH": _pos(szi="1.0")}
    ex, notifier, res = _sync({}, mine, fail_ops={"close_reduce_only"})

    assert res["failed"] == [{"coin": "ETH", "action": "close_reduce_only"}]
    assert res["flattened"] == []
    assert len(_warns(notifier)) == 1


def test_increase_open_failure_not_recorded_as_adjusted():
    # adjusted 只在執行成功才記錄——失敗只出現在 failed，不得兩處並存
    leader = {"ETH": _pos(szi="15")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, notifier, res = _sync(leader, mine, fail_ops={"market_open"})

    assert _ops(ex) == ["update_leverage", "market_open"]
    assert res["adjusted"] == []
    assert res["failed"] == [{"coin": "ETH", "action": "market_open"}]
    assert len(_warns(notifier)) == 1


def test_decrease_close_failure_not_recorded_as_adjusted():
    leader = {"ETH": _pos(szi="8")}
    mine = {"ETH": _pos(szi="1.0")}
    ex, notifier, res = _sync(leader, mine, fail_ops={"close_reduce_only"})

    assert _ops(ex) == ["close_reduce_only"]
    assert res["adjusted"] == []
    assert res["failed"] == [{"coin": "ETH", "action": "close_reduce_only"}]
    assert len(_warns(notifier)) == 1


def test_flip_close_fails_open_succeeds_best_effort_pinned():
    # 釘死設計決定：flip 兩腿皆 best-effort 嘗試（close 失敗仍重開，同 hl
    # trader.py:269-279 不檢查 close 回傳值）；但 adjusted 只在兩腿皆成功
    # 才記錄——此案 close 失敗 → adjusted 空、failed 記 close、opened 也空
    # （反轉重開不算新開）。
    leader = {"ETH": _pos(szi="12")}
    mine = {"ETH": _pos(szi="-1.0")}
    ex, notifier, res = _sync(leader, mine, fail_ops={"close_reduce_only"})

    assert _ops(ex) == ["close_reduce_only", "update_leverage", "market_open"]
    assert res["adjusted"] == []
    assert res["opened"] == []
    assert res["failed"] == [{"coin": "ETH", "action": "close_reduce_only"}]
    assert len(_warns(notifier)) == 1


# ── 8. spot／非主 dex coin 跳過 ─────────────────────────────────────
def test_spot_and_non_primary_dex_coins_skipped_in_open():
    leader = {
        "PURR/USDC": _pos(coin="PURR/USDC", szi="100"),
        "@85": _pos(coin="@85", szi="100"),
        "xyz:NVDA": _pos(coin="xyz:NVDA", szi="100"),
    }
    ex, notifier, res = _sync(leader, {}, scale="1")

    assert ex.records == []
    assert res["opened"] == []
    assert {s["coin"] for s in res["skipped"]} == {"PURR/USDC", "@85", "xyz:NVDA"}
    assert all(s["reason"] == "spot_or_non_primary_dex" for s in res["skipped"])


def test_spot_coin_skipped_in_flatten():
    mine = {"PURR/USDC": _pos(coin="PURR/USDC", szi="100")}
    ex, _, res = _sync({}, mine)

    assert ex.records == []
    assert res["flattened"] == []
    assert res["skipped"] == [{"coin": "PURR/USDC", "reason": "spot_or_non_primary_dex"}]


# ── 綜合：多標的一輪同步互不干擾 ─────────────────────────────────────
def test_mixed_sync_multiple_coins():
    leader = {
        "ETH": _pos(szi="10"),                 # 我沒有 → 新開
        "BTC": _pos(coin="BTC", szi="5"),      # 我有 0.5 → 容忍內（×0.1 = 0.5）
    }
    mine = {
        "BTC": _pos(coin="BTC", szi="0.5"),
        "SOL": _pos(coin="SOL", szi="-3"),     # leader 已平 → 趨平
    }
    ex, _, res = _sync(leader, mine)

    assert [r["coin"] for r in res["opened"]] == ["ETH"]
    assert res["adjusted"] == []
    assert res["flattened"] == [{"coin": "SOL", "side": "short", "size": Decimal("3")}]
    assert ("close_reduce_only", "SOL", True, Decimal("3")) in ex.records
