"""單元測試結構性斷網（工程原則：測試不碰真實世界）。

autouse fixture 對非 integration 測試禁用 socket 連線——確保「離線」是結構保證，
不是恰好沒人連網。integration 測試（-m integration 顯式執行）不受限。"""
import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(request):
    if request.node.get_closest_marker("integration"):
        yield
        return
    real_socket = socket.socket

    def guard(*args, **kwargs):
        raise RuntimeError("單元測試禁止網路連線（integration 測試請加 -m integration）")

    socket.socket = guard
    try:
        yield
    finally:
        socket.socket = real_socket
