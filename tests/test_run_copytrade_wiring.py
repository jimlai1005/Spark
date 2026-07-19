"""run_copytrade 多實例接線（M2 Task 5）。

驗證 select_keystore() 依 env FILET_KEYSTORE 選對後端（未設/keychain → Mac
開發用 MacKeychainBackend；envfile → VPS 用 EnvFileKeyStore，讀 FILET_KEYS_DIR），
以及 wrap_notifier() 只在 account_id 有值時才包 TaggedNotifier（dry/shadow 無
account 時原樣回傳 inner，不炸）。全程 monkeypatch env，不真取 key、不觸網。

另含 leader 解析接線（per-follower leader ＋ 白名單二次驗證）：env 路徑選檔、
manifest/env 兩條來源、以及「leader 不在白名單即拒絕啟動」的 main() 層證據。
"""
import json

import pytest

import scripts.run_copytrade as rc
from spark.copytrade.notifier import NullNotifier
from spark.filet.leader_resolve import SOURCE_ENV_DEFAULT, SOURCE_MANIFEST
from spark.filet.tagged_notifier import TaggedNotifier
from spark.keystore.envfile import EnvFileKeyStore
from spark.keystore.keychain import MacKeychainBackend


def test_select_keystore_default_keychain(monkeypatch):
    monkeypatch.delenv("FILET_KEYSTORE", raising=False)
    assert isinstance(rc.select_keystore(), MacKeychainBackend)


def test_select_keystore_envfile(monkeypatch, tmp_path):
    monkeypatch.setenv("FILET_KEYSTORE", "envfile")
    monkeypatch.setenv("FILET_KEYS_DIR", str(tmp_path))
    ks = rc.select_keystore()
    assert isinstance(ks, EnvFileKeyStore)


def test_wrap_notifier_tags_when_account(monkeypatch):
    n = rc.wrap_notifier(NullNotifier(), account_id="alice")
    assert isinstance(n, TaggedNotifier)


def test_wrap_notifier_passthrough_without_account(monkeypatch):
    base = NullNotifier()
    assert rc.wrap_notifier(base, account_id=None) is base


# ── leader 解析接線（per-follower ＋ 白名單二次驗證）────────────────────

_ME = "0x" + "11" * 20
_BUILDER = "0x" + "22" * 20
_LEADER = "0x" + "d4" * 20
_OTHER = "0x" + "e5" * 20


def _write_manifest(tmp_path, leader):
    """（重）寫 follower manifest。回傳路徑；leader=None ＝ 不指定（走 env 回退）。"""
    m = tmp_path / "followers.json"
    entry = {"account_id": "alice", "user_address": _ME,
             "builder_address": _BUILDER, "network": "mainnet", "label": ""}
    if leader is not None:
        entry["leader_address"] = leader
    m.write_text(json.dumps({"followers": [entry]}))
    return m


def _exchange_dir(tmp_path):
    """API 與引擎共用的**專屬交換目錄**（正式部署 /var/lib/filet-exchange）。

    ⭐ 兩端由同一個 helper 算出來，測試裡就不可能出現「寫端與讀端各拼一次路徑」
    的漂移——那正是 C3 在正式部署上發生的事。
    """
    d = tmp_path / "exchange"
    d.mkdir(exist_ok=True)
    return d


def _wire_env(monkeypatch, tmp_path, *, manifest_leader, allowlist):
    """佈置一份 manifest ＋ 白名單，並把引擎需要的 env 全部設好。"""
    m = _write_manifest(tmp_path, manifest_leader)
    lp = tmp_path / "leaders.json"
    lp.write_text(json.dumps({"leaders": allowlist}))
    monkeypatch.setenv("FILET_FOLLOWERS", str(m))
    monkeypatch.setenv("FILET_LEADERS_PATH", str(lp))
    monkeypatch.setenv("SPARK_USER_ADDR", _ME)
    monkeypatch.setenv("SPARK_BUILDER_ADDR", _BUILDER)
    monkeypatch.setenv("SPARK_ACCOUNT_ID", "alice")
    monkeypatch.setenv("SPARK_NETWORK", "mainnet")
    monkeypatch.setenv("FILET_EXCHANGE_DIR", str(_exchange_dir(tmp_path)))
    monkeypatch.delenv("COPY_LIVE_TRADING", raising=False)


def test_make_leader_resolver_reads_env_paths(monkeypatch, tmp_path):
    """FILET_FOLLOWERS / FILET_LEADERS_PATH 決定引擎驗哪兩份檔。"""
    _wire_env(monkeypatch, tmp_path, manifest_leader=_LEADER,
              allowlist=[{"address": _LEADER, "name": "Alpha"}])
    res = rc.make_leader_resolver("alice", _ME, _OTHER)()
    assert res.address == _LEADER and res.source == SOURCE_MANIFEST


def test_make_leader_resolver_env_fallback(monkeypatch, tmp_path):
    """manifest 未指定 → 回退 env 預設（且該預設仍要在白名單內）。"""
    _wire_env(monkeypatch, tmp_path, manifest_leader=None,
              allowlist=[{"address": _OTHER, "name": "Env"}])
    res = rc.make_leader_resolver("alice", _ME, _OTHER)()
    assert res.address == _OTHER and res.source == SOURCE_ENV_DEFAULT


def test_make_leader_resolver_ignores_routine_delisting(monkeypatch, tmp_path):
    """⭐ accepting_new=false（例行下架）→ 引擎照常解析成功，正在跟的人不受影響。"""
    _wire_env(monkeypatch, tmp_path, manifest_leader=_LEADER,
              allowlist=[{"address": _LEADER, "name": "Alpha", "accepting_new": False}])
    assert rc.make_leader_resolver("alice", _ME, _OTHER)().address == _LEADER


# ── ⭐ leader 撤銷的受控收尾（撤單 → reduce-only 全平 → 鎖死 kill switch）──

def test_revocation_wind_down_flattens_and_trips(tmp_path):
    """⭐ make_revocation_wind_down() 真的走 killswitch.trip：撤掉掛單、平掉部位、
    寫下 ARM 檔（且 payload 標明 reason=leader_revoked，不是一份看不出所以然的
    drawdown=0 紀錄）。"""
    from decimal import Decimal

    from spark.copytrade.killswitch import ARM_FILE_RELPATH, is_tripped
    from spark.copytrade.notifier import RecordingNotifier
    from spark.exchange.base import OpenOrder, OrderResult, Position

    order = OpenOrder(oid=7, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                      sz=Decimal("1"), reduce_only=False, is_trigger=False,
                      trigger_px=None, tpsl=None)
    position = Position(coin="ETH", szi=Decimal("2"), entry_px=Decimal("1900"),
                        leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                        margin_used=Decimal("100"))

    class _Ex:
        my_address = _ME

        def __init__(self):
            self.calls = []

        def get_open_orders(self):
            return [order]

        def cancel(self, coin, oid):
            self.calls.append(("cancel", coin, oid))
            return True

        def close_reduce_only(self, coin, is_buy, size, *, emergency=False):
            self.calls.append(("close", coin, is_buy, size, emergency))
            return OrderResult(ok=True, filled_size=size, avg_px=Decimal("100"), raw={})

    class _Adapter:
        def get_positions(self, address):
            assert address == _ME
            return [position]

    ex, notifier = _Ex(), RecordingNotifier()
    rc.make_revocation_wind_down(_Adapter(), ex, notifier, tmp_path)()

    assert ("cancel", "ETH", 7) in ex.calls               # 掛單撤掉
    assert ("close", "ETH", False, Decimal("2"), True) in ex.calls  # 部位平掉（緊急）
    assert is_tripped(tmp_path)                            # kill switch 鎖死
    payload = json.loads((tmp_path / ARM_FILE_RELPATH).read_text())
    assert payload["reason"] == "leader_revoked"           # 鎖死的理由寫在鎖上
    assert any("leader_revoked" in r[2] for r in notifier.records if r[0] == "critical")


def test_main_refuses_to_start_when_leader_revoked(monkeypatch, tmp_path, capsys):
    """⭐ 啟動時 leader 已被撤銷（enabled=false）→ 拒絕啟動（維持既有行為：
    本來就還沒開倉，不需收尾）。"""
    _wire_env(monkeypatch, tmp_path, manifest_leader=_LEADER,
              allowlist=[{"address": _LEADER, "name": "Alpha", "enabled": False}])
    with pytest.raises(SystemExit) as e:
        rc.main(["--once"])
    assert e.value.code == 2
    assert "leader 解析失敗，拒絕啟動" in capsys.readouterr().out


def test_main_refuses_to_start_when_leader_not_allowlisted(monkeypatch, tmp_path, capsys):
    """⭐ 啟動時 leader 不在白名單 → 拒絕啟動（SystemExit(2)），且在碰網路之前。

    本測試同時是「解析發生在 Info() 建構前」的結構性證據：conftest 的 autouse
    socket-ban 會讓任何連線炸成 RuntimeError，而這裡拿到的是乾淨的 SystemExit(2)。
    """
    _wire_env(monkeypatch, tmp_path, manifest_leader=_OTHER,
              allowlist=[{"address": _LEADER, "name": "Alpha"}])
    with pytest.raises(SystemExit) as e:
        rc.main(["--once"])
    assert e.value.code == 2
    assert "leader 解析失敗，拒絕啟動" in capsys.readouterr().out


# ── ⭐ 已驗證 leader → 實際交易路徑的接縫 ────────────────────────────────
# 這條鏈上風險最高的一段：白名單驗過的位址，必須真的是 run_cycle 拿去下單的位址。
# 2026-07-19 opus 對抗性審查實測「刪掉 main() 的 replace(leader_address=...)」與
# 「cycle() 內改用啟動時的 settings、丟棄每輪 refresh 結果」兩個變異，990 全綠都沒咬到
# ——程式碼當時是對的，但接縫零測試保護，任何重構動到那裡都不會有紅燈。


def _stub_network(monkeypatch):
    """把 main() 的網路依賴換成替身。

    main() 內是延後 import（`from hyperliquid.info import Info`），所以在模組上換掉
    屬性即可攔截。conftest 的 autouse socket-ban 之下這是唯一能走完 main() 的方式，
    也順帶證明了替身之外沒有其它對外連線。
    """
    import hyperliquid.info

    import spark.exchange.hyperliquid as hl

    class _Info:
        def __init__(self, *a, **k):
            pass

    class _Adapter:
        def __init__(self, *a, **k):
            pass

        def get_positions(self, address):
            return []

    monkeypatch.setattr(hyperliquid.info, "Info", _Info)
    monkeypatch.setattr(hl, "HyperliquidAdapter", _Adapter)


def _record_leaders(monkeypatch, seen):
    """把 run_cycle 換成只記錄「本輪拿到的 leader_address」的替身。"""
    def _fake_run_cycle(adapter, ex, settings, notifier, state, root):
        seen.append(settings.leader_address)
        return "report"

    monkeypatch.setattr(rc, "run_cycle", _fake_run_cycle)


def test_verified_leader_reaches_run_cycle(monkeypatch, tmp_path, capsys):
    """⭐ 啟動時：run_cycle 收到的 leader **等於白名單驗過的解析結果**，不是 env 原值。

    env COPY_LEADER_ADDRESS 刻意設成另一個位址，讓「解析結果沒有覆蓋 env 值」這個
    變異無所遁形：
    - run_cycle 拿到的位址（交易路徑的真相）
    - 啟動橫幅印出的位址（操作者判斷「我在跟誰」的唯一依據；印錯等於對人說謊）
    兩者都必須是已驗證的那一個。
    """
    _wire_env(monkeypatch, tmp_path, manifest_leader=_LEADER,
              allowlist=[{"address": _LEADER, "name": "Alpha"}])
    monkeypatch.setenv("COPY_LEADER_ADDRESS", _OTHER)   # 未經驗證的 env 原值
    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path))
    _stub_network(monkeypatch)
    seen = []
    _record_leaders(monkeypatch, seen)

    rc.main(["--once"])

    assert seen == [_LEADER]                 # 交易路徑吃的是已驗證位址
    out = capsys.readouterr().out
    assert f"leader={_LEADER}" in out        # 橫幅說的也是同一個
    assert f"leader={_OTHER}" not in out     # 未驗證的 env 值不得外洩到任何一處


def test_leader_change_mid_run_reaches_run_cycle(monkeypatch, tmp_path):
    """⭐ 執行中：第二輪解析出**不同**位址時，run_cycle 第二次必須收到**新**位址。

    每輪重新解析的意義全在這裡——客戶換 leader 不必重啟服務。若每輪 refresh 的結果
    被丟棄（沿用啟動時的 settings），換 leader 會靜默失效：引擎繼續跟舊 leader，
    而 manifest、web 介面、告警都顯示已經換了。
    """
    _wire_env(monkeypatch, tmp_path, manifest_leader=_LEADER,
              allowlist=[{"address": _LEADER, "name": "Alpha"},
                         {"address": _OTHER, "name": "Bravo"}])
    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path))
    _stub_network(monkeypatch)
    seen = []
    _record_leaders(monkeypatch, seen)

    def _fake_main_loop(mk_cycle, settings, notifier):
        mk_cycle()
        _write_manifest(tmp_path, _OTHER)   # 客戶在執行中換了 leader
        mk_cycle()

    monkeypatch.setattr(rc, "main_loop", _fake_main_loop)

    rc.main([])

    assert seen == [_LEADER, _OTHER]


# ── ⭐⭐ 客戶簽章的換 leader：從記錄檔一路到 run_cycle 的接縫 ─────────────
# 這一段與上面兩條同樣是「接縫測試」：模組單元測試證明 applier 本身對，這裡證明它
# 真的被接進 main() 的解析路徑、而且結果真的餵給了下單的那一段。少了它，把
# make_effective_leader_resolver 從 main() 拿掉會全綠通過。


def _signed_change(tmp_path, wallet, *, leader, account_id="alice", nonce="n1"):
    """簽一筆合法的換 leader 記錄並落到 API 慣例的位置（**專屬交換目錄**）。

    2026-07-19 C3 修法前錨在 pending.json 的同目錄——那讓共享產物與 API 私有產物
    同住一個目錄，實際部署時兩個進程讀寫不同的檔案。現在錨在 FILET_EXCHANGE_DIR。
    """
    from datetime import datetime, timezone

    from eth_account.messages import encode_defunct

    from spark.filet.leader_change import (build_leader_change_message,
                                           build_leader_change_record,
                                           leader_changes_path_for,
                                           write_leader_change)

    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = build_leader_change_message(account_id=account_id, leader_address=leader,
                                      nonce=nonce, issued_at=issued_at)
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    rec = build_leader_change_record(account_id=account_id, leader_address=leader,
                                     nonce=nonce, issued_at=issued_at,
                                     signature=sig, message=msg)
    exchange = _exchange_dir(tmp_path)
    write_leader_change(leader_changes_path_for(exchange), rec)
    return exchange


def _wire_signed(monkeypatch, tmp_path, *, allowlist):
    """`_wire_env` 的變體：follower 的 user_address 換成一把真錢包（才能簽章）。"""
    from eth_account import Account

    wallet = Account.create()
    m = tmp_path / "followers.json"
    m.write_text(json.dumps({"followers": [
        {"account_id": "alice", "user_address": wallet.address,
         "builder_address": _BUILDER, "network": "mainnet", "label": "",
         "leader_address": _LEADER}]}))
    lp = tmp_path / "leaders.json"
    lp.write_text(json.dumps({"leaders": allowlist}))
    monkeypatch.setenv("FILET_FOLLOWERS", str(m))
    monkeypatch.setenv("FILET_LEADERS_PATH", str(lp))
    monkeypatch.setenv("SPARK_USER_ADDR", wallet.address)
    monkeypatch.setenv("SPARK_BUILDER_ADDR", _BUILDER)
    monkeypatch.setenv("SPARK_ACCOUNT_ID", "alice")
    monkeypatch.setenv("SPARK_NETWORK", "mainnet")
    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FILET_EXCHANGE_DIR", str(_exchange_dir(tmp_path)))
    monkeypatch.delenv("COPY_LIVE_TRADING", raising=False)
    monkeypatch.delenv("COPY_TG_BOT_TOKEN", raising=False)
    return wallet


def test_customer_signed_change_reaches_run_cycle(monkeypatch, tmp_path):
    """⭐⭐ 客戶簽章的變更必須真的變成 run_cycle 拿去下單的那個位址。

    manifest 說跟 _LEADER，客戶簽章說換到 _OTHER —— 下單路徑必須看到 _OTHER。
    這是「引擎會依此改變拿客戶的錢跟誰交易」那句話的可執行版本。
    """
    wallet = _wire_signed(monkeypatch, tmp_path,
                          allowlist=[{"address": _LEADER, "name": "Alpha"},
                                     {"address": _OTHER, "name": "Bravo"}])
    _signed_change(tmp_path, wallet, leader=_OTHER)
    _stub_network(monkeypatch)
    seen = []
    _record_leaders(monkeypatch, seen)

    rc.main(["--once"])

    assert seen == [_OTHER]


def test_customer_signed_change_is_applied_once_across_cycles(monkeypatch, tmp_path):
    """⭐ 冪等的接線版：記錄檔在每個 cycle 都還在，但只能兌現一次。

    帳本內容在第二輪之後必須逐位元組不變——重複套用的症狀是每輪一次真實的
    平舊開新，而且看起來完全正常（每輪都像一次「換 leader 成功」）。
    """
    from spark.filet.leader_change_apply import LEDGER_RELPATH

    wallet = _wire_signed(monkeypatch, tmp_path,
                          allowlist=[{"address": _LEADER, "name": "Alpha"},
                                     {"address": _OTHER, "name": "Bravo"}])
    _signed_change(tmp_path, wallet, leader=_OTHER)
    _stub_network(monkeypatch)
    seen = []
    _record_leaders(monkeypatch, seen)
    ledger = tmp_path / "state" / LEDGER_RELPATH
    snapshots = []

    def _fake_main_loop(mk_cycle, settings, notifier):
        for _ in range(3):
            mk_cycle()
            snapshots.append(ledger.read_text())

    monkeypatch.setattr(rc, "main_loop", _fake_main_loop)
    rc.main([])

    assert seen == [_OTHER, _OTHER, _OTHER]      # 一直跟新 leader
    assert len(set(snapshots)) == 1              # ⭐ 帳本只被寫過一次


def test_revocation_preempts_a_valid_signed_change(monkeypatch, tmp_path):
    """⭐⭐ 撤銷優先：manifest 的 leader 已被撤銷、同時有一筆完全合法的客戶簽章
    變更指向另一個健康的 leader → 引擎**不得**用那筆變更把自己救活。

    安全動作優先於便利動作。啟動時撤銷＝拒絕啟動（本來就還沒開倉，不需收尾），
    而那筆變更必須原封不動地留著（帳本零寫入）——它不是被消耗掉，只是這一輪輪不到它。
    """
    from spark.filet.leader_change_apply import LEDGER_RELPATH

    wallet = _wire_signed(
        monkeypatch, tmp_path,
        allowlist=[{"address": _LEADER, "name": "Alpha", "enabled": False},
                   {"address": _OTHER, "name": "Bravo"}])
    _signed_change(tmp_path, wallet, leader=_OTHER)
    _stub_network(monkeypatch)

    with pytest.raises(SystemExit) as e:
        rc.main(["--once"])
    assert e.value.code == 2
    assert not (tmp_path / "state" / LEDGER_RELPATH).exists()   # ⭐ 未被兌現


def test_dry_run_without_account_id_skips_the_applier(monkeypatch, tmp_path):
    """dry/shadow 無 SPARK_ACCOUNT_ID → 沒有身分就沒有「哪一筆記錄是我的」，
    也沒有可信的 user_address 可比對簽章者 → 整條套用路徑跳過（不建帳本）。"""
    from spark.filet.leader_change_apply import LEDGER_RELPATH

    wallet = _wire_signed(monkeypatch, tmp_path,
                          allowlist=[{"address": _OTHER, "name": "Bravo"}])
    monkeypatch.delenv("SPARK_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("COPY_LEADER_ADDRESS", _OTHER)
    _signed_change(tmp_path, wallet, leader=_LEADER)
    _stub_network(monkeypatch)
    seen = []
    _record_leaders(monkeypatch, seen)

    rc.main(["--once", "--dry-run"])

    assert seen == [_OTHER]                                     # 走 env 回退
    assert not (tmp_path / "state" / LEDGER_RELPATH).exists()


# ── ⭐⭐ 客戶簽章的資金設定：從記錄檔一路到 run_cycle 的接縫 ──────────────
# 與上面的換 leader 接縫同一個理由：模組單元測試證明 applier 本身對，這裡證明它
# 真的被接進 main() 的 cycle、而且結果真的餵給了下單的那一段。少了它，把
# make_capital_settings_applier 從 cycle() 拿掉會全綠通過。


def _signed_capital(tmp_path, wallet, *, alloc="5000", util="0.4",
                    account_id="alice", nonce="c1"):
    """簽一筆合法的資金設定記錄並落到 API 慣例的位置（**專屬交換目錄**）。"""
    from datetime import datetime, timezone

    from eth_account.messages import encode_defunct

    from spark.filet.capital_settings import (build_capital_settings_message,
                                              build_capital_settings_record,
                                              canonical_capital_values,
                                              capital_settings_path_for,
                                              write_capital_settings)

    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, alloc_s, _, util_s = canonical_capital_values(alloc, util)
    msg = build_capital_settings_message(
        account_id=account_id, allocated_capital=alloc_s,
        capital_utilization=util_s, nonce=nonce, issued_at=issued_at)
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    rec = build_capital_settings_record(
        account_id=account_id, allocated_capital=alloc_s,
        capital_utilization=util_s, nonce=nonce, issued_at=issued_at,
        signature=sig, message=msg)
    exchange = _exchange_dir(tmp_path)
    write_capital_settings(capital_settings_path_for(exchange), rec)
    return exchange


def _record_capital(monkeypatch, seen):
    """把 run_cycle 換成記錄「本輪拿到的資金設定」的替身。"""
    def _fake_run_cycle(adapter, ex, settings, notifier, state, root):
        seen.append((settings.allocated_capital, settings.capital_utilization))
        return "report"

    monkeypatch.setattr(rc, "run_cycle", _fake_run_cycle)


def test_signed_capital_settings_reach_run_cycle(monkeypatch, tmp_path):
    """⭐⭐ 客戶簽章的資金設定必須真的變成 run_cycle 拿去算部位大小的那組值。

    env 預設是「用全部權益 × 100%」，客戶簽章說「5000 × 40%」——sizing 路徑必須
    看到後者。這是「這兩個值直接乘進部位大小」那句話的可執行版本。
    """
    from decimal import Decimal

    wallet = _wire_signed(monkeypatch, tmp_path,
                          allowlist=[{"address": _LEADER, "name": "Alpha"}])
    _signed_capital(tmp_path, wallet, alloc="5000", util="0.4")
    _stub_network(monkeypatch)
    seen = []
    _record_capital(monkeypatch, seen)

    rc.main(["--once"])

    assert seen == [(Decimal("5000.00"), Decimal("0.4000"))]


def test_capital_settings_applied_once_across_cycles(monkeypatch, tmp_path):
    """⭐ 冪等的接線版：記錄檔每輪都在，但只兌現一次（帳本逐位元組不變）。"""
    from decimal import Decimal

    from spark.filet.capital_settings_apply import LEDGER_RELPATH

    wallet = _wire_signed(monkeypatch, tmp_path,
                          allowlist=[{"address": _LEADER, "name": "Alpha"}])
    _signed_capital(tmp_path, wallet, alloc="5000", util="0.4")
    _stub_network(monkeypatch)
    seen = []
    _record_capital(monkeypatch, seen)
    ledger = tmp_path / "state" / LEDGER_RELPATH
    snapshots = []

    def _fake_main_loop(mk_cycle, settings, notifier):
        for _ in range(3):
            mk_cycle()
            snapshots.append(ledger.read_text())

    monkeypatch.setattr(rc, "main_loop", _fake_main_loop)
    rc.main([])

    assert seen == [(Decimal("5000.00"), Decimal("0.4000"))] * 3
    assert len(set(snapshots)) == 1              # ⭐ 帳本只被寫過一次


def test_lost_capital_ledger_stops_trading_for_the_cycle(monkeypatch, tmp_path):
    """⭐⭐ 帳本遺失（標記在、帳本不在）→ 本輪**零交易動作**，run_cycle 不被呼叫。

    絕不用 env 預設值繼續跑：那是一次沒有客戶授權的曝險變更，而且全程安靜。
    """
    from spark.filet.capital_settings_apply import (LEDGER_RELPATH,
                                                    ledger_init_marker_path)

    _wire_signed(monkeypatch, tmp_path,
                 allowlist=[{"address": _LEADER, "name": "Alpha"}])
    ledger = tmp_path / "state" / LEDGER_RELPATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger_init_marker_path(ledger).write_text("{}")     # 標記在，帳本不在
    _stub_network(monkeypatch)
    seen = []
    _record_capital(monkeypatch, seen)

    rc.main(["--once"])

    assert seen == []                                    # ⭐ 本輪零交易動作
    assert not ledger.exists()                           # 也沒有偷偷重建


def test_dry_run_without_account_id_skips_the_capital_applier(monkeypatch, tmp_path):
    """dry/shadow 無 SPARK_ACCOUNT_ID → 沒有身分就沒有可信的簽章者比對基準 →
    整條套用路徑跳過（不建帳本、走 env 預設）。"""
    from spark.filet.capital_settings_apply import LEDGER_RELPATH

    wallet = _wire_signed(monkeypatch, tmp_path,
                          allowlist=[{"address": _LEADER, "name": "Alpha"}])
    monkeypatch.delenv("SPARK_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("COPY_LEADER_ADDRESS", _LEADER)
    _signed_capital(tmp_path, wallet, alloc="5000", util="0.4")
    _stub_network(monkeypatch)
    seen = []
    _record_capital(monkeypatch, seen)

    rc.main(["--once", "--dry-run"])

    assert seen == [(rc.CopySettings().allocated_capital,
                     rc.CopySettings().capital_utilization)]   # env 預設
    assert not (tmp_path / "state" / LEDGER_RELPATH).exists()
