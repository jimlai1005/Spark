"""src/spark/filet/engine_health.py
引擎主動發布的**健康摘要心跳**（engine → API 的單向產物）。

⭐⭐ 為什麼是「引擎發布一份窄的產物」而不是「放寬狀態根權限」
------------------------------------------------------------
營運健康面板跑在 **filet-api** 進程，引擎的狀態根是 `filet-engine:filet-engine 0700`
——跨了一道權限邊界，於是面板預設每一格都是「未知」（正確但無用）。

看起來最短的修法是把狀態根放寬到 `0750`。**不採用**：面板需要的是幾個摘要值
（kill switch 有沒有跳、樣本夠不夠、跟誰、押多大），而狀態根裡裝的是引擎的**全部**
狀態——equity 樣本序列、kill switch ARM 檔、兩份已兌現 nonce 帳本、告警流水。
為了讀五個數字而把這一整包交給另一個進程，是拿「廣泛的讀取權」換「窄的資訊需求」。

改採與換 leader **同構**的設計：**發布一份窄的產物，優於開放廣泛的讀取權**。
API 對引擎的意圖是這樣傳的（寫一筆記錄到交換目錄，而不是讓 API 去改 manifest），
引擎對 API 的狀態也照同一個方向走一次，只是通道反向。

⭐ 交換目錄的最小權限拓撲（**兩個單向通道，沒有任何雙向可寫的路徑**）
--------------------------------------------------------------------
```
/var/lib/filet-exchange/              filet-api:filet-engine  0750   ← api→engine 通道
├── leader_changes.json                                              （API 寫、引擎讀）
├── capital_settings.json
└── engine/                           filet-engine:filet-api  0750   ← engine→api 通道
    └── health/                                                      （引擎寫、API 讀）
        └── <account_id>.json
```
- 根目錄的 owner 仍是 `filet-api`：引擎對它**沒有寫權**（原設計不變），所以引擎
  被打穿也污染不了客戶簽章記錄。
- 子目錄 `engine/` 的 owner 換成 `filet-engine`、group 換成 `filet-api`：引擎能寫
  **這一格**、API 只能讀。反向亦然（API 對 `engine/` 無寫權）。
- 因此「引擎可寫」的範圍恰好是它要發布的那一格，不是整個交換目錄。systemd 那邊
  對稱地只把 `engine/` 列入 `ReadWritePaths`（見 deploy/filet-follower@.service）。
- 目錄由部署時人工建立（RUNBOOK §5.5.1）：引擎對根目錄無寫權，**建不出**這個子目錄
  ——這是刻意的，它讓「引擎能寫什麼」是一個部署決定，而不是引擎自己決定的事。

⭐ 心跳只放**摘要**，結構性地不放密鑰材料
----------------------------------------
心跳的讀者是一個權限較低的進程，它落在一個 API 讀得到的目錄裡。所以「不要把簽章
寫進去」不能只是一句註解——`_reject_secret_material` 在**寫入的那一刻**掃描整份
payload，命中就拒寫（工程原則 5 的精神：讓它結構性成立，而不是靠記性）。
記錄裡的 `signature`／`message`／nonce 一律不進心跳：面板不需要它們，而它們一旦
外流就是把「客戶授權的證據」交給另一個信任域。

⚠️ 心跳寫入失敗**不得中斷跟單**（工程原則 3 的邊界）：這是可觀測性，不是安全關鍵
路徑。觀測層壞掉絕不能弄停被觀測的系統——`publish_heartbeat` 吞掉所有例外只 log。
反過來說，這也意味著「心跳不見了」有兩種成因（引擎沒跑／引擎跑了但寫不進去），
所以讀端必須把「心跳年齡」一併上呈，而不是只說一句「未知」。

⭐⭐ 過期的心跳**不是**目前狀態
------------------------------
心跳是快照，不是即時查詢。一份 40 分鐘前的心跳說「kill switch 未觸發」，只證明
40 分鐘前沒觸發——把它當成目前狀態顯示，正是這個面板最不能犯的錯（謊報健康）。
`read_heartbeat` 因此在過期時**結構性地不回傳 payload**（`data is None`），
只回最後心跳時刻與年齡：呼叫端就算想拿也拿不到那些值。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spark.filet.followers import validate_account_id

logger = logging.getLogger(__name__)

# 交換目錄下的 engine→api 子通道（見檔頭拓撲圖）。名稱寫在這裡是**單一定義**：
# 寫端（引擎）、讀端（API）、部署（RUNBOOK 與 systemd unit）三者引用同一個常數，
# 各自拼一次字串就會出現「引擎寫 A、API 讀 B」的靜默失效（C3 的形狀）。
ENGINE_PUBLISH_DIRNAME = "engine"
HEARTBEAT_DIRNAME = "health"

# ⭐ 心跳過期門檻（秒）。引擎每個 cycle 寫一次心跳，cycle 間隔預設 60s
# （`CopySettings.interval_s`），所以 600s ＝ **連續 10 個 cycle 沒有心跳**。
# 門檻若貼近 cycle 間隔，一次 GC 停頓或一輪慢的交易所查詢就會讓面板閃紅——
# 會誤報的面板等於沒有面板（操作者會學會忽略它）。
#
# ⭐⭐ 這個值刻意等於 `publicapi.ops.ENGINE_STALE_S`（equity 樣本的存活門檻），
# 並由 tests/test_engine_health.py 釘住相等。兩者回答的是同一個問題（「引擎最近
# 有沒有動」）、基準也同源（都是「引擎每 cycle 落一次的東西」的年齡）；兩個門檻
# 各自漂移的話，面板上「引擎存活」與「心跳新鮮」會在同一個時刻給出相反的答案，
# 而操作者無從判斷該信哪一個。
HEARTBEAT_STALE_S = 600

# 心跳頂層鍵集（多一個少一個都要有人主動改這行，並被測試逼著解釋為什麼）。
# 沿 CAPITAL_SETTINGS_FIELDS 的既有慣例：釘住它的是測試，不是註解。
HEARTBEAT_FIELDS = ("account_id", "written_at", "written_at_s", "killswitch_tripped",
                    "coverage", "alerts", "leader", "capital", "risk", "last_cycle")

# ⭐⭐ 禁止出現在心跳裡的鍵名片段（大小寫不敏感）。命中即拒寫，見
# `_reject_secret_material`。清單刻意含 `nonce`／`message`：它們本身不是密鑰，
# 但它們是「客戶授權憑證」的組成部分，而面板一個都不需要。
FORBIDDEN_KEY_PARTS = ("signature", "signing", "secret", "private", "passwd",
                       "password", "token", "credential", "seed", "mnemonic",
                       "agent_key", "apikey", "api_key", "nonce", "message")

# 長 hex 字串偵測（簽章 132 字元、私鑰 66 字元；位址只有 42 字元故不誤傷）。
# 鍵名檢查擋的是「有人加了一個叫 signature 的欄位」，本檢查擋的是「有人把簽章塞進
# 一個叫 detail 的欄位」——兩層都要，因為前者靠命名、後者不靠。
_LONG_HEX_RE = re.compile(r"0x[0-9a-fA-F]{64,}")


class HeartbeatRejected(ValueError):
    """心跳內容含密鑰材料 ⇒ **拒寫**（絕不是「寫了再說」）。"""


def engine_publish_dir_for(exchange_dir: str | Path) -> Path:
    """engine→api 通道的根（`<exchange_dir>/engine`）。**單一定義**，見檔頭拓撲。"""
    return Path(exchange_dir) / ENGINE_PUBLISH_DIRNAME


def heartbeat_dir_for(exchange_dir: str | Path) -> Path:
    return engine_publish_dir_for(exchange_dir) / HEARTBEAT_DIRNAME


def heartbeat_path_for(exchange_dir: str | Path, account_id: str) -> Path:
    """單一 follower 的心跳落點（**per-follower 一個檔**）。

    ⭐ 一個檔一個 follower，不是一份共用的大 JSON：共用檔要嘛需要 read-modify-write
    （多個引擎進程互相覆蓋彼此的那一段），要嘛需要鎖。per-follower 的檔案讓每個引擎
    只寫自己的那一格，寫入之間結構上不可能互相干擾。

    `account_id` 先過 `validate_account_id`：它會變成路徑的一段，而路徑穿越
    （`../..`）在這裡的後果是引擎往交換目錄以外的地方寫檔。它的來源目前是
    env／manifest（不是使用者輸入），這一行是縱深防禦。
    """
    validate_account_id(account_id)
    return heartbeat_dir_for(exchange_dir) / f"{account_id}.json"


def _reject_secret_material(payload: object, *, path: str = "") -> None:
    """遞迴掃描整份 payload；命中禁用鍵名或長 hex 值一律 raise（**拒寫**）。

    ⭐ 為什麼是執行期檢查而不是一條 review 準則：心跳的欄位清單會長大（下一個人要加
    「上次下單時間」「目前部位數」都很合理），而每一次加欄位都是一次「順手把整個記錄
    dict 塞進去」的機會。把它做成寫入邊界的一部分，加錯欄位的人會在第一次跑測試時
    就撞牆，而不是在正式機上把客戶的簽章複製到一個權限較低的目錄裡。
    """
    if isinstance(payload, dict):
        for k, v in payload.items():
            key = str(k)
            low = key.lower()
            hit = next((p for p in FORBIDDEN_KEY_PARTS if p in low), None)
            if hit is not None:
                raise HeartbeatRejected(
                    f"心跳含禁用欄位 {path}{key}（命中 {hit!r}）——心跳落在 API 讀得到"
                    f"的目錄，簽章／nonce／密鑰材料一律不得進入")
            _reject_secret_material(v, path=f"{path}{key}.")
        return
    if isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            _reject_secret_material(v, path=f"{path}{i}.")
        return
    if isinstance(payload, str) and _LONG_HEX_RE.search(payload):
        raise HeartbeatRejected(
            f"心跳的 {path.rstrip('.') or '<root>'} 含長 hex 字串（疑為簽章或私鑰）"
            f"——拒寫")


def build_heartbeat(*, account_id: str, now_s: float, killswitch_tripped: bool | None,
                    coverage, alerts_count: int | None,
                    leader_address: str | None, leader_source: str | None,
                    allocated_capital: str | None, capital_utilization: str | None,
                    use_full_equity: bool | None, capital_source: str,
                    capital_changed_at: str | None,
                    risk_controls_enabled: bool | None, cycle_result: str,
                    cycle_detail: str | None) -> dict:
    """組出一份心跳 payload（**唯一**的產生點，欄位順序＝HEARTBEAT_FIELDS）。

    時間同時落兩種形式，刻意的（工程原則 1）：
    - `written_at_s`（epoch 秒）是**年齡計算的唯一基準**——讀端拿自己的 `now_s`
      相減，兩側同單位、同一種時鐘語意。
    - `written_at`（UTC ISO）純粹給人看。年齡絕不從它重新解析出來：多一條解析路徑
      就多一個時區假設，而那個假設錯了的症狀是「心跳看起來永遠是新的」。

    `coverage` 收 `copytrade.equity.SampleCoverage` 或 None（讀取失敗）。這裡只投影
    四個摘要值，不投影樣本序列本身——面板要的是「夠不夠」，不是每一筆權益。

    ⭐ `alerts_count`（`killswitch.count_alerts` 的產物，`None` ＝引擎自己也讀不到）
    帶著 `known` 旗標落檔，理由與 `coverage` 完全一樣：`{"known": false}` 與
    `{"known": true, "count": 0}` 在面板上是兩件事，而後者是最令人安心的數字。
    這一格**沒有它面板就補不起來**——狀態根對 filet-api 不可讀（或路徑漂移）時，
    直讀的告警數恆為未知，心跳是唯一的來源。刻意是**必填參數**（無預設值）：
    下一個加欄位的人必須在呼叫點明講這個數從哪來，而不是繼承一個安靜的 None。
    """
    cov: dict = {"known": False, "count": None, "oldest_age_s": None,
                 "newest_age_s": None, "sufficient": None}
    if coverage is not None:
        cov = {"known": not coverage.read_error,
               "count": None if coverage.read_error else coverage.count,
               "oldest_age_s": None if coverage.read_error else coverage.oldest_age_s,
               "newest_age_s": coverage.newest_age_s,
               "sufficient": None if coverage.read_error else coverage.sufficient}
    return {
        "account_id": account_id,
        "written_at": datetime.fromtimestamp(now_s, timezone.utc).isoformat(),
        "written_at_s": float(now_s),
        "killswitch_tripped": killswitch_tripped,
        "coverage": cov,
        "alerts": {"known": alerts_count is not None, "count": alerts_count},
        "leader": {"address": leader_address, "source": leader_source},
        "capital": {"allocated_capital": allocated_capital,
                    "capital_utilization": capital_utilization,
                    "use_full_equity": use_full_equity,
                    "source": capital_source,
                    "changed_at": capital_changed_at},
        # ⭐ 風控總開關（2026-07-30）：`False` ＝ 這顆引擎的回撤 kill switch 與成本
        # 熔斷器都不執法（錢包主人自選）。放進心跳的理由與 `capital` 相同——狀態根
        # 對 filet-api 不可讀，面板沒有別的來源；而「這顆引擎沒有任何風控」正是操作者
        # 掃面板時最需要一眼看到的事。`None` ＝引擎自己也不知道（settings 尚未載入），
        # 刻意與 False 分開：面板不得把「未知」畫成「有風控」。
        "risk": {"controls_enabled": risk_controls_enabled},
        "last_cycle": {"result": cycle_result, "detail": cycle_detail},
    }


def write_heartbeat(path: str | Path, payload: dict) -> None:
    """原子落檔（tmp + os.replace，同目錄；tmp 名帶 pid，沿 equity.py `_save`）。

    ⭐ 掃描在寫入**之前**（見 `_reject_secret_material`）：命中就 raise，檔案不會產生。
    tmp 名帶 pid 的理由同 equity.py：兩個行程誤共用同一個落點時，固定 tmp 名會互相
    覆寫，rename 出去的是一份內容交錯的檔案——而讀端會把它當成一份完好的心跳。

    ⚠️ 本函式**會** raise（IO 失敗、密鑰命中）。吞例外的責任在 `publish_heartbeat`，
    刻意分兩層：測試要能看見寫入失敗的原因，正式路徑要能不被它中斷。
    """
    _reject_secret_material(payload)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def publish_heartbeat(path: str | Path, payload: dict) -> bool:
    """發布心跳；**任何失敗只 log 不 raise**，回傳是否成功落地。

    ⭐⭐ 這是「可觀測性不得中斷被觀測的系統」那條邊界的實作（工程原則 3 的反面）：
    交換目錄權限給錯、磁碟滿、子目錄沒建——這些都會讓心跳寫不出去，而它們沒有一個
    構成「應該停止管理客戶部位」的理由。已開的部位需要有人繼續管理。

    代價是誠實標註的：心跳寫不出去時，面板看到的是「心跳過期」，而那與「引擎沒跑」
    在面板上長得一樣。讀端因此一併上呈**心跳年齡**與最後心跳時刻，讓 admin 能配合
    `systemctl status` 分辨兩者（RUNBOOK §5.6）。
    """
    try:
        write_heartbeat(path, payload)
    except HeartbeatRejected:
        # ⭐ 這是**程式錯誤**（有人往心跳裡加了不該加的欄位），不是環境問題。
        # 拒寫已經生效（檔案沒產生），這裡大聲留痕；測試會在 CI 就抓到它。
        logger.exception("心跳含密鑰材料，已拒寫（跟單不受影響）")
        return False
    except (OSError, TypeError, ValueError) as e:
        logger.warning("心跳寫入失敗（%s），跟單不受影響: %r", path, e)
        return False
    return True


@dataclass(frozen=True)
class HeartbeatRead:
    """讀一份心跳的結果。

    ⭐⭐ `data` **只有在 `status == "ok"` 時非 None**——這是「過期的心跳不得被當成
    目前狀態顯示」的結構性保證，不是呼叫端要記得檢查的一條規矩。過期時呼叫端就算
    想拿那些值也拿不到；它能拿到的只有 `at` 與 `age_s`（要給人看的「上次心跳是
    什麼時候」）。

    `status`：
    - `ok`——心跳存在且在 `stale_s` 之內。
    - `stale`——心跳存在但過期（引擎沒跑，或引擎在跑但寫不進交換目錄）。
    - `missing`——沒有心跳檔（引擎從未寫過：剛 activate、或子目錄沒建）。
    - `unreadable`——檔案在但讀不出來／格式壞（**不是** missing：有東西在那裡）。
    """

    status: str
    at: str | None = None
    age_s: float | None = None
    data: dict | None = None

    @property
    def fresh(self) -> bool:
        return self.status == "ok"


def read_heartbeat(path: str | Path, now_s: float,
                   stale_s: float = HEARTBEAT_STALE_S) -> HeartbeatRead:
    """讀一份心跳並判定新鮮度。**過期一律不回傳 payload**（見 HeartbeatRead）。

    年齡＝`now_s - payload["written_at_s"]`，兩側都是 epoch 秒（工程原則 1：同源同
    單位）。`written_at_s` 缺漏或不是數值 → `unreadable`，**不是**「當成剛寫的」：
    一份算不出年齡的心跳沒辦法證明自己是新的，而預設它新鮮正好是謊報健康的方向。

    未來時刻的心跳（`age_s < 0`，時鐘跳動或手工放的檔）視為 `unreadable`：它同樣
    無法證明自己反映的是目前狀態，而「負年齡」在面板上只會讓人以為是顯示 bug。
    """
    p = Path(path)
    if not p.exists():
        return HeartbeatRead("missing")
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("心跳讀取失敗（%s）: %r", p, e)
        return HeartbeatRead("unreadable")
    if not isinstance(raw, dict):
        return HeartbeatRead("unreadable")
    written_s = raw.get("written_at_s")
    if not isinstance(written_s, (int, float)) or isinstance(written_s, bool):
        return HeartbeatRead("unreadable")
    age_s = float(now_s) - float(written_s)
    at = raw.get("written_at")
    at = at if isinstance(at, str) else None
    if age_s < 0:
        return HeartbeatRead("unreadable", at=at, age_s=age_s)
    if age_s > stale_s:
        # ⭐⭐ 過期：回時刻與年齡，**不回 data**。拿掉這一支，過期的心跳會被呼叫端
        # 當成目前狀態顯示——一份 40 分鐘前的「kill switch 未觸發」會在客戶的引擎
        # 已經熔斷的當下告訴 admin 一切正常。
        return HeartbeatRead("stale", at=at, age_s=age_s)
    return HeartbeatRead("ok", at=at, age_s=age_s, data=raw)
