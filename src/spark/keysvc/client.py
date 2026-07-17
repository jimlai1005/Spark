"""src/spark/keysvc/client.py
public API 用來呼叫 key-service 的 client：generate（寫）與 address（唯讀）。"""
import socket

from spark.keysvc.protocol import (AddressRequest, GenerateRequest,
                                   decode_response, encode_request)


class KeysvcError(RuntimeError):
    """keysvc 回 ok=False 時拋出。code 供呼叫端結構化分支（"exists"/"invalid"/
    "missing"/"internal"/None）——不得比對訊息字串。繼承 RuntimeError：既有
    `pytest.raises(RuntimeError)` 測試維持綠。"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class KeysvcClient:
    def __init__(self, sock_path: str):
        self._sock_path = sock_path

    def _call(self, req) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(self._sock_path)
            c.sendall(encode_request(req))
            line = c.makefile("rb").readline()
        resp = decode_response(line)
        if not resp.ok:
            raise KeysvcError(f"keysvc 失敗: {resp.error}", code=resp.code)
        return resp.agent_address

    def generate(self, account_id: str) -> str:
        """請 key-service 生成 agent、回 agent 地址。失敗 raise KeysvcError（含 code；
        不含私鑰——key-service 本來就不回私鑰）。"""
        return self._call(GenerateRequest(account_id))

    def address(self, account_id: str) -> str:
        """唯讀：查既有 agent 地址（desync 自癒用）。無 key → KeysvcError(code="missing")。"""
        return self._call(AddressRequest(account_id))
