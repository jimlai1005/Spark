"""可編程的離線假交易所，記錄所有呼叫供斷言。"""
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from spark.exchange.base import (
    ExchangeAdapter, Order, BuilderCode, Fill, TxResult, OrderResult,
    OpenOrder, Position, AccountSnapshot, EquityView, UserFill,
)

_DEFAULT_SIZE_DECIMALS = 4


class FakeAdapter(ExchangeAdapter):
    """離線假交易所（Phase 1：單 builder、單帳號）。

    刻意用單一狀態槽，非 per-builder/per-user map —— Phase 1 範圍只有一個 builder
    與一個帳號（多客戶為 non-goal）。多情境測試請各自 new 一個 FakeAdapter。
    需要 builder／user 帳號餘額不同的測試，用 `account_values`（address→Decimal 的
    明確 escape hatch）覆蓋個別地址；未列在 `account_values` 裡的地址回退到單一槽
    `account_value`。
    `approve_builder_fee` 不解析 max_rate，固定把 _max_fee 設為 100（代表 0.1% 上限）；
    max_rate 原值仍記錄在 self.calls 供斷言。

    讀側（open_orders/positions/account/equity/fills/mids/sz_decimals）皆為單一狀態槽
    的注入值，與地址無關 —— Phase 1 單帳號範圍不需要 per-address map。`account`/`equity`
    未注入時回傳全零值（而非 None），讓呼叫端不必額外判空。
    """
    def __init__(self, account_value=Decimal("0"), seeded_fills=None, account_values=None,
                 open_orders=(), positions=(), account=None, equity=None, fills=(),
                 mids=None, sz_decimals=None, daily_abs_pnl=(),
                 cancel_ok=True, modify_ok=True, market_open_ok=True,
                 close_reduce_only_ok=True, update_leverage_ok=True, extra_agents=()):
        self._account_value = Decimal(account_value)
        self._account_values = dict(account_values or {})
        self._max_fee = 0
        self._accrued = Decimal("0")
        # 鏈上 extraAgents 模擬（小寫正規化，同 HyperliquidAdapter.query_agent_addresses）；
        # onboarding 的 drift 測試用此注入「目前有效的 agent 地址」。
        self._extra_agents = [a.lower() for a in extra_agents]
        self._seeded_fills = list(seeded_fills or [])
        self.calls = defaultdict(list)
        self._open_orders = list(open_orders)
        self._positions = list(positions)
        self._account = account or AccountSnapshot(
            account_value=Decimal("0"), total_margin_used=Decimal("0"),
            withdrawable=Decimal("0"), total_ntl_pos=Decimal("0"))
        self._equity = equity or EquityView(current=Decimal("0"), recent_peak=Decimal("0"))
        self._fills = list(fills)
        self._mids = dict(mids or {})
        self._sz_decimals = dict(sz_decimals or {})
        self._daily_abs_pnl = list(daily_abs_pnl or [])
        # writes（M1 補齊）：可注入的成功/失敗結果，預設成功。
        self._cancel_ok = cancel_ok
        self._modify_ok = modify_ok
        self._market_open_ok = market_open_ok
        self._close_reduce_only_ok = close_reduce_only_ok
        self._update_leverage_ok = update_leverage_ok

    def get_account_value(self, address: str) -> Decimal:
        return self._account_values.get(address, self._account_value)

    def query_max_builder_fee(self, user: str, builder: str) -> int:
        return self._max_fee

    def query_builder_accrued(self, builder: str) -> Decimal:
        return self._accrued

    def query_agent_addresses(self, user: str) -> list[str]:
        self.calls["query_agent_addresses"].append({"user": user})
        return list(self._extra_agents)

    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]:
        return list(self._seeded_fills)

    def approve_builder_fee(self, main_signer, builder, max_rate) -> TxResult:
        self.calls["approve_builder_fee"].append(
            {"main_signer": main_signer, "builder": builder, "max_rate": max_rate})
        self._max_fee = 100  # 固定代表 0.1%；不解析 max_rate（見 class docstring）
        return TxResult(ok=True, raw={"status": "ok"})

    def approve_agent(self, main_signer, agent_name) -> TxResult:
        self.calls["approve_agent"].append(
            {"main_signer": main_signer, "agent_name": agent_name})
        return TxResult(ok=True, raw={"status": "ok"}, agent_key="0x" + "ab" * 32)

    def place_order(self, agent_signer, order: Order, builder: BuilderCode) -> OrderResult:
        self.calls["place_order"].append(
            {"agent_signer": agent_signer, "order": order, "builder": builder})
        notional = order.size * order.limit_px
        self._accrued += notional * Decimal(builder.f) / Decimal(100000)  # f/1000 % = f/100000
        return OrderResult(ok=True, filled_size=order.size, avg_px=order.limit_px,
                           raw={"status": "filled"})

    def cancel_order(self, agent_signer, coin: str, oid: int) -> bool:
        self.calls["cancel_order"].append({"agent_signer": agent_signer, "coin": coin, "oid": oid})
        return self._cancel_ok

    def modify_order(self, agent_signer, oid: int, order: Order) -> bool:
        self.calls["modify_order"].append({"agent_signer": agent_signer, "oid": oid, "order": order})
        return self._modify_ok

    def market_open(self, agent_signer, coin: str, is_buy: bool, size: Decimal,
                    slippage: Decimal, builder: BuilderCode) -> OrderResult:
        self.calls["market_open"].append({
            "agent_signer": agent_signer, "coin": coin, "is_buy": is_buy, "size": size,
            "slippage": slippage, "builder": builder})
        mid = self._mids.get(coin, Decimal("0"))
        ok = self._market_open_ok
        return OrderResult(ok=ok, filled_size=size if ok else Decimal("0"),
                           avg_px=mid, raw={"status": "ok" if ok else "err"})

    def close_reduce_only(self, agent_signer, coin: str, is_buy: bool, size: Decimal,
                          slippage: Decimal, builder: BuilderCode) -> OrderResult:
        self.calls["close_reduce_only"].append({
            "agent_signer": agent_signer, "coin": coin, "is_buy": is_buy, "size": size,
            "slippage": slippage, "builder": builder})
        if coin not in self._mids:
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                               raw={"error": f"no mid for {coin}"})
        ok = self._close_reduce_only_ok
        return OrderResult(ok=ok, filled_size=size if ok else Decimal("0"),
                           avg_px=self._mids[coin], raw={"status": "ok" if ok else "err"})

    def update_leverage(self, agent_signer, coin: str, leverage: int, is_cross: bool) -> bool:
        self.calls["update_leverage"].append({
            "agent_signer": agent_signer, "coin": coin, "leverage": leverage, "is_cross": is_cross})
        return self._update_leverage_ok

    # --- reads（copytrade M1）---
    def get_open_orders(self, address: str) -> list[OpenOrder]:
        self.calls["get_open_orders"].append({"address": address})
        return list(self._open_orders)

    def get_positions(self, address: str) -> list[Position]:
        self.calls["get_positions"].append({"address": address})
        return list(self._positions)

    def get_account_state(self, address: str) -> AccountSnapshot:
        self.calls["get_account_state"].append({"address": address})
        return self._account

    def get_equity_view(self, address: str) -> EquityView:
        self.calls["get_equity_view"].append({"address": address})
        return self._equity

    def get_user_fills(self, address: str, start: datetime, end: datetime) -> list[UserFill]:
        self.calls["get_user_fills"].append({"address": address, "start": start, "end": end})
        return list(self._fills)

    def get_all_mids(self) -> dict[str, Decimal]:
        self.calls["get_all_mids"].append({})
        return dict(self._mids)

    def get_size_decimals(self, coin: str) -> int:
        self.calls["get_size_decimals"].append({"coin": coin})
        return self._sz_decimals.get(coin, _DEFAULT_SIZE_DECIMALS)

    def get_daily_abs_pnl(self, address: str) -> list[Decimal]:
        self.calls["get_daily_abs_pnl"].append({"address": address})
        return list(self._daily_abs_pnl)
