"""src/spark/keysvc/client.py
public API 用來呼叫 key-service 的 client：generate（寫）與 address（唯讀）。"""
import socket

from spark.keysvc.protocol import (AddressRequest, GenerateRequest,
                                   decode_response, encode_request)

_TIMEOUT_S = 10.0  # 對齊 spark.publicapi.hl 的 IO 邊界慣例


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
        """單一 IO 邊界（工程原則 5）：把「readline 回空 bytes」（server 在 accept 後、
        sendall 前崩潰）與「decode_response 解碼失敗」兩種 IO/解碼層失敗，收斂成
        ConnectionError（OSError 子類）——讓 app 既有 `except OSError`/`except
        ConnectionError` 路徑接住、轉譯成 502（transient，可重試），而不是被誤判成
        JSONDecodeError（ValueError 子類，落不進上述 handler）而變成通用 500。
        注意：keysvc 正常回應的 ok=False（KeysvcError）不在此轉譯範圍內——那是
        semantic 失敗，維持原樣上拋。"""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(_TIMEOUT_S)  # 防 keysvc wedge 無限期阻塞（DoS，工程原則 3）
            c.connect(self._sock_path)
            c.sendall(encode_request(req))
            line = c.makefile("rb").readline()
        if line == b"":
            raise ConnectionError("keysvc 回應中斷：連線在收到完整回應前關閉")
        try:
            resp = decode_response(line)
        except ValueError as e:  # json.JSONDecodeError 等——截斷/壞回應，非 semantic
            raise ConnectionError(f"keysvc 回應中斷：解碼失敗 ({e})") from e
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
