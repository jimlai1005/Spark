from abc import ABC, abstractmethod
from typing import Any


class KeyStore(ABC):
    @abstractmethod
    def get_main_signer(self, account_id: str) -> Any: ...
    @abstractmethod
    def get_agent_signer(self, account_id: str) -> Any: ...
