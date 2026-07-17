"""tests/test_panic_all.py — 全域 panic（跨 follower）測試（M2 Task 8）。

⭐ load-bearing 斷言（2026-07-17 opus 交互審查抓出）：panic_all 對每個 follower
呼叫 trip() 時，ARM 檔必須落在該 follower **自己**的 state root
（FILET_STATE_BASE/<account_id>），不可共用單一根——否則主網出事跑
panic_all --yes 平了倉，但 ARM 落錯地方，引擎下個 cycle is_tripped()=False，
緊急平倉被一個 cycle 抹掉。見 test_panic_all_two_followers_succeed_exit_0 與
test_panic_all_arm_isolation_between_followers。

全部測試離線（Info/Exchange/HyperliquidAdapter monkeypatch 為假替身；keystore
預設假簽名，僅 test_panic_all_envfile_keystore_builds_no_key_printed 用真的
EnvFileKeyStore 讀本地 tmp_path 檔案——純檔案系統操作，非連網）。
"""
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from spark.copytrade.killswitch import ARM_FILE_RELPATH, FlattenReport
from spark.exchange.base import EquityView, OpenOrder, OrderResult, Position

# Task 1 測試已使用的離線假私鑰（非真實資金），此處沿用同一慣例。
_PK = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


def _open_order(coin="ETH", oid=1) -> OpenOrder:
    return OpenOrder(oid=oid, coin=coin, is_buy=True, limit_px=Decimal("2000"),
                     sz=Decimal("1"), reduce_only=False, is_trigger=False,
                     trigger_px=None, tpsl=None)


def _position(coin="ETH", szi="2") -> Position:
    return Position(coin=coin, szi=Decimal(szi), entry_px=Decimal("1900"), leverage=5,
                    is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("100"))


def _entry(account_id: str, addr: str, network: str = "mainnet",
          builder: str = "0x" + "b" * 40) -> dict:
    return {"account_id": account_id, "user_address": addr,
            "builder_address": builder, "network": network}


def _write_manifest(tmp_path: Path, entries: list, name: str = "followers.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"followers": entries}))
    return p


_ALICE_ADDR = "0x" + "a" * 40
_BOB_ADDR = "0x" + "c" * 40


# ── 全端假替身 fixture ──────────────────────────────────────────────────

@pytest.fixture
def fake_stack(monkeypatch, tmp_path):
    """Info/Exchange/HyperliquidAdapter 全部離線化；keystore 預設假簽名（不碰
    Keychain）。registry：user_address -> cfg dict，由各測試在呼叫 main() 前
    填好（size/close_ok/raise_on_*/oid/writes）。FILET_STATE_BASE 指到
    tmp_path/state。"""
    import scripts.panic_all as panic_all

    registry: dict[str, dict] = {}
    adapters: list = []

    class _FakeAdapter:
        def __init__(self, network, info=None, exchange=None):
            self.network = network
            self.exchange = exchange
            self._address = None
            adapters.append(self)

        def get_open_orders(self, address):
            self._address = address
            cfg = registry[address]
            if cfg.get("raise_on_open_orders"):
                raise cfg["raise_on_open_orders"]
            return [_open_order("ETH", cfg.get("oid", 1))]

        def get_positions(self, address):
            self._address = address
            cfg = registry[address]
            if cfg.get("raise_on_positions"):
                raise cfg["raise_on_positions"]
            return [_position("ETH", cfg.get("size", "2"))]

        def get_equity_view(self, address):
            self._address = address
            cfg = registry[address]
            if cfg.get("raise_on_equity"):
                raise cfg["raise_on_equity"]
            return EquityView(current=Decimal("900"), recent_peak=Decimal("1000"))

        def cancel_order(self, signer, coin, oid):
            cfg = registry[self._address]
            cfg.setdefault("writes", []).append(("cancel", coin, oid))
            return True

        def close_reduce_only(self, signer, coin, is_buy, size, slippage, builder):
            cfg = registry[self._address]
            cfg.setdefault("writes", []).append(("close", coin, is_buy, size))
            if cfg.get("close_ok", True):
                return OrderResult(ok=True, filled_size=size, avg_px=Decimal("100"), raw={})
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                               raw={"error": "rejected"})

    class _FakeKeystore:
        def get_agent_signer(self, account_id):
            return object()

    state_base = tmp_path / "state"
    monkeypatch.setenv("FILET_STATE_BASE", str(state_base))
    monkeypatch.setattr("hyperliquid.info.Info", lambda *a, **k: object())
    monkeypatch.setattr("hyperliquid.exchange.Exchange", lambda *a, **k: object())
    monkeypatch.setattr("spark.exchange.hyperliquid.HyperliquidAdapter", _FakeAdapter)
    monkeypatch.setattr(panic_all, "select_keystore", lambda: _FakeKeystore())

    return SimpleNamespace(panic_all=panic_all, registry=registry, adapters=adapters,
                           state_base=state_base, monkeypatch=monkeypatch, tmp_path=tmp_path)


def _arm_path(state_base: Path, account_id: str) -> Path:
    return state_base / account_id / ARM_FILE_RELPATH


# ── 1. 兩 follower 皆成功 ────────────────────────────────────────────────

def test_panic_all_two_followers_succeed_exit_0(fake_stack, capsys):
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry("alice", _ALICE_ADDR, "mainnet"),
        _entry("bob", _BOB_ADDR, "testnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}
    fake_stack.registry[_BOB_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code == 0

    out = capsys.readouterr().out
    assert "[alice]" in out and "[bob]" in out

    # ⭐ load-bearing：兩個 follower 的 ARM 各自落在自己的 state root，互不重疊。
    alice_arm = _arm_path(fake_stack.state_base, "alice")
    bob_arm = _arm_path(fake_stack.state_base, "bob")
    assert alice_arm.exists() and bob_arm.exists()
    alice_payload = json.loads(alice_arm.read_text())
    bob_payload = json.loads(bob_arm.read_text())
    assert alice_payload["failures"] == [] and bob_payload["failures"] == []


# ── 2. 一 follower failures 非空 → 其餘照平、退出碼非 0 ───────────────────

def test_panic_all_one_follower_failures_others_still_flattened(fake_stack, capsys):
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry("alice", _ALICE_ADDR, "mainnet"),
        _entry("bob", _BOB_ADDR, "mainnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": False}  # alice 平倉語意拒單
    fake_stack.registry[_BOB_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0

    out = capsys.readouterr().out
    assert "[alice]" in out and "失敗" in out and "ETH" in out
    # bob 沒被 alice 的失敗拖累——仍照常送出 close。
    bob_writes = [w for w in fake_stack.registry[_BOB_ADDR]["writes"] if w[0] == "close"]
    assert bob_writes == [("close", "ETH", False, Decimal("2"))]
    assert _arm_path(fake_stack.state_base, "alice").exists()
    assert _arm_path(fake_stack.state_base, "bob").exists()


# ── 3. 一 follower 連線拋例外 → 捕捉、其餘照平、退出碼非 0 ─────────────────

def test_panic_all_one_follower_connection_exception_isolated(fake_stack, capsys):
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry("alice", _ALICE_ADDR, "mainnet"),
        _entry("bob", _BOB_ADDR, "mainnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"raise_on_equity": ConnectionError("boom")}
    fake_stack.registry[_BOB_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0

    out = capsys.readouterr().out
    assert "[alice]" in out and "例外" in out
    assert "[bob]" in out and "完成" in out
    bob_writes = [w for w in fake_stack.registry[_BOB_ADDR]["writes"] if w[0] == "close"]
    assert bob_writes == [("close", "ETH", False, Decimal("2"))]
    # alice 連 equity 都讀不到——trip() 從未開始，不該有 ARM。
    assert not _arm_path(fake_stack.state_base, "alice").exists()
    assert _arm_path(fake_stack.state_base, "bob").exists()


# ── 4. manifest 壞條目 → 跳過仍平其餘、退出碼非 0 ──────────────────────────

def test_panic_all_bad_manifest_entry_skipped_still_flattens_others(fake_stack, capsys):
    bad = {"account_id": "evil", "user_address": "0xshort",
           "builder_address": "0x" + "b" * 40, "network": "mainnet"}
    manifest = _write_manifest(fake_stack.tmp_path, [bad, _entry("alice", _ALICE_ADDR)])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0

    out = capsys.readouterr().out
    assert "[MANIFEST]" in out
    assert _arm_path(fake_stack.state_base, "alice").exists()


# ── 5. dry-run：只列動作、零寫入、零 ARM ───────────────────────────────────

def test_panic_all_dry_run_lists_actions_zero_writes_zero_arm(fake_stack, capsys):
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry("alice", _ALICE_ADDR, "mainnet"),
        _entry("bob", _BOB_ADDR, "testnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {}
    fake_stack.registry[_BOB_ADDR] = {}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main([])
    assert ei.value.code == 0

    out = capsys.readouterr().out
    assert "[alice] [DRY-RUN]" in out and "[bob] [DRY-RUN]" in out
    assert "close ETH sell size=2" in out

    assert "writes" not in fake_stack.registry[_ALICE_ADDR]
    assert "writes" not in fake_stack.registry[_BOB_ADDR]
    assert not _arm_path(fake_stack.state_base, "alice").exists()
    assert not _arm_path(fake_stack.state_base, "bob").exists()


# ── 6. 退出碼＝各 follower _exit_code 跨 follower 做 OR ────────────────────

def test_panic_all_exit_code_is_or_across_followers(monkeypatch, tmp_path):
    """純聚合邏輯測試（monkeypatch run_single_follower，不經真 trip()/resilience
    重試）：alice 的 orders_not_cancelled=True（無 failures）也要讓整體退出碼非
    0，即使 bob 乾淨收場——驗證 OR 語意涵蓋 _exit_code 的兩種觸發條件。"""
    import scripts.panic_all as panic_all

    manifest = _write_manifest(tmp_path, [
        _entry("alice", _ALICE_ADDR, "mainnet"),
        _entry("bob", _BOB_ADDR, "mainnet"),
    ])
    monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    monkeypatch.setenv("FILET_STATE_BASE", str(tmp_path / "state"))
    monkeypatch.setattr(panic_all, "select_keystore", lambda: object())

    reports = {
        "alice": FlattenReport(cancelled=0, closed=(), failures=(),
                               arm_file="x", orders_not_cancelled=True),
        "bob": FlattenReport(cancelled=1, closed=("ETH",), failures=(),
                             arm_file="y", orders_not_cancelled=False),
    }

    def fake_run(*, account_id, **kwargs):
        return None, reports[account_id]

    monkeypatch.setattr(panic_all, "run_single_follower", fake_run)

    with pytest.raises(SystemExit) as ei:
        panic_all.main(["--yes"])
    assert ei.value.code != 0  # bob 乾淨也擋不住 alice 的 orders_not_cancelled 拉高整體


def test_panic_all_exit_code_zero_when_all_clean(monkeypatch, tmp_path):
    import scripts.panic_all as panic_all

    manifest = _write_manifest(tmp_path, [_entry("alice", _ALICE_ADDR, "mainnet")])
    monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    monkeypatch.setenv("FILET_STATE_BASE", str(tmp_path / "state"))
    monkeypatch.setattr(panic_all, "select_keystore", lambda: object())

    clean = FlattenReport(cancelled=1, closed=("ETH",), failures=(),
                          arm_file="x", orders_not_cancelled=False)
    monkeypatch.setattr(panic_all, "run_single_follower", lambda **kw: (None, clean))

    with pytest.raises(SystemExit) as ei:
        panic_all.main(["--yes"])
    assert ei.value.code == 0


# ── 7. envfile keystore 能正確建、不印任何 key ─────────────────────────────

def test_panic_all_envfile_keystore_builds_no_key_printed(fake_stack, capsys):
    from scripts.run_copytrade import select_keystore as real_select_keystore
    from spark.keystore.envfile import EnvFileKeyStore

    keys_dir = fake_stack.tmp_path / "keys"
    EnvFileKeyStore(keys_dir).import_agent_key("alice", _PK)

    fake_stack.monkeypatch.setattr(fake_stack.panic_all, "select_keystore", real_select_keystore)
    fake_stack.monkeypatch.setenv("FILET_KEYSTORE", "envfile")
    fake_stack.monkeypatch.setenv("FILET_KEYS_DIR", str(keys_dir))

    manifest = _write_manifest(fake_stack.tmp_path, [_entry("alice", _ALICE_ADDR, "mainnet")])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code == 0

    out = capsys.readouterr().out
    assert _PK not in out and _PK[2:] not in out
    assert _arm_path(fake_stack.state_base, "alice").exists()


# ── 8. ⭐ ARM 隔離：alice 的 panic 只落在 alice/，bob/ 不受影響 ────────────

def test_panic_all_arm_isolation_between_followers(fake_stack):
    """單一 follower（alice）manifest 下執行 --yes：alice 的 ARM 必須精確落在
    <FILET_STATE_BASE>/alice/ 之下；bob（從未在 manifest 中出現）的目錄必須
    完全不受影響——證明 state_root 是由 ref.account_id 算出而非硬編/共用根。"""
    manifest = _write_manifest(fake_stack.tmp_path, [_entry("alice", _ALICE_ADDR, "mainnet")])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code == 0

    alice_arm = _arm_path(fake_stack.state_base, "alice")
    bob_arm = _arm_path(fake_stack.state_base, "bob")
    assert alice_arm.exists(), "alice 的 ARM 必須落在她自己的 state root"
    assert not bob_arm.exists(), "bob 不在本次 manifest 中，其目錄不得被寫入任何東西"
