"""tests/test_run_api_wiring.py — Task 3.4：`scripts/run_api.py` 接線。

驗證 `EXPLORE_UPSTREAM_REFRESH` 是否真的決定 `explore-scheduler` 背景 thread
有沒有被啟動。全離線：
- `uvicorn.run` 換成 no-op（不起真的 HTTP server）。
- `threading.Thread.start` 換成只記錄呼叫（不真的跑 thread body）——排程
  thread 一啟動就會經 `HLGateway` 打上游，autouse socket-ban 會擋下，但那不是
  本測試要驗證的行為（排程本身的行為見 `tests/test_explore_scheduler.py`），
  這裡只驗證「接線有沒有啟動它」。
"""
from __future__ import annotations

import threading

import pytest

import scripts.run_api as run_api


def _env(tmp_path, **over):
    base = {
        "FILET_API_NETWORK": "testnet",
        "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
        "FILET_SIWE_DOMAIN": "filet.example",
        "FILET_SIWE_URI": "https://filet.example",
        "FILET_API_DB": str(tmp_path / "api.db"),
        "FILET_KEYSVC_SOCK": str(tmp_path / "keysvc.sock"),
        "FILET_PENDING_PATH": str(tmp_path / "pending.json"),
        "FILET_EXCHANGE_DIR": str(tmp_path / "exchange"),
        "FILET_STATE_BASE": str(tmp_path / "state"),
        "FILET_LEADERS_PATH": str(tmp_path / "leaders.json"),
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_real_server(monkeypatch):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)


@pytest.fixture
def _started_threads(monkeypatch):
    """記錄每一個被 `.start()` 過的 `threading.Thread` 實例，但不真的執行
    thread body（`name` 在 `__init__` 就已經定案，不受此影響）。"""
    started: list[threading.Thread] = []

    def fake_start(self):
        started.append(self)

    monkeypatch.setattr(threading.Thread, "start", fake_start)
    return started


def test_explore_upstream_refresh_unset_starts_no_scheduler_thread(
        tmp_path, monkeypatch, _started_threads):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EXPLORE_UPSTREAM_REFRESH", raising=False)
    monkeypatch.delenv("FILET_EXPLORE_DB", raising=False)

    run_api.main()

    assert not any(t.name == "explore-scheduler" for t in _started_threads)


def test_explore_upstream_refresh_enabled_starts_scheduler_thread(
        tmp_path, monkeypatch, _started_threads):
    env = _env(tmp_path, EXPLORE_UPSTREAM_REFRESH="1",
               FILET_EXPLORE_DB=str(tmp_path / "explore.db"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    run_api.main()

    matches = [t for t in _started_threads if t.name == "explore-scheduler"]
    assert len(matches) == 1
    assert matches[0].daemon is True
