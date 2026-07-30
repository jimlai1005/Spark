"""src/spark/filet/safe_fs.py
在**別人擁有的目錄**裡安全地建立檔案／目錄的唯一邊界（2026-07-30 安全審查 High）。

事故背景
--------
`pending.json`、`leader_changes.json`、`capital_settings.json`、per-follower env
這些檔位在 filet-api／filet-engine 擁有的目錄裡，而 root 的 auto-activate watcher
也會寫其中幾個。原本每個寫入點各自手寫「組 tmp 路徑 → `write_text` → `os.replace`
→ path-based `chmod`／`chown`」：

  - `write_text` 是 `O_WRONLY|O_CREAT|O_TRUNC`，**沒有 O_EXCL／O_NOFOLLOW** →
    目錄擁有者（被打穿的 filet-api）預先在 tmp 路徑放一個 symlink，root 就會
    **穿過它覆寫目標檔**；
  - `os.replace` 搬的是 **symlink 本身**，隨後 path-based 的 chmod／chown 又會
    **跟隨** link，把任意檔案的所有權交出去。
  攻擊者把 link 指向 `leaders.json`（策劃白名單，承重點：filet-api 必須寫不到），
  一輪之內就能讓它被 root 覆寫＋易主。tmp 名帶 pid 擋不住這件事：pid 空間可列舉，
  而 timer 每分鐘無限重試——預先埋好就一定命中，不是窄競態。

規則：**建檔與權限操作全程走 fd**（`mkstemp` 內含 `O_EXCL`；`fchmod`／`fchown`
不跟隨 symlink），目錄則以 `O_NOFOLLOW|O_DIRECTORY` 取 fd 後再設權限。
凡是寫「跨 user 讀取的檔」或「在別人的目錄裡建東西」，一律用本模組——
不要再手寫一次 tmp→replace（工程原則 5：邊界要是唯一入口，繞得過就等於沒有）。

`mode` 的選法見 user_leaders.py 的長註解：交換目錄無 setgid，group 位對讀端無效，
跨 user 讀取的檔要給 other 讀位；真正把外人擋在外面的是**目錄本身的 0750**。
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

# (uid, gid)；None ＝ 不做 chown（非 root 執行時的正常狀態）。
OwnerIds = tuple[int, int] | None


def named_owner_ids(user: str, group: str) -> OwnerIds:
    """使用者／群組名 → (uid, gid)。非 root 時回 None：測試與 dry-run 不該
    因為 chown 必然失敗而爆掉，root 下查無此帳號則照樣大聲（部署錯誤）。"""
    if os.geteuid() != 0:
        return None
    import grp
    import pwd
    return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid


def dir_owner_ids(directory: str | Path) -> OwnerIds:
    """目錄擁有者的 (uid, gid)，供 root 寫完把所有權還回去用（避免權限拓撲漂移，
    讓原本的擁有者下一次還寫得動）。非 root 時回 None。"""
    if os.geteuid() != 0:
        return None
    st = os.stat(directory)
    return st.st_uid, st.st_gid


def _write_atomic(path: str | Path, write_fn: Callable[[object], None], *,
                  mode: int, owner_ids: OwnerIds) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            write_fn(f)
            f.flush()
            # 對 fd 而非路徑：symlink 換不掉一個已開啟的 inode。
            os.fchmod(f.fileno(), mode)
            if owner_ids is not None:
                os.fchown(f.fileno(), *owner_ids)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path: str | Path, doc: dict, *, mode: int,
                      owner_ids: OwnerIds = None) -> None:
    _write_atomic(path, lambda f: json.dump(doc, f, ensure_ascii=False, indent=2),
                  mode=mode, owner_ids=owner_ids)


def write_text_atomic(path: str | Path, text: str, *, mode: int,
                      owner_ids: OwnerIds = None) -> None:
    _write_atomic(path, lambda f: f.write(text), mode=mode, owner_ids=owner_ids)


def ensure_dir_secure(path: str | Path, *, mode: int,
                      owner_ids: OwnerIds = None) -> None:
    """建目錄並設權限。⚠️ `mkdir(exist_ok=True)` 對「指向別處的 symlink」也算成功，
    所以權限一律對 `O_NOFOLLOW|O_DIRECTORY` 取得的 fd 設，不走路徑。"""
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        os.fchmod(fd, mode)
        if owner_ids is not None:
            os.fchown(fd, *owner_ids)
    finally:
        os.close(fd)
