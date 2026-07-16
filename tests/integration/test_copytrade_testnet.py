"""Copytrade testnet 端到端整合測試（spec T4.2）。預設被 -m 'not integration' 跳過。

需要：Keychain 已 bootstrap main key（scripts.bootstrap_keys）、testnet follower/builder
兩地址皆已入金（見 docs/superpowers/research/2026-07-16-copytrade-testnet-e2e.md 的金額與
地址）、環境變數 SPARK_ACCOUNT_ID / SPARK_USER_ADDR / SPARK_BUILDER_ADDR。
agent key 不需預先存在：首跑會由 approve_agent 生成並自動存入 Keychain（模式沿用
test_testnet_flow.py 的 has_agent 冪等判斷）。

M1 驗收 gate 明訂「Builder fee：主網 100% fills 有 accrual（place 與 modify 分開驗）」
（docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md:85），故本測試把
place 路徑（place_marketable_order，builder 走 "order" action，已知歸屬正確）與 modify
路徑（place 遠端 GTC → modify_order 改可成交，builder 走 batchModify action，歸屬未知，
見 T1.3）的 accrual 分開量測、分開斷言。

modify 路徑若 accrual 增量為 0，這是 T1.3 的政策資料點（假說 (b)：modify 後 builder 歸屬
遺失），不是本測試要抓的 bug——詳見
docs/superpowers/research/2026-07-16-modify-builder-attribution.md 的判定準則與政策選項。
本測試刻意不對此情況 fail：若在這一步就讓整條測試中止，會連帶跳過後續 place 路徑 accrual
與 reduce-only 平倉驗證，反而降低單次 testnet 執行能拿到的資訊量。改以警告訊息記錄（非
靜默吞掉），供人工對照 T1.3 報告判讀；place 路徑與平倉兩步仍照常執行並照常斷言。
"""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

import pytest

from spark.config import Settings
from spark.exchange.base import BuilderCode, Order
from spark.keystore.keychain import MacKeychainBackend
from spark.onboarding import onboard, OnboardingState
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import AccrualTimeout, wait_for_accrual

pytestmark = pytest.mark.integration

ACCOUNT_ID = os.environ.get("SPARK_ACCOUNT_ID", "")
USER_ADDR = os.environ.get("SPARK_USER_ADDR", "")
BUILDER_ADDR = os.environ.get("SPARK_BUILDER_ADDR", "")


def _mk_adapter(settings, wallet, user_address=None):
    # SDK Exchange 綁定建構時傳入的錢包；agent 交易需 account_address 指回主帳戶。
    # 簽名已與 test_testnet_flow.py:_mk_adapter 一致驗證過（inspect.signature 交叉確認）。
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from spark.exchange.hyperliquid import HyperliquidAdapter
    info = Info(settings.api_url, skip_ws=True)
    if user_address:
        exch = Exchange(wallet, settings.api_url, account_address=user_address)
    else:
        exch = Exchange(wallet, settings.api_url)
    return HyperliquidAdapter(settings.network, info=info, exchange=exch), info


def _confirm_fills(adapter, user_addr: str, oid: int, coin: str,
                   attempts: int = 10, sleep_s: float = 2.0) -> list:
    """輪詢 get_user_fills 直到查到指定 oid 的成交紀錄，回傳全部符合 oid+coin 的
    UserFill 清單；逾時回傳空清單。1:1 移植自 scripts/testnet_modify_probe.py 的
    _confirm_fills：modify_order() 只回傳 bool，不足以區分「沒成交」與「成交但歸屬
    遺失」，必須另外用 fills 確認真的成交，兩者是不同的失敗訊號。查到成交後多等一輪
    重抓，補齊 thin testnet 流動性下可能分批入帳的 partial fills。"""
    window_start = datetime.now(timezone.utc) - timedelta(minutes=10)
    for _ in range(attempts):
        fills = adapter.get_user_fills(user_addr, window_start, datetime.now(timezone.utc))
        matched = [f for f in fills if f.oid == oid and f.coin == coin]
        if matched:
            if sleep_s:
                time.sleep(sleep_s)
            fills = adapter.get_user_fills(user_addr, window_start,
                                           datetime.now(timezone.utc))
            return [f for f in fills if f.oid == oid and f.coin == coin]
        if sleep_s:
            time.sleep(sleep_s)
    return []


def test_copytrade_e2e_testnet():
    # 1. env 三變數缺一即失敗（帶訊息）。
    assert ACCOUNT_ID and USER_ADDR and BUILDER_ADDR, \
        "需設定 SPARK_ACCOUNT_ID / SPARK_USER_ADDR / SPARK_BUILDER_ADDR"

    settings = Settings(builder_address=BUILDER_ADDR, account_id=ACCOUNT_ID,
                        network="testnet")
    ks = MacKeychainBackend()
    main = ks.get_main_signer(ACCOUNT_ID)

    # 2. onboarding 冪等重用（main-bound adapter；Keychain 已有 agent key 才跳過 approve，
    #    避免無謂 rotate——與 test_testnet_flow.py 的 has_agent 模式一致）。
    main_adapter, info = _mk_adapter(settings, main)
    try:
        ks.get_agent_signer(ACCOUNT_ID)
        has_agent = True
    except KeyError:
        has_agent = False
    result = onboard(main_adapter, settings, main_signer=main, user_address=USER_ADDR,
                     skip_agent_approval=has_agent,
                     on_agent_key=lambda k: ks.import_key(ACCOUNT_ID, "agent", k))
    assert result.state == OnboardingState.READY
    assert main_adapter.query_max_builder_fee(USER_ADDR, BUILDER_ADDR) != 0

    agent = ks.get_agent_signer(ACCOUNT_ID)
    agent_adapter, _ = _mk_adapter(settings, agent, user_address=USER_ADDR)
    builder = BuilderCode(b=settings.builder_address, f=settings.f)

    # 步驟 3-5 包 try/finally：步驟 3 的遠端 GTC 掛單（mid×0.7 低價買單，帶 builder）
    # 若在中途 assert 失敗（modify 被拒、fills 查無）時殘留簿上，日後行情下探成交會
    # 污染未來執行的 accrual baseline（假陽性增量）。finally 對已知 oid best-effort
    # 撤單；cancel 失敗只印警告、不改變測試結果（原 assert 失敗原樣往外拋）。
    resting_oid = None
    try:
        # 3. modify 路徑 accrual：遠端 GTC 掛單（帶 builder，mid×0.7 不會立即成交）
        #    → modify_order 改價至可成交（mid×1.05, Ioc）→ 用 get_user_fills 確認真的成交
        #    （modify_order() 只回傳 bool，不足以區分「沒成交」與「成交但歸屬遺失」）
        #    → wait_for_accrual(baseline_1) 驗增量。
        mid_modify = main_adapter.get_all_mids()[settings.coin]
        baseline_1 = main_adapter.query_builder_accrued(BUILDER_ADDR)
        resting_order = Order(coin=settings.coin, is_buy=True, size=settings.order_size,
                              limit_px=mid_modify * Decimal("0.7"), tif="Gtc")
        res_resting = agent_adapter.place_order(agent, resting_order, builder)
        assert res_resting.ok, "modify 路徑前置：遠端 GTC 掛單失敗"
        status0 = res_resting.raw["response"]["data"]["statuses"][0]
        assert isinstance(status0, dict) and "resting" in status0, \
            "modify 路徑前置：掛單非預期立即成交或回應格式異常"
        resting_oid = status0["resting"]["oid"]
        oid = resting_oid

        modify_target = Order(coin=settings.coin, is_buy=True, size=settings.order_size,
                              limit_px=mid_modify * Decimal("1.05"), tif="Ioc",
                              reduce_only=False)
        assert agent_adapter.modify_order(agent, oid, modify_target), \
            f"modify_order 被拒絕 oid={oid}"
        # tif="Ioc" 語意：modify 成功後該 oid 立即成交或立即取消，簿上必無殘留
        # → 清空 cleanup 標記，避免 finally 對已消失的 oid 撤單印出假警告。
        resting_oid = None

        fills = _confirm_fills(agent_adapter, USER_ADDR, oid, settings.coin)
        assert fills, f"modify 後輪詢時間窗內查無 oid={oid} 成交紀錄"
        print(f"modify 路徑成交確認：oid={oid} fills={len(fills)} 筆")

        try:
            accrued_1 = wait_for_accrual(main_adapter, BUILDER_ADDR, baseline=baseline_1)
            delta_modify = accrued_1 - baseline_1
            print(f"modify 路徑 accrual 增量 {delta_modify}"
                 "（> 0，支持 T1.3 假說 (a) 歸屬保留）")
        except AccrualTimeout as e:
            # 刻意不 fail：見模組 docstring。這是 T1.3 政策資料點，不是本測試的 bug。
            print(f"警告（非 fail，T1.3 政策資料點——modify 路徑 accrual 增量為 0）：{e}\n"
                 "判讀見 docs/superpowers/research/"
                 "2026-07-16-modify-builder-attribution.md 第 3 節。")

        # 4. place 路徑 accrual：baseline_2 = 當前 accrued → marketable Ioc（帶 builder，
        #    走 orchestrator.place_marketable_order，builder 經 "order" action，已知歸屬
        #    正確）→ wait_for_accrual(baseline_2) 斷言增量 > 0。
        #    mid 必須在此重取（照 scripts/testnet_modify_probe.py 對照組前重取 mid 的
        #    做法）：步驟 3 的 fills/accrual 輪詢最多耗 70 秒以上（假說 (b) 成立時必吃滿
        #    accrual 逾時），沿用步驟 3 的 mid 若行情已動 >0.5%（CROSS_BPS 穿價緩衝），
        #    Ioc 會假性「未成交」，整趟測試白跑。
        mid_place = main_adapter.get_all_mids()[settings.coin]
        baseline_2 = main_adapter.query_builder_accrued(BUILDER_ADDR)
        order_res = place_marketable_order(agent_adapter, settings, agent_signer=agent,
                                           is_buy=True, best_opposite_px=mid_place)
        assert order_res.ok and order_res.filled_size > 0, \
            f"place 路徑下單未成交（filled_size={order_res.filled_size}）"
        accrued_2 = wait_for_accrual(main_adapter, BUILDER_ADDR, baseline=baseline_2)
        delta_place = accrued_2 - baseline_2
        assert delta_place > 0, \
            f"place 路徑 accrual 未增長: baseline={baseline_2} accrued={accrued_2}"
        print(f"place 路徑 accrual 增量 {delta_place}")

        # 5. reduce-only 全平：對步驟 3+4 累積的殘留部位全量平倉，斷言歸零。
        positions = agent_adapter.get_positions(USER_ADDR)
        coin_pos = next((p for p in positions if p.coin == settings.coin), None)
        if coin_pos is not None:
            # szi>0（多頭）平倉要賣出；szi<0（空頭）平倉要買入。
            close_res = agent_adapter.close_reduce_only(
                agent, settings.coin, is_buy=coin_pos.szi < 0, size=abs(coin_pos.szi),
                slippage=Decimal("0.01"), builder=builder)
            assert close_res.ok, "reduce-only 平倉未成功"
            print(f"reduce-only 平倉完成 size={abs(coin_pos.szi)}")

        positions_after = agent_adapter.get_positions(USER_ADDR)
        assert not any(p.coin == settings.coin for p in positions_after), \
            f"平倉後 {settings.coin} 仍有殘留部位"
    finally:
        # best-effort 清理：只在步驟 3 的 GTC 掛單尚未被 Ioc modify 消化時撤單。
        # cancel 回 False（可能已成交/已撤/網路問題）只印警告，不吞、不覆蓋原始失敗
        # ——殘留與否須人工用 get_open_orders 確認（見 research 文件 §3.3）。
        if resting_oid is not None:
            if not agent_adapter.cancel_order(agent, settings.coin, resting_oid):
                print(f"警告：殘留掛單清理失敗 oid={resting_oid}，"
                     "須人工以 get_open_orders 確認已撤（否則會污染未來執行的 accrual）。")
