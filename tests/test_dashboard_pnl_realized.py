"""tests/test_dashboard_pnl_realized.py
Opus 審查 Warning 5：`pnl.realized` 恆為 `null`——舊算法 `cum_pnl(30 天窗) −
unrealized(全部位開倉以來的快照)` 混了兩個不同基準的窗口，長期持倉時可以錯到
反號（工程原則 1：比較/相減的兩側必須同源同窗口）。本檔鎖住新契約：即使
`perf.status=="ok"` 且目前持倉有非 None 的未實現損益，`realized` 仍然回 `null`
（不拼湊一個異基準的近似值），其餘 pnl 欄位（`net`／`unrealized`）不受影響。

全離線（tests/conftest.py 的 autouse socket-ban；TestClient 段落沿
test_me_dashboard.py 的既有 `_allow_local_sockets` fixture 放行 loopback）。
"""
import socket

import pytest

from tests.test_me_dashboard import (_logged_in, acct, clearinghouse, fill,
                                     portfolio_rows, position, write_hb)

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def test_realized_is_always_none_even_with_ok_perf_and_unrealized(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    write_hb(cfg, acct(wallet))
    # 持倉有未實現損益（unrealized 非 None）＋ portfolio 回應是合法的 perpMonth
    # 窗口（perf.status == "ok"）——舊算法在這個組合下會產出一個異基準的
    # `realized` 數字；新契約要求它維持 null。
    hl.clearinghouse[addr] = clearinghouse(positions=[position(upnl="12.34")])
    hl.portfolios[addr] = portfolio_rows([(0, "1000", "0"), (10, "1050", "50")])
    hl.fills[addr] = [fill(addr)]

    body = client.get("/api/me/dashboard").json()

    assert body["pnl"] is not None
    assert body["pnl"]["realized"] is None
    assert body["pnl"]["unrealized"] == "12.34"
    assert body["pnl"]["net"] is not None  # net 仍照舊算法（同窗口 cum_pnl − fees），不受影響
