"""src/spark/filet/leaders.py
策劃 leader 白名單（JSON）→ list[LeaderRef]。

**這份檔案是資安邊界，不是設定檔**。客戶可選的 leader 只能來自這裡；白名單本身
只有管理端能寫（檔案權限由部署保證，filet-api 對它不該有寫權——同 followers.json
的權限拓撲，見 publicapi/pending.py 檔頭）。

⭐ 兩種「下架」是兩件事，用兩個旗標（2026-07-19 opus 對抗性審查 Critical）
----------------------------------------------------------------------
早期只有一個 `enabled`，被迫同時承擔「例行下架」與「安全撤銷」兩種語意，於是
**管理端把出事的 leader 標成 enabled:false 之後，正在跟他的 follower 會繼續跟下去**
（引擎執行中的解析失敗一律「沿用上一個已驗證 leader」）——操作者卻因為「我已下架＝
已止血」的錯誤認知而不去按真正的停止鍵。現在拆成：

- `enabled`（安全撤銷）：False ＝ **這個 leader 現在有問題**（帳號被盜、對敲、策略
  失控）。後果：新客戶選不到，**且正在跟的 follower 立刻進入受控收尾**——停止開新倉、
  平掉現有部位、鎖死 kill switch（見 leader_resolve.LeaderRevokedError 與 LeaderWatch）。
  這是有實際 taker 成本、需人工 re-arm 的重動作，只在真的要止血時用。
- `accepting_new`（例行下架）：False ＝ **不再開放新客戶選擇**（名額滿、策略調整中、
  準備退場）。後果：新客戶選不到，**正在跟的 follower 完全不受影響**，繼續正常跟單。

**把 leader 整筆從清單移除 等同 enabled:false**（引擎驗不到就是撤銷）——所以例行
下架請改 `accepting_new`，不要刪條目，否則會意外觸發所有跟隨者的收尾。

⚠️⚠️ 但這條等價關係**只在「本檔是唯一來源」時成立**（2026-07-27 Finding 2）
----------------------------------------------------------------------
自從有了 user-sourced registry（`user_leaders.py`），引擎驗的是**合併清單**，而合併
是「精選優先、缺則由 registry 遞補」（`merge_leaders`）。於是同一個位址若**同時**
存在於兩檔，刪掉精選那筆並不是撤銷——registry 那筆 `enabled:true` 會遞補上來，
`is_still_permitted` 從 False 翻回 True，**撤銷靜默失效**（錨定：
tests/test_filet_user_leaders.py::test_deleting_the_curated_entry_falls_back_to_the_registry_entry）。

所以：**唯一在任何情況下都有效的撤銷，是「確保存在一筆 `enabled:false` 的精選條目」**
——不是刪條目。不要靠記性做這件事，用 `scripts/revoke_leader.py <address>`：它冪等地
把該位址在精選檔設成 `enabled:false`（沒有就補一筆）、在 registry 有同位址時一併停用，
最後**自己重載兩檔合併驗一次** `is_still_permitted is False`，不成立就非零退出。

呼叫端不該自己記得該看哪個旗標，所以本模組**不提供**籠統的 `is_allowed`，
只提供兩個把用途寫在名字裡的述詞：`is_selectable`（選擇時）與
`is_still_permitted`（引擎持續驗證時）。
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
    # ⭐ 兩個旗標的語意差異見檔頭：enabled=安全撤銷（會收尾正在跟的人）、
    # accepting_new=例行下架（只擋新客戶）。搞混的代價是「以為止血了其實沒有」。
    enabled: bool = True
    accepting_new: bool = True


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
        accepting_new = entry.get("accepting_new", True)
        if not isinstance(accepting_new, bool):
            raise ValueError(
                f"leaders[{i}].accepting_new 須為布林: {accepting_new!r}")
        seen.add(addr)
        leaders.append(LeaderRef(addr, name, description, enabled, accepting_new))
    return leaders


def _lookup(address: str, leaders: list[LeaderRef]) -> LeaderRef | None:
    """位址比較一律正規化小寫（同基準，工程原則 1）。

    address 不合法（非 0x + 40 hex）→ None，不 raise：呼叫點是閘門，
    「不合法」的正確語意就是「查無此人」→ 兩個述詞都會因此回 False（不放行）。
    """
    try:
        target = normalize_hex_address("address", address)
    except ValueError:
        return None
    return next((x for x in leaders if x.address == target), None)


def is_selectable(address: str, leaders: list[LeaderRef]) -> bool:
    """**新客戶現在可以選這個 leader 嗎**（activate CLI／選單用的述詞）。

    兩個旗標都要過：`enabled`（沒有安全問題）**且** `accepting_new`（還收新客戶）。
    這是比 is_still_permitted 嚴格的一側——擋錯了只是少一個選項，放錯了是把新客戶
    送給一個已經停止接客或已出事的 leader。
    """
    ref = _lookup(address, leaders)
    return ref is not None and ref.enabled and ref.accepting_new


def is_still_permitted(address: str, leaders: list[LeaderRef]) -> bool:
    """**正在跟這個 leader 的 follower 可以繼續跟嗎**（引擎每輪驗證用的述詞）。

    只看 `enabled`：`accepting_new=False` 是例行下架，對已在跟的人**無影響**
    （硬要把他們趕走等於用一次真實的平倉成本去執行一個純行銷決策）。
    回 False ＝ 安全撤銷或整筆不在**傳進來的這份清單**裡 → 呼叫端必須受控收尾，
    不得沿用舊值。⚠️ 引擎傳進來的是**合併清單**，所以「從精選檔刪掉條目」不等於
    這裡會回 False（registry 可能有同位址的 enabled:true 條目遞補）——檔頭的但書。
    """
    ref = _lookup(address, leaders)
    return ref is not None and ref.enabled
