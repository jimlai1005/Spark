"""src/spark/publicapi/store.py
API 狀態落地（SQLite 單檔，spec 資料模型）：SIWE nonce（單次使用）、session、
onboarding 進度。金鑰/簽名/typed data 一律不落地——前端持有 typed data、簽完
直送 HL（設計定案 1），本表只存地址與進度。"""
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from spark.filet.followers import validate_account_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    chain_id INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    expiry REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    expiry REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS onboarding (
    account_id TEXT PRIMARY KEY,
    user_address TEXT NOT NULL,
    agent_address TEXT
);
CREATE TABLE IF NOT EXISTS billing (
    account_id TEXT PRIMARY KEY,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    updated_at REAL NOT NULL DEFAULT 0,
    last_event_created INTEGER NOT NULL DEFAULT 0,
    last_event_id TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class NonceRecord:
    address: str
    chain_id: int
    issued_at: str


BILLING_STATUSES = frozenset({"none", "active", "past_due", "canceled"})

# entitlement 高低（opus 總審 F1）：同秒平手時，只允許往低權益方向套用——
# active 不得覆寫同秒的 canceled/past_due。none 與 canceled 同級（都是無權益）。
_ENTITLEMENT_RANK = {"none": 0, "canceled": 0, "past_due": 1, "active": 2}


@dataclass(frozen=True)
class BillingRecord:
    account_id: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    status: str
    updated_at: float
    last_event_created: int  # 已套用的最新 Stripe event.created（亂序守衛水位）
    last_event_id: str       # 已套用的最新 Stripe event.id（重放冪等水位，opus 總審 F1）


class ApiStore:
    """單一連線 + lock（FastAPI handler 跑 threadpool，需 thread-safe）。"""

    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            self._migrate_billing_last_event_id()

    def _migrate_billing_last_event_id(self) -> None:
        """additive column migration（opus 總審 F1）：`CREATE TABLE IF NOT EXISTS`
        不會幫已存在的舊 billing 表補新欄——舊表（此欄新增前建立）重開時明確補上，
        不炸、預設空字串（等同「從未記錄過重放水位」）。呼叫端已持鎖（__init__）。"""
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(billing)")}
        if "last_event_id" not in cols:
            self._db.execute(
                "ALTER TABLE billing ADD COLUMN last_event_id TEXT NOT NULL DEFAULT ''")

    # --- SIWE nonce（單次使用） ---
    def issue_nonce(self, address: str, chain_id: int, issued_at: str,
                    *, now_s: float, ttl_s: int) -> str:
        nonce = secrets.token_hex(16)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO nonces (nonce, address, chain_id, issued_at, expiry) "
                "VALUES (?, ?, ?, ?, ?)",
                (nonce, address, chain_id, issued_at, now_s + ttl_s))
        return nonce

    def consume_nonce(self, nonce: str, *, now_s: float) -> NonceRecord | None:
        """單次使用的結構性保證：原子 UPDATE consumed 0→1，rowcount != 1 即
        「不存在／已用過／已過期」一律 None——不是先查再改的 TOCTOU。"""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE nonces SET consumed = 1 "
                "WHERE nonce = ? AND consumed = 0 AND expiry > ?", (nonce, now_s))
            if cur.rowcount != 1:
                return None
            row = self._db.execute(
                "SELECT address, chain_id, issued_at FROM nonces WHERE nonce = ?",
                (nonce,)).fetchone()
        return NonceRecord(address=row[0], chain_id=row[1], issued_at=row[2])

    # --- session ---
    def create_session(self, address: str, *, now_s: float, ttl_s: int) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions (session_id, address, expiry) VALUES (?, ?, ?)",
                (sid, address, now_s + ttl_s))
        return sid

    def get_session_address(self, session_id: str, *, now_s: float) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT address FROM sessions WHERE session_id = ? AND expiry > ?",
                (session_id, now_s)).fetchone()
        return row[0] if row else None

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # --- onboarding 進度 ---
    def ensure_onboarding(self, account_id: str, user_address: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO onboarding (account_id, user_address) VALUES (?, ?)",
                (account_id, user_address))

    def get_agent_address(self, account_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT agent_address FROM onboarding WHERE account_id = ?",
                (account_id,)).fetchone()
        return row[0] if row else None

    def set_agent_address(self, account_id: str, agent_address: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE onboarding SET agent_address = ? WHERE account_id = ?",
                             (agent_address, account_id))

    # --- billing（M3 計費骨幹；無金額/卡號等敏感資料——紅線 7） ---
    def get_billing(self, account_id: str) -> BillingRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT account_id, stripe_customer_id, stripe_subscription_id, "
                "status, updated_at, last_event_created, last_event_id FROM billing "
                "WHERE account_id = ?", (account_id,)).fetchone()
        return BillingRecord(*row) if row else None

    def get_billing_by_subscription(self, subscription_id: str) -> BillingRecord | None:
        """webhook subscription 事件無 metadata 時的 fallback 對應（設計定案 4）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT account_id, stripe_customer_id, stripe_subscription_id, "
                "status, updated_at, last_event_created, last_event_id FROM billing "
                "WHERE stripe_subscription_id = ?", (subscription_id,)).fetchone()
        return BillingRecord(*row) if row else None

    def upsert_billing(self, account_id: str, *, status: str,
                       stripe_customer_id: str | None = None,
                       stripe_subscription_id: str | None = None,
                       now_s: float, event_created: int = 0, event_id: str = "") -> None:
        """upsert：**拆開「去重」與「順序」**（opus 總審 F1——原本共用一條 `>=` 既服務
        重放冪等也服務亂序守衛，兩個目的疊在同一個運算子上留下同秒縫：Stripe
        `event.created` 是秒級精度、不保證投遞順序，同秒的 canceled 與 active 若
        active 晚到，舊寫法會把已取消訂閱覆寫回 active）。
        - **重放冪等靠 `event_id`**：`event_id` 非空且等於既有列的 `last_event_id`
          → 整筆 no-op（含 `updated_at` 都不動）——同一事件重送，狀態原地不動。
          `event_id` 未帶（""）時略過此判斷（相容不帶 id 的呼叫端／直接呼叫）。
        - **順序守衛靠 `event_created` 嚴格比較**：較新 `event_created` 一律套用；
          較舊整筆 no-op（已取消訂閱不因晚到的舊 active 事件復活）。
        - **同秒平手（`event_created` 相等、且不是同 `event_id` 重放）偏向低權益**：
          新狀態的 entitlement rank（`_ENTITLEMENT_RANK`）<= 既有 rank 才套用——
          active 不得覆寫同秒已存在的 canceled/past_due，但 canceled 可覆寫同秒 active
          （降級允許、升級拒絕）。
        決策邏輯在 lock 內以 Python read-then-write 判斷——既有 `self._lock` 已序列化
        所有存取，鎖內無競態；比複雜 SQL WHERE 清楚（opus 總審 F1）。
        id 欄（customer/subscription）為 None 時 COALESCE 保留既有值。status 白名單
        強制——webhook 映射層是唯一寫入者，這裡是縱深防禦。
        account_id 在此驗證（單一邊界，工程原則 5）：account_id 會流進檔案路徑
        （keystore、systemd %i），所有呼叫端（含未來新增）都繞不開這層。"""
        validate_account_id(account_id)
        if status not in BILLING_STATUSES:
            raise ValueError(f"未知 billing status: {status!r}（須為 {sorted(BILLING_STATUSES)}）")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT stripe_customer_id, stripe_subscription_id, status, "
                "last_event_created, last_event_id FROM billing WHERE account_id = ?",
                (account_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO billing (account_id, stripe_customer_id, "
                    "stripe_subscription_id, status, updated_at, last_event_created, "
                    "last_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_id, stripe_customer_id, stripe_subscription_id, status,
                     now_s, event_created, event_id))
                return
            cur_customer, cur_sub, cur_status, cur_created, cur_event_id = row
            if event_id != "" and event_id == cur_event_id:
                return  # 重放冪等：同 event id，整筆 no-op（含病態的同 id 異狀態重放）
            if event_created > cur_created:
                apply_ = True
            elif event_created == cur_created:
                apply_ = _ENTITLEMENT_RANK[status] <= _ENTITLEMENT_RANK[cur_status]
            else:
                apply_ = False
            if not apply_:
                return
            new_customer = (stripe_customer_id if stripe_customer_id is not None
                            else cur_customer)
            new_sub = (stripe_subscription_id if stripe_subscription_id is not None
                      else cur_sub)
            self._db.execute(
                "UPDATE billing SET stripe_customer_id = ?, stripe_subscription_id = ?, "
                "status = ?, updated_at = ?, last_event_created = ?, last_event_id = ? "
                "WHERE account_id = ?",
                (new_customer, new_sub, status, now_s, event_created, event_id,
                 account_id))
