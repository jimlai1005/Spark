"""src/spark/keystore/envfile.py
檔案後端 keystore：引擎專用。只讀 agent key；get_main_signer 一律拒絕
（非託管不變量結構化——引擎進程物理上不持有主錢包鑰匙）。"""
import os
import stat
from pathlib import Path
from eth_account import Account
from spark.filet.followers import validate_account_id
from spark.keystore.base import KeyStore


class EnvFileKeyStore(KeyStore):
    """agent key 存 <root>/<account_id>/agent.key（純 hex、權限 600）。
    get_agent_signer 讀檔前硬檢查權限。get_main_signer 一律 raise。
    私鑰不進 log/repr/例外（例外只提路徑）。"""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    def _agent_path(self, account_id: str) -> Path:
        return self._root / account_id / "agent.key"

    def get_main_signer(self, account_id: str):
        raise PermissionError(
            "engine keystore holds no main keys (non-custodial invariant); "
            "main-key signing belongs to the onboarding backend only")

    def get_agent_signer(self, account_id: str):
        validate_account_id(account_id)  # 縱深防禦：建路徑前先鎖字元集（M2 Task 10）
        path = self._agent_path(account_id)
        if not path.exists():
            raise KeyError(f"no agent key for account {account_id} at {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(
                f"agent key {path} has unsafe permissions {oct(mode)}; expected 0o600")
        return Account.from_key(path.read_text().strip())

    def import_agent_key(self, account_id: str, private_key: str) -> None:
        """寫入 agent key（供 onboarding 後端，Phase C 用）。父目錄 700、檔案 600。"""
        validate_account_id(account_id)  # 縱深防禦：建路徑前先鎖字元集（M2 Task 10）
        d = self._root / account_id
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        path = self._agent_path(account_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, private_key.strip().encode())
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
