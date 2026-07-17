"""src/spark/keysvc/server.py
key-service 的核心處理。generate：生成 agent keypair、寫入 keystore、只回地址。
私鑰絕不進回應/log。serve_forever：unix socket accept 迴圈，連線先過 SO_PEERCRED 授權。"""
import logging
import os
import socket
from collections.abc import Callable
from pathlib import Path

from eth_account import Account

from spark.keystore.envfile import EnvFileKeyStore
from spark.keysvc.protocol import GenerateRequest, Response, decode_request, encode_response

logger = logging.getLogger(__name__)


def handle_generate(req: GenerateRequest, ks: EnvFileKeyStore) -> Response:
    """生成 agent keypair → 寫 keystore（O_EXCL）→ 回地址。任何失敗回 Response(ok=False)，
    私鑰絕不進回應/log/例外訊息。"""
    try:
        acct = Account.create()  # os.urandom 亂數；私鑰只存在此區域變數
        ks.import_agent_key(req.account_id, acct.key.hex())  # O_EXCL：存在即 FileExistsError
    except FileExistsError:
        return Response(ok=False, error=f"account {req.account_id} 已有 agent key，不重生")
    except ValueError as e:  # validate_account_id 等——e 不含私鑰
        return Response(ok=False, error=str(e))
    except Exception:  # noqa: BLE001 — 不外洩細節（可能含路徑，不含私鑰）
        logger.exception("keysvc generate 失敗 account=%s", req.account_id)  # 不 log 私鑰
        return Response(ok=False, error="internal error")
    return Response(ok=True, agent_address=acct.address)


def serve_forever(sock_path: str, ks: EnvFileKeyStore,
                  authorize_peer: Callable[[socket.socket], bool],
                  stop=None) -> None:
    """監聽 unix socket；每個連線：授權 → 讀一個 request → 處理 → 回一個 response → 關。
    未授權連線直接關閉不處理。stop（threading.Event）供測試/優雅停止。"""
    p = Path(sock_path)
    if p.exists():
        p.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o660)
    srv.listen(8)
    srv.settimeout(0.5)
    try:
        while stop is None or not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                if not authorize_peer(conn):
                    logger.warning("keysvc 拒絕未授權連線")
                    continue
                line = conn.makefile("rb").readline()
                try:
                    req = decode_request(line)
                    resp = handle_generate(req, ks)
                except ValueError as e:
                    resp = Response(ok=False, error=str(e))
                except Exception:
                    logger.exception("keysvc 處理連線失敗")
                    resp = Response(ok=False, error="bad request")
                conn.sendall(encode_response(resp))
    finally:
        srv.close()
        if p.exists():
            p.unlink()
