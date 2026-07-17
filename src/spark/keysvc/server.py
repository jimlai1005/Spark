"""src/spark/keysvc/server.py
key-service 的核心處理。generate：生成 agent keypair、寫入 keystore、只回地址。
私鑰絕不進回應/log。（socket accept 迴圈在 Task 4 加入，與授權器一起。）"""
import logging

from eth_account import Account

from spark.keystore.envfile import EnvFileKeyStore
from spark.keysvc.protocol import GenerateRequest, Response

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
