"""src/spark/publicapi/siwe.py
EIP-4361（SIWE）：伺服器權威重建訊息 + eth_account 驗簽（EIP-191 personal_sign）。
刻意不解析前端送來的自由文本：domain/URI 出自伺服器設定、nonce/issued_at 出自伺服器
儲存，前端只能簽「伺服器重建得出來的訊息」——綁 domain/URI 因此是結構保證。"""
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address


def build_siwe_message(*, domain: str, uri: str, address: str, chain_id: int,
                       nonce: str, issued_at: str) -> str:
    """EIP-4361 標準版型（Version 1）；address 以 EIP-55 checksum 呈現。"""
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{to_checksum_address(address)}\n"
        "\n"
        "Sign in to Filet.\n"
        "\n"
        f"URI: {uri}\n"
        "Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def recover_siwe_signer(message: str, signature: str) -> str:
    """personal_sign recover；壞簽名拋例外（eth_account 系），呼叫端轉 401。"""
    return Account.recover_message(encode_defunct(text=message), signature=signature)
