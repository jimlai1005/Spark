"""src/spark/keysvc/client.py
public API 用來呼叫 key-service 的 client。只有 generate。"""
import socket

from spark.keysvc.protocol import (GenerateRequest, decode_response,
                                    encode_request)


class KeysvcClient:
    def __init__(self, sock_path: str):
        self._sock_path = sock_path

    def generate(self, account_id: str) -> str:
        """請 key-service 生成 agent、回 agent 地址。失敗 raise RuntimeError（含錯誤訊息，
        不含私鑰——key-service 本來就不回私鑰）。"""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(self._sock_path)
            c.sendall(encode_request(GenerateRequest(account_id)))
            line = c.makefile("rb").readline()
        resp = decode_response(line)
        if not resp.ok:
            raise RuntimeError(f"keysvc generate 失敗: {resp.error}")
        return resp.agent_address
