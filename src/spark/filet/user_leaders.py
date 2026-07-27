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
registry 檔釘在**交換目錄**（`FILET_EXCHANGE_DIR`：API 寫、引擎讀的單向目錄，
`leader_changes.json` 的既有拓撲），由本函式單一推導——API
（ApiConfig.user_leaders_path）與引擎（run_copytrade 經
leader_change_apply.resolve_user_leaders_path 接進 leader_resolve／
leader_change_apply）共用，結構上不可能各自推導出不同的檔（同一類 C3 漂移的
既有修法：leader_change.leader_changes_path_for）。
⭐ **刻意不是**精選白名單的 sibling（2026-07-27 review F2）：sibling 佈局迫使
filet-api 取得白名單**目錄**的寫權（原子寫 temp+rename 需要），而目錄寫權足以
unlink leaders.json 本身——「filet-api 對白名單無寫權」（leaders.py 檔頭）的
承重不變量就破了。交換目錄的權限拓撲本來就是為「API 寫、引擎讀」建的。

條目形狀：沿 leaders.json 的頂層形狀與欄位，另加 `source: "user"` 與
`added_by`（加入者 account_id，稽核欄位）。兩個新欄位 fail-fast 驗證：registry
只由本模組的 record_user_leader 寫入，欄位缺漏代表檔案被手改壞或寫入邏輯出錯，
靜默容忍會讓稽核線索無聲蒸發。

寫入互斥（review F3）
--------------------
registry 有兩類寫者：API 進程（record_user_leader，且可能多 worker）與 operator
手編（kill-switch：把條目改成 enabled=false）。read-modify-write 若無互斥，
後寫者會拿舊快照把前寫者的變更**無聲蓋掉**——掉的是 operator 的停用位（撤銷
失效）或剛准入的條目（引擎把消失讀成撤銷 → 真實收尾成本）。所以
record_user_leader 以 sidecar lock 檔（`user_leaders.json.lock`＋`flock LOCK_EX`）
包住**整段** read-modify-write，並在取得鎖之後才重讀檔案做冪等判斷與合併。
⚠️ operator 手編也應先取同一把鎖再編輯（例：
`flock /var/lib/filet-exchange/user_leaders.json.lock -c "vi …/user_leaders.json"`），
或改走未來的管理 CLI。lock 檔**永不刪除**：unlink＋flock 並存會讓兩個持鎖者
各鎖到不同 inode，互斥形同虛設。
"""
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from spark.filet.followers import normalize_hex_address
from spark.filet.leaders import LeaderRef, load_leaders

USER_LEADERS_FILENAME = "user_leaders.json"
USER_SOURCE = "user"


def user_leaders_path_for(exchange_dir: str | Path) -> str:
    """交換目錄（`FILET_EXCHANGE_DIR`）→ user registry 路徑（沿
    leader_change.leader_changes_path_for 的同一錨點與同一理由）。

    API 寫端與引擎讀端**必須**共用本推導（單一定義）：各自拼路徑是「API 寫 A、
    引擎讀 B 而兩邊 log 都正常」那類靜默漂移的起點。錨點是交換目錄而非精選
    白名單的 sibling——理由見模組 docstring（review F2：白名單目錄寫權）。
    """
    return str(Path(exchange_dir) / USER_LEADERS_FILENAME)


def merge_leaders(curated: list[LeaderRef],
                  user: list[LeaderRef]) -> list[LeaderRef]:
    """精選白名單 ＋ user registry → 引擎驗證用的合併清單。

    ⭐ **精選條目一律優先**：同位址的 user 條目被忽略。這是 operator kill-switch
    的承重點——精選檔把某位址標成 enabled=false（安全撤銷）後，user registry 對
    同位址的 enabled=true 條目若能勝出，自訂路徑就成了繞過撤銷的後門。
    反向（user 條目 enabled=false）同樣有效：operator 可直接編輯 registry 檔
    執行對 user-sourced leader 的撤銷。

    ⚠️ **「刪掉精選條目」不是撤銷**（2026-07-27 Finding 2）：優先權靠的是「精選檔
    裡有那一筆」，刪掉它就沒有東西壓過 registry 的 `enabled:true` 條目，遞補上來
    ＝撤銷失效。leaders.py 檔頭早期那條「移除條目 ≡ enabled:false」的等價關係在
    本函式存在之後只對「不在 registry 的位址」成立。**恆定有效的撤銷 ＝ 確保精選檔
    裡存在一筆 `enabled:false` 的條目**（`scripts/revoke_leader.py` 冪等執行並自驗）。
    """
    curated_addrs = {r.address for r in curated}
    return list(curated) + [u for u in user if u.address not in curated_addrs]


def load_user_leaders(path: str | Path) -> list[LeaderRef]:
    """讀 user registry → list[LeaderRef]。

    「檔案不存在」有兩種語意，用 init-marker 區分（沿 leader_change_apply 帳本
    標記的既有模式；2026-07-27 二輪 review A）：
    - 缺席＋**無**標記 → 從未有用戶自訂 leader → 空清單（合法狀態）。
    - 缺席＋**有**標記 → registry 已初始化後被刪（redeploy/rsync 漏檔/誤刪）→
      raise。當成空清單的話，合併清單會瞬間少掉已准入的 leader，引擎把消失讀成
      撤銷 → 平倉＋鎖 kill switch——「讀不到 ≠ 撤銷」，一律走呼叫端既有的
      ValueError 安全路徑（resolve→transient、apply→沿用現狀、API→503）。
    格式錯誤 → raise（fail-fast，沿 load_leaders：壞掉時寧可讓呼叫端大聲處理，
    「壞掉就當空清單」會讓已准入的自訂 leader 靜默消失——引擎側那會被讀成撤銷收尾）。
    """
    p = Path(path)
    if not p.exists():
        if registry_init_marker_path(p).exists():
            raise ValueError(
                f"user registry 遺失：{p} 不存在，但初始化標記在"
                f"（{registry_init_marker_path(p)}）——已初始化的 registry 消失"
                f"不是合法空狀態，拒絕當成空清單（那會讓引擎把已准入的自訂 leader"
                f"讀成撤銷）。請從備份還原 registry，或確認後手動移除標記")
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


def registry_lock_path(path: str | Path) -> Path:
    """sidecar lock 檔的落點（單一定義；operator 手編也該鎖**這一個**檔）。"""
    p = Path(path)
    return p.with_name(p.name + ".lock")


def registry_init_marker_path(path: str | Path) -> Path:
    """init-marker 的落點（單一定義；語義見 load_user_leaders docstring）。
    沿 leader_change_apply 帳本標記的 `<檔名>.init` 命名慣例。"""
    p = Path(path)
    return p.with_name(p.name + ".init")


_LOCK_TIMEOUT_S = 2.0
_LOCK_POLL_S = 0.05


def _flock_with_deadline(lock_f, lock_path: Path) -> None:
    """有界等待的 LOCK_EX（2026-07-27 二輪 review B）。

    阻塞式 flock 會讓 operator 手編持鎖（模組 docstring 建議的 `flock … -c vi`）
    期間，每個自訂 select 把一條 API worker 執行緒無限期掛死——堆疊多了耗盡
    threadpool。改 LOCK_NB＋短輪詢：逾時 raise TimeoutError（OSError 子類，
    呼叫端既有的「寫入失敗 → 5xx、不記錄換 leader」路徑自動接手；客戶稍後重試，
    冪等寫入保證安全）。"""
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"user registry lock 逾時（{_LOCK_TIMEOUT_S}s）：{lock_path} "
                    f"被其他寫者持有（另一個 API worker 或 operator 手編中）"
                    f"——放棄本次寫入，請稍後重試") from None
            time.sleep(_LOCK_POLL_S)


@contextmanager
def registry_lock(path: str | Path):
    """registry 的 read-modify-write 互斥（**唯一定義**，所有寫者共用）。

    寫者有兩類：API 進程的 `record_user_leader`，與 operator 的撤銷工具
    （`scripts/revoke_leader.py`）。兩邊各自寫一次 flock 樣板，遲早有一邊漏掉
    有界等待或鎖錯檔名——所以做成一個進入點：拿得到鎖才進得了 with 區塊
    （工程原則 5 的互斥版）。逾時 → TimeoutError（OSError 子類），呼叫端既有的
    「寫入失敗 → 大聲失敗、不宣告成功」路徑自動接手。

    ⚠️ lock 檔永不刪除（理由見模組 docstring）；落點單一定義見 registry_lock_path。
    """
    lock_path = registry_lock_path(path)
    with open(lock_path, "w") as lock_f:
        _flock_with_deadline(lock_f, lock_path)   # 出 with（close）自動釋放
        yield


def record_user_leader(path: str | Path, *, address: str, added_by: str) -> bool:
    """把一個通過准入檢查的自訂 leader 寫進 registry。**只有 public API 該呼叫**。

    回傳 True＝已寫入；False＝同位址已存在（**冪等**：跳過，不動既有檔——客戶
    重送同一個 POST 不得產生第二筆，也不得覆寫第一筆的 added_by 稽核欄位）。

    ⭐ 互斥（review F3）：整段 read-modify-write 以 `flock LOCK_EX` 包住（lock 檔
    見 registry_lock_path），且冪等判斷與合併用的內容是**取得鎖之後重讀**的——
    鎖前讀到的快照可能已被另一個寫者換掉，拿它合併就是無聲蓋寫。lock 檔永不刪除
    （理由見模組 docstring）。

    ⭐ 失敗語意（工程原則 2、3）：
    - 既有檔壞掉（ValueError）→ **原樣上拋，絕不覆寫**：覆寫等於把先前所有已准入
      的自訂 leader 靜默清空，引擎側會把那讀成集體撤銷（真實的收尾成本）。
    - 寫入失敗（OSError）→ 原樣上拋，呼叫端必須回 5xx 且**不得**記錄換 leader
      ——leader 進不了 registry 卻記了換手，引擎會拒絕一個客戶已簽章的意圖。
    - 寫入**原子**（temp file ＋ os.replace，同目錄保證同檔案系統）：引擎每輪
      都在讀這個檔，絕不能讓它讀到半份 JSON（讀端刻意**不必**取鎖）。
    """
    p = Path(path)
    addr = normalize_hex_address("address", address)
    if not isinstance(added_by, str) or not added_by.strip():
        raise ValueError(f"added_by 不得為空: {added_by!r}（稽核欄位，缺了整筆免談）")
    # 落點目錄（交換目錄）不存在就建（沿 write_leader_change 對同一目錄的慣例）；
    # 建不出來（路徑被檔案佔住、權限）→ OSError 原樣上拋，呼叫端回 5xx。
    p.parent.mkdir(parents=True, exist_ok=True)
    with registry_lock(p):   # 互斥的單一定義（operator 撤銷工具走同一把鎖）
        # ⭐ 取得鎖之後才讀：這份內容在寫入落地前不會再被別的持鎖寫者更動。
        existing_raw: list = []
        if p.exists():
            refs = load_user_leaders(p)   # fail-fast：壞檔不得被覆寫
            if any(r.address == addr for r in refs):
                _ensure_init_marker(p)    # 冪等跳過也補標記（pre-marker 版本寫的檔）
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
        # ⭐ 順序刻意「先 registry、後標記」（沿帳本 init-marker 的落檔順序）：
        # 中途失敗時「有檔無標記」下一輪被判成正常（照讀，正確）；反過來
        # 「有標記無檔」會被判成遺失（假告警）。標記寫失敗 → OSError 上拋、
        # 呼叫端回 5xx 不記錄換手；客戶重試走冪等跳過分支把標記補回來（自癒）。
        _ensure_init_marker(p)
    return True


def _ensure_init_marker(p: Path) -> None:
    marker = registry_init_marker_path(p)
    if not marker.exists():
        marker.touch()
