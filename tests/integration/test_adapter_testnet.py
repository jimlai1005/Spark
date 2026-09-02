"""tests/integration/test_adapter_testnet.py
Adapter 直連情境（golive-regression plan T2，§3 T2「A 組」），取代舊
`test_copytrade_testnet.py`（已刪除，見 D2）。真 Hyperliquid testnet，直接用
`HyperliquidAdapter` 操作 agent key，不經 FastAPI app。

⚠️ **本檔依賴 `test_e2e_noncustodial.py` 先跑完**：customer 的 agent key（由
E2E 的 S2 產生、S3 在鏈上核准）由本檔直接複用，省下重複 onboarding 一輪拋棄式
錢包與 testnet 的 $1 新帳戶手續費（見 harness `_faucet_topup_perp`/`fund` 的
docstring）。若 agent key 不存在，下面的 fixture 會 `pytest.fail`（不是
`pytest.skip`）並指向正確的執行順序：

    uv run pytest -m integration \
        tests/integration/test_e2e_noncustodial.py \
        tests/integration/test_adapter_testnet.py -q -x -s -p no:cacheprovider

A1 額外使用 `leader`（session 已入金的另一顆拋棄式錢包，與 customer 不同人，無
自我成交風險）的**主鑰**主動送出一筆會穿價的 IOC 反向單，逼出成交——原始研究
（`scripts/testnet_modify_probe.py`、2026-07-19）證實 HL 的 batchModify 是
post-only 語意（modify 到 IOC 或穿價一律被拒），且改單後的訂單只能被動等對手
成交，仰賴 thin testnet 流動性自然吃單耗時可達數分鐘；本測試用第二顆帳號主動
交叉，讓「modify 不丟 builder 歸屬」這個結論在有限時間內可重驗。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from spark.exchange.base import BuilderCode, Order
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.keystore.envfile import EnvFileKeyStore
from spark.publicapi.config import derive_account_id

from tests.integration.harness import TESTNET_URL, wait_until

pytestmark = pytest.mark.integration

_COIN = "ETH"
_info = Info(TESTNET_URL, skip_ws=True)


@pytest.fixture(scope="module")
def account_id(customer) -> str:
    return derive_account_id(customer.address)


@pytest.fixture(scope="module")
def agent_signer(keysvc, account_id, customer):
    ks = EnvFileKeyStore(str(keysvc.keys_dir))
    try:
        return ks.get_agent_signer(account_id)
    except (KeyError, PermissionError) as e:
        pytest.fail(
            "A 組依賴 test_e2e_noncustodial.py 先跑完 S2（產生 agent key）與 S3"
            f"（鏈上核准）——customer={customer.address} account_id={account_id} "
            f"尚無可用 agent key（{e}）。請依本檔檔頭給的順序執行，不要用目錄形式"
            "單獨跑 tests/integration（pytest 會依檔名字母序把本檔排在 E2E 之前）。")


@pytest.fixture(scope="module")
def adapter(agent_signer, customer) -> HyperliquidAdapter:
    exch = Exchange(agent_signer, TESTNET_URL, account_address=customer.address)
    return HyperliquidAdapter("testnet", info=_info, exchange=exch)


@pytest.fixture(scope="module")
def read_adapter() -> HyperliquidAdapter:
    return HyperliquidAdapter("testnet", info=_info, exchange=None)


def _size_for(adapter: HyperliquidAdapter, notional: Decimal, mid: Decimal) -> Decimal:
    sz_dec = adapter.get_size_decimals(_COIN)
    quantum = Decimal(1).scaleb(-sz_dec)
    size = (notional / mid).quantize(quantum)
    assert size > 0, f"算出的 size={size} 非正（notional={notional} mid={mid}）"
    return size


def _open_order_for(adapter: HyperliquidAdapter, address: str):
    opens = [o for o in adapter.get_open_orders(address) if o.coin == _COIN]
    return opens[0] if opens else None


def _position_for(adapter: HyperliquidAdapter, address: str):
    positions = [p for p in adapter.get_positions(address) if p.coin == _COIN]
    return positions[0] if positions else None


# ---------------------------------------------------------------------------
# A1 — place_order (GTC 遠價) → get_open_orders → modify_order → 成交仍帶 builder fee
# ---------------------------------------------------------------------------


def test_a1_modify_order_preserves_builder_attribution(adapter, read_adapter, customer,
                                                        builder_address, leader):
    mid = Decimal(str(_info.all_mids()[_COIN]))
    far_px = mid * Decimal("0.5")
    # ⭐ HL 的 $10 最小名目門檻是用**委託價 × size** 算的，不是 mid × size——
    # 遠價掛單若拿 mid 折算 size，遠低於市場價的 far_px 會讓實際委託名目跌破
    # $10 而被拒（首次實測踩到：size=15/mid≈0.00625，0.00625×far_px≈$7.5）。
    size = _size_for(adapter, Decimal("15"), far_px)
    builder = BuilderCode(b=builder_address, f=1)

    resting = Order(coin=_COIN, is_buy=True, size=size, limit_px=far_px, tif="Gtc")
    res = adapter.place_order(None, resting, builder)
    print(f"[A1] 遠端掛單 result={res}")
    assert res.ok, res.raw

    assert wait_until(lambda: _open_order_for(adapter, customer.address) is not None,
                      timeout=30), "遠端掛單未出現在 get_open_orders"
    oid = _open_order_for(adapter, customer.address).oid

    baseline_accrued = read_adapter.query_builder_accrued(builder_address)

    # 改到 bid/ask 價差中點：post-only 語意下這是不會被拒的改法（穿價／IOC 皆被拒，
    # 見本檔檔頭引用的 2026-07-19 結論）。⚠️ 第一次實測改成 `ask - spread/10`
    # 被拒（"Post only order would have immediately matched"）——`_round_px`
    # 的 5 位有效數字四捨五入把貼著 ask 的價位**捨進成剛好等於 ask**（thin
    # book 下 spread 只有幾檔 tick），等同穿價；改用價差中點留出足夠緩衝，
    # 不靠近到會被捨入吃掉的邊界。
    book = _info.l2_snapshot(_COIN)["levels"]
    best_bid_px = Decimal(str(book[0][0]["px"]))
    best_ask_px = Decimal(str(book[1][0]["px"]))
    new_px = best_bid_px + (best_ask_px - best_bid_px) / Decimal("2")
    modify_target = Order(coin=_COIN, is_buy=True, size=size, limit_px=new_px, tif="Gtc")
    modified = adapter.modify_order(None, oid, modify_target)
    print(f"[A1] modify_order(oid={oid} -> px={new_px}) ok={modified}")
    assert modified, "modify 被拒（見上方 stdout 的 HL 拒單訊息）"

    # leader 用主鑰主動送一筆確定會吃到我方新掛價的 IOC 賣單，逼出成交
    # （不仰賴 thin testnet book 自然吃單）。
    ex_leader = Exchange(leader.account, TESTNET_URL)
    cross_px = new_px * Decimal("0.99")
    cross_res = ex_leader.order(_COIN, False, float(size), float(cross_px),
                                {"limit": {"tif": "Ioc"}}, reduce_only=False)
    print(f"[A1] leader 反向 IOC 賣單 result={cross_res}")

    def _filled() -> bool:
        return (_open_order_for(adapter, customer.address) is None
                and _position_for(adapter, customer.address) is not None
                and _position_for(adapter, customer.address).szi != 0)

    assert wait_until(_filled, timeout=60), "modify 後訂單未在時限內成交"

    assert wait_until(
        lambda: read_adapter.query_builder_accrued(builder_address) > baseline_accrued,
        timeout=60), ("builder accrued 未增加——modify 後成交可能丟失 builder 歸屬"
                      "（與 2026-07-19 研究結論相反，需人工深查）")

    fills = adapter.get_user_fills(customer.address,
                                   datetime.now(timezone.utc) - timedelta(minutes=10),
                                   datetime.now(timezone.utc))
    matching = [f for f in fills if f.coin == _COIN]
    assert matching, "查無 ETH 成交明細"
    assert any(f.builder_fee > 0 for f in matching), (
        f"modify 後成交的 builder_fee 欄位為 0（fills={matching}）")
    print(f"[A1] fills={matching}")


# ---------------------------------------------------------------------------
# A2 — market_open 成交帶 builder
# ---------------------------------------------------------------------------


def test_a2_market_open_has_builder_attribution(adapter, read_adapter, agent_signer,
                                                 customer, builder_address):
    mid = Decimal(str(_info.all_mids()[_COIN]))
    size = _size_for(adapter, Decimal("12"), mid)
    builder = BuilderCode(b=builder_address, f=1)
    baseline = read_adapter.query_builder_accrued(builder_address)

    res = adapter.market_open(agent_signer, _COIN, True, size, Decimal("0.05"), builder)
    print(f"[A2] market_open result={res}")
    assert res.ok, res.raw
    assert res.filled_size > 0

    assert wait_until(
        lambda: read_adapter.query_builder_accrued(builder_address) > baseline,
        timeout=30), "market_open 成交後 builder accrued 未增加"


# ---------------------------------------------------------------------------
# A3 — close_reduce_only 全平
# ---------------------------------------------------------------------------


def test_a3_close_reduce_only_flattens(adapter, agent_signer, customer, builder_address):
    builder = BuilderCode(b=builder_address, f=1)
    pos = _position_for(adapter, customer.address)
    assert pos is not None and pos.szi != 0, "沒有可平的 ETH 部位（A1/A2 未成功？）"

    is_buy_to_close = pos.szi < 0  # 空頭平倉方向為買；多頭平倉方向為賣
    res = adapter.close_reduce_only(agent_signer, _COIN, is_buy_to_close, abs(pos.szi),
                                    Decimal("0.05"), builder)
    print(f"[A3] close_reduce_only result={res}")
    assert res.ok, res.raw

    assert wait_until(lambda: _position_for(adapter, customer.address) is None
                      or _position_for(adapter, customer.address).szi == 0,
                      timeout=60), "全平未在時限內完成"


# ---------------------------------------------------------------------------
# A4 — 讀取型方法解析不拋、欄位型別正確
# ---------------------------------------------------------------------------


def test_a4_read_methods_parse_without_raising(adapter, read_adapter, customer,
                                               builder_address):
    equity = adapter.get_equity_view(customer.address)
    assert isinstance(equity.current, Decimal)
    assert isinstance(equity.recent_peak, Decimal)

    snap = adapter.get_account_state(customer.address)
    assert isinstance(snap.account_value, Decimal)
    assert isinstance(snap.total_margin_used, Decimal)
    assert isinstance(snap.withdrawable, Decimal)
    assert isinstance(snap.total_ntl_pos, Decimal)

    # ⭐⭐ 產品發現（非本測試斷言錯誤，見派工回報第 6 點）：`harness.fund()` 用
    # `Exchange.usd_transfer`（usdSend）打錢，HL 的 `userNonFundingLedgerUpdates`
    # 把這類錢包對錢包的內部轉帳記成 `delta.type == "internalTransfer"`（實測
    # 原始 payload：`{"type": "internalTransfer", "usdc": "150.0", ...,
    # "fee": "1.0"}`——`fee` 欄位證實了 harness `_faucet_topup_perp` docstring
    # 提到的「新帳戶 $1 手續費」觀察）。但
    # `spark/exchange/ledger_flows.py::FLOW_FIELDS` 白名單只有
    # `vaultDeposit`／`deposit`／`withdraw`／`vaultWithdraw`（另有
    # `accountClassTransfer` 特例），**沒有 `internalTransfer`**——結果是
    # `get_ledger_flows` 對「用 usdSend 資助的帳戶」回傳的 `flows` 永遠是空的，
    # 這筆真實入金完全消失，只出現在第二個回傳值 `unknown_types` 裡。這不是
    # edge case：usdSend／「Send」是 Hyperliquid UI 上錢包對錢包最常見的資助
    # 方式之一（不只是本 harness 的水龍頭手法）。本測試改成如實斷言目前的行為，
    # 不斷言 plan 原文假設的「flows 非空」。
    # 2026-09-02 T9 修復後：internalTransfer 已納入白名單（方向由查詢位址決定）。
    # 本 customer 在 S1 由水龍頭 usdSend 資助 → 至少一筆 inbound（+usdc−fee）。
    flows, unknown_types = adapter.get_ledger_flows(customer.address, start_ms=0)
    assert "internalTransfer" not in unknown_types, unknown_types
    inbound = [f for f in flows if f.usdc > 0]
    assert inbound, f"T9 後應看到水龍頭資助那筆 inbound internalTransfer；flows={flows}"
    assert max(f.usdc for f in inbound) >= Decimal("100"), inbound
    print(f"[A4] ledger flows={len(flows)}（inbound {len(inbound)} 筆，最大 "
          f"{max(f.usdc for f in inbound)}） unknown_types={unknown_types}")

    lev, is_cross = adapter.get_active_asset_leverage(customer.address, _COIN)
    assert isinstance(lev, int)
    assert isinstance(is_cross, bool)

    agents = read_adapter.query_agent_addresses(customer.address)
    assert isinstance(agents, list)
    assert all(isinstance(a, str) for a in agents)

    accrued = read_adapter.query_builder_accrued(builder_address)
    assert isinstance(accrued, Decimal)
    print(f"[A4] leverage={lev}/{is_cross} agents={agents} accrued={accrued}")
