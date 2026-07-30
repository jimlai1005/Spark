"""tests/test_safe_fs.py
跨 user 交換檔的原子寫入邊界（2026-07-30 安全審查 High：symlink 提權）。

⭐ 這裡釘的是**攻擊者預先在 tmp 路徑放 symlink** 的情境：舊寫法用固定／可預測的
tmp 名 ＋ `write_text`（無 O_EXCL、無 O_NOFOLLOW）＋ path-based chmod/chown，
於是 root 會穿過 link 覆寫目標檔、並把目標檔的所有權交出去。
下面的測試不需要 root：`O_EXCL` 的行為（撞到既有 inode 就失敗）本身就是那道防線，
非 root 也驗得到；權限操作走 fd 則由「目標檔沒被動到」間接驗證。
"""
import json
import os

import pytest

from spark.filet.safe_fs import (ensure_dir_secure, write_json_atomic,
                                 write_text_atomic)


def test_writes_and_sets_mode(tmp_path):
    p = tmp_path / "doc.json"
    write_json_atomic(p, {"a": 1}, mode=0o640)
    assert json.loads(p.read_text()) == {"a": 1}
    assert (os.stat(p).st_mode & 0o777) == 0o640


def test_replaces_existing_atomically(tmp_path):
    p = tmp_path / "doc.json"
    write_json_atomic(p, {"v": 1}, mode=0o644)
    write_json_atomic(p, {"v": 2}, mode=0o644)
    assert json.loads(p.read_text()) == {"v": 2}


def test_no_tmp_left_behind(tmp_path):
    p = tmp_path / "doc.json"
    write_json_atomic(p, {"a": 1}, mode=0o644)
    assert [f.name for f in tmp_path.iterdir()] == ["doc.json"]


def test_planted_symlink_at_predictable_tmp_name_is_not_followed(tmp_path):
    """⭐ 核心回歸測試：攻擊者猜到 tmp 名並預先放 symlink 指向受害檔案時，
    受害檔案不得被寫入。mkstemp 的 O_EXCL ＋ 隨機後綴讓這件事不可能發生。"""
    victim = tmp_path / "leaders.json"
    victim.write_text('{"leaders": [{"address": "0xabc"}]}')
    target_dir = tmp_path / "queue"
    target_dir.mkdir()
    p = target_dir / "pending.json"
    # 把舊實作可能用到的每一種 tmp 名都預先埋成指向 victim 的 symlink。
    for name in [f"pending.tmp.{os.getpid()}", "pending.tmp",
                 f".pending.json-{os.getpid()}.tmp"]:
        (target_dir / name).symlink_to(victim)

    write_json_atomic(p, {"pending": []}, mode=0o640)

    # victim 原封不動：既沒被覆寫、也還是原本的內容。
    assert json.loads(victim.read_text()) == {"leaders": [{"address": "0xabc"}]}
    # 真正的目標檔寫成功，而且它是**真實檔案**不是 symlink。
    assert json.loads(p.read_text()) == {"pending": []}
    assert not p.is_symlink()


def test_existing_target_symlink_is_replaced_not_followed(tmp_path):
    """目標路徑本身被換成 symlink 時，os.replace 覆蓋的是那個 link，
    不是它指向的檔案（受害檔案內容不變）。"""
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": true}')
    p = tmp_path / "doc.json"
    p.symlink_to(victim)

    write_json_atomic(p, {"new": 1}, mode=0o644)

    assert json.loads(victim.read_text()) == {"keep": True}
    assert not p.is_symlink()
    assert json.loads(p.read_text()) == {"new": 1}


def test_write_text_atomic_same_guarantees(tmp_path):
    """per-follower env 走的是 text 版：同樣不得跟隨預埋的 symlink。"""
    victim = tmp_path / "secret.env"
    victim.write_text("KEEP=1\n")
    p = tmp_path / "follower.env"
    (tmp_path / f"follower.env.tmp.{os.getpid()}").symlink_to(victim)

    write_text_atomic(p, "LIVE=true\n", mode=0o640)

    assert victim.read_text() == "KEEP=1\n"
    assert p.read_text() == "LIVE=true\n"
    assert (os.stat(p).st_mode & 0o777) == 0o640


def test_ensure_dir_secure_creates_and_sets_mode(tmp_path):
    d = tmp_path / "state" / "f01"
    ensure_dir_secure(d, mode=0o700)
    assert d.is_dir()
    assert (os.stat(d).st_mode & 0o777) == 0o700


def test_ensure_dir_secure_refuses_symlinked_dir(tmp_path):
    """⭐ `mkdir(exist_ok=True)` 對「指向別處的 symlink」也算成功——若之後用
    path-based chmod，權限就會打在別人的目錄上。O_NOFOLLOW 讓這件事直接失敗。"""
    victim = tmp_path / "victim-dir"
    victim.mkdir(mode=0o755)
    link = tmp_path / "state"
    link.symlink_to(victim)

    with pytest.raises(OSError):
        ensure_dir_secure(link, mode=0o700)
    assert (os.stat(victim).st_mode & 0o777) == 0o755   # 受害目錄權限未被改動


def test_failure_cleans_up_tmp(tmp_path):
    p = tmp_path / "doc.json"

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        write_json_atomic(p, {"bad": Unserializable()}, mode=0o644)
    assert list(tmp_path.iterdir()) == []      # tmp 沒有殘留
    assert not p.exists()                      # 目標未被建立
