"""src/spark/publicapi/siwe.py
EIP-4361（SIWE）：伺服器權威重建訊息 + eth_account 驗簽（EIP-191 personal_sign）。
刻意不解析前端送來的自由文本：domain/URI 出自伺服器設定、nonce/issued_at 出自伺服器
儲存，前端只能簽「伺服器重建得出來的訊息」——綁 domain/URI 因此是結構保證。"""
from eth_utils import to_checksum_address

# ⭐ recover 原語下沉到 spark.filet.signing（2026-07-19）：引擎也要驗客戶簽章
# （換 leader 記錄的二次驗證，見 filet/leader_change.py），而依賴方向是單向的
# publicapi → filet——留在這裡的話引擎 import 就會成環。兩邊必須是**同一份**驗簽
# 程式碼（工程原則 1：同源），故本模組只保留 SIWE 專屬的訊息版型。
from spark.filet.signing import recover_personal_sign_address


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
    """personal_sign recover（回正規化小寫）；壞簽名拋例外，呼叫端轉 401。

    薄殼：實作在 spark.filet.signing.recover_personal_sign_address（見 import 處的
    依賴方向說明）。保留這個名字是為了讓 SIWE 呼叫點讀起來仍是 SIWE 語意。
    """
    return recover_personal_sign_address(message, signature)
