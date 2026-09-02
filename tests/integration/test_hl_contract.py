"""tests/integration/test_hl_contract.py

T3（2026-09-02-golive-regression plan）：HL 真實 payload 契約測試——**唯讀**，
testnet 與 mainnet 皆跑。動機（工程原則 1 事故 #5）：「欄位名是假設，不是事實」；
離線測試的假資料照抄的正是同一份未經驗證的假設，只有真實回應能抓出上游欄位漂移。

本檔**只**打唯讀查詢端點（見 `_hl_info_post`，`API_URLS[network] + "/info"`）；
不做任何下單/授權/轉帳等寫入呼叫。每個 type 對每個網路只打一次（見
`_assert_call_budget`：模組結束時斷言總呼叫數 < 40），呼叫前一律節流 0.7 秒。

每個 type 的測試流程分兩段：
1. **原始 JSON**：對 HL 唯讀端點打一次真請求，取得未加工的回應。
2. **餵給我方解析函式**：把同一份原始 JSON 用假 info／post_fn 注入
   `HyperliquidAdapter`／`HLGateway`，讓解析邏輯跑一次但**不觸發第二次網路請求**
   （`_CannedInfo`／`_canned_post_fn`）。兩段皆標明於各測試函式內。

欄位名警報器（`_check_field_contract`）：
(a) 我方程式碼假設存在的欄位（`*_KEYS` 常數，附 `檔案:行號` 出處）缺席 → FAIL。
(b) 與 `tests/fixtures/hl_payload_keys/<network>-<type>.json` 的歷史 baseline 比對：
    baseline 有、現在沒有 → FAIL（疑似上游欄位名漂移）；現在多出的 → WARN。
    首次執行（無 baseline）→ 建立 baseline 並印出 "baseline created"。
fixture 只存**鍵名與型別 marker**，不存任何實際數值（不留位址／金額）。

已知範圍限制（誠實標註，非隱藏）：
- `userFills`（非 by-time）：程式碼中**沒有任何呼叫點**使用這個 type，故不測。
- `HLGateway.vault_details`／`candle_snapshot`／`user_details`：不在本 task 點名的
  型別清單內；`user_details` 打的也不是 `/info`（另一個唯讀 explorer 端點），
  刻意不在本檔涵蓋範圍，以維持「本檔僅打查詢端點」的單一不變量好稽核。
- `HLGateway.clearinghouse_state` 的實際請求體不帶 `dex` 鍵，
  `HyperliquidAdapter.get_account_state`/`get_positions`（經 SDK `user_state`）
  的請求體帶 `dex=""`——兩者理論上等價（HL 預設 dex 即為空字串），本檔只實際
  發送一次（`dex=""` 版本）並把同一份原始回應同時餵給兩條解析路徑，兩個請求體
  的等價性本身未被本測試逐一verify（若哪天出現差異，需要另外查證）。
"""
import json
import time
import warnings
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from spark.config import API_URLS
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.filet.leader_perf import (
    PERP_PERIODS, STATUS_INSUFFICIENT, STATUS_OK, compute_perp_performance,
)
from spark.publicapi.hl import HLGateway

pytestmark = pytest.mark.integration

NETWORKS = ("testnet", "mainnet")
# 兩個位址兩網皆有歷史（plan §3 T3 指定）。BUILDER_ADDR 同時是 prod 主網 follower
# 的錢包，用它跑大部分「有真實資料」的查詢；CUSTOMER_ADDR 用於需要「另一個位址」
# 的查詢（maxBuilderFee 的 user 參數、extraAgents），避免整份測試只靠單一位址。
BUILDER_ADDR = "0xbAC652a5fb611c1bdc3b9d244cc7e0cc03123662"
CUSTOMER_ADDR = "0xfb9c52f56f03d786ad5d435aa70fe45d80569760"

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "hl_payload_keys"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

_TIMEOUT_S = 15.0
_SLEEP_S = 0.7
_MAX_CALLS = 40
_call_count = {"n": 0}


def _hl_info_post(network: str, body: dict):
    """唯一的出網函式：只打唯讀查詢端點，回傳原始 JSON。呼叫前節流 0.7s、
    計數累加；總數上限由 `_assert_call_budget` 在模組結束時斷言。"""
    url = f"{API_URLS[network]}/info"
    time.sleep(_SLEEP_S)
    _call_count["n"] += 1
    resp = httpx.post(url, json=body, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="module", autouse=True)
def _assert_call_budget():
    yield
    assert _call_count["n"] < _MAX_CALLS, (
        f"契約測試對 HL 的請求數 {_call_count['n']} 已達上限 {_MAX_CALLS}——"
        "檢查是否有測試重複打了同一個 type")


class _CannedInfo:
    """假 info 物件：只允許呼叫指定的單一方法，回傳事先抓好的原始 JSON。
    保證『把原始 JSON 餵給解析函式』這一步零額外網路請求。"""

    def __init__(self, method: str, value):
        self._method = method
        self._value = value

    def __getattr__(self, name):
        if name != self._method:
            raise AssertionError(f"契約測試不預期呼叫 info.{name}()")
        return lambda *a, **k: self._value


def _canned_post_fn(value):
    def _fn(_url, _body):
        return value
    return _fn


def _no_sleep(_seconds):
    return None


def _decimal_ok(v) -> bool:
    return isinstance(v, Decimal) and v.is_finite()


def _first_item_keys(raw):
    """萃取可比對的鍵集合：dict 回自身鍵；list 回第一筆元素的鍵；其餘（純量、
    空 list、非 dict 元素的 list）回 None——代表這個值沒有穩定鍵集可比對。"""
    if isinstance(raw, dict):
        return set(raw.keys())
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return set(raw[0].keys())
    return None


def _check_field_contract(network: str, type_name: str, raw, required: set) -> None:
    """(a) `required`（我方程式碼假設存在的欄位）缺席 → FAIL。
    (b) 與 baseline fixture 比對整個觀察到的鍵集合：baseline 有、現在沒有 → FAIL；
        現在多出的 → WARN。無 baseline → 建立並印出 'baseline created'。"""
    observed = _first_item_keys(raw)
    if observed is None:
        warnings.warn(
            f"{network}/{type_name}: 回應無穩定鍵集可比對"
            f"（型別={type(raw).__name__}），略過欄位比對")
        return
    missing_required = required - observed
    assert not missing_required, (
        f"{network}/{type_name} 缺少我方程式碼假設存在的欄位: {sorted(missing_required)}"
        f"（觀察到的欄位: {sorted(observed)}）")
    extra_vs_required = observed - required
    if extra_vs_required:
        warnings.warn(
            f"{network}/{type_name}: 觀察到我方未讀取的欄位（僅供留意）: "
            f"{sorted(extra_vs_required)}")

    fixture_path = FIXTURE_DIR / f"{network}-{type_name}.json"
    if not fixture_path.exists():
        fixture_path.write_text(
            json.dumps({"keys": sorted(observed)}, indent=2, ensure_ascii=False) + "\n")
        print(f"[baseline created] {fixture_path}")
        return
    baseline = set(json.loads(fixture_path.read_text())["keys"])
    missing_from_baseline = baseline - observed
    assert not missing_from_baseline, (
        f"{network}/{type_name} 相較 baseline 缺少欄位（疑似上游欄位名漂移）: "
        f"{sorted(missing_from_baseline)}")
    extra_from_baseline = observed - baseline
    if extra_from_baseline:
        warnings.warn(
            f"{network}/{type_name}: 相較 baseline 多出欄位: {sorted(extra_from_baseline)}")


# --- REQUIRED_KEYS：我方程式碼實際讀取的欄位（附出處，缺一個都會讓解析炸掉） ---

# spark.exchange.hyperliquid.py:167-174（get_account_state）
# src/spark/publicapi/hl_explore.py:634-639,661（_account_value/_parse_positions）
CLEARINGHOUSE_STATE_KEYS = {"assetPositions", "marginSummary", "withdrawable"}
CLEARINGHOUSE_MARGIN_SUMMARY_KEYS = {"accountValue", "totalMarginUsed", "totalNtlPos"}
# spark.exchange.hyperliquid.py:146-164；hl_explore.py:666-674
CLEARINGHOUSE_POSITION_KEYS = {
    "coin", "szi", "entryPx", "leverage", "unrealizedPnl", "marginUsed"}
CLEARINGHOUSE_LEVERAGE_KEYS = {"type", "value"}

# src/spark/publicapi/hl.py:176-186（spot_usdc_balance）
SPOT_CLEARINGHOUSE_STATE_KEYS = {"balances"}
SPOT_BALANCE_KEYS = {"coin", "total"}

# spark.exchange.hyperliquid.py:124-140（get_open_orders 直接下標的欄位）
FRONTEND_OPEN_ORDER_KEYS = {"oid", "coin", "side", "limitPx", "sz"}

# spark.exchange.hyperliquid.py:243-253；src/spark/publicapi/hl.py:56-63,75-83
USER_FILL_KEYS = {"time", "coin", "px", "sz", "side", "crossed", "oid"}

# spark.exchange.hyperliquid.py:86-87；src/spark/publicapi/hl.py:310-311
EXTRA_AGENT_KEYS = {"address"}

# spark.exchange.hyperliquid.py:324-325（get_ledger_flows：item 層）
LEDGER_UPDATE_ITEM_KEYS = {"time", "delta"}
# spark.exchange.ledger_flows.py:36,45,65,76（delta.get("type") 恆讀）
LEDGER_DELTA_KEYS = {"type"}

# spark.exchange.hyperliquid.py:301-302（get_size_decimals）
META_KEYS = {"universe"}
META_UNIVERSE_ITEM_KEYS = {"name", "szDecimals"}

# spark.exchange.hyperliquid.py:196-203（get_equity_view extract 用的 payload 欄位）；
# src/spark/filet/leader_perf.py:264-265（extract_window）
PORTFOLIO_REQUIRED_PERIODS = {
    "day", "week", "month", "allTime",
    "perpDay", "perpWeek", "perpMonth", "perpAllTime",
}
PORTFOLIO_WINDOW_PAYLOAD_KEYS = {"accountValueHistory", "pnlHistory"}

# spark.exchange.hyperliquid.py:52-55（query_builder_accrued）
REFERRAL_KEYS = {"builderRewards"}

# spark.exchange.hyperliquid.py:346-354（get_active_asset_leverage）
ACTIVE_ASSET_DATA_KEYS = {"leverage"}
ACTIVE_ASSET_LEVERAGE_KEYS = {"type", "value"}


@pytest.mark.parametrize("network", NETWORKS)
def test_clearinghouse_state(network):
    address = BUILDER_ADDR
    # 請求體照抄 hyperliquid.info.Info.user_state（.venv 原始碼查證，見 info.py:128）。
    raw = _hl_info_post(network, {"type": "clearinghouseState", "user": address, "dex": ""})
    assert isinstance(raw, dict)
    _check_field_contract(network, "clearinghouseState", raw, CLEARINGHOUSE_STATE_KEYS)
    _check_field_contract(
        network, "clearinghouseState.marginSummary",
        raw["marginSummary"], CLEARINGHOUSE_MARGIN_SUMMARY_KEYS)

    positions = raw.get("assetPositions") or []
    if positions:
        pos = positions[0]["position"]
        _check_field_contract(
            network, "clearinghouseState.position", pos, CLEARINGHOUSE_POSITION_KEYS)
        _check_field_contract(
            network, "clearinghouseState.leverage",
            pos["leverage"], CLEARINGHOUSE_LEVERAGE_KEYS)
    else:
        warnings.warn(
            f"{network}/clearinghouseState: {address} 目前無持倉，"
            "略過 position/leverage 巢狀欄位比對")

    # --- 餵給 HyperliquidAdapter（零額外網路請求） ---
    adapter = HyperliquidAdapter(network, info=_CannedInfo("user_state", raw))
    snap = adapter.get_account_state(address)
    assert _decimal_ok(snap.account_value)
    assert _decimal_ok(snap.total_margin_used)
    assert _decimal_ok(snap.withdrawable)
    assert _decimal_ok(snap.total_ntl_pos)

    for p in adapter.get_positions(address):
        assert _decimal_ok(p.szi) and p.szi != 0
        assert _decimal_ok(p.entry_px)
        assert isinstance(p.leverage, int)
        assert isinstance(p.is_cross, bool)
        assert _decimal_ok(p.unrealized_pnl)
        assert _decimal_ok(p.margin_used)

    # --- HLGateway：同一份原始 JSON ---
    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    assert gw.clearinghouse_state(address) == raw
    assert _decimal_ok(gw.get_account_value(address))


@pytest.mark.parametrize("network", NETWORKS)
def test_spot_clearinghouse_state(network):
    address = BUILDER_ADDR
    # 請求體照抄 hyperliquid.info.Info.spot_user_state（info.py:130-131；無 dex 鍵）。
    raw = _hl_info_post(network, {"type": "spotClearinghouseState", "user": address})
    assert isinstance(raw, dict)
    _check_field_contract(network, "spotClearinghouseState", raw, SPOT_CLEARINGHOUSE_STATE_KEYS)

    balances = raw.get("balances") or []
    if balances:
        _check_field_contract(
            network, "spotClearinghouseState.balance", balances[0], SPOT_BALANCE_KEYS)
    else:
        warnings.warn(
            f"{network}/spotClearinghouseState: {address} 無 spot 餘額，"
            "略過 balance 巢狀欄位比對")

    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    assert _decimal_ok(gw.spot_usdc_balance(address))


@pytest.mark.parametrize("network", NETWORKS)
def test_frontend_open_orders(network):
    address = BUILDER_ADDR
    raw = _hl_info_post(network, {"type": "frontendOpenOrders", "user": address, "dex": ""})
    assert isinstance(raw, list)
    _check_field_contract(network, "frontendOpenOrders", raw, FRONTEND_OPEN_ORDER_KEYS)
    if not raw:
        warnings.warn(f"{network}/frontendOpenOrders: {address} 目前無掛單，僅驗證型別為 list")

    adapter = HyperliquidAdapter(network, info=_CannedInfo("frontend_open_orders", raw))
    orders = adapter.get_open_orders(address)
    assert len(orders) == len(raw)
    for o in orders:
        assert isinstance(o.oid, int)
        assert isinstance(o.coin, str) and o.coin
        assert _decimal_ok(o.limit_px)
        assert _decimal_ok(o.sz)
        assert isinstance(o.is_buy, bool)


@pytest.mark.parametrize("network", NETWORKS)
def test_user_fills_by_time(network):
    address = BUILDER_ADDR
    end = datetime.now(timezone.utc)
    # 窗口刻意收窄為近 6 小時，而不是「從 0 查到現在」：BUILDER_ADDR 同時是 prod
    # 主網 follower（實測 2026-09-02：從 0 查到現在在 mainnet 剛好回滿 2000 筆
    # 上限，觸發 HyperliquidAdapter.get_user_fills 的 FillsTruncatedError——
    # 那是**正確**行為（見 hyperliquid.py:220-241 docstring），但契約測試要驗的是
    # 欄位形狀，不是刻意去撞這個安全閥；縮小窗口才是對的修法，不是放寬斷言。
    start = end - timedelta(hours=6)
    now_ms = int(end.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)
    # 請求體對齊 HLGateway.get_user_fills（src/spark/publicapi/hl.py:220-222）；
    # 不含 SDK Info.user_fills_by_time 額外帶的 aggregateByTime——該旗標只影響
    # 是否合併 partial fills，不改變單筆欄位形狀，因此本檔只打一次即同時驗證
    # adapter 與 gateway 兩條解析路徑（見檔頭「已知範圍限制」）。
    raw = _hl_info_post(
        network,
        {"type": "userFillsByTime", "user": address, "startTime": start_ms, "endTime": now_ms})
    assert isinstance(raw, list)
    _check_field_contract(network, "userFillsByTime", raw, USER_FILL_KEYS)
    if not raw:
        warnings.warn(f"{network}/userFillsByTime: {address} 查無成交，僅驗證型別為 list")
    else:
        times = [int(f["time"]) for f in raw]
        if times != sorted(times):
            warnings.warn(
                f"{network}/userFillsByTime: 原始回應**不是**依時間升冪排列——"
                "HLGateway.get_user_fills_paged 的分頁假設可能不成立，需人工複核")

    adapter = HyperliquidAdapter(network, info=_CannedInfo("user_fills_by_time", raw))
    fills = adapter.get_user_fills(address, start, end)
    for f in fills:
        assert _decimal_ok(f.px) and _decimal_ok(f.sz)
        assert _decimal_ok(f.fee) and _decimal_ok(f.builder_fee)
        assert isinstance(f.crossed, bool)
        assert isinstance(f.oid, int)

    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    gw_fills = gw.get_user_fills(address, start, end)
    assert len(gw_fills) == len(fills) == len(raw)
    detail = gw.get_fills_detail(address, start, end)
    assert len(detail) == len(raw)
    for d in detail:
        assert isinstance(d["px"], str) and isinstance(d["sz"], str)


@pytest.mark.parametrize("network", NETWORKS)
def test_extra_agents(network):
    address = CUSTOMER_ADDR
    raw = _hl_info_post(network, {"type": "extraAgents", "user": address})
    assert isinstance(raw, list)
    _check_field_contract(network, "extraAgents", raw, EXTRA_AGENT_KEYS)
    if not raw:
        warnings.warn(f"{network}/extraAgents: {address} 目前無已授權 agent，僅驗證型別為 list")

    adapter = HyperliquidAdapter(network, info=_CannedInfo("post", raw))
    agents = adapter.query_agent_addresses(address)
    assert all(a == a.lower() for a in agents)

    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    assert gw.agent_addresses(address) == agents


@pytest.mark.parametrize("network", NETWORKS)
def test_max_builder_fee(network):
    raw = _hl_info_post(
        network, {"type": "maxBuilderFee", "user": CUSTOMER_ADDR, "builder": BUILDER_ADDR})
    assert isinstance(raw, (int, float))

    # 純量型別：沒有欄位名可比對，改記錄根型別當 baseline（防「哪天回應被包成
    # 物件」這種形狀劇變，而不是欄位改名——與 `_check_field_contract` 不同機制，
    # 故不重用它，直接寫一份最小 marker fixture）。
    fixture_path = FIXTURE_DIR / f"{network}-maxBuilderFee.json"
    root_type = type(raw).__name__
    if not fixture_path.exists():
        fixture_path.write_text(json.dumps({"root_type": root_type}, indent=2) + "\n")
        print(f"[baseline created] {fixture_path}")
    else:
        baseline = json.loads(fixture_path.read_text())
        assert baseline["root_type"] == root_type, (
            f"{network}/maxBuilderFee 根型別由 {baseline['root_type']} 變成 {root_type}")

    adapter = HyperliquidAdapter(network, info=_CannedInfo("post", raw))
    fee = adapter.query_max_builder_fee(CUSTOMER_ADDR, BUILDER_ADDR)
    assert isinstance(fee, int) and fee >= 0

    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    assert gw.max_builder_fee(CUSTOMER_ADDR, BUILDER_ADDR) == fee


@pytest.mark.parametrize("network", NETWORKS)
def test_user_non_funding_ledger_updates(network):
    address = BUILDER_ADDR
    raw = _hl_info_post(
        network, {"type": "userNonFundingLedgerUpdates", "user": address, "startTime": 0})
    assert isinstance(raw, list)
    _check_field_contract(network, "userNonFundingLedgerUpdates", raw, LEDGER_UPDATE_ITEM_KEYS)

    if raw:
        _check_field_contract(
            network, "userNonFundingLedgerUpdates.delta",
            raw[0].get("delta", {}), LEDGER_DELTA_KEYS)
        times = [int(it.get("time", 0)) for it in raw]
        if times != sorted(times):
            warnings.warn(f"{network}/userNonFundingLedgerUpdates: 回應不是依時間升冪排列")
    else:
        warnings.warn(f"{network}/userNonFundingLedgerUpdates: {address} 查無帳本流水")

    adapter = HyperliquidAdapter(network, info=_CannedInfo("post", raw))
    flows, unknown_types = adapter.get_ledger_flows(address, 0)
    for f in flows:
        assert isinstance(f.time_ms, int)
        assert _decimal_ok(f.usdc)
    assert isinstance(unknown_types, list)

    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    assert gw.non_funding_ledger_updates(address, 0) == raw


@pytest.mark.parametrize("network", NETWORKS)
def test_meta(network):
    raw = _hl_info_post(network, {"type": "meta", "dex": ""})
    assert isinstance(raw, dict)
    _check_field_contract(network, "meta", raw, META_KEYS)

    universe = raw.get("universe") or []
    assert universe, f"{network}/meta: universe 為空（不應發生，perp meta 一定有幣種）"
    _check_field_contract(network, "meta.universe_item", universe[0], META_UNIVERSE_ITEM_KEYS)

    adapter = HyperliquidAdapter(network, info=_CannedInfo("meta", raw))
    dec = adapter.get_size_decimals(universe[0]["name"])
    assert isinstance(dec, int) and dec >= 0


@pytest.mark.parametrize("network", NETWORKS)
def test_all_mids(network):
    raw = _hl_info_post(network, {"type": "allMids", "dex": ""})
    assert isinstance(raw, dict)
    assert raw, f"{network}/allMids: 回應為空字典"

    # allMids 的鍵是動態幣種名（隨上市/下市變動），套用通用的固定欄位名 baseline
    # 比對會把「正常的幣種增減」誤判成欄位漂移，因此本 type 不走
    # `_check_field_contract`；改記錄一個結構性 marker，偵測『回應形狀
    # 從 dict[str,str] 整個變掉』這種真正的契約破壞。
    for k, v in raw.items():
        assert isinstance(k, str)
        assert isinstance(v, str)
        if not k.startswith("@"):
            assert _decimal_ok(Decimal(v))

    fixture_path = FIXTURE_DIR / f"{network}-allMids.json"
    marker = {"root_type": "dict", "value_type": "str"}
    if not fixture_path.exists():
        fixture_path.write_text(json.dumps(marker, indent=2) + "\n")
        print(f"[baseline created] {fixture_path}")
    else:
        baseline = json.loads(fixture_path.read_text())
        assert baseline == marker, f"{network}/allMids 回應形狀已改變: {baseline} -> {marker}"

    adapter = HyperliquidAdapter(network, info=_CannedInfo("all_mids", raw))
    for coin, px in adapter.get_all_mids().items():
        assert not coin.startswith("@")
        assert _decimal_ok(px)


@pytest.mark.parametrize("network", NETWORKS)
def test_portfolio(network):
    address = BUILDER_ADDR
    raw = _hl_info_post(network, {"type": "portfolio", "user": address})
    assert isinstance(raw, list)

    periods = {row[0] for row in raw if isinstance(row, (list, tuple)) and len(row) == 2}
    missing_periods = PORTFOLIO_REQUIRED_PERIODS - periods
    assert not missing_periods, f"{network}/portfolio 缺少期別: {missing_periods}"

    fixture_path = FIXTURE_DIR / f"{network}-portfolio.json"
    if not fixture_path.exists():
        fixture_path.write_text(
            json.dumps({"periods": sorted(periods)}, indent=2, ensure_ascii=False) + "\n")
        print(f"[baseline created] {fixture_path}")
    else:
        baseline_periods = set(json.loads(fixture_path.read_text())["periods"])
        missing_from_baseline = baseline_periods - periods
        assert not missing_from_baseline, (
            f"{network}/portfolio 相較 baseline 缺少期別: {missing_from_baseline}")
        extra = periods - baseline_periods
        if extra:
            warnings.warn(f"{network}/portfolio 相較 baseline 多出期別: {sorted(extra)}")

    payload = next(row[1] for row in raw if row[0] == "day")
    _check_field_contract(
        network, "portfolio.window_payload", payload, PORTFOLIO_WINDOW_PAYLOAD_KEYS)

    av_hist = payload.get("accountValueHistory") or []
    if len(av_hist) >= 2:
        ts_list = [int(ts) for ts, _ in av_hist]
        assert ts_list == sorted(ts_list), (
            f"{network}/portfolio day.accountValueHistory 時間戳非單調遞增")
    for _, v in av_hist:
        assert _decimal_ok(Decimal(str(v)))

    adapter = HyperliquidAdapter(network, info=_CannedInfo("portfolio", raw))
    equity = adapter.get_equity_view(address)
    assert _decimal_ok(equity.current) and _decimal_ok(equity.recent_peak)
    for v in adapter.get_daily_abs_pnl(address):
        assert _decimal_ok(v)

    perf = compute_perp_performance(raw)
    assert set(perf.keys()) == set(PERP_PERIODS)
    for result in perf.values():
        assert result["status"] in (STATUS_OK, STATUS_INSUFFICIENT)

    gw = HLGateway(API_URLS[network], post_fn=_canned_post_fn(raw), sleep_fn=_no_sleep)
    assert gw.portfolio(address) == raw


@pytest.mark.parametrize("network", NETWORKS)
def test_referral_builder_rewards(network):
    raw = _hl_info_post(network, {"type": "referral", "user": BUILDER_ADDR})
    assert isinstance(raw, dict)
    _check_field_contract(network, "referral", raw, REFERRAL_KEYS)

    adapter = HyperliquidAdapter(network, info=_CannedInfo("query_referral_state", raw))
    assert _decimal_ok(adapter.query_builder_accrued(BUILDER_ADDR))


@pytest.mark.parametrize("network", NETWORKS)
def test_active_asset_data(network):
    address = BUILDER_ADDR
    coin = "ETH"
    raw = _hl_info_post(network, {"type": "activeAssetData", "user": address, "coin": coin})
    assert isinstance(raw, dict)
    _check_field_contract(network, "activeAssetData", raw, ACTIVE_ASSET_DATA_KEYS)
    _check_field_contract(
        network, "activeAssetData.leverage", raw["leverage"], ACTIVE_ASSET_LEVERAGE_KEYS)

    adapter = HyperliquidAdapter(network, info=_CannedInfo("post", raw))
    lev_val, is_cross = adapter.get_active_asset_leverage(address, coin)
    assert isinstance(lev_val, int)
    assert isinstance(is_cross, bool)
