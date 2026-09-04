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
                # I-17：探索榜磁碟快照路徑同樣釘進 tmp_path——不釘會落回
                # dataclass 預設的相對路徑 `var/copytrade/explore_index.json`，
                # 任何直接呼叫 `ExploreIndex.build_sync()`（見 test_public_explore.py
                # `_app()` helper）的測試就會真的寫進 repo 工作樹（工程原則：
                # 測試不得碰真實世界）。
                explore_cache_path=str(tmp_path / "explore_index.json"),
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
        # 自訂 leader 准入預覽用：per-address clearinghouseState ＋ 可注入失敗。
        # 預設「空帳戶」（權益 0、無持倉）＝預覽回 exists=false 的那一側（放行帶警示）。
        self.clearinghouse: dict[str, dict] = {}
        self.clearinghouse_error: dict[str, Exception] = {}
        # vault 自動偵測用：per-address vaultDetails fixture ＋ 可注入失敗。
        # ⭐ 預設 **None**——真實 API 對非 vault 位址回 JSON null（_info → None），
        # 這正是「絕大多數位址不是 vault」的那一側；vault 情境由測試顯式注入。
        self.vaults: dict[str, dict] = {}
        self.vault_details_error: dict[str, Exception] = {}
        # vault advisory 檢查的資料面（同 preflight）：portfolio ＋ ledger fixture。
        # 預設空（塞什麼回什麼的既有慣例）；只有 vault 位址會被查到這兩份。
        self.portfolios: dict[str, list] = {}
        self.portfolio_error: dict[str, Exception] = {}
        self.ledger_updates: dict[str, list] = {}
        # M3 round4 Task R4-2（真實入金查詢）：per-address 可注入失敗，同其他
        # 查詢的既有 `_error` 慣例——上游任何失敗只降級 `initial_deposit_usd`
        # 為 None，不得拖累整頁。
        self.ledger_updates_error: dict[str, Exception] = {}
        # 預設「塞什麼就回什麼」（多數測試不在意窗口）。收入對帳的窗口正確性測試
        # 需要真的依 [start, end] 過濾——設 True 打開，否則「窗口取錯」在 fake 上
        # 看不出來（正是 opus 對抗審查 Critical 能潛伏的原因）。
        self.window_aware = False
        # 自助查帳 tab（M3 round2 Task 7）用：per-address 裁切成交明細 ＋ explorer
        # userDetails 原始 payload，各自可注入失敗（HL/explorer 各自獨立的上游）。
        self.fills_detail: dict[str, list] = {}
        self.fills_detail_error: dict[str, Exception] = {}
        # 探索清單／交易員詳情（hl_explore Task 3，2026-09-05）用：per-address
        # **原始** HL fills dict（含 dir/oid/startPosition/closedPnl，未經
        # `_fill_detail_dict` 裁切）＋可注入失敗，同其餘查詢的既有 `_error`
        # 慣例——鏡射真實 `HLGateway.get_fills_raw_paged`（見 hl.py Task 3a）。
        self.fills_raw: dict[str, list] = {}
        self.fills_raw_error: dict[str, Exception] = {}
        # Task 8 Step 4（2026-09-05，reviewer Warning 3：max_pages 單一來源）：
        # 記錄每次呼叫實際收到的 `(address, max_pages)`，讓測試能斷言探索與
        # 詳情兩條路徑真的讀到同一個 env 值，不必間接推論。
        self.fills_raw_calls: list[tuple[str, int | None]] = []
        self.user_details_payload: dict[str, dict] = {}
        self.user_details_error: dict[str, Exception] = {}
        # I-19（EquityCurve overlay benchmarks）：per-coin K 線 fixture ＋可注入
        # 失敗，同其餘查詢的既有 `_error` 慣例。鍵是 `coin`（`hl.candle_snapshot`
        # 的第一個參數，如 `"BTC"`／`"xyz:SP500"`），不做大小寫正規化——真實代號
        # 本身就有大小寫混合（`xyz:SP500`），normalize 反而會失真。
        self.candles: dict[str, list] = {}
        self.candle_error: dict[str, Exception] = {}

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

    def get_user_fills_paged(self, address: str, start, end, *, max_pages=None):
        """R-A（2026-08-30，C2/C3 修法）：`app.py` 的費用明細改吃這個分頁介面。
        `FakeHL` 的測試 fixture 從不模擬超過 2000 筆的真實分頁/截斷情境（那條
        路徑由 `tests/test_publicapi_hl.py` 直接對 `HLGateway` 單測）——這裡單純
        委派給既有 `get_user_fills`（同一份 window_aware／fills_error 語意），
        回傳 `truncated=False`，讓所有既有呼叫端與測試行為不變。"""
        return self.get_user_fills(address, start, end), False

    def spot_usdc_balance(self, address: str) -> Decimal:
        err = self.spot_error.get(address.lower())
        if err is not None:
            raise err
        return self.spot_usdc.get(address.lower(), Decimal("0"))

    def clearinghouse_state(self, address: str) -> dict:
        err = self.clearinghouse_error.get(address.lower())
        if err is not None:
            raise err
        return self.clearinghouse.get(
            address.lower(),
            {"marginSummary": {"accountValue": "0.0"}, "assetPositions": []})

    def vault_details(self, vault_address: str):
        err = self.vault_details_error.get(vault_address.lower())
        if err is not None:
            raise err
        return self.vaults.get(vault_address.lower())   # 非 vault → None（真實 API 行為）

    def portfolio(self, address: str) -> list:
        err = self.portfolio_error.get(address.lower())
        if err is not None:
            raise err
        return self.portfolios.get(address.lower(), [])

    def non_funding_ledger_updates(self, user: str, start_ms: int) -> list:
        err = self.ledger_updates_error.get(user.lower())
        if err is not None:
            raise err
        return self.ledger_updates.get(user.lower(), [])

    def max_builder_fee(self, user: str, builder: str) -> int:
        return self.max_fees.get((user.lower(), builder.lower()), 0)

    def agent_addresses(self, user: str) -> list[str]:
        return [a.lower() for a in self.agents.get(user.lower(), [])]

    def get_fills_detail(self, address: str, start, end) -> list[dict]:
        err = self.fills_detail_error.get(address.lower())
        if err is not None:
            raise err
        return list(self.fills_detail.get(address.lower(), []))

    def get_fills_detail_paged(self, address: str, start, end, *, max_pages=None):
        """I-18：`/api/me/fills` 改吃這個分頁介面。`FakeHL` 同 `get_user_fills_paged`
        的既有簡化慣例——單頁測試委派給 `get_fills_detail`（同一份
        `fills_detail`／`fills_detail_error` 語意），回傳 `truncated=False`；
        真正跨 2000 筆邊界的分頁行為由 `tests/test_publicapi_hl.py` 對
        `HLGateway` 直接單測。"""
        return self.get_fills_detail(address, start, end), False

    def get_fills_raw_paged(self, address: str, start, end, *, max_pages=None):
        """`HLGateway.get_fills_raw_paged`（Task 3a）的假版：同 `get_fills_detail_paged`
        的既有簡化慣例——單頁測試委派給 `fills_raw`（未裁切原始形狀）字典，
        回傳 `truncated=False`；真正跨頁截斷行為由 `tests/test_publicapi_hl.py`
        對 `HLGateway` 直接單測。每次呼叫記進 `fills_raw_calls`（Task 8 Step 4）。"""
        self.fills_raw_calls.append((address.lower(), max_pages))
        err = self.fills_raw_error.get(address.lower())
        if err is not None:
            raise err
        return list(self.fills_raw.get(address.lower(), [])), False

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list:
        err = self.candle_error.get(coin)
        if err is not None:
            raise err
        return self.candles.get(coin, [])

    def user_details(self, address: str) -> dict:
        err = self.user_details_error.get(address.lower())
        if err is not None:
            raise err
        return self.user_details_payload.get(address.lower(), {"txs": []})


def make_app(tmp_path, cfg=None, billing=None, now_fn=None, notifier=None, mailer=None):
    """now_fn 可注入假時鐘（TTL 類測試用）——不給就走 create_app 的預設 time.time。
    notifier 可注入 RecordingNotifier（vault advisory 告警測試用）——不給就走
    create_app 的預設（TG 鍵缺席 → NullNotifier，log-only）。
    mailer 可注入 FakeMailer（/contact 測試用）——不給就走 create_app 的預設
    （cfg 有 SMTP 設定才建 SmtpMailer，否則 None）。"""
    cfg = cfg or make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    kw = {"billing": billing} if now_fn is None else {"billing": billing,
                                                      "now_fn": now_fn}
    if notifier is not None:
        kw["notifier"] = notifier
    if mailer is not None:
        kw["mailer"] = mailer
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
