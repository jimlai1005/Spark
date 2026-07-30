"""src/spark/filet/risk_prefs.py
錢包主人自選的風控偏好（2026-07-30）：**單一定義點**——合法區間、預設值、以及
它們對應的引擎 env 鍵，全部只在本檔宣告一次。

四個消費端共用本檔，刻意不各自帶一份常數：
- filet-api（`POST /api/me/risk/message`／`POST /api/me/risk`）驗證客戶送來的值；
- 待簽訊息與記錄（`risk_settings.py`）用它正規化每一個要簽的數值；
- auto-activate watcher（`_compose_env`）把**已驗章**的偏好變成該 follower 的 env 行；
- 前端由 `GET /api/me/risk` 取得 `specs` 畫表單（不硬編任何數字）。
三份常數的下場是「畫面允許 0.01、API 收下、引擎拒絕啟動」，而症狀出現在啟用那一刻。

## 為什麼「關閉風控」是預設值
2026-07-30 使用者裁決：新錢包預設不啟用任何風控（回撤 kill switch 與成本熔斷）。
產品目前僅內部使用（見專案 CLAUDE.md 紅線 5 例外條款）。本檔的 `enabled` 預設
因此是 **False**——這與 `CopySettings.risk_controls_enabled` 的預設（True）刻意相反：
那裡的 True 保護「env 沒寫這一行」的既有部署（沒設定不該等於沒保護），這裡的 False
是新錢包的產品預設。兩者都由 watcher 明確寫進 env，不留給任何一邊的隱含預設。

## 信任模型（2026-07-30 起：**客戶簽章**）
本檔只定義「合法的偏好長什麼樣」。**誰有權設定它**由 `risk_settings.py` 回答：
客戶用自己的私鑰簽一份逐項列出門檻的原文，API（`POST /api/me/risk`）只落一筆
簽章記錄，引擎與 auto-activate watcher 在採用之前**各自重新驗章**。與換 leader
（`leader_change`）、資金設定（`capital_settings`）同一套信任錨、同一組原語。
- 這裡調整的全是**保護門檻**，不是部位大小。「押多大」仍由 `capital_settings` 的
  簽章路徑決定，本檔動不了它——被打穿的 filet-api 只能削弱保護，不能直接放大曝險。
  但削弱保護同樣是真錢，所以防線一樣，不因為「危害小一點」而降級。
- 改動前的路徑（偏好寫進 pending 條目、無簽章、只在啟用那一刻烙進 env）已移除：
  它的緩解是「暴露窗只有啟用前的一分鐘」，而要讓**已啟用**的客戶隨時改風控，
  那個緩解就消失了。移除 `set_pending_risk` 是為了不留下第二份沒有簽章、
  卻長得同樣權威的意圖（見 pending.py 該處的註記）。
- 仍然成立的事實：引擎**不重讀 env**。env 裡的風控行是這顆引擎的**啟動姿態**
  （watcher 建檔時寫一次、`_ensure_env_file` 不覆寫既有檔）；執行期的調整走簽章
  記錄，由 `risk_settings_apply` 每輪疊在 settings 之上。兩條路徑的值都源自
  本檔的同一組 spec，所以「畫面允許、引擎拒絕」這類漂移仍然寫不出來。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# ── 可調參數的單一定義點 ──────────────────────────────────────────────
# 每項：(env 鍵, 型別, 預設, 下界, 上界)。下界／上界對 bool 為 None。
#
# ⭐ 為什麼**不**開放成本熔斷器的兩個門檻給客戶調：那道閘門守的是「客戶資金被交易
# 磨損的速度」，而磨損的受益方是我方（builder fee）。把它交給客戶調鬆，等於讓
# 利益衝突的一方去說服另一方拿掉針對自己的限制（見 config.py 該欄位的論證）。
# 客戶要的是「全開或全關」，那由 `enabled` 表達就夠了。
#
# ⭐ max_drawdown_pct 的區間刻意收窄成 [0.05, 0.50]：
# - 下界 0.05：更小的值會讓正常波動每輪觸發熔斷 → 強制平倉客戶自己的部位，
#   而客戶按下去的時候以為自己在「加強保護」。
# - 上界 0.50：本欄位同時決定成本閘分母的下限，誤觸面放大倍率 = 1/(1-dd)
#   （config.py:125-138）。0.50 已是 2.00×，再高就是拿成本閘的誤觸換回撤的寬容。
RISK_PARAM_SPECS: tuple[dict, ...] = (
    # ── group="tracking"：**不受風控開關影響**，永遠生效 ─────────────────
    # 放在這一組的參數與「要不要風控」無關，UI 也必須畫在 checkbox 之外——
    # 藏在 checkbox 底下會讓關掉風控的客戶以為自己也關掉了它。
    {"name": "size_tolerance", "env": "COPY_SIZE_TOLERANCE", "type": "decimal",
     "group": "tracking", "default": "0.08", "min": "0.02", "max": "0.25",
     "label": "與 leader 的部位差異容忍度", "recommended": "0.08",
     "help": "你的部位與 leader（按比例換算後）差異超過此值才會校正。"
             "調小＝跟得更緊，但校正次數變多、手續費與滑價成本上升；"
             "調大＝成本低，但你的部位會與 leader 有較大偏離。建議 8%。"},
    # ── group="risk"：只有啟用風控時才生效 ──────────────────────────────
    {"name": "max_drawdown_pct", "env": "COPY_MAX_DRAWDOWN_PCT", "type": "decimal",
     "group": "risk", "recommended": "0.20",
     "default": "0.20", "min": "0.05", "max": "0.50",
     "label": "7 天滾動回撤上限",
     "help": "以最近 7 天內的權益高水位為基準；跌幅超過此值即熔斷。"},
    {"name": "max_total_drawdown_pct", "env": "COPY_MAX_TOTAL_DRAWDOWN_PCT",
     "type": "decimal", "group": "risk", "recommended": "0.40",
     "default": "0.40", "min": "0", "max": "0.80",
     "label": "累計回撤上限",
     "help": "以開始跟單以來的高水位為基準的絕對底線（0 ＝ 停用這一道）。"
             "慢跌可能每個 7 天窗都不超標卻累積成大額虧損，這一道是為此存在。"},
    {"name": "flatten_on_breach", "env": "COPY_FLATTEN_ON_BREACH", "type": "bool",
     "group": "risk", "recommended": True,
     "default": True, "min": None, "max": None,
     "label": "熔斷時自動平倉",
     "help": "開：熔斷即撤單並全平。關：熔斷只停止交易動作並告警，"
             "既有部位留在市場上（軟暫停）。"},
    # ⭐ 冷靜期（2026-07-30 使用者裁決）：熔斷後多久自動恢復跟單。
    # 以小時為單位（不是比例），所以 `unit` 欄位存在——前端據此決定要不要做
    # 百分比換算。0 ＝ 只有你自己按「立即恢復」或人工處理才會解鎖。
    {"name": "cooldown_hours", "env": "COPY_RISK_COOLDOWN_HOURS", "type": "decimal",
     "group": "risk", "unit": "hours", "recommended": "12",
     "default": "12", "min": "0", "max": "168",
     "label": "熔斷後的冷靜期（小時）",
     "help": "熔斷後經過這段時間就自動恢復跟單，權益基準已在熔斷當下重置。"
             "建議 12 小時：短到不會把你鎖在門外，長到足以讓觸發熔斷的那段行情過去。"
             "設 0 ＝ 不自動恢復（只有你按「立即恢復跟單」才解鎖）。"},
)

_SPEC_BY_NAME = {s["name"]: s for s in RISK_PARAM_SPECS}

# 比例的最小刻度（0.1%）——與前端百分比輸入的精度對齊，見 `_as_bounded_decimal`。
_RATIO_STEP = Decimal("0.001")

# watcher 一律代入的風控鍵（含總開關）。範本裡出現任何一個 → 整輪 fail-closed。
# 理由同 `filet_auto_activate.GENERATED_KEYS`：範本與代入區塊重複定義同一個鍵，
# 哪個生效取決於 EnvironmentFile 的載入細節——歧義本身就是錯，不留給讀者猜。
RISK_ENV_KEYS: tuple[str, ...] = (
    ("COPY_RISK_CONTROLS_ENABLED",) + tuple(s["env"] for s in RISK_PARAM_SPECS))


class RiskPrefsError(ValueError):
    """語意錯誤（4xx，不得自動重試）。`reason` 供呼叫端對映客戶可讀訊息。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def default_prefs() -> dict:
    """新錢包的預設：**不啟用風控**（使用者裁決，見檔頭）。

    細項參數同時帶上預設值——不是因為它們會生效（enabled=False 時引擎根本不執法），
    而是為了讓前端表單在客戶勾選「啟用」的那一刻就有值可顯示，不必自己編一份。
    """
    # ⭐ 預設值也走 `_as_bounded_decimal`：正規化（對齊刻度、定點格式）後，
    # 「規格裡的預設」「API 回的值」「env 裡的值」三處是同一個字串。不這樣做的話
    # 規格寫 "0.20" 而 env 寫 "0.2"，任何字串比對（例如前端判斷「有沒有改過」）
    # 都會把兩個相同的值看成不同。
    return {"enabled": False,
            **{s["name"]: (s["default"] if s["type"] == "bool"
                           else _as_bounded_decimal(s, s["default"]))
               for s in RISK_PARAM_SPECS}}


def _as_bool(name: str, v) -> bool:
    if isinstance(v, bool):
        return v
    raise RiskPrefsError(f"{name}_not_bool", f"{name} 必須是 true 或 false")


def _as_bounded_decimal(spec: dict, v) -> str:
    name = spec["name"]
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise RiskPrefsError(f"{name}_not_number", f"{name} 必須是數字") from None
    if not d.is_finite():
        raise RiskPrefsError(f"{name}_not_finite", f"{name} 必須是有限數字")
    lo, hi = Decimal(spec["min"]), Decimal(spec["max"])
    # ⭐ 超界一律拒絕，**不夾取**（沿 capital_settings 的既有裁決）：夾取會讓流程
    # 順利跑完，代價是客戶送了 A、系統套用了 B，而且沒有人會知道。
    if not (lo <= d <= hi):
        raise RiskPrefsError(f"{name}_out_of_range",
                             f"{name} 必須在 {lo} 與 {hi} 之間（收到 {d}）")
    # 對齊 0.1% 刻度後以**定點**格式輸出。兩個理由：
    # (a) `str(Decimal("0E+1"))` 會給 "0E+1"，而 env 檔裡一行
    #     `COPY_MAX_TOTAL_DRAWDOWN_PCT=0E+9` 引擎讀得懂、人讀不懂；
    # (b) 前端以百分比、最多一位小數呈現，落檔值對齊同一刻度才不會出現
    #     「畫面 33.3%、實際 0.3333」＝客戶送 A、系統套用 B（審查 F7）。
    d = d.quantize(_RATIO_STEP).normalize()
    return f"{d:f}"


def canonical_prefs(payload: dict | None, *, base: dict | None = None,
                    require_enabled: bool = False) -> dict:
    """驗證＋正規化偏好；回傳可直接落檔的 dict。

    `enabled=False` 時**仍然驗證細項**（不是忽略它們）：客戶下次勾選啟用時，
    落檔的就是這些值——現在收下一個超界的數字，等於埋一顆啟用當下才炸的地雷。
    未知的鍵直接拒絕（打錯字不該被靜默忽略）。

    ⭐⭐ `base`／`require_enabled` 是為了關掉一條 fail-open 的寫入路徑（審查 F1）：
    缺鍵補值的來源必須是「**這個帳號目前存的值**」，不是產品預設。否則一個
    `{"prefs": {}}` 的請求（表單重置、重試掉了 body、第三方呼叫）會把客戶已經
    開啟的風控靜默關掉，而回應是 200——與 `CopySettings.risk_controls_enabled`
    的預設 True 所依據的同一條理由相牴觸：**沒設定不該等於沒保護**。
    - 讀取路徑（顯示、watcher 落檔前重驗）：`base=None` ⇒ 缺鍵補產品預設。
    - 寫入路徑（API POST）：`base=目前存的偏好`、`require_enabled=True`
      ⇒ 客戶必須**明確**表達開或關，其餘欄位沿用他既有的值。
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise RiskPrefsError("not_an_object", "風控偏好必須是一個物件")
    allowed = {"enabled", *_SPEC_BY_NAME}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise RiskPrefsError("unknown_field", f"不認得的欄位：{', '.join(unknown)}")
    if require_enabled and "enabled" not in payload:
        raise RiskPrefsError(
            "enabled_missing",
            "必須明確指定 enabled（true＝啟用風控、false＝不啟用）——"
            "省略它會被讀成「關閉」，那不該由一個缺漏的欄位決定")

    fallback = base if base is not None else default_prefs()
    out = {"enabled": _as_bool("enabled",
                               payload.get("enabled", fallback["enabled"]))}
    for spec in RISK_PARAM_SPECS:
        name = spec["name"]
        raw = payload.get(name, fallback.get(name, spec["default"]))
        out[name] = (_as_bool(name, raw) if spec["type"] == "bool"
                     else _as_bounded_decimal(spec, raw))
    return out


def risk_env_lines(prefs: dict | None) -> list[str]:
    """偏好 → env 行（watcher 代入區塊用）。

    ⚠️ **fail-closed 的方向在此**：`prefs` 壞掉（不是 None，是驗不過）時本函式
    上拋，由呼叫端決定；而呼叫端（watcher）的處置是**改用「風控開啟」的安全側預設**
    並大聲記錄——絕不因為讀不懂客戶的偏好就沿用「風控關閉」。
    `None`（客戶從未表達）走 `default_prefs()`＝關閉，那是合法的產品預設，不是失敗。
    """
    prefs = canonical_prefs(prefs)   # 再驗一次：落檔後被手改過的值不該被信任
    lines = [f"COPY_RISK_CONTROLS_ENABLED={'true' if prefs['enabled'] else 'false'}"]
    for spec in RISK_PARAM_SPECS:
        v = prefs[spec["name"]]
        lines.append(f"{spec['env']}={'true' if v is True else 'false' if v is False else v}")
    return lines


def safe_fallback_prefs() -> dict:
    """讀不懂客戶偏好時要用的那一份：風控**開啟**，細項全預設。

    為什麼不是「沿用產品預設（關閉）」：那會讓「資料壞掉」與「客戶選擇不要保護」
    產生同一個結果，而前者是我們的錯、後者是客戶的決定。壞資料的正確方向是
    往保護靠（fail-closed），並讓告警把它變成一件有人會處理的事。
    """
    return {**default_prefs(), "enabled": True}


def prefs_summary(prefs: dict | None) -> dict:
    """給 API 回應／面板用的投影（正規化後的值 ＋ specs，讓前端不必硬編任何數字）。"""
    return {"prefs": canonical_prefs(prefs), "specs": list(RISK_PARAM_SPECS),
            "defaults": default_prefs()}
