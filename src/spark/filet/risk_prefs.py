"""src/spark/filet/risk_prefs.py
錢包主人自選的風控偏好（2026-07-30）：**單一定義點**——合法區間、預設值、以及
它們對應的引擎 env 鍵，全部只在本檔宣告一次。

三個消費端共用本檔，刻意不各自帶一份常數：
- filet-api（`POST /api/me/risk`）驗證客戶送來的值；
- auto-activate watcher（`_compose_env`）把偏好變成該 follower 的 env 行；
- 前端由 `GET /api/me/risk` 取得 `specs` 畫表單（不硬編任何數字）。
三份常數的下場是「畫面允許 0.01、API 收下、引擎拒絕啟動」，而症狀出現在啟用那一刻。

## 為什麼「關閉風控」是預設值
2026-07-30 使用者裁決：新錢包預設不啟用任何風控（回撤 kill switch 與成本熔斷）。
產品目前僅內部使用（見專案 CLAUDE.md 紅線 5 例外條款）。本檔的 `enabled` 預設
因此是 **False**——這與 `CopySettings.risk_controls_enabled` 的預設（True）刻意相反：
那裡的 True 保護「env 沒寫這一行」的既有部署（沒設定不該等於沒保護），這裡的 False
是新錢包的產品預設。兩者都由 watcher 明確寫進 env，不留給任何一邊的隱含預設。

## 信任模型（**已知缺口，對外開放前必須補**）
本偏好由 filet-api 寫進 pending 條目，**沒有客戶簽章**——與換 leader（`leader_change`）
和資金設定（`capital_settings`）不同。刻意的取捨與它的邊界：
- 這裡調整的全是**保護門檻**，不是部位大小。「押多大」仍由 `capital_settings` 的
  簽章路徑決定，本檔動不了它——所以被打穿的 filet-api 能削弱保護，不能直接放大曝險。
- 偏好只在**啟用那一刻**被讀取並烙進 `/etc/filet/followers/<id>.env`，而該檔對
  filet-api 不可寫（root/filet-engine）。啟用之後 filet-api 再也影響不了這顆引擎的
  風控姿態——暴露窗只有「客戶送出偏好」到「watcher 建 env」之間的一分鐘。
- 引擎**不**在每輪重讀本偏好（那會讓 filet-api 取得對執行中引擎的持續影響力，
  違反 pending.py 檔頭的權限拓撲）。已啟用的帳號要改風控＝ops 動作，
  API 因此對這種情形回明確的 409，不假裝寫入成功（見 app.py 的 `/api/me/risk`）。
⚠️ 對外開放前必須把本路徑升級為簽章記錄（照 `capital_settings.py` 的形狀）。
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
    {"name": "max_drawdown_pct", "env": "COPY_MAX_DRAWDOWN_PCT", "type": "decimal",
     "default": "0.20", "min": "0.05", "max": "0.50",
     "label": "7 天滾動回撤上限",
     "help": "以最近 7 天內的權益高水位為基準；跌幅超過此值即熔斷。"},
    {"name": "max_total_drawdown_pct", "env": "COPY_MAX_TOTAL_DRAWDOWN_PCT",
     "type": "decimal", "default": "0.40", "min": "0", "max": "0.80",
     "label": "累計回撤上限",
     "help": "以開始跟單以來的高水位為基準的絕對底線（0 ＝ 停用這一道）。"
             "慢跌可能每個 7 天窗都不超標卻累積成大額虧損，這一道是為此存在。"},
    {"name": "flatten_on_breach", "env": "COPY_FLATTEN_ON_BREACH", "type": "bool",
     "default": True, "min": None, "max": None,
     "label": "熔斷時自動平倉",
     "help": "開：熔斷即撤單並全平，之後需人工解鎖才恢復跟單。"
             "關：熔斷只停止交易動作並告警，既有部位留在市場上（軟暫停）。"},
)

_SPEC_BY_NAME = {s["name"]: s for s in RISK_PARAM_SPECS}

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
    return {"enabled": False,
            **{s["name"]: (s["default"] if s["type"] == "bool" else str(s["default"]))
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
    return str(d)


def canonical_prefs(payload: dict | None) -> dict:
    """驗證＋正規化客戶送來的偏好；回傳可直接落檔的 dict。

    `enabled=False` 時**仍然驗證細項**（不是忽略它們）：客戶下次勾選啟用時，
    落檔的就是這些值——現在收下一個超界的數字，等於埋一顆啟用當下才炸的地雷。
    未提供的鍵取預設值；未知的鍵直接拒絕（打錯字不該被靜默忽略）。
    """
    if payload is None:
        return default_prefs()
    if not isinstance(payload, dict):
        raise RiskPrefsError("not_an_object", "風控偏好必須是一個物件")
    allowed = {"enabled", *_SPEC_BY_NAME}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise RiskPrefsError("unknown_field", f"不認得的欄位：{', '.join(unknown)}")

    out = {"enabled": _as_bool("enabled", payload.get("enabled", False))}
    for spec in RISK_PARAM_SPECS:
        name = spec["name"]
        raw = payload.get(name, spec["default"])
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
