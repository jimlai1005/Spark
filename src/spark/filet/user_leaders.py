"""src/spark/filet/user_leaders.py
用戶自訂 leader 的 **user-sourced registry**（user_leaders.json）：載入、合併、寫入。

與精選白名單（leaders.py）的關係
--------------------------------
- **精選白名單**是管理端手編的資安邊界，filet-api 對它**沒有寫權**（leaders.py 檔頭）。
- **本 registry** 是 public API 在自訂 leader 通過准入檢查後**自動寫入**的獨立檔——
  兩檔分開，服務自動寫入與人工編輯才不會互相蓋寫（spec 2026-07-27-custom-leader-input）。
- 讀取時兩檔**合併**（merge_leaders），且**精選條目一律優先**：同位址的 user 條目
  被忽略。這是 operator kill-switch 的承重點——精選檔把某位址標成 enabled=false 之後，
  用戶不能經自訂路徑重新准入他（合併後看到的是精選那筆 enabled=false 的條目）。
- user-sourced 條目的權限語義是「全域 permitted、不 listed」：引擎驗證（
  is_still_permitted）看得到它，公開目錄（/api/leaders）不含它。「僅本人可用」由
  API 層的可見性實現，不是引擎層的帳戶綁定（任何用戶本就可自行輸入同一位址走准入）。

路徑推導（user_leaders_path_for）
--------------------------------
registry 檔釘在精選白名單的**同目錄**（sibling），由唯一的 `FILET_LEADERS_PATH`
推導——API（ApiConfig.user_leaders_path）與引擎（leader_resolve／leader_change_apply）
共用本函式，結構上不可能各自推導出不同的檔（同一類 C3 漂移的既有修法：
leader_change.leader_changes_path_for）。部署面（目錄對 filet-api 的寫權）是
spec 明訂的晨間檢查點，不在本模組職責內。

條目形狀：沿 leaders.json 的頂層形狀與欄位，另加 `source: "user"` 與
`added_by`（加入者 account_id，稽核欄位）。兩個新欄位 fail-fast 驗證：registry
只由本模組的 record_user_leader 寫入，欄位缺漏代表檔案被手改壞或寫入邏輯出錯，
靜默容忍會讓稽核線索無聲蒸發。
"""
import json
import os
import tempfile
from pathlib import Path

from spark.filet.followers import normalize_hex_address
from spark.filet.leaders import LeaderRef, load_leaders

USER_LEADERS_FILENAME = "user_leaders.json"
USER_SOURCE = "user"


def user_leaders_path_for(leaders_path: str | Path) -> str:
    """精選白名單路徑 → user registry 路徑（同目錄 sibling）。

    API 寫端與引擎讀端**必須**共用本推導（單一定義）：各自拼路徑是「API 寫 A、
    引擎讀 B 而兩邊 log 都正常」那類靜默漂移的起點。
    """
    return str(Path(leaders_path).parent / USER_LEADERS_FILENAME)


def merge_leaders(curated: list[LeaderRef],
                  user: list[LeaderRef]) -> list[LeaderRef]:
    """精選白名單 ＋ user registry → 引擎驗證用的合併清單。

    ⭐ **精選條目一律優先**：同位址的 user 條目被忽略。這是 operator kill-switch
    的承重點——精選檔把某位址標成 enabled=false（安全撤銷）後，user registry 對
    同位址的 enabled=true 條目若能勝出，自訂路徑就成了繞過撤銷的後門。
    反向（user 條目 enabled=false）同樣有效：operator 可直接編輯 registry 檔
    執行對 user-sourced leader 的撤銷。
    """
    curated_addrs = {r.address for r in curated}
    return list(curated) + [u for u in user if u.address not in curated_addrs]


def load_user_leaders(path: str | Path) -> list[LeaderRef]:
    """讀 user registry → list[LeaderRef]。

    檔案不存在 → 空清單（尚無任何用戶自訂 leader，合法狀態）。
    格式錯誤 → raise（fail-fast，沿 load_leaders：壞掉時寧可讓呼叫端大聲處理，
    「壞掉就當空清單」會讓已准入的自訂 leader 靜默消失——引擎側那會被讀成撤銷收尾）。
    """
    p = Path(path)
    if not p.exists():
        return []
    refs = load_leaders(p)  # 基礎欄位的形狀驗證與正規化沿用單一定義（不重抄）
    _validate_registry_fields(p)
    return refs


def _validate_registry_fields(p: Path) -> None:
    """registry 專屬欄位（source／added_by）的 fail-fast 驗證。

    只在 load_leaders 成功之後呼叫——頂層形狀（dict、leaders 為 dict 陣列）
    已被它保證，這裡的重讀不會撞到已拒絕的形狀。
    """
    raw = json.loads(p.read_text()).get("leaders", [])
    for i, entry in enumerate(raw):
        if entry.get("source") != USER_SOURCE:
            raise ValueError(
                f"user registry leaders[{i}].source 須為 {USER_SOURCE!r}: "
                f"{entry.get('source')!r}（{p}）——出現別的值代表精選條目被手搬進"
                f"user registry，繞過了人工所有權的分界")
        added_by = entry.get("added_by")
        if not isinstance(added_by, str) or not added_by.strip():
            raise ValueError(
                f"user registry leaders[{i}].added_by 不得為空: {added_by!r}（{p}）"
                f"——這是稽核線索（此位址由哪個帳戶加入），缺漏即 fail-fast")


def record_user_leader(path: str | Path, *, address: str, added_by: str) -> bool:
    """把一個通過准入檢查的自訂 leader 寫進 registry。**只有 public API 該呼叫**。

    回傳 True＝已寫入；False＝同位址已存在（**冪等**：跳過，不動既有檔——客戶
    重送同一個 POST 不得產生第二筆，也不得覆寫第一筆的 added_by 稽核欄位）。

    ⭐ 失敗語意（工程原則 2、3）：
    - 既有檔壞掉（ValueError）→ **原樣上拋，絕不覆寫**：覆寫等於把先前所有已准入
      的自訂 leader 靜默清空，引擎側會把那讀成集體撤銷（真實的收尾成本）。
    - 寫入失敗（OSError）→ 原樣上拋，呼叫端必須回 5xx 且**不得**記錄換 leader
      ——leader 進不了 registry 卻記了換手，引擎會拒絕一個客戶已簽章的意圖。
    - 寫入**原子**（temp file ＋ os.replace，同目錄保證同檔案系統）：引擎每輪
      都在讀這個檔，絕不能讓它讀到半份 JSON。
    """
    p = Path(path)
    addr = normalize_hex_address("address", address)
    if not isinstance(added_by, str) or not added_by.strip():
        raise ValueError(f"added_by 不得為空: {added_by!r}（稽核欄位，缺了整筆免談）")
    existing_raw: list = []
    if p.exists():
        refs = load_user_leaders(p)   # fail-fast：壞檔不得被覆寫
        if any(r.address == addr for r in refs):
            return False
        existing_raw = json.loads(p.read_text()).get("leaders", [])
    entry = {"address": addr, "name": addr, "description": "",
             "enabled": True, "accepting_new": True,
             "source": USER_SOURCE, "added_by": added_by}
    doc = {"leaders": existing_raw + [entry]}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".user_leaders-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
