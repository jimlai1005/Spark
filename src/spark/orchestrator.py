"""Phase 1 orchestrator：用 agent_signer 下可成交（Ioc）限價單，夾帶 builder code。
唯一觸碰 agent_signer 的業務碼；不碰 main key，不含 cancel/rebalance。"""
from decimal import Decimal
from spark.config import Settings
from spark.exchange.base import ExchangeAdapter, Order, BuilderCode, OrderResult, Signer

CROSS_BPS = Decimal("0.001")  # 穿價 buffer：0.1% 確保吃到對手盤


def place_marketable_order(adapter: ExchangeAdapter, settings: Settings, agent_signer: Signer,
                           is_buy: bool, best_opposite_px: Decimal) -> OrderResult:
    if is_buy:
        limit_px = best_opposite_px * (Decimal("1") + CROSS_BPS)
    else:
        limit_px = best_opposite_px * (Decimal("1") - CROSS_BPS)
    # 注意：穿價計算會產生多位小數的意圖價；HL 的價格格式化（5 位有效數字四捨五入）
    # 由 HyperliquidAdapter._round_px 在送單邊界處理，orchestrator 保持與交易所無關。
    order = Order(coin=settings.coin, is_buy=is_buy, size=settings.order_size,
                  limit_px=limit_px, tif="Ioc")
    builder = BuilderCode(b=settings.builder_address, f=settings.f)
    return adapter.place_order(agent_signer, order, builder)
