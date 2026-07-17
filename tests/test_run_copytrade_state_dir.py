"""per-follower 狀態根隔離（M2 Task 4）。

驗證 FILET_STATE_DIR 決定狀態根（缺省＝repo 根，保留 M1 單實例行為）；
兩個不同狀態根的 kill switch（ARM 檔）互不干擾；--status 路徑（_print_status）
用注入的 state_root 讀 ARM，不會誤讀共用 repo 根的狀態（連坐 bug 的直接迴歸測試）。
"""
from decimal import Decimal

import scripts.run_copytrade as rc
from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import ARM_FILE_RELPATH, is_tripped
from spark.exchange.base import EquityView


def test_resolve_state_dir_defaults_to_repo(monkeypatch):
    monkeypatch.delenv("FILET_STATE_DIR", raising=False)
    assert rc.resolve_state_dir() == rc._REPO_ROOT


def test_resolve_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path))
    assert rc.resolve_state_dir() == tmp_path


def test_resolve_state_dir_relative_env_becomes_absolute(monkeypatch, tmp_path):
    """T4 reviewer minor：相對路徑 FILET_STATE_DIR 必須 .resolve() 成絕對——否則
    同一相對路徑在不同啟動目錄（CWD）下會靜默指向不同狀態根，kill switch ARM
    檔可能因此失聯。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FILET_STATE_DIR", "relative/sub")
    result = rc.resolve_state_dir()
    assert result.is_absolute()
    assert result == (tmp_path / "relative" / "sub").resolve()


def test_two_state_dirs_isolate_killswitch(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    (a / ARM_FILE_RELPATH).parent.mkdir(parents=True)
    (a / ARM_FILE_RELPATH).write_text("tripped")
    assert is_tripped(a) is True    # A 已 trip
    assert is_tripped(b) is False   # B 不受影響（隔離）


class _FakeAdapter:
    """--status 只讀路徑用的假 adapter：零網路、零外呼。"""

    def get_equity_view(self, addr):
        return EquityView(current=Decimal("100"), recent_peak=Decimal("100"))

    def get_positions(self, addr):
        return []

    def get_open_orders(self, addr):
        return []


def test_status_reports_tripped_from_injected_state_root(tmp_path, capsys):
    """--status 的 _print_status 呼叫點必須吃 state_root，而非硬編的共用 repo 根
    ——否則 follower B 會誤讀 follower A trip() 寫的 ARM 檔（連坐）。"""
    tripped_dir = tmp_path / "follower_a"
    (tripped_dir / ARM_FILE_RELPATH).parent.mkdir(parents=True)
    (tripped_dir / ARM_FILE_RELPATH).write_text("tripped")
    healthy_dir = tmp_path / "follower_b"

    rc._print_status(_FakeAdapter(), "0x" + "a" * 40, CopySettings(), tripped_dir)
    assert "killswitch tripped: True" in capsys.readouterr().out

    rc._print_status(_FakeAdapter(), "0x" + "a" * 40, CopySettings(), healthy_dir)
    assert "killswitch tripped: False" in capsys.readouterr().out
