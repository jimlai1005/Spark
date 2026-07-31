"""tests/test_backfill_leader_kinds.py — backfill 既有 registry 的 kind 欄位。

沿 scripts/revoke_leader.py 形狀：
- read-modify-write 取 registry_lock
- 逐條目打 HLGateway.vault_details
- 是 vault 且 kind!=vault → 改寫
- 自我驗收：重載並驗證每筆 kind 與探測結果一致
- 冪等：重跑零改動
- 網路失敗：該條目標 error、繼續其餘、結尾非零退出

測試：fake gateway 三情境、冪等重跑、dry-run 不寫檔、flock、round-trip 不破壞既有欄位。
"""
import json

import pytest

from scripts.backfill_leader_kinds import backfill
from spark.filet.user_leaders import load_user_leaders, record_user_leader


_TARGET = "0x" + "a1" * 20
_OTHER = "0x" + "b2" * 20
_VAULT = "0x" + "cc" * 20
_ACCT = "f" + "c3" * 20


@pytest.fixture
def registry_path(tmp_path):
    """exchange 目錄下的 user_leaders.json。"""
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    return exchange / "user_leaders.json"


class FakeGateway:
    """Mock HLGateway.vault_details 供測試用。預設回 None（非 vault）。"""

    def __init__(self):
        self.vault_addresses = {}  # { address: dict (vault) or None (非 vault) or Exception }
        self.calls = []

    def vault_details(self, address: str):
        self.calls.append(address)
        result = self.vault_addresses.get(address)
        if isinstance(result, Exception):
            raise result
        return result


# ── 基本情況：vault 探測 ────────────────────────────────────────────────────

def test_backfills_vault_kind_for_a_vault_address(registry_path):
    """位址探測出是 vault（vault_details 回 dict）→ 補寫 kind:vault。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    result = backfill(registry_path, gateway=fake_gw)

    assert result.changed == 1
    assert result.errors == 0
    assert _TARGET in result.report
    entries = load_user_leaders(registry_path)
    assert len(entries) == 1
    assert entries[0].kind == "vault"


def test_does_not_write_kind_for_non_vault_addresses(registry_path):
    """非 vault（vault_details 回 None）→ 不寫 kind（維持缺省語意）。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = None

    result = backfill(registry_path, gateway=fake_gw)

    assert result.changed == 0
    assert result.errors == 0
    raw = json.loads(registry_path.read_text())["leaders"]
    assert raw[0].get("kind", "standard") == "standard"  # 非 vault 不動（record_user_leader 自 2026-07-31 起寫顯式 standard）


def test_skips_already_backfilled_vault(registry_path):
    """已是 kind:vault → skip。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    # 手動補 kind:vault
    raw = json.loads(registry_path.read_text())["leaders"]
    raw[0]["kind"] = "vault"
    registry_path.write_text(json.dumps({"leaders": raw}))

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    result = backfill(registry_path, gateway=fake_gw)

    assert result.changed == 0
    assert result.errors == 0
    assert "skip" in result.report[_TARGET].lower()


# ── 網路失敗 ────────────────────────────────────────────────────────────────

def test_continues_on_gateway_error(registry_path):
    """vault_details 拋例外 → 該條目記 error、繼續其餘。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    record_user_leader(registry_path, address=_OTHER, added_by=_ACCT)

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = RuntimeError("網路錯誤")
    fake_gw.vault_addresses[_OTHER] = {"name": "Vault Beta"}  # 應該被處理

    result = backfill(registry_path, gateway=fake_gw)

    assert result.errors == 1
    assert result.changed == 1  # 只有 _OTHER 被改

    # 檢查原始 JSON
    raw = json.loads(registry_path.read_text())["leaders"]
    raw_by_addr = {e["address"]: e for e in raw}

    # _TARGET 未被改動，JSON 裡無 kind 欄位
    assert raw_by_addr[_TARGET].get("kind", "standard") == "standard"  # 探測失敗 → 條目不動
    # _OTHER 被改寫成 vault
    assert raw_by_addr[_OTHER].get("kind") == "vault"


def test_returns_nonzero_exit_code_on_errors(registry_path):
    """任何 error 條目 → exit 非零。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = RuntimeError("網路錯誤")

    result = backfill(registry_path, gateway=fake_gw)

    assert result.exit_code != 0


# ── 冪等性 ────────────────────────────────────────────────────────────────

def test_running_twice_is_idempotent(registry_path):
    """重跑零改動。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    first = backfill(registry_path, gateway=fake_gw)
    content_after_first = registry_path.read_text()

    # 第二次
    fake_gw.calls = []
    second = backfill(registry_path, gateway=fake_gw)

    assert first.changed == 1
    assert second.changed == 0  # 零改動
    assert registry_path.read_text() == content_after_first  # 一個位元組都不動
    assert second.exit_code == 0  # 冪等情況下仍成功


# ── dry-run ────────────────────────────────────────────────────────────────

def test_dry_run_does_not_write(registry_path):
    """dry-run 不寫檔。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    before = registry_path.read_text()
    mtime_before = registry_path.stat().st_mtime

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    result = backfill(registry_path, gateway=fake_gw, dry_run=True)

    # 內容不變，mtime 不變
    assert registry_path.read_text() == before
    assert registry_path.stat().st_mtime == mtime_before
    # 但結果說「會改」
    assert result.changed == 1


# ── 驗收與 round-trip ───────────────────────────────────────────────────────

def test_self_verification_on_successful_changes(registry_path):
    """改完後重載驗證：每筆 kind 與探測結果一致。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    record_user_leader(registry_path, address=_OTHER, added_by=_ACCT)

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}
    fake_gw.vault_addresses[_OTHER] = None  # 非 vault

    result = backfill(registry_path, gateway=fake_gw)

    # 自我驗收通過則 exit_code=0
    assert result.exit_code == 0

    # 檢查原始 JSON
    raw = json.loads(registry_path.read_text())["leaders"]
    raw_by_addr = {e["address"]: e for e in raw}

    assert raw_by_addr[_TARGET].get("kind") == "vault"
    assert raw_by_addr[_OTHER].get("kind", "standard") == "standard"  # 非 vault 不動


def test_preserves_existing_fields_on_kind_change(registry_path):
    """只改 kind，其他欄位（source、added_by）原封不動（round-trip）。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    backfill(registry_path, gateway=fake_gw)

    raw = json.loads(registry_path.read_text())["leaders"]
    assert raw[0]["source"] == "user"
    assert raw[0]["added_by"] == _ACCT
    assert raw[0]["enabled"] is True  # 原值維持
    assert raw[0]["kind"] == "vault"  # 新欄位補上


# ── 多條目 ──────────────────────────────────────────────────────────────────

def test_processes_multiple_entries(registry_path):
    """多個位址、混合 vault/non-vault。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    record_user_leader(registry_path, address=_OTHER, added_by=_ACCT)
    record_user_leader(registry_path, address=_VAULT, added_by=_ACCT)

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault A"}
    fake_gw.vault_addresses[_OTHER] = None
    fake_gw.vault_addresses[_VAULT] = {"name": "Vault C"}

    result = backfill(registry_path, gateway=fake_gw)

    assert result.changed == 2  # 兩個 vault
    assert result.errors == 0

    # 檢查原始 JSON
    raw = json.loads(registry_path.read_text())["leaders"]
    raw_by_addr = {e["address"]: e for e in raw}
    assert raw_by_addr[_TARGET].get("kind") == "vault"
    assert raw_by_addr[_OTHER].get("kind", "standard") == "standard"  # 非 vault 不動
    assert raw_by_addr[_VAULT].get("kind") == "vault"


# ── 空 registry ─────────────────────────────────────────────────────────────

def test_handles_empty_registry(registry_path):
    """registry 為空或尚未建立。"""
    # 不建檔或建空檔
    fake_gw = FakeGateway()

    result = backfill(registry_path, gateway=fake_gw)

    assert result.changed == 0
    assert result.errors == 0
    assert result.exit_code == 0


# ── 大小寫正規化 ────────────────────────────────────────────────────────────

def test_address_case_variants_refer_to_same_entry(registry_path):
    """位址大小寫變體比對時應正規化（工程原則 1）。"""
    # 小寫存
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)

    fake_gw = FakeGateway()
    # 大寫查
    upper_target = _TARGET.replace("a1", "A1")
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault"}
    fake_gw.vault_addresses[upper_target] = {"name": "Vault"}

    result = backfill(registry_path, gateway=fake_gw)

    # 應該只調用一次（正規化後同位址）
    assert result.changed == 1
    entries = load_user_leaders(registry_path)
    assert entries[0].kind == "vault"


# ── 輸出格式 ────────────────────────────────────────────────────────────────

def test_output_format_shows_each_entry(registry_path):
    """輸出每條目一行，格式 <address> <原kind> → <新kind> [action]。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    result = backfill(registry_path, gateway=fake_gw)

    # report 是 dict，每個 address 對應一行訊息
    assert _TARGET in result.report
    msg = result.report[_TARGET]
    assert "changed" in msg.lower() or "vault" in msg.lower()


# ── F4：探測一次到位（鎖外）＋鎖內重讀防護 ──────────────────────────────────

def test_probes_each_entry_exactly_once(registry_path):
    """F4：每條目**恰好一次**網路探測——自我驗收改用段 1 的快取結果比對重載內容，
    不得重新探測（原實作驗收重探＝API 呼叫加倍）。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    record_user_leader(registry_path, address=_OTHER, added_by=_ACCT)

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault A"}
    fake_gw.vault_addresses[_OTHER] = {"name": "Vault B"}

    result = backfill(registry_path, gateway=fake_gw)

    assert result.changed == 2 and result.exit_code == 0
    assert len(fake_gw.calls) == 2  # == 條目數（含驗收在內總共一輪探測）


class _ConcurrentWriteGateway(FakeGateway):
    """段 1（鎖外探測）期間，第三方把條目改成 kind:vault——模擬「探測與取鎖之間
    檔案被他人改寫」。段 2 鎖內重讀必須看到新狀態、冪等不重寫。"""

    def __init__(self, registry_path):
        super().__init__()
        self._registry_path = registry_path

    def vault_details(self, address: str):
        result = super().vault_details(address)
        raw = json.loads(self._registry_path.read_text())
        for e in raw["leaders"]:
            e["kind"] = "vault"
        self._registry_path.write_text(json.dumps(raw))
        return result


def test_concurrent_kind_write_between_probe_and_lock_is_idempotent(registry_path):
    """F4：探測（鎖外）之後、取鎖之前條目已被他人改成 vault → 鎖內重讀看到新值，
    零改寫（冪等；不得拿鎖外快照覆蓋鎖內現實）。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)
    gw = _ConcurrentWriteGateway(registry_path)
    gw.vault_addresses[_TARGET] = {"name": "Vault A"}

    result = backfill(registry_path, gateway=gw)

    assert result.exit_code == 0
    assert result.changed == 0  # 鎖內重讀已見 vault → 不重寫
    raw = json.loads(registry_path.read_text())["leaders"]
    assert raw[0]["kind"] == "vault"


# ── 自我驗收：真的驗，不是假驗 ───────────────────────────────────────────────

def test_verification_fails_if_kind_not_actually_written(registry_path):
    """⭐ 自我驗收若只是靠記憶體就太水了。這裡把驗收結果動手腳
    → 應該拋而不報 OK。"""
    record_user_leader(registry_path, address=_TARGET, added_by=_ACCT)

    fake_gw = FakeGateway()
    fake_gw.vault_addresses[_TARGET] = {"name": "Vault Alpha"}

    # 正常 backfill 成功
    result = backfill(registry_path, gateway=fake_gw)
    assert result.exit_code == 0

    # 驗證已經檢查檔案裡真的有 kind:vault
    entries = load_user_leaders(registry_path)
    assert entries[0].kind == "vault"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
