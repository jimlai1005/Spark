"""測試共用：給 FakeAdapter 的 leader 補上「交易所帳戶總值」（plan 2026-11-22 T2）。

`run_cycle` 的 leader 權益分母改讀 `adapter.get_account_total(leader)`。FakeAdapter
預設沒有 `account_totals`（查無即 ValueError）——這是刻意的：測試替身不得替正式碼掩蓋
「沒讀到帳戶總值」。因此需要跑 `run_cycle` 的既有測試，在 helper 層補上這一筆，數值沿用
該 adapter 原本回給 leader 的 perp `account_value`，使既有斷言不變。"""
import time

from spark.copytrade.config import CopySettings
from spark.exchange.base import AccountTotal
from spark.exchange.fakes import FakeAdapter

LEADER = CopySettings().leader_address


def seed_leader_total(adapter: FakeAdapter, leader: str = LEADER) -> FakeAdapter:
    """leader 的 total ＝ 原本 leader 的 perp `account_value`、時間戳＝測試當下。
    已明確給了 leader total 的不覆蓋。

    時間戳用 `time.time_ns`（不是常被 monkeypatch 的 `time.time`）：釘死時鐘的測試把
    run_cycle 的 now 釘在過去，真實 now 落在「未來」＝不算過期（時鐘偏差路徑），
    且不消耗 `_ticking_clock` 的計次。"""
    key = leader.lower()
    if key not in adapter._account_totals:
        state = adapter._account_states.get(key, adapter._account)
        adapter._account_totals[key] = AccountTotal(
            value=state.account_value, as_of_ms=time.time_ns() // 1_000_000)
    return adapter
