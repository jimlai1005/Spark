"""tests/test_script_default_paths.py — 緊急／排程工具的預設路徑錨定（CWD 無關）。

⭐ 本檔測的是**行為**，不是常數字串：每個案例都在 `tmp_path` 底下放一個
CWD 相對路徑會命中的「誘餌」manifest，然後 chdir 過去實際呼叫 `main()`，
斷言腳本解析到的是 repo 根那一份、**沒有**碰到誘餌。

為什麼要有這一層：`panic_all` 是緊急工具，出事時操作者從哪個目錄 ssh 進來無法預期。
CWD 相對的預設路徑會在最需要它的那一刻讓它找不到 manifest（大聲失敗，非 fail-open，
但緊急工具停擺本身不可接受）；`filet_daily_report` 的 SNAPSHOT_PATH 更壞——讀錯位置
會被當成「無檔＝0」，於是把全期累積量當成當日增量報成北極星數字。
"""
from pathlib import Path

import pytest

import scripts.filet_daily_report as fdr
import scripts.panic_all as panic_all

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_MANIFEST = _REPO_ROOT / "var" / "filet" / "followers.json"


@pytest.fixture
def decoy_cwd(tmp_path, monkeypatch):
    """在 tmp_path 造一份 CWD 相對路徑會命中的誘餌 manifest，並 chdir 過去。

    誘餌**必須真的存在**——否則「沒讀到誘餌」可能只是因為它不存在，
    測試會在 CWD 相對的實作下也一樣通過（＝測了個寂寞）。
    """
    decoy = tmp_path / "var" / "filet" / "followers.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text('{"followers": []}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FILET_FOLLOWERS", raising=False)
    assert Path("var/filet/followers.json").resolve() == decoy  # 誘餌確實在 CWD 下
    return decoy


def _capture_manifest_path(module, monkeypatch) -> list[Path]:
    """攔截該模組解析出來的 manifest 路徑；以 FileNotFoundError 讓 main() 早退
    （不觸網、不建 keystore、不寫任何檔）。"""
    seen: list[Path] = []

    def _fake(path):
        seen.append(Path(path))
        raise FileNotFoundError(path)

    monkeypatch.setattr(module, "load_followers_tolerant", _fake)
    return seen


@pytest.mark.parametrize("module,call", [
    (panic_all, lambda m: m.main([])),          # dry-run（無 --yes，零寫入）
    (fdr, lambda m: m.main()),
])
def test_manifest_resolves_to_repo_root_from_foreign_cwd(module, call, decoy_cwd,
                                                         monkeypatch):
    """⭐ 從別的工作目錄呼叫 → 仍解析到 repo 根的 manifest，不吃 CWD 下的誘餌。"""
    seen = _capture_manifest_path(module, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        call(module)
    assert ei.value.code == 2                    # manifest 不存在 → 大聲退出
    assert len(seen) == 1
    resolved = seen[0]
    assert resolved.is_absolute(), resolved
    assert resolved == _EXPECTED_MANIFEST, resolved
    assert resolved != decoy_cwd                 # 這一行才是本修復的重點


@pytest.mark.parametrize("module", [panic_all, fdr])
def test_default_manifest_constant_is_repo_anchored(module):
    """常數層的同源檢查（與上面的行為測試互補，不取代它）。"""
    p = Path(module.DEFAULT_MANIFEST)
    assert p.is_absolute(), p
    assert p == _EXPECTED_MANIFEST, p


def test_daily_report_snapshot_and_reports_dir_also_anchored(decoy_cwd):
    """SNAPSHOT_PATH／REPORTS_DIR 與 DEFAULT_MANIFEST 同源於 VAR_DIR：
    只錨定 manifest 會讓同一份 var/filet 一半絕對一半相對（讀寫兩邊各指一處）。
    快照讀錯位置＝「無檔視為 0」→ 全期累積量被當成當日增量報出去。"""
    for p in (fdr.SNAPSHOT_PATH, fdr.REPORTS_DIR, fdr.VAR_DIR):
        assert Path(p).is_absolute(), p
        assert Path(p).is_relative_to(_REPO_ROOT), p
    assert fdr.VAR_DIR == _REPO_ROOT / "var" / "filet"
    # chdir 到誘餌目錄之後，模組常數不得跟著漂移（＝它們不是 CWD 相對的）
    assert Path(fdr.SNAPSHOT_PATH).parent != decoy_cwd.parent


def test_env_override_still_wins(tmp_path, monkeypatch):
    """錨定不得吃掉既有的 FILET_FOLLOWERS 覆寫（部署照舊可用）。"""
    override = tmp_path / "custom.json"
    monkeypatch.setenv("FILET_FOLLOWERS", str(override))
    seen = _capture_manifest_path(panic_all, monkeypatch)
    with pytest.raises(SystemExit):
        panic_all.main([])
    assert seen == [override]
