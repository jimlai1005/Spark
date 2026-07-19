"""src/spark/filet/leader_perf.py
Leader 績效指標（**perp 基準**）——純函式，零網路。

輸入是 HL `portfolio()` 的原始回應結構
（`[[period, {accountValueHistory:[[ts_ms, str], ...], pnlHistory:[[...]], vlm}], ...]`），
輸出是計算結果 dict。規格來源：
`docs/superpowers/research/2026-07-19-leader-performance-metrics.md`。

⭐ 三條「錯誤要寫不出來」的結構性閘門（每一條都對應一個會誤導客戶投真錢的數字）
--------------------------------------------------------------------------------

1. **只吃 perp 窗**（`PERP_PERIODS`）。`portfolio()` 的預設窗 `day/week/month/allTime`
   是 **spot + perp 總和，且官方明載含 vault 餘額**；copytrade 只鏡像 perp，用預設
   窗算出來的報酬**有一部分是客戶根本複製不到的**（HLP vault 被動收益、升值的現貨
   HYPE 都會顯示成「交易績效」）。本模組的 `extract_window` 直接拒絕非 perp 期別，
   呼叫端無法「不小心」傳進預設窗。

2. **MDD 只算在權益指數 `I_t` 上，永不算在 `accountValue` 上**。leader 提領 50%
   會讓 AV 腰斬 → 用 AV 算 MDD 直接產生幻影回撤（本專案事故 #1 同型）；反向也成立：
   leader 一路入金會讓 AV 單調上升，把真實的虧損完全遮住 → AV 基準的 MDD = 0。
   `I_t` 已把出入金中性化，是唯一正確的基準。

3. **不足 90 天，結構上回不出年化欄位**。不是「呼叫端記得不要顯示」——
   `annualized_return` 這個**鍵根本不存在**於回傳的 dict 裡。「7 天賺 3%」被顯示成
   「年化 365%」是本專案最容易犯的誤導，而它只要有人忘記檢查一次就會發生。
   同理，`covered_days < 30` 時連 `twr`／`max_drawdown` 的鍵都不存在（研究文件 2d
   的分級揭露表：< 30 天禁止任何 %報酬率）。

出入金為什麼不必另外查 ledger（工程原則 1：同源、同基準、同處計算）
------------------------------------------------------------------
官方定義 `pnlHistory` 就是「已扣除出入金」的累積 PnL：`P(t) = AV(t) − F(t)`。
`accountValueHistory` 與 `pnlHistory` **出自同一次回應、同一組時間戳**，所以

    ΔP_t = P(t) − P(t−1)        # 純交易損益（已含 funding、手續費、builder fee）
    ΔF_t = ΔAV_t − ΔP_t         # 該區間淨外部現金流（不必查第二個端點）
    r_t  = ΔP_t / AV(t−1)       # 分段報酬
    TWR  = Π(1 + r_t) − 1
    I_t  = Π(1 + r_i)           # 權益指數（出入金已中性化）

拉 `userNonFundingLedgerUpdates` 來湊現金流會把比較的兩側拆成兩個端點（時間戳對齊
誤差、延遲不一致）——那正是工程原則 1 禁止的事。該端點留給**定期對帳**，不進主路徑。

已知極限（下游必須原樣傳達給使用者，不得包裝成精確值）
--------------------------------------------------
- **MDD 是下界不是真值**：官方取樣為 15 分鐘一點（＋出入金當下額外取樣），日內
  來回（爆倉邊緣走一遭又回來）完全隱形 → MDD **系統性低估**。見 `MDD_SAMPLING_NOTE`。
- **leader 報酬是跟單者報酬的上界，不是期望值**：滑價、延遲、資金規模、槓桿上限
  都不同。見 `UPPER_BOUND_NOTE`。這是任何 API 都解決不了的，只能誠實揭露。
- `allTime` 窗是降採樣的（第三方來源指約 93 點，未經本專案實測）→ 對長帳戶可能是
  雙週間隔，`sample_count` 與 `covered_days` 一律回傳，讓下游自己判斷解析度夠不夠。
"""
import logging
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

# ⭐ 唯一合法的期別集合（見檔頭閘門 1）。預設窗 day/week/month/allTime 刻意不在此列。
PERP_PERIODS = ("perpDay", "perpWeek", "perpMonth", "perpAllTime")

_MS_PER_DAY = Decimal("86400000")
DAYS_PER_YEAR = Decimal("365")

# 分母地板（USDC）。與 HL 官方 ROI 定義的 `max(100, …)` 同精神：leader 提領到近乎 0
# 再入金，會讓 r_t = ΔP/AV(t−1) 產生爆炸性的假報酬（分母趨近 0）。低於地板的區間
# 一律不計入複利，並在 `skipped_intervals` 誠實回報跳過了幾段。
DENOMINATOR_FLOOR = Decimal("100")

# 分級揭露門檻（研究文件 2d）。門檻寫成常數而不是散在判斷式裡：
# 「多少天可以顯示什麼」是會被討論、會被外部審查的判準，必須有單一可引用的位置。
MIN_DAYS_FOR_RETURN = Decimal("30")        # < 30 天：只給 $ 金額，禁止任何 %
MIN_DAYS_FOR_ANNUALIZATION = Decimal("90")  # < 90 天：禁止年化（硬規則）

MDD_SAMPLING_NOTE = (
    "MDD 由 15 分鐘取樣的權益指數推得，取樣間隔內的來回不可見 → "
    "**系統性低估**，應讀作回撤的下界而非精確值。")
UPPER_BOUND_NOTE = (
    "leader 的報酬是跟單者報酬的**上界**，不是期望值：滑價、延遲、資金規模與"
    "槓桿上限都不同，leader 在流動性差的幣種大額進出時跟單者拿不到同樣價格。")
BASIS_NOTE = (
    "基準為 **perp only**（perpDay/perpWeek/perpMonth/perpAllTime 窗），"
    "與 copytrade 實際鏡像的範圍一致；不含 spot 與 vault 餘額。")

# 揭露層級（由 covered_days 決定，見檔頭閘門 3）。層級決定「哪些鍵存在」，
# 不是「哪些鍵是 null」——null 會被下游 `?? 0` 掉，缺鍵不會。
TIER_INSUFFICIENT = "insufficient"    # 連 $ 金額都不能給
TIER_PNL_ONLY = "pnl_only"            # 只有累積 PnL（$）
TIER_WINDOW_RETURN = "window_return"  # ＋ 窗口 TWR、MDD、權益指數
TIER_ANNUALIZABLE = "annualizable"    # ＋ 年化

# status/reason 的機器可讀碼（給下游分辨「為什麼沒有數字」；文案由 UI 決定）
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient"


def _insufficient(period: str, reason: str, sample_count: int = 0) -> dict[str, Any]:
    """資料不足的回傳。⭐ **不含任何數值結果鍵**（連 `cum_pnl` 都沒有）。

    為什麼不回 0／None：1 個資料點算出來的 `cum_pnl` 恆為 0，而「0」在畫面上是
    一個有意義且**錯誤**的訊息（「這個 leader 這段時間沒賺沒賠」）。NaN 同理，
    只是換一種形式的髒資料。缺鍵才能逼下游顯式處理「沒有資料」這個狀態。
    """
    return {
        "period": period,
        "basis": "perp",
        "status": STATUS_INSUFFICIENT,
        "reason": reason,
        "disclosure_tier": TIER_INSUFFICIENT,
        "sample_count": sample_count,
        "covered_days": None,
        "first_ts_ms": None,
        "last_ts_ms": None,
        "skipped_intervals": 0,
        "mdd_note": MDD_SAMPLING_NOTE,
        "upper_bound_note": UPPER_BOUND_NOTE,
        "basis_note": BASIS_NOTE,
    }


def _parse_series(raw: Any) -> list[tuple[int, Decimal]] | None:
    """`[[ts_ms, "val"], ...]` → `[(int, Decimal), ...]`；任一筆不合形狀 → None。

    容錯到「回 None」為止，不 raise：本模組的呼叫端是 cron 與唯讀 API，上游 schema
    漂移不該讓整批快照或整個目錄頁炸掉——但也絕不猜測缺漏值（缺一筆就整段作廢，
    由 `_insufficient` 大聲說「資料不足」）。
    """
    if not isinstance(raw, list):
        return None
    out: list[tuple[int, Decimal]] = []
    for item in raw:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            return None
        ts, val = item
        if isinstance(ts, bool) or not isinstance(ts, (int, float, str)):
            return None
        try:
            ts_i = int(ts)
            v = Decimal(str(val))
        except (ValueError, TypeError, InvalidOperation):
            return None
        if not v.is_finite():
            return None
        out.append((ts_i, v))
    return out


def extract_window(portfolio_rows: Any, period: str
                   ) -> tuple[list[tuple[int, Decimal]], list[tuple[int, Decimal]]] | None:
    """從 `portfolio()` 回應取出某個 **perp** 窗的 (accountValueHistory, pnlHistory)。

    ⭐ `period` 不在 `PERP_PERIODS` 內 → `ValueError`（檔頭閘門 1）。這是刻意的
    程式錯誤而非資料錯誤：傳進 `"month"` 代表呼叫端要算的是 spot+perp+vault 的
    總值績效，那是一個**客戶複製不到**的數字，不該有一條靜默的路徑通往它。

    查無該期別／欄位缺／形狀不符 → None（資料錯誤，由呼叫端轉成「資料不足」）。
    """
    if period not in PERP_PERIODS:
        raise ValueError(
            f"只接受 perp 窗 {PERP_PERIODS}（預設窗含 spot 與 vault，"
            f"不是 copytrade 鏡像得到的範圍）: {period!r}")
    if not isinstance(portfolio_rows, list):
        return None
    for row in portfolio_rows:
        if not (isinstance(row, (list, tuple)) and len(row) == 2 and row[0] == period):
            continue
        payload = row[1]
        if not isinstance(payload, dict):
            return None
        av = _parse_series(payload.get("accountValueHistory"))
        pnl = _parse_series(payload.get("pnlHistory"))
        if av is None or pnl is None:
            return None
        return av, pnl
    return None


def compute_window_performance(portfolio_rows: Any, period: str) -> dict[str, Any]:
    """單一 perp 窗的績效計算。**純函式、不觸網**。

    回傳 dict 的鍵集合**隨資料充足度變動**（見檔頭閘門 3 與 `_insufficient`）：
    - `status == "insufficient"`：無任何數值結果鍵。
    - `disclosure_tier == "pnl_only"`：＋ `cum_pnl`。
    - `disclosure_tier == "window_return"`：＋ `twr`、`max_drawdown`、`equity_index`。
    - `disclosure_tier == "annualizable"`：＋ `annualized_return`。

    ⭐ 「鍵不存在」而不是「鍵為 None」是刻意的：下游用 `d.get("annualized_return")`
    拿到 None 還是可能被 `or 0` 吃掉，而 `"annualized_return" in d` 是二元的、
    測試釘得住的。
    """
    window = extract_window(portfolio_rows, period)
    if window is None:
        return _insufficient(period, "window_missing")
    av, pnl = window

    # ⭐ 同源不變量的實際檢查（工程原則 1）：兩序列必須是同一組時間戳。官方文件保證
    # 它們同一次回應同一組取樣，但「保證」不是「驗證」——若哪天 schema 變了而我們
    # 靜默地把 AV[i] 配 PnL[j]，算出來的 ΔF 全是噪音，而每個數字看起來都很正常。
    if len(av) != len(pnl) or any(a[0] != p[0] for a, p in zip(av, pnl)):
        logger.error(
            "portfolio %s 窗的 accountValueHistory 與 pnlHistory 時間戳不同步"
            "（len=%d/%d）——兩序列非同源，拒絕計算", period, len(av), len(pnl))
        return _insufficient(period, "series_misaligned", sample_count=len(pnl))

    if len(pnl) < 2:
        # 1 點算不出任何區間報酬；cum_pnl 恆為 0 更是誤導（見 _insufficient）。
        return _insufficient(period, "need_at_least_two_samples", sample_count=len(pnl))

    first_ts, last_ts = pnl[0][0], pnl[-1][0]
    if last_ts <= first_ts:
        logger.error("portfolio %s 窗時間戳非遞增（first=%s last=%s）",
                     period, first_ts, last_ts)
        return _insufficient(period, "non_monotonic_timestamps", sample_count=len(pnl))
    covered_days = (Decimal(last_ts - first_ts) / _MS_PER_DAY).quantize(Decimal("0.0001"))

    # --- 分段報酬與權益指數（出入金中性化的唯一正確基準） ---
    equity_index: list[Decimal] = [Decimal("1")]
    skipped = 0
    net_flow = Decimal("0")
    for i in range(1, len(pnl)):
        d_pnl = pnl[i][1] - pnl[i - 1][1]
        d_av = av[i][1] - av[i - 1][1]
        net_flow += d_av - d_pnl          # ΔF_t：淨外部現金流，同一回應內解出
        prev_av = av[i - 1][1]
        if prev_av < DENOMINATOR_FLOOR:
            # 分母地板：不計入複利（r_t 視為 0），但**記數**——被跳過的區間數是
            # 「這個數字有多少沒算進去」的誠實揭露，不可靜默吞掉（工程原則 3）。
            skipped += 1
            equity_index.append(equity_index[-1])
            continue
        try:
            r = d_pnl / prev_av
        except (DivisionByZero, InvalidOperation):  # 地板已擋住，這裡是縱深防禦
            skipped += 1
            equity_index.append(equity_index[-1])
            continue
        equity_index.append(equity_index[-1] * (Decimal("1") + r))

    cum_pnl = pnl[-1][1] - pnl[0][1]

    out: dict[str, Any] = {
        "period": period,
        "basis": "perp",
        "status": STATUS_OK,
        "reason": None,
        "sample_count": len(pnl),
        "covered_days": covered_days,
        "first_ts_ms": first_ts,
        "last_ts_ms": last_ts,
        "skipped_intervals": skipped,
        "net_external_flow": net_flow,
        "mdd_note": MDD_SAMPLING_NOTE,
        "upper_bound_note": UPPER_BOUND_NOTE,
        "basis_note": BASIS_NOTE,
    }

    # --- 分級揭露：層級決定哪些鍵存在 ---
    if covered_days < MIN_DAYS_FOR_RETURN:
        # < 30 天：只給 $ 金額。%報酬率在這個樣本長度下噪音遠大於訊號，
        # 而畫面上的「+38%」不會附帶「這是 6 天的數字」這個脈絡。
        out["disclosure_tier"] = TIER_PNL_ONLY
        out["cum_pnl"] = cum_pnl
        return out

    out["cum_pnl"] = cum_pnl
    out["twr"] = equity_index[-1] - Decimal("1")
    out["equity_index"] = tuple(equity_index)
    out["max_drawdown"] = _max_drawdown(equity_index)

    if covered_days < MIN_DAYS_FOR_ANNUALIZATION:
        out["disclosure_tier"] = TIER_WINDOW_RETURN
        return out                       # ⭐ 年化的鍵在此路徑上結構性不存在

    annualized = _annualize(equity_index[-1], covered_days)
    if annualized is None:
        # 帳戶被歸零（1+TWR <= 0）→ 年化在數學上沒有定義。降級成 window_return，
        # 而不是回一個 -100% 之類看起來像結論的數字。
        out["disclosure_tier"] = TIER_WINDOW_RETURN
        return out
    out["disclosure_tier"] = TIER_ANNUALIZABLE
    out["annualized_return"] = annualized
    return out


def _max_drawdown(equity_index: list[Decimal]) -> Decimal:
    """⭐⭐ MDD **只吃權益指數 `I_t`**（函式簽名結構性保證：它拿不到 accountValue）。

    見檔頭閘門 2：AV 基準的 MDD 在提領時產生幻影回撤、在入金時把真實虧損遮成 0，
    兩個方向都會誤導。這個函式故意只接受一個序列，讓「傳錯基準」需要呼叫端主動
    去別的地方撈資料，而不是打錯一個變數名就發生。
    """
    peak = equity_index[0]
    mdd = Decimal("0")
    for v in equity_index:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def _annualize(growth: Decimal, covered_days: Decimal) -> Decimal | None:
    """`(1+TWR)^(365/天數) − 1`。呼叫點已保證 `covered_days >= 90`（硬規則）。

    `growth <= 0`（帳戶被歸零或更糟）→ None：非正數的非整數次方沒有實數解。
    """
    if growth <= 0 or covered_days <= 0:
        return None
    try:
        return growth ** (DAYS_PER_YEAR / covered_days) - Decimal("1")
    except (InvalidOperation, OverflowError, ValueError):
        logger.error("年化計算失敗 growth=%s covered_days=%s", growth, covered_days)
        return None


def compute_perp_performance(portfolio_rows: Any,
                             periods: tuple[str, ...] = PERP_PERIODS
                             ) -> dict[str, dict[str, Any]]:
    """一次 `portfolio()` 回應 → 各 perp 窗的績效 dict（`{period: result}`）。

    一次呼叫拿到全部 8 個窗是 HL 的既有行為，所以算多個窗**不會**增加任何請求。
    """
    return {p: compute_window_performance(portfolio_rows, p) for p in periods}


def jsonable_performance(perf: dict[str, Any], *, include_equity_index: bool = False
                         ) -> dict[str, Any]:
    """績效 dict → 可 JSON 序列化（Decimal → str，沿 leaderboard.py 的落地慣例）。

    ⭐ 缺鍵一律**保持缺鍵**（不補 None）：`annualized_return` 的結構性缺席是本模組
    最重要的保證，序列化時補成 `null` 等於把它降級成「呼叫端記得檢查」。

    `equity_index` 預設**不輸出**：它是每窗數十到數百點的序列，落進每日快照會讓
    檔案膨脹一個數量級，而快照的用途是純量指標。要畫圖的呼叫端顯式打開。
    """
    out: dict[str, Any] = {}
    for k, v in perf.items():
        if k == "equity_index":
            out["equity_index_len"] = len(v)
            if include_equity_index:
                out["equity_index"] = [str(x) for x in v]
            continue
        out[k] = str(v) if isinstance(v, Decimal) else v
    return out
