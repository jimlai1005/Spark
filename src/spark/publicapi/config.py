"""src/spark/publicapi/config.py
Public API 設定與身分衍生。
- normalize_address：所有地址比對的單一基準（0x + 40 hex 小寫）——SIWE recover vs
  nonce 綁定地址、agent vs extraAgents、admin 白名單、builder 核對，一律先過這裡
  （工程原則 1：同基準比較）。
- derive_account_id：spec 資料模型定死——"f" + 地址小寫去 0x 完整 40 hex（41 字元、
  1:1 不截斷、恆過 validate_account_id，無使用者輸入、無路徑穿越）。"""
import os
from dataclasses import dataclass
from decimal import Decimal

from spark.config import API_URLS, MIN_BUILDER_BALANCE, Settings
from spark.filet.followers import validate_account_id

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
    agent_name: str = "filet"         # research：一律給名字，避開 SDK 空名刪欄位特例
    # 常數單一來源（opus 審 M4）：不重新宣告字面量，直接引用 spark.config 既有常數。
    # max_rate 無模組級常數——dataclass 的純預設值即類屬性，Settings.max_rate == "0.1%"（D6）。
    max_fee_rate: str = Settings.max_rate
    # 兩種語意共用同一鏈上門檻常數（builder 啟用門檻 100 USDC），兩個別名指向同一來源：
    min_user_deposit: Decimal = MIN_BUILDER_BALANCE     # 使用者入金門檻（status/verify funded）
    min_builder_balance: Decimal = MIN_BUILDER_BALANCE  # builder 資格門檻（payload pre-flight）
    session_ttl_s: int = 7 * 24 * 3600
    nonce_ttl_s: int = 300

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet"

    @property
    def api_url(self) -> str:
        return API_URLS[self.network]

    @classmethod
    def from_env(cls, env=None) -> "ApiConfig":
        env = os.environ if env is None else env
        required = ["FILET_API_NETWORK", "FILET_BUILDER_ADDR", "FILET_SIWE_DOMAIN",
                    "FILET_SIWE_URI", "FILET_API_DB", "FILET_KEYSVC_SOCK",
                    "FILET_PENDING_PATH"]
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ValueError(f"缺少環境變數: {', '.join(missing)}")
        network = env["FILET_API_NETWORK"]
        if network not in API_URLS:
            raise ValueError(f"unknown network: {network}")
        admins = frozenset(normalize_address(a.strip())
                           for a in env.get("FILET_ADMIN_ADDRESSES", "").split(",")
                           if a.strip())
        return cls(network=network,
                   builder_address=normalize_address(env["FILET_BUILDER_ADDR"]),
                   siwe_domain=env["FILET_SIWE_DOMAIN"],
                   siwe_uri=env["FILET_SIWE_URI"],
                   db_path=env["FILET_API_DB"],
                   keysvc_sock=env["FILET_KEYSVC_SOCK"],
                   pending_path=env["FILET_PENDING_PATH"],
                   admin_addresses=admins)
