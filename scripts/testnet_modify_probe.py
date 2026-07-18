"""testnet 探針：驗證 modify-first 主動路徑（SDK 0.24.0 modify_order 無 builder 參數）
下單成交後，builder fee 歸屬是否仍然計入。對照兩條路徑：

  A（對照組）：orchestrator.place_marketable_order → 直接下帶 builder 的 IOC 成交單
              （builder 走 "order" action 的 builder 欄位，已知歸屬正確，見
              hyperliquid/utils/signing.py:519-527 order_wires_to_order_action）。
  B（實驗組）：place_order 先建立一筆遠端 GTC 掛單（帶 builder）→ modify_order 改價
              至可成交（SDK modify_order()/bulk_modify_orders_new() 建構的 batchModify
              action 結構上無 builder 欄位，見 hyperliquid/exchange.py:190-238）→
              觀察成交後 builder 累計費是否仍增加。

背景：spec T1.3。詳見
docs/superpowers/research/2026-07-16-modify-builder-attribution.md（含判定準則與政策選項）。
本腳本只產生數據，不做政策決定。

用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\
      [SPARK_NETWORK=testnet] uv run python -m scripts.testnet_modify_probe

⚠️ 僅限 testnet：SPARK_NETWORK=mainnet 會被拒絕執行（本腳本會下真實測試單，不可對主網跑）。
"""
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from spark.config import Settings
from spark.exchange.base import BuilderCode, Order
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.keystore.keychain import MacKeychainBackend
from spark.onboarding import onboard
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import AccrualTimeout, wait_for_accrual

USAGE = (
    "用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. "
    "[SPARK_NETWORK=testnet] uv run python -m scripts.testnet_modify_probe"
)
REQUIRED_ENV = ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")


def _check_env() -> tuple[str, str, str, str]:
    """驗證必要環境變數並拒絕主網。這必須是 main() 的第一步——缺變數或誤填 mainnet
    時必須零網路呼叫即退出（呼叫端不得在這步之前建構任何 Info/Exchange）。"""
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"缺少環境變數: {', '.join(missing)}")
        print(USAGE)
        raise SystemExit(2)
    network = os.environ.get("SPARK_NETWORK", "testnet")
    if network == "mainnet":
        print("拒絕執行：本探針會下真實測試單，SPARK_NETWORK=mainnet 一律拒跑（僅限 testnet）。")
        raise SystemExit(2)
    return (os.environ["SPARK_ACCOUNT_ID"], os.environ["SPARK_USER_ADDR"],
            os.environ["SPARK_BUILDER_ADDR"], network)


def _confirm_fills(adapter, user_addr: str, oid: int, coin: str,
                   attempts: int = 10, sleep_s: float = 2.0) -> list:
    """輪詢 get_user_fills 直到查到指定 oid 的成交紀錄，回傳**全部**符合 oid+coin 的
    UserFill 清單；逾時回傳空清單。

    modify_order() 只回傳 bool，需要另外確認「改單後是否真的成交」——不能只靠 builder
    累計費的增量反推：增量為 0 可能是「根本沒成交」，也可能是「成交但歸屬遺失」，兩者
    對應完全不同的政策意涵（前者是腳本/流動性問題，後者才是 T1.3 要驗證的風險），
    必須分開觀察，不能混為一談。

    必須蒐集全部而非第一筆：thin testnet 流動性下，同一 oid 的 IOC 單可能分批成交
    多筆（partial fills）。只取第一筆會低估 notional（ratio 分母），使 ratio 系統性
    偏高——可能把本應人工深查的部分歸屬異常誤判為假說 (a)。故首次查到成交後再多等
    一輪重抓，補齊稍晚入帳的分批成交，回傳完整清單由呼叫端加總 Σ(sz×px)。
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=10)
    for _ in range(attempts):
        fills = adapter.get_user_fills(user_addr, window_start, datetime.now(timezone.utc))
        matched = [f for f in fills if f.oid == oid and f.coin == coin]
        if matched:
            # 已有成交：多等一輪再重抓一次，蒐集可能稍晚入帳的分批成交。
            if sleep_s:
                time.sleep(sleep_s)
            fills = adapter.get_user_fills(user_addr, window_start,
                                           datetime.now(timezone.utc))
            return [f for f in fills if f.oid == oid and f.coin == coin]
        if sleep_s:
            time.sleep(sleep_s)
    return []


def _expected_fee(notional: Decimal, f: int) -> Decimal:
    """f 是「十分之一 bp」（見 spark.config.Settings.f 的欄位註解：20 = 0.02%）。
    fraction = f / 100000（f=20 → 0.0002 = 0.02%）。"""
    return notional * Decimal(f) / Decimal(100000)


def _print_group(label: str, size: Decimal, notional: Decimal, expected_fee: Decimal,
                 delta: Decimal) -> Decimal | None:
    ratio = (delta / expected_fee) if expected_fee > 0 else None
    print(f"[{label}] size={size} notional={notional} expected_fee={expected_fee} "
         f"actual_delta={delta} ratio={ratio if ratio is not None else 'n/a'}")
    return ratio


def main():
    account_id, user_addr, builder_addr, network = _check_env()

    settings = Settings(builder_address=builder_addr, account_id=account_id, network=network)
    ks = MacKeychainBackend()
    main_signer = ks.get_main_signer(account_id)
    info = Info(settings.api_url, skip_ws=True)

    main_adapter = HyperliquidAdapter(
        network, info=info, exchange=Exchange(main_signer, settings.api_url))
    try:
        local_agent_address = ks.get_agent_signer(account_id).address
    except KeyError:
        local_agent_address = None
    onboard(main_adapter, settings, main_signer=main_signer, user_address=user_addr,
            local_agent_address=local_agent_address,
            on_agent_key=lambda k: ks.import_key(account_id, "agent", k))
    print("onboarding OK（agent 授權狀態已對照鏈上 extraAgents 查詢驅動）")

    agent = ks.get_agent_signer(account_id)
    agent_adapter = HyperliquidAdapter(
        network, info=info,
        exchange=Exchange(agent, settings.api_url, account_address=user_addr))
    builder = BuilderCode(b=settings.builder_address, f=settings.f)

    # ---------- 對照組 A：place 路徑（builder 走 order action，已知歸屬）----------
    print("\n=== 對照組 A：place_marketable_order ===")
    mid = main_adapter.get_all_mids()[settings.coin]
    baseline_a = main_adapter.query_builder_accrued(settings.builder_address)
    res_a = place_marketable_order(agent_adapter, settings, agent_signer=agent,
                                   is_buy=True, best_opposite_px=mid)
    if not res_a.ok:
        raise SystemExit(
            f"[對照組 A] 下單未成交，已完成步驟：onboarding、baseline_a 查詢。raw={res_a.raw}")
    accrued_a = wait_for_accrual(main_adapter, settings.builder_address, baseline=baseline_a)
    delta_a = accrued_a - baseline_a
    # filled_size/avg_px 出自 HL 回應的 totalSz/avgPx（_parse_order_response），
    # 本身已是跨分批成交的聚合值——不需另行加總 fills（對照實驗組 B 的 Σ(sz×px)）。
    notional_a = res_a.filled_size * res_a.avg_px
    expected_a = _expected_fee(notional_a, settings.f)
    _print_group("A(place)", res_a.filled_size, notional_a, expected_a, delta_a)

    # ---------- 實驗組 B：遠端 GTC 掛單 → modify_order 改可成交 ----------
    print("\n=== 實驗組 B：place 遠端 GTC → modify_order（SDK modify 結構上無 builder 參數）===")
    baseline_b = main_adapter.query_builder_accrued(settings.builder_address)
    mid_b = main_adapter.get_all_mids()[settings.coin]
    resting_order = Order(coin=settings.coin, is_buy=True, size=settings.order_size,
                          limit_px=mid_b * Decimal("0.7"), tif="Gtc")
    res_resting = agent_adapter.place_order(agent, resting_order, builder)
    if not res_resting.ok:
        raise SystemExit(
            f"[實驗組 B] 遠端掛單失敗，已完成步驟：對照組 A 全部完成（Δ_place={delta_a}）。"
            f"raw={res_resting.raw}")
    try:
        status0 = res_resting.raw["response"]["data"]["statuses"][0]
    except (KeyError, IndexError) as e:
        raise SystemExit(
            f"[實驗組 B] 掛單成功但回應無 statuses[0]，已完成步驟：對照組 A 全部完成"
            f"（Δ_place={delta_a}）。raw={res_resting.raw}") from e
    if isinstance(status0, dict) and "filled" in status0:
        raise SystemExit(
            f"[實驗組 B] 遠端掛單意外立即成交（mid×0.7 竟成交，流動性異常），"
            f"實驗組作廢本輪。已完成步驟：對照組 A 全部完成（Δ_place={delta_a}）。"
            f"statuses[0]={status0}")
    try:
        oid = status0["resting"]["oid"]
    except (KeyError, TypeError) as e:
        raise SystemExit(
            f"[實驗組 B] 掛單成功但回應無 resting.oid，已完成步驟：對照組 A 全部完成"
            f"（Δ_place={delta_a}）。raw={res_resting.raw}") from e
    print(f"[實驗組 B] 遠端掛單成功 oid={oid} limit_px={resting_order.limit_px}")

    modify_target = Order(coin=settings.coin, is_buy=True, size=settings.order_size,
                          limit_px=mid_b * Decimal("1.05"), tif="Ioc", reduce_only=False)
    modify_ok = agent_adapter.modify_order(agent, oid, modify_target)
    if not modify_ok:
        raise SystemExit(
            f"[實驗組 B] modify_order 被拒絕（oid={oid}），已完成步驟：對照組 A 全部完成"
            f"（Δ_place={delta_a}）、遠端掛單成功。")

    fills = _confirm_fills(agent_adapter, user_addr, oid, settings.coin)
    if not fills:
        print(f"警告：[實驗組 B] modify 後於輪詢時間窗內查無 oid={oid} 的成交紀錄——"
             "下面的 Δ_modify 若為 0，無法單靠此腳本區分「根本沒成交」與「成交但歸屬遺失」，"
             "須人工核對 get_open_orders / 交易所前端後再解讀。")
        notional_b = settings.order_size * modify_target.limit_px  # 估計值：無實際成交價可用
    else:
        # thin testnet 流動性下同一 oid 可能分批成交：notional = Σ(sz×px)，
        # 各筆 fee 一併加總輸出作為佐證（fee 是 taker fee，非 builder fee，僅供對照）。
        notional_b = sum((f.sz * f.px for f in fills), Decimal("0"))
        total_sz = sum((f.sz for f in fills), Decimal("0"))
        total_fee = sum((f.fee for f in fills), Decimal("0"))
        print(f"[實驗組 B] 確認成交 fills={len(fills)} 筆 total_sz={total_sz} "
             f"notional={notional_b} total_fee={total_fee} oid={oid}")
        for f in fills:
            print(f"  - sz={f.sz} px={f.px} fee={f.fee} time={f.time.isoformat()}")

    try:
        accrued_b = wait_for_accrual(main_adapter, settings.builder_address, baseline=baseline_b)
        delta_b = accrued_b - baseline_b
    except AccrualTimeout as e:
        # 刻意只在實驗組（B）攔截：逾時本身就是「假說 b：歸屬遺失」的直接證據，不是腳本錯誤，
        # 攔截後仍把訊息印出來（不吞例外），delta 記為 0 讓下面照常產出結構化比較。
        # 對照組（A，已知歸屬正確的路徑）若逾時代表整體設置本身有問題，不攔截、讓它照常炸開。
        print(f"警告：[實驗組 B] {e}")
        delta_b = Decimal("0")

    expected_b = _expected_fee(notional_b, settings.f)
    ratio_b = _print_group("B(modify)", settings.order_size, notional_b, expected_b, delta_b)

    print("\n=== 判定（僅描述數據落點，政策決定見研究報告，本腳本不下結論）===")
    if ratio_b is None:
        print("無法判定：expected_fee=0（notional 為 0，可能完全沒有成交）。")
    elif ratio_b > Decimal("0.9"):
        print(f"ratio_b={ratio_b} > 0.9 → 支持假說 (a)：modify 後訂單仍歸屬 builder。")
    elif ratio_b < Decimal("0.1"):
        print(f"ratio_b={ratio_b} < 0.1 → 支持假說 (b)：modify 後 builder 歸屬遺失。")
    else:
        print(f"ratio_b={ratio_b} 落在 0.1~0.9 之間 → 異常區間，需人工深查（部分成交／時序競態等）。")


if __name__ == "__main__":
    main()
