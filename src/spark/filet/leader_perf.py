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

   ⭐ 2026-08-31 追加（issue log I-15 使用者裁決，**不取代**上一段，只加開一條窄門）：
   「只吃 perp 窗」的前提是「perp 是唯一 copytrade 能複製的範圍」——這對資金全倉在
   perp 的帳戶成立，但對資金停泊在 spot、經常 spot↔perp 內部轉帳進出的帳戶會反過來
   出錯：`perpAllTime`/`perpMonth` 把每一筆內部轉帳都算成損益，產生幻影回撤／幻影
   波動（實證：`0xfB9C…9760`，perp-only 讀出回撤 −19%／波動 134%／勝率 18%，與
   參考工具逐位吻合的 combined 序列 1002.24→1197.9 真值天差地遠——工程原則事故 #3
   同型：equity basis 是錢包形態專屬的，不能通用套用）。`/api/public/strategies*`／
   `/api/public/traders/{address}`／`/api/public/explore` 三個**展示**端點因此改吃
   `COMBINED_PERIODS`（`day/week/month/allTime`，HL `portfolio()` 的預設窗，
   spot+perp 合併＋vault），`extract_window` 的閘門相應加開這四個期別。**不影響**
   既有呼叫端：`leaderboard.py` 每日快照與 follower dashboard（`/api/me/dashboard`
   的 `perpMonth`、`/api/me/fees` 的 `perpAllTime`）仍只請求 `PERP_PERIODS`——這些
   帳戶全倉在 perp、且要與 copytrade 引擎本身同基準，perp-only 對它們仍是正確答案，
   這扇新窄門對它們不存在。合併窗算出的 `basis`／`basis_note` 欄位相應回
   `"combined"`／`COMBINED_BASIS_NOTE`（見 `_basis_for`），不得繼續標「perp」誤導
   下游（那組欄位目前未被 strategies/traders 端點外流，但本模組的輸出契約本身
   必須誠實）。

2. **MDD 只算在權益指數 `I_t` 上，永不算在 `accountValue` 上**。leader 提領 50%
   會讓 AV 腰斬 → 用 AV 算 MDD 直接產生幻影回撤（本專案事故 #1 同型）；反向也成立：
   leader 一路入金會讓 AV 單調上升，把真實的虧損完全遮住 → AV 基準的 MDD = 0。
   `I_t` 已把出入金中性化，是唯一正確的基準。

3. **資料不足的指標一律帶「指標層級」的不足標記**。⚠️ 2026-07-19 使用者裁決改版：
   此閘門原本的載體是「鍵不存在」（< 30 天無 `twr`／`max_drawdown`、< 90 天無
   `annualized_return`）。現行語意是 **「顯示，但註記」**——數字給，但每一個受影響
   的指標旁邊都帶著一個**自己的**布林標記，且年化額外帶「由幾天外推」的天數。
   標記做在**指標層級**而不是只在 `disclosure_tier` 說一次：前端可能只渲染其中一個
   指標（例如只顯示 MDD），一個全域旗標在那個畫面上會整個漏掉。
   見下方「揭露模型改版」段落，含對前端結構性防線的影響。

揭露模型改版（2026-07-19；⭐ 前端必須跟進）
------------------------------------------
**改動前**：分級揭露的載體是**鍵的存在與否**。`web/src/lib/redline.test.ts` 有一條
防線禁止前端對績效欄位使用 `??`／`||`，其前提正是「缺鍵＝這個數字不該被顯示，補上
預設值就是把結構性保證退化成前端的記性」。

**改動後**：`twr`／`max_drawdown`／`annualized_return` 在資料充足度不足時**照樣回傳**，
所以「鍵永遠存在」。那條防線的**意義因此改變**——它不再是「防止把不該顯示的東西顯示
出來」，而是「防止把**不足標記**吃掉」。前端真正該被禁止的是：拿到
`twr_insufficient_data`／`annualized_return_extrapolated_from_days` 卻用 `??`／`||`
把它們消音，或在標記為 True 時仍把數字渲染成與充足資料無異的樣子。
（本模組不碰 `web/`；防線調整由前端任務處理。）

**新的欄位契約**（`status == "ok"` 且 `sample_count >= 2` 時）：
- `twr`、`max_drawdown`、`equity_index`、`cum_pnl`：**恆存在**。
- `twr_insufficient_data`、`max_drawdown_insufficient_data`：bool，
  `covered_days < MIN_DAYS_FOR_RETURN`（30 天）時為 True。
- `annualized_return`：存在，**除非**年化在數學上無定義（`1+TWR <= 0`，帳戶被歸零）。
- `annualized_return_insufficient_data`：bool，`covered_days < MIN_DAYS_FOR_ANNUALIZATION`
  （90 天）時為 True。
- `annualized_return_extrapolated_from_days`：Decimal，**實際涵蓋天數**。年化本質上
  就是外推（除非窗剛好 365 天），這個欄位讓前端能寫出「由 N 天外推」而不是把一個
  複利放大後的數字當成事實。與 `annualized_return` 同生共死（一起在、一起不在），
  刻意不獨立存在——標記與它所描述的數字必須同源（工程原則 1）。
- `disclosure_tier`：**保留**（前端已在用），但語意從「給不給看」改為
  **「資料充足度分級」**。層級值不變（相容），純粹是 `covered_days` 的函式。

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

# 2026-08-31 追加（I-15，見檔頭閘門 1 附加段）：spot+perp 合併窗，僅供「展示」端點
# （strategies/traders/explore）在錢包資金停泊 spot、經常 spot↔perp 內部轉帳時當
# 正確 basis 使用。**不是**「perp 閘門放寬」——`PERP_PERIODS` 本身一個字元沒動，
# 這是另一組獨立列表，`extract_window` 對兩者的聯集開放。
COMBINED_PERIODS = ("day", "week", "month", "allTime")

_MS_PER_DAY = Decimal("86400000")
DAYS_PER_YEAR = Decimal("365")

# 分母地板（USDC）。與 HL 官方 ROI 定義的 `max(100, …)` 同精神：leader 提領到近乎 0
# 再入金，會讓 r_t = ΔP/AV(t−1) 產生爆炸性的假報酬（分母趨近 0）。低於地板的區間
# 一律不計入複利，並在 `skipped_intervals` 誠實回報跳過了幾段。
DENOMINATOR_FLOOR = Decimal("100")

# 資料充足度門檻（研究文件 2d；2026-07-19 起語意為「足不足」而非「給不給」）。
# 門檻寫成常數而不是散在判斷式裡：「多少天的資料算充足」是會被討論、會被外部審查的
# 判準，必須有單一可引用的位置。
MIN_DAYS_FOR_RETURN = Decimal("30")        # < 30 天：%報酬率噪音 >> 訊號 → 標記不足
MIN_DAYS_FOR_ANNUALIZATION = Decimal("90")  # < 90 天：年化是激進外推 → 標記不足

# 比率型指標（Sharpe/Sortino/年化波動）的資料充足度門檻。這三個指標比 TWR/MDD
# 對樣本數更敏感（標準差在薄樣本下噪音極大），門檻獨立於上面兩個、且值更嚴格。
# ⚠️ 2026-08-30 使用者裁決 D15：原 60 天降為 30 天（自營策略 59 天實盤，目的是讓它
# 能完整呈現指標並可跟單），與 strategies.py 的 CAGR_SAMPLE_THRESHOLD_DAYS 同步降為 30。
RATIO_MIN_DAYS = Decimal("30")             # < 30 天：比率指標噪音 >> 訊號 → 標記不足

DAYS_PER_YEAR_SQRT = DAYS_PER_YEAR.sqrt()   # √365，比率指標公式共用，避免重複開方

MDD_SAMPLING_NOTE = (
    "MDD 由 15 分鐘取樣的權益指數推得，取樣間隔內的來回不可見 → "
    "**系統性低估**，應讀作回撤的下界而非精確值。")
UPPER_BOUND_NOTE = (
    "leader 的報酬是跟單者報酬的**上界**，不是期望值：滑價、延遲、資金規模與"
    "槓桿上限都不同，leader 在流動性差的幣種大額進出時跟單者拿不到同樣價格。")
BASIS_NOTE = (
    "基準為 **perp only**（perpDay/perpWeek/perpMonth/perpAllTime 窗），"
    "與 copytrade 實際鏡像的範圍一致；不含 spot 與 vault 餘額。")
# 2026-08-31 追加（I-15）：COMBINED_PERIODS 窗（day/week/month/allTime）的對應文案。
COMBINED_BASIS_NOTE = (
    "基準為 **spot + perp 合併帳戶**（HL portfolio 預設窗 day/week/month/allTime，"
    "含 vault 餘額）；此帳戶資金停泊 spot 並經內部轉帳進出 perp，perp-only 序列會"
    "把轉帳誤算成損益，合併基準才是這顆錢包的真實績效。跟單者僅鏡像 perp 部位，"
    "不保證能複製 spot 部分的損益（見 `UPPER_BOUND_NOTE`）。")


def _basis_for(period: str) -> tuple[str, str]:
    """`period` → `(basis, basis_note)`。`period` 屬 `COMBINED_PERIODS` → 合併家族
    文案；否則（`PERP_PERIODS`）沿用既有 perp 文案。單一來源，`_insufficient()`
    與 `compute_window_performance()` 共用，避免兩處各自判斷而漂移。"""
    if period in COMBINED_PERIODS:
        return "combined", COMBINED_BASIS_NOTE
    return "perp", BASIS_NOTE

# 資料充足度分級（純粹是 covered_days 的函式，見檔頭「揭露模型改版」）。
# ⚠️ 層級**值**刻意不改名（前端已在用；改名是一次無謂的破壞性變更），但語意已從
# 「這一層給哪些鍵」變成「這一層的資料有多厚」。歷史名稱因此讀起來偏保守：
# `pnl_only` 現在**仍會**回 twr／MDD，只是它們全部帶 `*_insufficient_data = True`。
TIER_INSUFFICIENT = "insufficient"    # 算不出任何指標（<2 點／缺窗／序列不同步）
TIER_PNL_ONLY = "pnl_only"            # covered_days < 30：%報酬率可看但資料很薄
TIER_WINDOW_RETURN = "window_return"  # 30 ≤ covered_days < 90：窗口報酬足、年化仍是外推
TIER_ANNUALIZABLE = "annualizable"    # covered_days ≥ 90：年化的資料基礎足夠

# ⭐ 指標層級的不足標記欄位名（單一來源）。下游投影白名單（publicapi/app.py 的
# `_LEADER_PERF_FIELDS`）由此常數拼出來，不各自抄一份字串——標記與它所描述的數字
# 若在投影層走散，前端會拿到一個沒有任何警示的外推數字，那正是本次改版要避免的事。
INSUFFICIENCY_MARKERS = (
    "twr_insufficient_data",
    "max_drawdown_insufficient_data",
    "annualized_return_insufficient_data",
    "annualized_return_extrapolated_from_days",
)

# status/reason 的機器可讀碼（給下游分辨「為什麼沒有數字」；文案由 UI 決定）
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient"


def _insufficient(period: str, reason: str, sample_count: int = 0) -> dict[str, Any]:
    """資料不足的回傳。⭐ **不含任何數值結果鍵**（連 `cum_pnl` 都沒有）。

    為什麼不回 0／None：1 個資料點算出來的 `cum_pnl` 恆為 0，而「0」在畫面上是
    一個有意義且**錯誤**的訊息（「這個 leader 這段時間沒賺沒賠」）。NaN 同理，
    只是換一種形式的髒資料。缺鍵才能逼下游顯式處理「沒有資料」這個狀態。
    """
    basis, basis_note = _basis_for(period)
    return {
        "period": period,
        "basis": basis,
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
        "basis_note": basis_note,
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
    """從 `portfolio()` 回應取出某個窗的 (accountValueHistory, pnlHistory)。

    ⭐ `period` 不在 `PERP_PERIODS ∪ COMBINED_PERIODS` 內 → `ValueError`（檔頭
    閘門 1）。這是刻意的程式錯誤而非資料錯誤：任意字串都會靜默通過等於閘門形同
    虛設。`PERP_PERIODS`（copytrade 鏡像範圍）與 `COMBINED_PERIODS`（2026-08-31
    I-15 追加，僅供展示端點在錢包資金停泊 spot 時當正確 basis）是**兩個獨立**
    白名單的聯集，不是把 perp 閘門整個拿掉——呼叫端仍須明確選邊，沒有第三種
    「隨便一個字串」的靜默路徑。

    查無該期別／欄位缺／形狀不符 → None（資料錯誤，由呼叫端轉成「資料不足」）。
    """
    if period not in PERP_PERIODS and period not in COMBINED_PERIODS:
        raise ValueError(
            f"只接受 {PERP_PERIODS + COMBINED_PERIODS}"
            f"（perp 窗＝copytrade 鏡像範圍；day/week/month/allTime＝"
            f"2026-08-31 I-15 追加，僅供展示端點在合併基準正確的錢包形態使用）: "
            f"{period!r}")
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


def compute_ratio_metrics(returns: list[Decimal]) -> dict[str, Any]:
    """由日報酬樣本 `r_i` 算比率型指標（Sharpe/Sortino/年化波動/日勝率/最佳最差日）。

    **純函式**：只管公式，不管「多少天算充足」的閘門——那是 `RATIO_MIN_DAYS` 由
    呼叫端（`compute_window_performance`）套用的事。規格與數值錨例見
    `docs/superpowers/plans/2026-08-28-redesign-strategy-platform.md` Task 4。

    慣例：365 日/年、無風險利率 0%、樣本標準差 ddof=1、Sortino 分母為**全樣本**
    （對 0 門檻的下檔平方平均，不是只在虧損日取樣）。

    \\( SR = \\frac{\\bar r}{s}\\sqrt{365} \\)，
    \\( SE(SR) = \\sqrt{\\frac{1+SR_d^2/2}{N}}\\sqrt{365} \\)（\\(SR_d=\\bar r/s\\)，日頻），
    \\( \\sigma_{ann} = s\\sqrt{365} \\)，
    \\( Sortino = \\frac{\\bar r}{DD}\\sqrt{365} \\)，
    \\( DD = \\sqrt{\\frac{1}{N}\\sum_i \\min(r_i,0)^2} \\)。

    只回傳**數學上算得出來**的鍵——分母為 0（或樣本數不足以定義該分母）的指標，
    整組（數值＋它自己的不足標記，由呼叫端加）一起缺席，沿用本檔 `_annualize` 的
    「標記絕不單獨存在」慣例（工程原則 1 的同源要求）：
    - `N < 2`（樣本標準差 ddof=1 需要至少 2 點）或樣本標準差 `s == 0`
      （Sharpe 分母為 0）→ `sharpe`／`sharpe_se`／`annualized_vol` 三鍵一起缺席。
    - 全樣本無下檔日（`DD == 0`，Sortino 分母為 0）→ `sortino` 缺席。
    - `win_rate`／`best_day_return`／`worst_day_return`：`N >= 1` 即可，
      **不設任何門檻**（plan 明載「勝率與最佳最差日不設閘」）。
    """
    n = len(returns)
    out: dict[str, Any] = {"sample_count": n}
    if n == 0:
        return out

    out["win_rate"] = Decimal(sum(1 for r in returns if r > 0)) / Decimal(n)
    out["best_day_return"] = max(returns)
    out["worst_day_return"] = min(returns)

    mean = sum(returns, Decimal("0")) / Decimal(n)

    # --- Sortino：全樣本分母，對 0 門檻。分母為 0（無下檔日）→ 沒有數字可標。 ---
    downside_sq_sum = sum(min(r, Decimal("0")) ** 2 for r in returns)
    dd = (downside_sq_sum / Decimal(n)).sqrt()
    if dd != 0:
        out["sortino"] = (mean / dd) * DAYS_PER_YEAR_SQRT

    # --- Sharpe / SE / 年化波動：樣本標準差 ddof=1，需要 N>=2 且 s!=0。 ---
    if n >= 2:
        variance = sum((r - mean) ** 2 for r in returns) / Decimal(n - 1)
        s = variance.sqrt()
        out["annualized_vol"] = s * DAYS_PER_YEAR_SQRT
        if s != 0:
            sr_daily = mean / s
            out["sharpe"] = sr_daily * DAYS_PER_YEAR_SQRT
            out["sharpe_se"] = (
                (Decimal("1") + sr_daily ** 2 / Decimal("2")) / Decimal(n)
            ).sqrt() * DAYS_PER_YEAR_SQRT
    return out


def compute_window_performance(portfolio_rows: Any, period: str) -> dict[str, Any]:
    """單一 perp 窗的績效計算。**純函式、不觸網**。

    回傳的鍵集合（2026-07-19 改版，見檔頭「揭露模型改版」）：
    - `status == "insufficient"`：**無任何數值結果鍵**（連 `cum_pnl` 都沒有）。
      這一類是「算不出來」——缺窗、<2 個取樣點、兩序列時間戳不同步——與「算得出來
      但資料薄」是完全不同的處境，前者沒有任何數字可言，不受本次改版影響。
    - `status == "ok"`：`cum_pnl`／`twr`／`max_drawdown`／`equity_index` **恆存在**，
      各自帶 `*_insufficient_data` 標記；`annualized_return` 存在（除非數學上無定義），
      帶 `annualized_return_insufficient_data` ＋ `annualized_return_extrapolated_from_days`。
      `win_rate`／`best_day_return`／`worst_day_return`：N>=1 即存在，不設閘。
      `sharpe`／`sharpe_se`／`annualized_vol`／`sortino`：見 `compute_ratio_metrics`——
      數學上算不出來的（N<2、標準差=0、DD=0）整組（含 `*_insufficient_data`）缺席；
      算得出來的一律帶 `*_insufficient_data`＝`covered_days < RATIO_MIN_DAYS`（30 天）。

    ⭐ 為什麼標記做在**每個指標**上而不是只有 `disclosure_tier` 一個全域欄位：
    前端不保證整組一起渲染——只顯示 MDD 的卡片、只顯示年化的排行榜列，都會讓一個
    全域旗標完全不出現在那個畫面上。標記與它所描述的數字綁在一起，才不會在任何一種
    渲染組合下走散（工程原則 1 的同源要求推到 UI 邊界）。
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
    basis, basis_note = _basis_for(period)

    out: dict[str, Any] = {
        "period": period,
        "basis": basis,
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
        "basis_note": basis_note,
    }

    # --- 資料充足度分級：層級是 covered_days 的純函式，不決定哪些鍵存在 ---
    out["disclosure_tier"] = _tier_for(covered_days)

    # ⭐ 兩個不足判定各自寫出來、各自掛在自己的指標旁邊。用同一個布林變數餵兩個
    # 欄位也會過測試，但那會在「哪天 MDD 有了自己的門檻」時變成一個無聲的錯誤。
    thin_return = covered_days < MIN_DAYS_FOR_RETURN

    out["cum_pnl"] = cum_pnl
    out["twr"] = equity_index[-1] - Decimal("1")
    out["equity_index"] = tuple(equity_index)
    out["max_drawdown"] = _max_drawdown(equity_index)
    out["twr_insufficient_data"] = thin_return
    out["max_drawdown_insufficient_data"] = thin_return

    # --- 比率型指標（Sharpe/Sortino/年化波動/日勝率/最佳最差日）---
    # r_i 直接由權益指數推導（沿用既有日對齊邏輯）：equity_index 已把分母地板
    # 跳過的區間就地補 0（見上面 `skipped` 迴圈），所以這裡不必另外處理跳過段。
    ratio_returns = [equity_index[i] / equity_index[i - 1] - Decimal("1")
                     for i in range(1, len(equity_index))]
    ratio = compute_ratio_metrics(ratio_returns)
    ratio_thin = covered_days < RATIO_MIN_DAYS   # 獨立門檻，見 RATIO_MIN_DAYS 註解
    for key in ("win_rate", "best_day_return", "worst_day_return"):
        if key in ratio:
            out[key] = ratio[key]
    if "annualized_vol" in ratio:
        out["annualized_vol"] = ratio["annualized_vol"]
        out["annualized_vol_insufficient_data"] = ratio_thin
    if "sharpe" in ratio:
        out["sharpe"] = ratio["sharpe"]
        out["sharpe_se"] = ratio["sharpe_se"]
        out["sharpe_insufficient_data"] = ratio_thin
    if "sortino" in ratio:
        out["sortino"] = ratio["sortino"]
        out["sortino_insufficient_data"] = ratio_thin

    annualized = _annualize(equity_index[-1], covered_days)
    if annualized is None:
        # 帳戶被歸零（1+TWR <= 0）→ 年化在**數學上**沒有定義（非正數的非整數次方
        # 無實數解）。這與「資料不足」是兩回事：不足是可以標記後照樣顯示的，無定義
        # 沒有任何數字可標。三個 annualized_* 鍵在此路徑上一起缺席——標記絕不單獨
        # 存在，否則前端會看到「由 40 天外推」卻沒有被外推的數字。
        return out

    out["annualized_return"] = annualized
    out["annualized_return_insufficient_data"] = (
        covered_days < MIN_DAYS_FOR_ANNUALIZATION)
    # 年化**本質上就是外推**（除非窗剛好 365 天），所以這個欄位無條件回傳實際涵蓋
    # 天數，而不是只在不足時才給：前端要能一律寫出「由 N 天外推」。
    out["annualized_return_extrapolated_from_days"] = covered_days
    return out


def _tier_for(covered_days: Decimal) -> str:
    """`covered_days` → 資料充足度分級。純函式、無副作用、與鍵的存在與否無關。

    ⭐ 刻意做成 covered_days 的單純函式：分級一旦開始參考「這次算不算得出年化」
    之類的計算結果，同樣的天數就會依帳戶狀況給出不同層級，而前端拿層級去決定版面
    （例如「annualizable 才顯示年化欄位」）時會看到版面自己跳動。
    """
    if covered_days < MIN_DAYS_FOR_RETURN:
        return TIER_PNL_ONLY
    if covered_days < MIN_DAYS_FOR_ANNUALIZATION:
        return TIER_WINDOW_RETURN
    return TIER_ANNUALIZABLE


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
    """`(1+TWR)^(365/天數) − 1`。

    ⚠️ 2026-07-19 起呼叫點**不再**保證 `covered_days >= 90`：短窗照樣年化，但結果
    一定伴隨 `annualized_return_insufficient_data=True` 與
    `annualized_return_extrapolated_from_days`（實際天數）一起送出去。短窗年化在
    數值上是激進外推（7 天賺 3% → 年化 365%），揭露責任因此完全落在那兩個標記上。

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

    ⭐ 缺鍵一律**保持缺鍵**（不補 None）。改版後 `annualized_return` 的缺席只剩一種
    原因——年化在數學上無定義（帳戶歸零）——補成 `null` 會讓那個處境看起來像「有這個
    欄位只是還沒算」。`*_insufficient_data` 是 bool，原樣通過（不轉字串：前端要能
    直接 `if (marker)`，把它變成 `"False"` 這種**真值為 True** 的字串是致命的）。

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
