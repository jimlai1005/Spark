"""src/spark/filet/leaders.py
策劃 leader 白名單（JSON）→ list[LeaderRef]。

**這份檔案是資安邊界，不是設定檔**。客戶可選的 leader 只能來自這裡；白名單本身
只有管理端能寫（檔案權限由部署保證，filet-api 對它不該有寫權——同 followers.json
的權限拓撲，見 publicapi/pending.py 檔頭）。
"""
import json
from dataclasses import dataclass
from pathlib import Path

# 位址正規化沿用 filet 內部的單一定義（同基準比較，工程原則 1）。
# 不用 publicapi.config.normalize_address：那會造成循環相依，理由見該函式 docstring。
from spark.filet.followers import normalize_hex_address


@dataclass(frozen=True)
class LeaderRef:
    address: str          # 正規化小寫
    name: str             # 顯示名稱
    description: str = ""
    enabled: bool = True   # 下架用（不刪除，保留歷史）


def load_leaders(path: str | Path) -> list[LeaderRef]:
    """讀 leader 白名單。

    **資安核心**：這份清單是「客戶可以選的 leader」的唯一合法來源，只有管理端能寫
    （檔案權限由部署保證）。API 寫入客戶選擇時要驗、**引擎使用前要再驗一次**——
    後者是防「API 進程被打穿後把 follower 指向惡意 leader」的結構性防線，
    不得因為「API 已經驗過」而省略。（威脅模型：攻擊者若能改 manifest 的
    leader_address，可把 follower 指向瘋狂交易榨 builder fee 或反向交易的地址；
    白名單檔他寫不到，所以引擎側的二次驗證才是真正擋下來的那一道。）

    檔案不存在 → 回空清單（**不是**錯誤：尚未策劃任何 leader 是合法狀態）。
    格式錯誤 → raise（fail-fast：白名單壞掉時寧可停也不要放行未經審核的 leader；
    「壞掉就當空清單」會讓一個手滑的編輯靜默放行／擋掉所有人，兩邊都不可接受——
    這裡選擇大聲炸）。
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"leader 白名單不是合法 JSON: {p}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"leader 白名單頂層須為物件: {p}")
    raw = data.get("leaders", [])
    if not isinstance(raw, list):
        raise ValueError(f"leader 白名單的 leaders 須為陣列: {p}")
    leaders: list[LeaderRef] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"leaders[{i}] 須為物件: {entry!r}")
        addr = normalize_hex_address(f"leaders[{i}].address", entry.get("address", ""))
        if addr in seen:
            # 重複條目 = 兩筆可能矛盾的 enabled 狀態（一筆下架一筆啟用），
            # 靜默取其一等於讓下架失效 → fail-fast。
            raise ValueError(f"leaders[{i}] address 重複: {addr}")
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"leaders[{i}].name 不得為空: {name!r}")
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"leaders[{i}].description 須為字串: {description!r}")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            # 不接受 truthy 值（"false"、0、1）：下架旗標被字串 "false" 判成 True
            # 是靜默放行下架中的 leader，屬安全關鍵誤判 → 只收真正的 bool。
            raise ValueError(f"leaders[{i}].enabled 須為布林: {enabled!r}")
        seen.add(addr)
        leaders.append(LeaderRef(addr, name, description, enabled))
    return leaders


def is_allowed_leader(address: str, leaders: list[LeaderRef]) -> bool:
    """該地址是否在白名單且 enabled。位址比較一律正規化小寫（同基準，工程原則 1）。

    address 不合法（非 0x + 40 hex）→ False，不 raise：呼叫點是閘門，
    「不合法」的正確語意就是「不放行」。
    """
    try:
        target = normalize_hex_address("address", address)
    except ValueError:
        return False
    return any(x.address == target and x.enabled for x in leaders)
