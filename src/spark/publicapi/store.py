"""src/spark/publicapi/store.py
API 狀態落地（SQLite 單檔，spec 資料模型）：SIWE nonce（單次使用）、session、
onboarding 進度。金鑰/簽名/typed data 一律不落地——前端持有 typed data、簽完
直送 HL（設計定案 1），本表只存地址與進度。"""
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

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
"""


@dataclass(frozen=True)
class NonceRecord:
    address: str
    chain_id: int
    issued_at: str


class ApiStore:
    """單一連線 + lock（FastAPI handler 跑 threadpool，需 thread-safe）。"""

    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

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
