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

from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import ARM_FILE_RELPATH, FlattenReport
from spark.copytrade.notifier import RecordingNotifier
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
            self._pos_calls = 0  # 供 positions_fail_times（transient 重試模擬）計數
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
                raise cfg["raise_on_positions"]  # 每次都拋（模擬持續失敗）
            self._pos_calls += 1
            if self._pos_calls <= cfg.get("positions_fail_times", 0):
                raise TimeoutError(f"positions timeout attempt {self._pos_calls}")
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
    """建構/連線例外（Exchange 建構拋例外，非前置讀取——讀取已 degrade 見 F2 測試）
    → 外層 try/except 捕捉、critical、其餘照平、退出碼非 0，該 follower 無 ARM。"""
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry("alice", _ALICE_ADDR, "mainnet"),
        _entry("bob", _BOB_ADDR, "mainnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}
    fake_stack.registry[_BOB_ADDR] = {"close_ok": True}

    # Exchange 建構對 alice 拋連線例外（發生在任何 trip 動作之前）。
    def exchange_factory(*a, **k):
        if k.get("account_address") == _ALICE_ADDR:
            raise ConnectionError("boom building exchange")
        return object()

    fake_stack.monkeypatch.setattr("hyperliquid.exchange.Exchange", exchange_factory)

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0

    out = capsys.readouterr().out
    assert "[alice]" in out and "例外" in out
    assert "[bob]" in out and "完成" in out
    bob_writes = [w for w in fake_stack.registry[_BOB_ADDR]["writes"] if w[0] == "close"]
    assert bob_writes == [("close", "ETH", False, Decimal("2"))]
    # alice 的 Exchange 建構就炸——trip() 從未開始，不該有 ARM。
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
    assert "[CRITICAL]" in out  # 壞條目升為 critical（F4）；細節斷言見 F4 專測
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


# ── F1（opus Critical + re-review）：account_id 路徑穿越 → 拒絕、非 0、零 ARM ──

@pytest.mark.parametrize("evil_id", ["../bob", "/etc/x", "..", "alice/../bob"])
def test_panic_all_path_traversal_rejected_no_arm_outside_base(fake_stack, capsys, evil_id):
    """account_id 含 ../、絕對路徑、..、或 X/../Y 互消（opus re-review 追加的
    「base 內兄弟目錄誤導」）→ 兩層防線任一擋下都必須拒絕該 follower（不寫任何
    ARM）、critical、退出碼非 0；同 manifest 的合法 follower（alice）仍照常平倉；
    resolve 目標不得出現任何 ARM 檔。

    M2 Task 10 起，這些字元（`/`、`..`）先在 manifest load boundary 被
    `validate_account_id` 擋下（`load_followers_tolerant` 收進 errors、不再進
    refs）——比 `_state_root_for` 的 resolve-後檢查更早一層，兩者是縱深防禦的
    兩道獨立防線，本測試斷言外層（load boundary）攔截的訊息與效果；
    `_state_root_for` 本身仍是給繞過 followers.py 直呼叫的呼叫端的第二道防線
    （見該函式 docstring：「不倚賴上游驗證」）。"""
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry(evil_id, _BOB_ADDR, "mainnet"),
        _entry("alice", _ALICE_ADDR, "mainnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}
    fake_stack.registry[_BOB_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0, "路徑穿越的 follower 必須拉高退出碼（不得假成功 exit 0）"

    out = capsys.readouterr().out
    assert "[CRITICAL]" in out and "完全未被平倉" in out
    # resolve 目標（base 外的兄弟/絕對路徑，或 base 內被誤導的別人目錄）不得被寫 ARM。
    escaped = (Path(str(fake_stack.state_base)) / evil_id).resolve()
    assert not (escaped / ARM_FILE_RELPATH).exists(), "穿越/誤導目標不得被寫入 ARM"
    # 合法 follower 不受連累，照常平倉。
    assert _arm_path(fake_stack.state_base, "alice").exists()


def test_panic_all_sibling_misdirection_isolated_from_real_follower(fake_stack, capsys):
    """opus re-review 追加：惡意 entry account_id="alice/../bob" resolve 成
    base/bob，parent 檢查通過但會與**真正的** bob follower 共寫同一 killswitch.tripped。
    守衛須擋下惡意 entry（此 manifest 無真 bob）→ base/bob 完全不被寫 ARM，證明
    兄弟目錄誤導無法冒充/污染任何 follower 的狀態；合法 alice 照常平倉、退出碼非 0。

    M2 Task 10 起，`alice/../bob` 含 `/` 先被 `validate_account_id`（manifest load
    boundary）擋下，訊息與效果同上一測試——見該測試的 M2 Task 10 說明。"""
    manifest = _write_manifest(fake_stack.tmp_path, [
        _entry("alice/../bob", _BOB_ADDR, "mainnet"),  # 惡意：想寫進 base/bob
        _entry("alice", _ALICE_ADDR, "mainnet"),
    ])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}
    fake_stack.registry[_BOB_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0

    out = capsys.readouterr().out
    assert "[CRITICAL]" in out and "完全未被平倉" in out
    # base/bob（惡意 entry 的 resolve 目標）不得被建立 ARM——與真 bob 目錄隔離。
    assert not _arm_path(fake_stack.state_base, "bob").exists(), \
        "兄弟目錄誤導不得寫進 base/bob（真 bob 若存在也不得被此惡意 entry 污染）"
    assert _arm_path(fake_stack.state_base, "alice").exists()


# ── F1b（Task 10 fast-follow，reviewer Important）：_state_root_for 三道防線直測 ──
#
# Task 10 起，account_id 含 `/`／`..` 會先在 manifest load boundary 被
# validate_account_id 攔下，上面的 F1 整合測試因此不再到達 _state_root_for；但
# _state_root_for 是給「繞過 followers.py 直呼叫」的第二道縱深防禦（見其 docstring
# 「不倚賴上游驗證」），這道守錢碼必須有自己的直接單元測試，否則零覆蓋。
# 三道防線：(1) 輸入含 sep/../空 → 拒；(2) resolve 後 sr.parent 須為 base（擋逃出
# base）；(3) sr.name 須等於字面 account_id（擋 symlink/互消改指）。第 2/3 道靠
# symlink 情境獨立行使——不含 `/` 也不含 `..` 故過第 1 道，卻在 resolve 後偏移。


def test_state_root_for_normal_account_id_under_base(monkeypatch, tmp_path):
    """正常路徑：合法 account_id → base/<id>（resolve 正規化後）。"""
    from scripts.panic_all import _state_root_for

    monkeypatch.setenv("FILET_STATE_BASE", str(tmp_path))
    assert _state_root_for("alice") == (tmp_path / "alice").resolve()


@pytest.mark.parametrize("bad_id", ["a/b", "..", "", "/etc/x"])
def test_state_root_for_first_line_rejects_bad_input(monkeypatch, tmp_path, bad_id):
    """第 1 道（輸入形狀）：含路徑分隔 `/`（"a/b"、絕對路徑 "/etc/x"）、含 `..`
    分段（".."）、或空字串 → 直接 ValueError，不進 resolve。"""
    from scripts.panic_all import _state_root_for

    monkeypatch.setenv("FILET_STATE_BASE", str(tmp_path))
    with pytest.raises(ValueError):
        _state_root_for(bad_id)


def test_state_root_for_second_line_rejects_symlink_escaping_base(monkeypatch, tmp_path):
    """第 2 道（sr.parent==base.resolve()）：account_id 不含 sep/..（過第 1 道），
    但 base/<id> 是指向 base 外目錄的 symlink → resolve 後 parent 落在 base 外 →
    ValueError。獨立行使第 2 道（逃出 base）。"""
    from scripts.panic_all import _state_root_for

    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "evil").symlink_to(outside)  # base/evil → tmp/outside（base 外）
    monkeypatch.setenv("FILET_STATE_BASE", str(base))
    with pytest.raises(ValueError):
        _state_root_for("evil")


def test_state_root_for_third_line_rejects_symlink_sibling_redirect(monkeypatch, tmp_path):
    """第 3 道（sr.name==account_id）：account_id 不含 sep/..（過第 1 道），
    base/<id> 是指向 base 內**兄弟目錄**的 symlink → resolve 後 parent 仍是 base
    （過第 2 道）但 name 變成別人（bob≠alias）→ ValueError。獨立行使第 3 道
    （symlink/互消改指到別的 follower 目錄，會污染真 follower 的 killswitch.tripped）。"""
    from scripts.panic_all import _state_root_for

    (tmp_path / "bob").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "bob")  # base/alias → base/bob（兄弟）
    monkeypatch.setenv("FILET_STATE_BASE", str(tmp_path))
    with pytest.raises(ValueError):
        _state_root_for("alias")


# ── F2（opus Important）：前置讀取 resilience 重試 + 讀不到時鎖死 degrade ──────

def _run_single(fake_stack, notifier, *, account_id="alice", user_addr=_ALICE_ADDR,
                sleep_fn=None):
    """直接呼叫 run_single_follower（--yes）的小工具，注入假 keystore 與 sleep_fn。"""
    from scripts.panic import run_single_follower

    class _FakeKeystore:
        def get_agent_signer(self, aid):
            return object()

    kwargs = dict(
        network="mainnet", account_id=account_id, user_addr=user_addr,
        builder_addr="0x" + "b" * 40, execute=True,
        state_root=fake_stack.state_base / account_id,
        copy_settings=CopySettings(), notifier=notifier, keystore=_FakeKeystore())
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    return run_single_follower(**kwargs)


def test_run_single_follower_transient_read_retried_then_succeeds(fake_stack):
    """get_positions 前兩次 transient 逾時、第三次成功 → resilience 邊界重試
    （退避 0.6/1.2，注入 sleep_fn 不真睡）；最終平倉照常、ARM 寫入、無 degrade。"""
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True, "positions_fail_times": 2}
    sleeps: list[float] = []
    notifier = RecordingNotifier()

    _, report = _run_single(fake_stack, notifier, sleep_fn=sleeps.append)

    assert sleeps == [0.6, 1.2], "前置讀取的 transient 逾時應由 resilience 邊界重試"
    assert report.failures == (), "重試後成功不得留 degrade sentinel"
    assert ("close", "ETH", False, Decimal("2")) in fake_stack.registry[_ALICE_ADDR]["writes"]
    assert _arm_path(fake_stack.state_base, "alice").exists()


def test_run_single_follower_persistent_read_failure_still_arms_and_criticals(fake_stack):
    """get_positions 持續 transient 失敗 → 重試耗盡 → degrade：仍寫 ARM 鎖死該
    follower（撤可讀掛單）＋critical＋report.failures 含 sentinel（_exit_code 非 0），
    但不平倉（positions 未知）。鎖死優先於完美平倉。"""
    fake_stack.registry[_ALICE_ADDR] = {
        "close_ok": True, "raise_on_positions": TimeoutError("persistent timeout")}
    sleeps: list[float] = []
    notifier = RecordingNotifier()

    _, report = _run_single(fake_stack, notifier, sleep_fn=sleeps.append)

    assert sleeps == [0.6, 1.2], "3 次嘗試 = 2 次退避"
    assert _arm_path(fake_stack.state_base, "alice").exists(), "讀不到部位也要鎖死"
    assert "positions_unavailable" in report.failures, "degrade 須反映進退出碼訊號"
    from scripts.panic import _exit_code
    assert _exit_code(report) != 0
    criticals = [r for r in notifier.records if r[0] == "critical"]
    assert any("positions 讀取失敗" in r[2] and "未平倉" in r[2] for r in criticals)
    # 掛單仍可讀 → trip 照常撤（撤可讀的掛單），但沒有 close 動作（部位未知）。
    writes = fake_stack.registry[_ALICE_ADDR].get("writes", [])
    assert any(w[0] == "cancel" for w in writes)
    assert not any(w[0] == "close" for w in writes)


# ── F4（opus Important）：壞 manifest 條目升為 critical（不只 print）─────────

def test_panic_all_bad_manifest_entry_emits_critical(fake_stack, capsys):
    """壞 manifest 條目（跳過的帳號可能藏裸露活倉）必須發 critical，且訊息點明
    此帳號完全未被平倉、可能有裸露暴險——不得只 print 讓 grep CRITICAL 的監控漏看。"""
    bad = {"account_id": "evil", "user_address": "0xshort",
           "builder_address": "0x" + "b" * 40, "network": "mainnet"}
    manifest = _write_manifest(fake_stack.tmp_path, [bad, _entry("alice", _ALICE_ADDR)])
    fake_stack.monkeypatch.setenv("FILET_FOLLOWERS", str(manifest))
    fake_stack.registry[_ALICE_ADDR] = {"close_ok": True}

    with pytest.raises(SystemExit) as ei:
        fake_stack.panic_all.main(["--yes"])
    assert ei.value.code != 0

    out = capsys.readouterr().out
    assert "[CRITICAL]" in out, "壞條目必須升為 critical（不只 [MANIFEST] print）"
    assert "未被平倉" in out and "裸露暴險" in out
