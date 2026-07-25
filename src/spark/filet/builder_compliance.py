"""src/spark/filet/builder_compliance.py
builder 地址的 **builder-fee 資格**監控——唯讀、純判定、零寫入面。

為什麼需要這個模組（營收風險，2026-07-19 發現）
------------------------------------------------
HL 官方文件對 builder code 的生效條件寫得很清楚：

    "**The builder** must have at least 100 USDC in perps account value and must
     use standard as the account abstraction mode."

主詞是 **builder**（我方），不是客戶——已實測確認。兩個條件任一不成立，builder fee
就**無聲**停止累積：客戶照樣下單、成交照樣送出、每一份 log 都正常，只有收入曲線
悄悄變平。而在此之前我方對這兩個條件是**零監控**的：

- 條件 1（perp 帳戶淨值 ≥ 100 USDC）會被一次**提領**觸發。提領是完全正常的營運動作，
  沒有任何一個現有告警會提到它跌破了 builder 門檻。
- 條件 2（abstraction mode）是帳戶層級的設定，可能被 UI 上一次點擊改掉。

日報是唯一會定期看的地方，所以判定住在這裡、告警接進日報（工程原則 3：安全／營收
關鍵的失敗必須大聲，不得只記在 log 裡）。

⚠️ 為什麼只放行 `"disabled"`（實測 + 保守決策）
----------------------------------------------
實測 `userAbstraction` 端點回傳的值有三種：

- `"disabled"` —— 對應官方文件說的 **standard** 模式，也是我方 builder 位址**目前**
  的實測值。**唯一放行值**。
- `"unifiedAccount"` —— 明確不是 standard。不合規。
- `"default"` —— 「從未設定過」的帳戶會回這個值。它的語意**未經官方確認**：它很可能
  等同 standard（「預設就是標準模式」），但我們沒有任何一手證據。

**決定：`"default"` 一律視為不合規。** 理由是兩種猜錯的代價不對稱——猜「合規」而錯，
builder fee 會靜默歸零而監控告訴我們一切正常（正是本模組要根除的失效）；猜「不合規」
而錯，代價只是一則需要人工確認一次的告警。在拿到官方確認之前，不賭未經驗證的語意。
（若日後官方確認 `"default"` == standard，把它加進 `COMPLIANT_ABSTRACTION_MODES`
即可，並請同步更新本段與對應測試。）
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from spark.config import MIN_BUILDER_BALANCE

logger = logging.getLogger(__name__)

# builder 的 perp 帳戶淨值門檻（USDC）。⭐ 刻意是 `MIN_BUILDER_BALANCE` 的**別名**而
# 不是重寫一次 `Decimal("100")`：同一條 HL 規則已經在 onboarding 的 pre-flight 擋板
# （publicapi/app.py 的 payload_approve_builder_fee）用著同一個數字。兩份字面量會漂移，
# 而漂移後的症狀是「擋板擋在 100、監控報在 120」這種沒人看得出來的不一致。
BUILDER_MIN_PERP_EQUITY: Decimal = MIN_BUILDER_BALANCE

# ⭐ 放行清單（白名單，不是黑名單）。白名單的意義：任何**沒見過**的新模式值一律
# 判定為不合規並告警。黑名單會讓 HL 明天新增一種模式時靜默放行——而那正是這個
# 監控存在的理由。理由與 "default" 的取捨見檔頭。
COMPLIANT_ABSTRACTION_MODES: frozenset[str] = frozenset({"disabled"})

# 查詢失敗時的模式佔位值。**不是**合規值：見 `BuilderCompliance.mode_ok`。
MODE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuilderCompliance:
    """單一 builder 位址的資格判定結果。

    ⭐ 三態而非兩態：`equity_ok`／`mode_ok` 是 `bool | None`，`None` ＝ **查詢失敗，
    不知道**。把「不知道」壓成 `False`（告警）或 `True`（放行）都會弄丟資訊：前者
    讓操作者分不清「真的跌破門檻」與「API 掛了」，後者是 fail-open ——對一個會無聲
    斷掉營收的條件，靜默放行是最糟的失效方向（工程原則 3）。
    `is_compliant` 因此只在**兩者都明確為 True** 時才是 True。
    """
    builder: str
    equity: Decimal | None = None
    equity_ok: bool | None = None
    mode: str | None = None
    mode_ok: bool | None = None
    # 查詢失敗的原因（人看的字串）。⭐ 只放例外訊息，不放位址以外的任何識別資訊。
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_compliant(self) -> bool:
        """兩個條件**都**明確通過才算合規。未知 → False（fail-closed）。"""
        return self.equity_ok is True and self.mode_ok is True

    @property
    def has_unknown(self) -> bool:
        return self.equity_ok is None or self.mode_ok is None


def check_builder_compliance(adapter, builder: str) -> BuilderCompliance:
    """對單一 builder 位址跑兩個唯讀查詢並判定。**不 raise**。

    兩個查詢**各自**錯誤隔離：淨值查得到但模式查不到（或反過來）時，查得到的那一半
    仍然要給出明確判定——一個查詢失敗把另一個也拖成「未知」，會讓真正跌破門檻的
    情況被一個不相干的 API 故障蓋掉。

    ⭐ 失敗分類（工程原則 2／5）：這裡把**所有**查詢例外都當成 transient／未知
    （`equity_ok = None`），不區分連線失敗與 schema 錯誤。理由：本函式的下游動作對
    兩者是同一件事——「說不知道並告警，下一輪日報再查」。讀取不經 resilience 邊界、不會重試（專案慣例：讀取不進 resilience，見 resilience.py）。網路抖動時本函式會把實際合規的 builder 判為「資格未知」並發告警——方向安全（只會過度告警），且日報一天僅一次，屬可接受的取捨。

    `adapter` 只需具備兩個唯讀方法（`get_account_value`／`query_user_abstraction`）；
    刻意不宣告成 `ExchangeAdapter` 型別註記——本模組結構上用不到任何寫入方法，
    收窄依賴讓「這裡不可能下單」是看得出來的。
    """
    equity: Decimal | None = None
    equity_ok: bool | None = None
    mode: str | None = None
    mode_ok: bool | None = None
    errors: list[str] = []

    try:
        # ⭐ `get_account_value` 讀的是 `clearinghouseState.marginSummary.accountValue`
        # ＝ **perp** 帳戶淨值，正是官方條件說的那個量（同源同單位，工程原則 1）。
        # 不可換成含 spot 的總值：spot 有錢而 perp 空的帳戶會被判成合規，但 builder
        # fee 實際上已經斷了。
        equity = adapter.get_account_value(builder)
        equity_ok = equity >= BUILDER_MIN_PERP_EQUITY
    except Exception as e:  # noqa: BLE001 — 查詢失敗＝未知，絕不當成合規
        errors.append(f"perp 淨值查詢失敗: {e}")

    try:
        mode = adapter.query_user_abstraction(builder)
        mode_ok = mode in COMPLIANT_ABSTRACTION_MODES
    except Exception as e:  # noqa: BLE001
        errors.append(f"abstraction mode 查詢失敗: {e}")
        mode = MODE_UNKNOWN

    return BuilderCompliance(builder=builder, equity=equity, equity_ok=equity_ok,
                             mode=mode, mode_ok=mode_ok, errors=tuple(errors))


@dataclass(frozen=True)
class ComplianceAlert:
    """單一告警＝(dedup_key, text)。

    ⭐ `dedup_key` 帶 **builder 位址 + 條件**（`builder_equity_<addr>`／
    `builder_mode_<addr>`）。推播端（Notifier）用它做 TTL 去重：同一個 builder 的
    同一個條件持續不合規時不洗版，但不同 builder／不同條件各自獨立、互不遮蔽。
    """
    dedup_key: str
    text: str


def compliance_alert_items(result: BuilderCompliance) -> list[ComplianceAlert]:
    """判定結果 → 結構化告警清單（空清單＝完全合規，無話可說）。

    ⭐ 兩個條件**各自**產生一則告警，不合併成一句「builder 不合規」：兩者的處置動作
    完全不同（入金 vs 改帳戶設定），而一則含糊的告警會讓操作者只修掉他先看到的那個，
    以為修完了。兩者皆不符時**兩則都要出現**。

    `compliance_alerts()`（純字串清單）由本函式的 `text` 導出——兩者的字串逐字相同，
    是**單一定義**：報表本文用字串清單，推播用附帶 dedup_key 的結構化清單，不會漂移。
    """
    items: list[ComplianceAlert] = []
    b = result.builder

    if result.equity_ok is None:
        items.append(ComplianceAlert(
            f"builder_equity_{b}",
            f"**builder 資格未知**：{b} 的 perp 淨值查不到，無法確認是否 ≥ "
            f"{BUILDER_MIN_PERP_EQUITY} USDC 門檻。**不得視為合規**——低於門檻時 "
            "builder fee 會無聲停止累積。請人工確認。"))
    elif not result.equity_ok:
        items.append(ComplianceAlert(
            f"builder_equity_{b}",
            f"**builder fee 可能已停止累積**：{b} 的 perp 淨值 {result.equity} USDC "
            f"低於 HL 官方門檻 {BUILDER_MIN_PERP_EQUITY} USDC。成交會照常發生但**不會"
            "產生 builder fee**，且沒有任何其他訊號會顯示這件事。請立即入金。"))

    if result.mode_ok is None:
        items.append(ComplianceAlert(
            f"builder_mode_{b}",
            f"**builder 資格未知**：{b} 的 account abstraction mode 查不到，無法確認"
            "是否為 standard。**不得視為合規**。請人工確認。"))
    elif not result.mode_ok:
        items.append(ComplianceAlert(
            f"builder_mode_{b}",
            f"**builder fee 可能已停止累積**：{b} 的 account abstraction mode 為 "
            f"{result.mode!r}，不在放行清單 {sorted(COMPLIANT_ABSTRACTION_MODES)} 內"
            "（HL 官方要求 standard 模式）。注意 'default' 亦刻意視為不合規——其語意"
            "未經官方確認，不賭。請人工確認帳戶設定。"))

    for i, e in enumerate(result.errors):
        items.append(ComplianceAlert(
            f"builder_error_{b}_{i}", f"builder {b} 合規查詢錯誤：{e}"))
    return items


def compliance_alerts(result: BuilderCompliance) -> list[str]:
    """判定結果 → 告警字串清單（空清單＝完全合規）。`compliance_alert_items` 的字串視圖。"""
    return [item.text for item in compliance_alert_items(result)]


def check_builders(adapter_for, builders) -> tuple[list[BuilderCompliance], list[str]]:
    """一組 builder 位址 → (判定清單, 全部告警字串)。日報的單一進入點。

    `adapter_for`: Callable[[], adapter]——由呼叫端決定用哪個網路的 adapter
    （沿 filet_daily_report 既有的 `adapter_for(network)` 慣例）。
    """
    results = [check_builder_compliance(adapter_for(), b) for b in builders]
    alerts: list[str] = []
    for r in results:
        alerts.extend(compliance_alerts(r))
    return results, alerts
