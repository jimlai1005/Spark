"""標的/下單相關的無狀態工具。1:1 port 自 hl-copytrader src/instrument.py:13-66，
Decimal 化、拿掉 xyz 專屬分支，坐標型別改用 spark 的 OrderSpec。

刻意未移植清單（hl-copytrader src/instrument.py 原始碼行號對應）：
  - `_route_order_error`（hl:69-80）——把下單錯誤路由到 Telegram 告警，屬 notifier
    整合範疇，非無狀態純函式核心，留給後續整合任務。
  - `_meta_field`/`get_sz_decimals`/`get_max_leverage`/`get_only_isolated`（hl:83-105）
    ——直接查 SDK meta universe，屬 exchange adapter 讀側範疇；spark 已由
    `ExchangeAdapter.get_size_decimals` 等方法承接，此處不重複。

結構差異：
  - spark 的 `OrderSpec`（copytrade/orders.py）不攜帶 hl 原本的 `order_type_name` 欄位
    （人類可讀字串，只在解析階段用來衍生 tpsl/is_market，衍生結果已是獨立欄位）。
    `tif` 則完整攜帶並由 `_order_type_and_px` 使用（1:1 對照 hl instrument.py:46）。
"""
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.copytrade.orders import OrderSpec


def _is_spot_coin(coin: str) -> bool:
    """判斷是否為現貨標的（不支援跟單）。1:1 port 自 hl instrument.py:13-20。

    Hyperliquid 現貨有兩種表示法：交易對名稱含 '/'（如 PURR/USDC），或索引代號以 '@'
    開頭（如 @85）。現貨不能設槓桿、size 規則不同，故一律跳過。
    """
    return coin.startswith("@") or "/" in coin


def _round_size(size: Decimal, sz_decimals: int) -> Decimal:
    """依標的的 size 精度捨入。port 自 hl instrument.py:23-25。

    hl 用 `math.floor(size * factor) / factor`——對非負 size（本引擎的 size 恆為非負，
    負向只用獨立的 is_buy 旗標表示）等同「無條件捨去」（truncate toward zero）。
    這裡用 Decimal `ROUND_DOWN` 達成同語意：ROUND_DOWN 對非負值即 floor。
    """
    quantum = Decimal(1).scaleb(-sz_decimals)
    return size.quantize(quantum, rounding=ROUND_DOWN)


def _coin_dex(coin: str) -> str:
    """標的所屬 DEX：'xyz:NVDA' → 'xyz'；'BTC' → ''（預設 DEX）。1:1 port 自 hl:28-30。"""
    return coin.split(":")[0] if ":" in coin else ""


def _order_type_and_px(spec: "OrderSpec") -> tuple[Decimal, dict]:
    """由 spec 算出下單價與 SDK order_type dict。1:1 port 自 hl instrument.py:33-47。

    回傳 (px, order_type)。限價單 tif 取自 spec.tif（hl instrument.py:46）。
    """
    if spec.is_trigger:
        px = spec.limit_px or spec.trigger_px or Decimal("0")
        order_type = {
            "trigger": {
                "triggerPx": spec.trigger_px,
                "isMarket": spec.is_market,
                "tpsl": spec.tpsl or "sl",
            }
        }
    else:
        px = spec.limit_px
        order_type = {"limit": {"tif": spec.tif}}
    return px, order_type


def _extract_order_error(result) -> str:
    """從下單/改單回應取出第一個錯誤訊息；無錯誤回空字串。1:1 port 自 hl instrument.py:50-66。

    Hyperliquid 兩種錯誤形態：
      - 頂層錯誤（限流、簽名等）：{'status':'err','response':'<錯誤字串>'} ← response 是 str
      - 單筆委託錯誤：{'status':'ok','response':{'data':{'statuses':[{'error':...}]}}}
    """
    if not isinstance(result, dict):
        return ""
    resp = result.get("response")
    if isinstance(resp, str):  # 頂層錯誤，response 直接是字串（勿對它 .get()）
        return resp
    if not isinstance(resp, dict):
        return ""
    for st in resp.get("data", {}).get("statuses", []):
        if isinstance(st, dict) and st.get("error"):
            return st["error"]
    return ""
