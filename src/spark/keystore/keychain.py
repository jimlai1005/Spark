"""macOS Keychain 後端。私鑰只存 Keychain，不落 repo/log。"""
import keyring
from eth_account import Account
from spark.keystore.base import KeyStore


class MacKeychainBackend(KeyStore):
    def __init__(self, service: str = "spark"):
        self._service = service

    def _entry(self, account_id: str, role: str) -> str:
        return f"{account_id}:{role}"

    def import_key(self, account_id: str, role: str, private_key: str) -> None:
        """一次性匯入。role ∈ {'main','agent'}。"""
        if role not in ("main", "agent"):
            raise ValueError(f"role must be main/agent, got {role}")
        keyring.set_password(self._service, self._entry(account_id, role), private_key)

    def _load(self, account_id: str, role: str):
        pk = keyring.get_password(self._service, self._entry(account_id, role))
        if pk is None:
            raise KeyError(f"no {role} key for account {account_id}")
        return Account.from_key(pk)

    def get_main_signer(self, account_id: str):
        return self._load(account_id, "main")

    def get_agent_signer(self, account_id: str):
        return self._load(account_id, "agent")
