"""src/spark/keysvc/peercred.py
SO_PEERCRED 連線者授權（Linux）。回一個 authorize_peer(sock)->bool。
非 Linux（開發 macOS）沒有 SO_PEERCRED——生產在 Linux 跑，測試用 mock 打包驗邏輯。"""
import socket
import struct
from collections.abc import Callable

_PEERCRED_FMT = "3I"  # pid, uid, gid（unsigned：uid>2^31 才不會被解成負數）
# socket.SO_PEERCRED 只存在於 Linux——macOS 沒有這個常數，連 import 都不會有它。
# 用 getattr 保留 Linux 上的真實值，macOS 上退回硬編的 Linux 常數（17），
# 讓本模組在 macOS 開發機上仍可 import／被 mock 測試（syscall 本身仍只在 Linux 生效）。
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)


def make_peercred_authorizer(allowed_uids: set[int]) -> Callable[[socket.socket], bool]:
    def authorize(sock: socket.socket) -> bool:
        raw = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED,
                              struct.calcsize(_PEERCRED_FMT))
        _pid, uid, _gid = struct.unpack(_PEERCRED_FMT, raw)
        return uid in allowed_uids
    return authorize
