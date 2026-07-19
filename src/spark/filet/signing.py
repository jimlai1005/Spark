"""src/spark/filet/signing.py
EIP-191（personal_sign）簽章還原——**全 repo 唯一的 recover 呼叫點**。

放在 `spark.filet` 而不是 `spark.publicapi` 的理由（依賴方向）
-------------------------------------------------------------
驗簽有兩個使用者，而且必須用**同一份**程式碼（工程原則 1：同源；兩份實作遲早漂移，
漂移的方向不對稱——寬鬆的那一份會放行嚴格的那一份會擋下的東西）：

- **API**（`spark.publicapi`）：SIWE 登入、客戶簽章的換 leader 請求。
- **引擎**（`spark.filet` / `scripts`）：套用換 leader 記錄前的**二次驗章**
  （見 leader_change.py 檔頭的威脅模型）。

repo 的依賴方向是單向的 `publicapi → filet`（publicapi/app.py、config.py、store.py、
pending.py、billing.py、ops.py 皆 import spark.filet；filet 下無任何模組 import
publicapi——followers.py:53-57 已把這個方向寫成慣例）。若把驗簽留在
`publicapi/siwe.py`，引擎要用就得 `filet → publicapi`，正好把方向掉頭成環。
所以原語下沉到 filet，publicapi/siwe.py 改為薄薄地引用本模組。

本模組刻意只做「還原簽章者」一件事，不含任何訊息語意（SIWE 版型、換 leader 版型
各自在自己的模組建訊息）——它是被共用的那一層，長出語意就會逼兩邊互相遷就。
"""
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.filet.followers import normalize_hex_address


def recover_personal_sign_address(message: str, signature: str) -> str:
    """personal_sign（EIP-191）還原簽章者，回**正規化小寫**位址。

    回小寫而非 eth_account 原本的 EIP-55 checksum：這個回傳值的唯一用途是拿去和
    另一個位址比對，兩側必須同基準（工程原則 1）。讓呼叫端各自記得 normalize
    是把同基準的責任外包給記性——大小寫不同的兩個相同位址會比成不相等，
    方向是 fail-closed（擋下合法客戶）但同樣是錯的。

    簽名格式壞掉／長度不對／不是 hex → 拋例外（eth_account 系的 ValueError 等）。
    **不在此吞掉**：呼叫端才知道該轉成哪種對外語意（API 轉 4xx、引擎轉拒絕套用），
    在這裡靜默回 None 會讓「驗不出來」與「驗出別人」長得一樣。
    """
    return normalize_hex_address(
        "recovered_signer",
        Account.recover_message(encode_defunct(text=message), signature=signature))
