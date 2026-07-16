"""單一 IO resilience 邊界（範圍：交易執行 = HL SDK Exchange 寫入）。
1:1 語意移植自 hl-copytrader src/resilience.py（唯讀參考，未執行其程式或測試）。
分類 + 重試/驗證後重試邏輯只活在這一個檔案；讀取(Info)/CSV/HTTP 一律不走這裡。
設計依據：hl-copytrader docs/superpowers/specs/2026-06-21-io-resilience-boundary-design.md
（「偏向假設已送達」：verify 查不出來一律當已送達，寧可漏跟也不重複下單）。

相對 hl 原版的結構偏差（語意不變，逐項附理由）：
1. 時間注入：hl 原版直呼 time.sleep；本版把 sleep_fn 參數化（預設 time.sleep），
   run() 與 ResilientExchange 建構子皆收 sleep_fn，供測試斷言退避序列而不真睡。
2. ResilientExchange.__getattr__ 透傳：spark 的 HyperliquidAdapter 呼叫面比 hl trader
   廣（還會呼叫 approve_builder_fee/approve_agent 等 onboarding 方法），未顯式包裝
   的屬性一律委派給內層 exchange；hl 原版無此需求（trader 只呼叫五個寫入方法）。
3. market_open/order 的 _verify：本任務（Task 4）接線時 HyperliquidAdapter 一律不傳
   _verify（尚未實作以讀側重查持倉/掛單確認送達的 verify 閉包——那是後續任務範疇）。
   效果：這兩個方法在目前接線下等同「單次嘗試、transient 直接上拋、絕不盲目重送」，
   「寧漏跟不重複下單」的偏向由此自然成立（沒有 verify 就不會誤判成功並重送）。
"""
import logging
import time

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.6

# 驗證確認「其實已送達」時回傳的成功哨兵（原始 SDK 回應已隨斷線遺失）。
VERIFIED_OK = {"status": "ok", "_resilience": "verified"}

_TRANSIENT_MARKERS = (
    "connection reset", "connection aborted", "connection broken",
    "remote end closed", "timed out", "timeout", "max retries",
    "temporarily unavailable", "bad gateway", "service unavailable",
    "502", "503", "504",
)


def _is_transient_error(exc: Exception) -> bool:
    """是否為可重試的暫時性網路錯誤，而非語意錯誤（保證金不足/訂單被拒）。"""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True  # 內建 ConnectionResetError 屬 ConnectionError
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def run(fn, *, what, idempotent, verify=None, attempts=None,
        base_delay=RETRY_BASE_DELAY, sleep_fn=time.sleep):
    """透過 resilience 邊界執行外部寫入 fn。
    - idempotent=True：暫時性錯誤直接重試（reduce-only/冪等，重送安全）。
    - idempotent=False 且 verify 提供：驗證後重試 —— 暫時性錯誤時呼叫 verify()，
      確認『已送達』→ 回 VERIFIED_OK 不重送；確認『沒送達』→ 才重送。
      verify 偏向『假設已送達』：查不出來（True 或自己拋例外）一律當已送達，
      寧可漏跟也不重複下單。
    - idempotent=False 且 verify=None：只跑一次、暫時性錯誤直接拋出（=維持舊行為）。
    語意錯誤一律不重試、直接拋出（由呼叫端 except 告警）。
    sleep_fn：實際延遲呼叫，預設 time.sleep；測試可注入不真睡的替身並斷言收到的延遲序列。
    """
    can_retry = idempotent or (verify is not None)
    if attempts is None:
        attempts = RETRY_ATTEMPTS if can_retry else 1
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if not _is_transient_error(e) or not can_retry:
                raise
            if i == attempts:
                # 重試耗盡有專屬告警：監控端得以區分「語意錯誤直接上拋」與
                # 「暫時性錯誤重試打光」兩種失敗（bare raise 保留原例外鏈）。
                logger.warning("%s：重試 %d 次仍失敗，放棄並上拋", what, attempts)
                raise
            if not idempotent:  # 非冪等但有 verify → 驗證後決定是否重送
                try:
                    landed = verify()
                except Exception as verify_exc:
                    # 兩種信心層級分開記：這一句是「查不出來、保守假設已送達」，
                    # 不是「已真實驗證」——監控端不得把它當成已確認送達。
                    logger.warning(
                        "%s：連線中斷且 verify 查詢失敗（%s），"
                        "保守假設已送達（未真實驗證，不重送）", what, verify_exc)
                    return VERIFIED_OK
                if landed:
                    logger.warning("%s：連線中斷但已驗證送達，視為成功（不重送）", what)
                    return VERIFIED_OK
            delay = base_delay * (2 ** (i - 1))
            logger.warning(
                f"{what}：暫時性錯誤（第 {i}/{attempts} 次），{delay:.1f}s 後重試: {e}"
            )
            sleep_fn(delay)
    raise RuntimeError("resilience.run 未預期離開迴圈")  # pragma: no cover


class ResilientExchange:
    """包住 SDK Exchange，所有交易寫入經此分類。Adapter 只持有這個包裝物件。
    冪等 / reduce-only → 直接重試；market_open / 非 reduce-only 掛單 → 驗證後重試
    （由呼叫端以 _verify 提供驗證，目前無呼叫端提供 → 等同單次嘗試不重送）；
    modify_order → 不重試（已自帶 cancel→重掛 退回）。

    未顯式包裝的屬性（如 approve_builder_fee/approve_agent）一律經 __getattr__
    透傳給內層 exchange——這些是 onboarding 一次性動作，不在本任務的重試範圍內。
    """

    def __init__(self, exchange, sleep_fn=time.sleep):
        self._ex = exchange
        self._sleep_fn = sleep_fn

    def __getattr__(self, name):
        return getattr(self._ex, name)

    # 冪等 / reduce-only → 直接重試
    def market_close(self, *a, **k):
        return run(lambda: self._ex.market_close(*a, **k),
                   what="平倉", idempotent=True, sleep_fn=self._sleep_fn)

    def update_leverage(self, *a, **k):
        return run(lambda: self._ex.update_leverage(*a, **k),
                   what="設定槓桿", idempotent=True, sleep_fn=self._sleep_fn)

    def cancel(self, *a, **k):
        return run(lambda: self._ex.cancel(*a, **k),
                   what="取消掛單", idempotent=True, sleep_fn=self._sleep_fn)

    # 非冪等 → 驗證後重試（無 _verify 則只跑一次、不重試）
    def market_open(self, *a, _verify=None, **k):
        return run(lambda: self._ex.market_open(*a, **k),
                   what="開倉", idempotent=False, verify=_verify, sleep_fn=self._sleep_fn)

    def order(self, *a, reduce_only=False, _verify=None, **k):
        idem = bool(reduce_only)  # reduce-only 掛單冪等、可直接重試
        return run(lambda: self._ex.order(*a, reduce_only=reduce_only, **k),
                   what="掛單", idempotent=idem,
                   verify=(None if idem else _verify), sleep_fn=self._sleep_fn)

    # 已自帶 cancel→重掛 退回機制 → 不加重試
    def modify_order(self, *a, **k):
        return run(lambda: self._ex.modify_order(*a, **k),
                   what="改單", idempotent=False, sleep_fn=self._sleep_fn)
