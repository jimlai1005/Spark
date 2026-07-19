"""tests/publicapi_helpers.py — Public API 測試共用件（非測試檔）。
FakeKeysvc / FakeHL 是唯二的外部依賴替身；SIWE 與 EIP-712 簽名用真密碼學。"""
import secrets
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_defunct

from spark.keysvc.client import KeysvcError
from spark.publicapi.app import create_app
from spark.publicapi.config import ApiConfig
from spark.publicapi.store import ApiStore

BUILDER = "0x" + "b1" * 20


def make_cfg(tmp_path, **over):
    base = dict(network="testnet", builder_address=BUILDER,
                siwe_domain="filet.example", siwe_uri="https://filet.example",
                db_path=str(tmp_path / "api.db"),
                keysvc_sock=str(tmp_path / "keysvc.sock"),
                pending_path=str(tmp_path / "pending.json"),
                # ⭐ 交換目錄與 pending 分家（C3 修法）：共享產物有自己的目錄，
                # 測試也照這個拓撲擺，才會真的走到「兩個不同目錄」的路徑。
                exchange_dir=str(tmp_path / "exchange"),
                # ⭐ 與 exchange_dir 同樣是**必填無預設**（漏設即拒絕啟動）：測試
                # 也逐個明講狀態根在哪，才會真的走到「API 與引擎兩份路徑推導」的路徑。
                state_base=str(tmp_path / "state"),
                # ⭐ leader 白名單同樣**必填無預設**（2026-07-20，同模式第三次）。
                # 釘在 tmp_path 還有第二個作用（工程原則 4）：舊版的類預設是
                # DEFAULT_LEADERS_PATH ＝ **repo 工作樹裡的** var/filet/leaders.json，
                # 只要有人在本機建了那個檔，沒指定白名單的測試就會靜默改讀真實營運資料。
                leaders_path=str(tmp_path / "leaders.json"),
                admin_addresses=frozenset())
    base.update(over)
    return ApiConfig(**base)


class FakeKeysvc:
    """模擬 KeysvcClient：鏡像真 client 的 KeysvcError.code 行為（"exists"/"missing"），
    generate 一次成功、重呼 code="exists"（O_EXCL 語意）、address 唯讀；可注入失敗。"""

    def __init__(self):
        self.generated: dict[str, str] = {}
        self.fail: Exception | None = None          # generate 的注入失敗
        self.address_fail: Exception | None = None  # address 的注入失敗

    def generate(self, account_id: str) -> str:
        if self.fail is not None:
            raise self.fail
        if account_id in self.generated:
            raise KeysvcError(f"keysvc 失敗: account {account_id} 已有 agent key",
                              code="exists")
        addr = "0x" + secrets.token_hex(20)
        self.generated[account_id] = addr
        return addr

    def address(self, account_id: str) -> str:
        if self.address_fail is not None:
            raise self.address_fail
        if account_id not in self.generated:
            raise KeysvcError(f"keysvc 失敗: account {account_id} 無 agent key",
                              code="missing")
        return self.generated[account_id]


class FakeHL:
    """模擬 HLGateway（唯讀）；鏈上狀態由測試直接塞（模擬前端直送 HL 後授權上鏈）。
    鍵一律小寫（同 normalize 基準）。刻意與真 HLGateway 同面：無任何提交方法。"""

    def __init__(self):
        self.account_values: dict[str, Decimal] = {}
        self.max_fees: dict[tuple[str, str], int] = {}
        self.agents: dict[str, list[str]] = {}
        # ops（跨客戶聚合）用：per-address fills ＋ 可注入的 per-address 查詢失敗，
        # 讓「一個客戶壞不影響其他客戶」能被實測而非口頭保證。
        self.fills: dict[str, list] = {}
        self.fills_error: dict[str, Exception] = {}
        self.account_value_error: dict[str, Exception] = {}
        # spot「卡住資金」偵測用：per-address spot USDC ＋ 可注入的查詢失敗。
        # 預設 0＝沒有卡住的錢（多數測試不在意這條路徑）。
        self.spot_usdc: dict[str, Decimal] = {}
        self.spot_error: dict[str, Exception] = {}
        # 預設「塞什麼就回什麼」（多數測試不在意窗口）。收入對帳的窗口正確性測試
        # 需要真的依 [start, end] 過濾——設 True 打開，否則「窗口取錯」在 fake 上
        # 看不出來（正是 opus 對抗審查 Critical 能潛伏的原因）。
        self.window_aware = False

    def get_account_value(self, address: str) -> Decimal:
        err = self.account_value_error.get(address.lower())
        if err is not None:
            raise err
        return self.account_values.get(address.lower(), Decimal("0"))

    def get_user_fills(self, address: str, start, end) -> list:
        err = self.fills_error.get(address.lower())
        if err is not None:
            raise err
        fills = list(self.fills.get(address.lower(), []))
        if self.window_aware:
            fills = [f for f in fills if start <= f.time <= end]
        return fills

    def spot_usdc_balance(self, address: str) -> Decimal:
        err = self.spot_error.get(address.lower())
        if err is not None:
            raise err
        return self.spot_usdc.get(address.lower(), Decimal("0"))

    def max_builder_fee(self, user: str, builder: str) -> int:
        return self.max_fees.get((user.lower(), builder.lower()), 0)

    def agent_addresses(self, user: str) -> list[str]:
        return [a.lower() for a in self.agents.get(user.lower(), [])]


def make_app(tmp_path, cfg=None, billing=None, now_fn=None):
    """now_fn 可注入假時鐘（TTL 類測試用）——不給就走 create_app 的預設 time.time。"""
    cfg = cfg or make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    kw = {"billing": billing} if now_fn is None else {"billing": billing,
                                                      "now_fn": now_fn}
    return create_app(cfg, store, keysvc, hl, **kw), cfg, store, keysvc, hl


def login(client, wallet=None):
    """完整 SIWE 登入（真密碼學）。session cookie 落在 client 的 cookie jar。"""
    wallet = wallet or Account.create()
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    assert r.status_code == 200, r.text
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return wallet


# ---------- billing 測試共用（M3） ----------
STRIPE_WEBHOOK_SECRET = "whsec_test_secret"


def billing_cfg(tmp_path, **over):
    """stripe 三元組齊備的 cfg。預設用 dict 合併（不是直接傳 kwarg）——呼叫端才能
    覆寫 stripe_price_id 等預設值而不撞上 "multiple values for keyword argument"。"""
    base = dict(stripe_secret_key="sk_test_abc",
                stripe_webhook_secret=STRIPE_WEBHOOK_SECRET,
                stripe_price_id="price_test_1")
    base.update(over)
    return make_cfg(tmp_path, **base)


def stripe_sig(payload: bytes, secret: str = STRIPE_WEBHOOK_SECRET,
               t: int | None = None) -> str:
    import hashlib
    import hmac
    import time as _time
    t = int(_time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"
