"""src/spark/publicapi/config.py
Public API 設定與身分衍生。
- normalize_address：所有地址比對的單一基準（0x + 40 hex 小寫）——SIWE recover vs
  nonce 綁定地址、agent vs extraAgents、admin 白名單、builder 核對，一律先過這裡
  （工程原則 1：同基準比較）。
- derive_account_id：spec 資料模型定死——"f" + 地址小寫去 0x 完整 40 hex（41 字元、
  1:1 不截斷、恆過 validate_account_id，無使用者輸入、無路徑穿越）。"""
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from spark.config import API_URLS, MIN_BUILDER_BALANCE, Settings
from spark.filet.followers import validate_account_id
from spark.filet.capital_settings import capital_settings_path_for
from spark.filet.leader_change import leader_changes_path_for
from spark.filet.leader_resolve import DEFAULT_LEADERS_PATH

_HEX = set("0123456789abcdefABCDEF")


def normalize_address(addr: str) -> str:
    if not (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42
            and all(c in _HEX for c in addr[2:])):
        raise ValueError(f"不是合法地址（0x + 40 hex）: {addr!r}")
    return addr.lower()


def derive_account_id(user_address: str) -> str:
    acct = "f" + normalize_address(user_address)[2:]
    validate_account_id(acct)  # 縱深防禦（結構上必過；單一真相沿 followers.py）
    return acct


@dataclass(frozen=True)
class ApiConfig:
    network: str                      # testnet | mainnet
    builder_address: str              # 伺服器常數，絕非使用者輸入（spec opus 審查 m3）
    siwe_domain: str                  # SIWE 訊息綁 domain/URI（防跨站釣魚重放）
    siwe_uri: str
    db_path: str
    keysvc_sock: str
    pending_path: str
    admin_addresses: frozenset[str]   # normalize 過的管理員地址白名單
    # ⭐ 客戶簽章換 leader 記錄的**專屬交換目錄**（API 寫、引擎讀，引擎無寫權）。
    # 與 pending.json 分家的理由見 leader_changes_path 的 docstring。
    # ⚠️ **刻意沒有預設值**（連 dataclass 層都沒有）：給了預設值就會有人在不知情的
    # 情況下把記錄寫到那個預設目錄去——正式部署是靜默錯位（C3 本體），測試裡則是
    # 直接寫進 repo 的工作樹。每個建構點都必須明講這個檔要落在哪。
    exchange_dir: str
    # ⭐⭐ per-follower 引擎狀態根的基底（唯讀；API 讀 skipped 小額落檔與健康直讀）。
    # **刻意沒有預設值**，與 `exchange_dir` 完全同一個處理與同一個理由
    # （見 from_env 的 required 清單與 leader_change_apply.require_exchange_dir）：
    # 這個值與引擎的 `FILET_STATE_DIR`（systemd `/opt/filet/state/%i`）是**兩份獨立
    # 推導**，兩者今天相等純粹因為兩個檔案裡的兩個字面量剛好一樣。
    # 舊版給了隱含預設 `/opt/filet/state`，於是漏設／設錯的症狀是**靜默的**：
    # API 去看一個引擎根本沒在寫的目錄，每一個狀態根都 `absent`，而面板會把
    # 「沒有 ARM 檔」讀成「kill switch 未觸發」——在引擎已經熔斷、部位已被平掉的
    # 當下告訴管理員一切正常。拿掉預設值讓漏設的那一邊**直接拒絕啟動**，
    # 「起不來」刻意優先於「起來了但面板謊報健康」。
    state_base: str
    agent_name: str = "filet"         # research：一律給名字，避開 SDK 空名刪欄位特例
    # --- 營運後台（/ops，admin only）唯讀資料來源 ---
    # followers manifest：web 層**只讀**（寫入只有人工 activate CLI，見 pending.py 檔頭）。
    # 預設值沿引擎既有慣例（scripts/filet_daily_report.py、panic_all.py 皆用此路徑）。
    followers_path: str = "var/filet/followers.json"
    # builder accrued 歷史序列（北極星實收，由 scripts/copytrade_daily_report.py 每日附加）。
    # API 進程不自己查 accrued——查一次的職責在日報腳本，這裡只讀它落下的檔（紅線：不加總）。
    accrued_history_path: str = "var/copytrade/accrued_history.jsonl"
    # --- leader 目錄（/api/leaders，session-gated）唯讀資料來源 ---
    # ⭐ 直接引用引擎的 DEFAULT_LEADERS_PATH，不另寫一個字面量預設（同源，工程原則 1）：
    # 目錄頁列出的「可選 leader」與引擎放行的清單必須是**同一個檔**。兩邊各自宣告
    # 預設路徑 → 管理端在引擎那份撤銷、API 這份仍列著他，客戶選了一個已撤銷的 leader。
    leaders_path: str = DEFAULT_LEADERS_PATH
    # watchlist 每日快照目錄（scripts/watchlist_snapshot.py 的落檔處，cron 00:10 UTC）。
    # 由 DEFAULT_LEADERS_PATH 的父目錄推導＝<repo>/var/filet/leaderboard/watchlist，
    # 對齊 watchlist_snapshot 的 <FILET_DATA_DIR>/leaderboard/watchlist 版面；
    # 一併繼承它的 repo 根錨定（絕對路徑，不隨 API 進程的 CWD 漂移）。
    watchlist_dir: str = str(
        Path(DEFAULT_LEADERS_PATH).parent / "leaderboard" / "watchlist")
    # 常數單一來源（opus 審 M4）：不重新宣告字面量，直接引用 spark.config 既有常數。
    # max_rate 無模組級常數——dataclass 的純預設值即類屬性，Settings.max_rate == "0.1%"（D6）。
    max_fee_rate: str = Settings.max_rate
    # 兩種語意共用同一鏈上門檻常數（builder 啟用門檻 100 USDC），兩個別名指向同一來源：
    min_user_deposit: Decimal = MIN_BUILDER_BALANCE     # 使用者入金門檻（status/verify funded）
    min_builder_balance: Decimal = MIN_BUILDER_BALANCE  # builder 資格門檻（payload pre-flight）
    session_ttl_s: int = 7 * 24 * 3600
    nonce_ttl_s: int = 300
    # --- M3 計費骨幹（全 optional；未設＝billing 停用，onboarding 不受影響） ---
    # ⭐ 紅線 1：只收 Stripe 測試 key（sk_test_）——真實收費是人工決策（M0 律師條款
    # 未結案），__post_init__ 結構性拒收非測試 key。repr=False：secret 不進 log/repr。
    stripe_secret_key: str | None = field(default=None, repr=False)
    stripe_webhook_secret: str | None = field(default=None, repr=False)
    stripe_price_id: str | None = None
    # 定價頁顯示字串（例："$29 / 月"）。⭐ 刻意**不納入**三元組的同設或同缺驗證：
    # 它只是顯示用字串，缺了不影響任何金流路徑（plan_catalog 回 price_display=None，
    # 前端顯示「價格待定」）。價格數字使用者尚未拍板 → 走設定不寫死在程式碼。
    stripe_price_display: str | None = None

    def __post_init__(self):
        if self.stripe_secret_key is not None and \
                not self.stripe_secret_key.startswith("sk_test_"):
            raise ValueError(
                "FILET_STRIPE_SECRET_KEY 必須是 Stripe 測試 key（sk_test_ 前綴）——"
                "真實收費是人工決策（M0 律師條款未結案），結構性拒收非測試 key")
        # 三元組同設或同缺（opus Finding 2）：不只 from_env——任何建構路徑的半開
        # 狀態（如漏 webhook secret → 驗簽必失敗）都直接拒，結構性收斂
        trio = (self.stripe_secret_key, self.stripe_webhook_secret, self.stripe_price_id)
        if any(v is not None for v in trio) and not all(v is not None for v in trio):
            raise ValueError("Stripe 設定不完整（secret key / webhook secret / price id "
                             "三個一起設或都不設）")

    @property
    def leader_changes_path(self) -> str:
        """客戶簽章的換 leader 記錄落點（filet/leader_change.py 的格式）。

        ⭐⭐ 錨在**專屬交換目錄** `exchange_dir`，不是 `pending_path`
        （2026-07-19 opus 審查 C3 的修法）：pending.json 是 **API 私有**產物（含
        活化前的客戶資料，只有 filet-api 該讀得到），變更記錄卻是 **API 與引擎共享**
        的產物。把共享物錨在私有物身上會逼兩者同目錄，而該目錄的權限只能滿足其中
        一方——實際結果是 API 寫 /var/lib/filet-api/、引擎讀 repo 內的 var/filet/，
        路徑根本不一致，功能完全不通，而 API 仍回客戶「下一個 cycle 生效」。

        API **不**在此直接改 manifest：一方面進程本就沒有寫權，另一方面繞過引擎的
        二次驗章等於把整套簽章設計退化成裝飾（leader_change.py 檔頭的威脅模型）。

        推導本身放在 `leader_change.leader_changes_path_for`：寫端（本處）與讀端
        （引擎的 leader_change_apply）必須是同一份定義，否則「API 寫 A、引擎讀 B」
        會靜默發生而兩邊看起來都正常。
        """
        return leader_changes_path_for(self.exchange_dir)

    @property
    def capital_settings_path(self) -> str:
        """客戶簽章的資金設定記錄落點（filet/capital_settings.py 的格式）。

        與 `leader_changes_path` 同一個交換目錄、同一個權限拓撲（API 寫、引擎讀），
        但**分開一個檔**：兩份記錄的讀者是兩個獨立的套用器，共用一個檔會讓其中
        一方的格式問題連坐另一方，而這兩件事各自都能造成資金損失。
        """
        return capital_settings_path_for(self.exchange_dir)

    @property
    def billing_enabled(self) -> bool:
        return self.stripe_secret_key is not None

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet"

    @property
    def api_url(self) -> str:
        return API_URLS[self.network]

    @classmethod
    def from_env(cls, env=None) -> "ApiConfig":
        env = os.environ if env is None else env
        # ⭐ FILET_EXCHANGE_DIR 是**必填**（opus 審查 I2）：它與引擎的
        # require_exchange_dir 是同一個變數的兩端，任一端漏設都會讓 API 寫的記錄
        # 引擎永遠讀不到，而兩邊的 log 都正常。刻意不給隱含預設值——漏設的那一邊
        # 直接拒絕啟動，比「起來了但功能靜默失效」早好幾天被發現。
        # ⭐ FILET_STATE_BASE 同列必填、同一個理由（2026-07-19 opus 審查 Critical）：
        # 它與引擎 unit 的 FILET_STATE_DIR 是同一條路徑的兩份推導，漏設／漂移的
        # 症狀是健康面板把每一個 follower 都讀成 absent，而 absent 曾被當成
        # 「kill switch 未觸發」上呈。詳見 `state_base` 欄位與 ops.state_root_status。
        required = ["FILET_API_NETWORK", "FILET_BUILDER_ADDR", "FILET_SIWE_DOMAIN",
                    "FILET_SIWE_URI", "FILET_API_DB", "FILET_KEYSVC_SOCK",
                    "FILET_PENDING_PATH", "FILET_EXCHANGE_DIR", "FILET_STATE_BASE"]
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ValueError(f"缺少環境變數: {', '.join(missing)}")
        network = env["FILET_API_NETWORK"]
        if network not in API_URLS:
            raise ValueError(f"unknown network: {network}")
        admins = frozenset(normalize_address(a.strip())
                           for a in env.get("FILET_ADMIN_ADDRESSES", "").split(",")
                           if a.strip())
        stripe_env = {k: (env.get(k) or None)
                      for k in ("FILET_STRIPE_SECRET_KEY", "FILET_STRIPE_WEBHOOK_SECRET",
                                "FILET_STRIPE_PRICE_ID")}
        present = [k for k, v in stripe_env.items() if v]
        if present and len(present) != 3:
            missing = sorted(set(stripe_env) - set(present))
            raise ValueError(
                f"Stripe 設定不完整（三個一起設或都不設）: 缺少 {', '.join(missing)}")
        # followers manifest 路徑：優先 FILET_FOLLOWERS_PATH，回退引擎既有的
        # FILET_FOLLOWERS（filet_daily_report/panic_all 用的同一個變數）——同一份檔案
        # 兩個 env 名是營運誤設的溫床，這裡讓一個變數就能同時餵引擎與 API。
        followers_path = (env.get("FILET_FOLLOWERS_PATH") or env.get("FILET_FOLLOWERS")
                          or cls.followers_path)
        # leader 白名單：沿引擎與 activate CLI 既有的 FILET_LEADERS_PATH（同一變數餵三方，
        # 同上「一個變數就不會半邊誤設」的理由）。
        leaders_path = env.get("FILET_LEADERS_PATH") or cls.leaders_path
        # watchlist 快照目錄：優先顯式 FILET_WATCHLIST_DIR，否則由 cron 用的
        # FILET_DATA_DIR 推導出同一個版面（<data>/leaderboard/watchlist），
        # 兩邊都未設才用類預設——避免「cron 寫 A、API 讀 B」的靜默錯位。
        data_dir = env.get("FILET_DATA_DIR")
        watchlist_dir = (env.get("FILET_WATCHLIST_DIR")
                         or (str(Path(data_dir) / "leaderboard" / "watchlist")
                             if data_dir else cls.watchlist_dir))
        return cls(network=network,
                   builder_address=normalize_address(env["FILET_BUILDER_ADDR"]),
                   siwe_domain=env["FILET_SIWE_DOMAIN"],
                   siwe_uri=env["FILET_SIWE_URI"],
                   db_path=env["FILET_API_DB"],
                   keysvc_sock=env["FILET_KEYSVC_SOCK"],
                   pending_path=env["FILET_PENDING_PATH"],
                   exchange_dir=env["FILET_EXCHANGE_DIR"],
                   followers_path=followers_path,
                   accrued_history_path=(env.get("FILET_ACCRUED_HISTORY_PATH")
                                         or cls.accrued_history_path),
                   state_base=env["FILET_STATE_BASE"],
                   leaders_path=leaders_path,
                   watchlist_dir=watchlist_dir,
                   admin_addresses=admins,
                   stripe_secret_key=stripe_env["FILET_STRIPE_SECRET_KEY"],
                   stripe_webhook_secret=stripe_env["FILET_STRIPE_WEBHOOK_SECRET"],
                   stripe_price_id=stripe_env["FILET_STRIPE_PRICE_ID"],
                   # 獨立讀取（不進 stripe_env）：納入 stripe_env 會被上面的
                   # 「三個一起設」檢查算成第四個成員，讓純顯示字串能擋下啟動
                   stripe_price_display=env.get("FILET_STRIPE_PRICE_DISPLAY") or None)
