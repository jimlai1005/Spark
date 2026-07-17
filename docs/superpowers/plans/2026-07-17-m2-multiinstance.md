# M2 Phase A — 引擎多實例地基 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 M1 的單 follower 跟單引擎擴成 process-per-follower 多實例：檔案後端 keystore（結構性不持有主鑰）、**per-follower 狀態隔離**、follower 登錄、**正確的北極星匯總（builder fee 不重複計）**、告警標籤、systemd 部署、全域 panic、leaderboard 快照。

**Architecture:** M1 的 `run_copytrade` 已是 env 驅動的單 follower 進程，但帶三個**單實例假設**，多實例必須先拆解（2026-07-17 opus 計畫審查抓出）：
1. **單一 `var/` 狀態根**（kill switch ARM 檔、alerts.log、shadow、accrued 快照全掛 repo/var/copytrade/）→ 多 follower 共用會讓一人的 kill switch 連坐全部。**修法：狀態根參數化 `FILET_STATE_DIR`，每 follower 一個。**
2. **builder fee accrued 是全域量**：`query_builder_accrued(builder)` 回的是該 builder 位址的**全域**累積；M2 全部 follower 共用同一 builder，跨 follower 加總 accrued_delta = N 倍高估。**修法：北極星＝builder 層級查一次（不加總）；per-follower 只做 fills 衍生的活動歸屬。**
3. **日報取數管線內嵌在 script main()**：無可複用函式。**修法：抽 `collect_follower_summary`。**

**Tech Stack:** Python 3.11 + uv、既有 spark copytrade 引擎、systemd（部署）、pytest（離線、socket-ban）、Decimal 全程。

**Spec:** `~/projects/obsidian/pandora/filet/M2-closed-alpha-設計.md`（vault，四不變量與元件定義以該檔為準）。本計畫只涵蓋設計的**元件一（引擎多實例）**；元件二（dashboard，待原型 v1 反饋）與元件三（VPS + onboarding 後端，待供應商決策）各自成獨立計畫。

## 執行狀態（2026-07-17 完成）

**Task 0–10 全部完成＋hardening**，每任務經 fresh-context 雙階段審查（⭐ 任務加紅線逐條檢，Task 8 panic_all 加 opus 第二意見）。整合驗證：`uv run pytest -q` = **546 passed, 2 deselected**；`uv run ruff check src tests scripts` 乾淨。

任務→commit 對照：T0=3b5372f｜T1=1700b97｜T2=3f2395a｜T3=3dc1d55｜T4=2578b23｜T5=53cc3ee｜
T6=ad206a4+54518fc｜T7=5bcbfb7｜T8=3df115d+8afa1fd+402b88c｜T9=68e3dfc+aa13bdb｜T10=57e0628+0eefa11。

**雙審抓掉的實質問題**（開工前 opus 計畫審 2 輪＋執行中逐任務審）：
1. kill-switch 狀態連坐（多 follower 共用 var/ 根）→ FILET_STATE_DIR per-follower 隔離。
2. 北極星 builder fee 被 N 倍高估（多 follower 共用 builder，跨 follower 加總）→ builder 層級查一次；大小寫去重（避免同位址不同大小寫重複計）。
3. **panic_all 路徑穿越 Critical**（opus 第二意見連 2 輪逼出）：`_state_root_for` 對壞 account_id 零防護 → ARM 寫錯地方 → 引擎下 cycle 重開倉 → **靜默漏平＋exit 0 假成功**。第 1 輪抓「逃出 base」，第 2 輪抓「`alice/../bob` 兄弟目錄誤導」。三道防線窮舉 20+ payload 全封＋直接迴歸測試。
4. panic 前置讀取無 resilience（API 過載逾時→整個 follower 漏平）→ 重試＋lock-degrade（讀不到仍寫 ARM 鎖死）。
5. leaderboard NaN/Inf pnl 炸整批 → 大聲跳過單列。
6. account_id 路徑安全（keystore/registry 雙層驗證，防 Phase C onboarding 後端未來從使用者輸入生成）。

**待接續（非本 Phase）**：元件三 VPS + onboarding 後端（主鑰唯一持有者，資安最高，含 opus 第二意見）；元件二 dashboard（原型 v1 已定，待工程計畫）。**Phase C follow-up**：panic degrade 的 alerts.log 持久 forensic（需動 killswitch.py，本 Phase off-limits）。M1 收尾（testnet 實測/shadow/dogfood）並行。

---

## 全域紅線（每個任務的實作者與 reviewer 都先讀）

1. ⭐ **非託管不變量結構化**：引擎 keystore **物理上不得能取得主錢包鑰匙**。`EnvFileKeyStore.get_main_signer` 一律 raise。主鑰簽名只存在於 onboarding 後端（Phase C）。
2. ⭐ 私鑰不得出現在 log / repr / 例外訊息（例外只提路徑）。
3. agent key 檔案權限必須 600；group/other 任何位元被設 → 載入時大聲拒絕（工程原則 3）。
4. ⭐ **per-follower 狀態隔離**：kill switch ARM 檔、alerts.log、shadow 必須 per-follower（每 follower 一個 `FILET_STATE_DIR`）。一個 follower 的狀態不得被另一個讀到（工程原則 1：不同 follower 是不同 basis）。**引擎與 panic_all 對同一 follower 必須推導出同一個 state root**（否則 panic 寫的 ARM 落錯地方、被引擎下個 cycle 抹掉）。（註：builder fee accrued 快照**不**在此列——它是 builder 位址的全域量、單一快照，見紅線 5。）
5. ⭐ **北極星不重複計**：total builder fee 是 builder 位址的全域量，查一次、存單一 builder 層級快照；**絕不**跨 follower 加總 accrued_delta。
6. 跨 follower 工具（panic、日報）：單一 follower 失敗不得中止其他；失敗大聲告警並反映在退出碼（工程原則 4）。
7. 測試全離線：sockets-ban；不連網、不真發通知、不真動 systemd、不讀真 .env。
8. 內部一律 Decimal；float 只在 adapter↔SDK 邊界。hl-copytrader 唯讀。

## 檔案結構（本計畫鎖定）

```
src/spark/
├── keystore/envfile.py           # Task 1：EnvFileKeyStore
└── filet/
    ├── __init__.py               # Task 2
    ├── followers.py              # Task 2：FollowerRef + load_followers
    ├── tagged_notifier.py        # Task 3：TaggedNotifier
    └── aggregate.py              # Task 6：collect_follower_summary + 北極星匯總
scripts/
├── filet_daily_report.py         # Task 6：跨 follower 日報 CLI
├── panic_all.py                  # Task 8：全域 panic
└── leaderboard_snapshot.py       # Task 9：HL leaderboard 快照
deploy/
├── filet-follower@.service       # Task 7
├── follower.env.example          # Task 7
└── reload_follower.sh            # Task 7
（run_copytrade.py 於 Task 4/5 就地擴充：狀態根、keystore 選擇、TaggedNotifier 接線）
```

## 模型分工與 review gate

| Task | 主題 | 實作 | 驗收 | 加驗 |
|---|---|---|---|---|
| 0 | 分支＋doc | haiku | sonnet read-back | — |
| 1 | EnvFileKeyStore ⭐ | sonnet | sonnet fresh | ⭐ 紅線 1/2/3 |
| 2 | follower 登錄 | sonnet | sonnet | — |
| 3 | TaggedNotifier | haiku | sonnet | — |
| 4 | 狀態根隔離 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 4（kill-switch 隔離）|
| 5 | run_copytrade 接線 | sonnet | sonnet | keystore 選擇 + notifier 標籤實跑 |
| 6 | 北極星匯總 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 5（不重複計）|
| 7 | systemd 模板 | haiku | sonnet：read-back + 本地 verify | — |
| 8 | 全域 panic ⭐ | sonnet | sonnet | ⭐ + **opus 第二意見**（碰錢＋災難路徑）|
| 9 | leaderboard 快照 | sonnet | sonnet | — |

- 每任務：實作 → fresh-context 驗收 → commit。同任務失敗兩輪 → 換方法或升級。
- 全部 commit 落 `feat/m2-multiinstance`（自 `feat/copytrade-m1` 分出）。不 push、不動 main。M1 併入 main 後本分支 rebase。

---

### Task 0: 分支、計畫落檔、基線

- [ ] **Step 1** `git checkout -b feat/m2-multiinstance feat/copytrade-m1`；`git branch --show-current` 應為 `feat/m2-multiinstance`。
- [ ] **Step 2** `uv run pytest -q` → `456 passed, 2 deselected`；`uv run ruff check src tests scripts` → 乾淨。不符則停下回報。
- [ ] **Step 3** `git add docs/superpowers/plans/2026-07-17-m2-multiinstance.md && git commit -m "docs: M2 Phase A multi-instance implementation plan"`（帶 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` footer；以下所有 commit 同）。

---

### Task 1: EnvFileKeyStore（agent-only 檔案後端）⭐

**Files:** Create `src/spark/keystore/envfile.py`、`tests/test_envfile_keystore.py`。先讀 `src/spark/keystore/base.py`、`src/spark/keystore/keychain.py`。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_envfile_keystore.py"""
import os, stat, pytest
from eth_account import Account
from spark.keystore.envfile import EnvFileKeyStore

_PK = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
_ADDR = Account.from_key(_PK).address

def test_import_and_read_agent_roundtrip(tmp_path):
    ks = EnvFileKeyStore(tmp_path); ks.import_agent_key("acct1", _PK)
    assert ks.get_agent_signer("acct1").address == _ADDR

def test_imported_key_file_is_600(tmp_path):
    ks = EnvFileKeyStore(tmp_path); ks.import_agent_key("acct1", _PK)
    assert stat.S_IMODE((tmp_path/"acct1"/"agent.key").stat().st_mode) == 0o600

def test_get_main_signer_always_refuses(tmp_path):
    with pytest.raises(PermissionError):
        EnvFileKeyStore(tmp_path).get_main_signer("acct1")

def test_unsafe_permissions_refused(tmp_path):
    ks = EnvFileKeyStore(tmp_path); ks.import_agent_key("acct1", _PK)
    os.chmod(tmp_path/"acct1"/"agent.key", 0o644)
    with pytest.raises(PermissionError):
        ks.get_agent_signer("acct1")

def test_missing_key_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        EnvFileKeyStore(tmp_path).get_agent_signer("nope")

def test_private_key_never_in_exception(tmp_path):
    ks = EnvFileKeyStore(tmp_path); ks.import_agent_key("acct1", _PK)
    os.chmod(tmp_path/"acct1"/"agent.key", 0o644)
    try:
        ks.get_agent_signer("acct1")
    except PermissionError as e:
        assert _PK not in str(e) and _PK[2:] not in str(e)
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作**

```python
"""src/spark/keystore/envfile.py
檔案後端 keystore：引擎專用。只讀 agent key；get_main_signer 一律拒絕
（非託管不變量結構化——引擎進程物理上不持有主錢包鑰匙）。"""
import os, stat
from pathlib import Path
from eth_account import Account
from spark.keystore.base import KeyStore


class EnvFileKeyStore(KeyStore):
    """agent key 存 <root>/<account_id>/agent.key（純 hex、權限 600）。
    get_agent_signer 讀檔前硬檢查權限。get_main_signer 一律 raise。
    私鑰不進 log/repr/例外（例外只提路徑）。"""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    def _agent_path(self, account_id: str) -> Path:
        return self._root / account_id / "agent.key"

    def get_main_signer(self, account_id: str):
        raise PermissionError(
            "engine keystore holds no main keys (non-custodial invariant); "
            "main-key signing belongs to the onboarding backend only")

    def get_agent_signer(self, account_id: str):
        path = self._agent_path(account_id)
        if not path.exists():
            raise KeyError(f"no agent key for account {account_id} at {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(
                f"agent key {path} has unsafe permissions {oct(mode)}; expected 0o600")
        return Account.from_key(path.read_text().strip())

    def import_agent_key(self, account_id: str, private_key: str) -> None:
        """寫入 agent key（供 onboarding 後端，Phase C 用）。父目錄 700、檔案 600。"""
        d = self._root / account_id
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        path = self._agent_path(account_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, private_key.strip().encode())
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
```

- [ ] **Step 4** 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: EnvFileKeyStore — agent-only file backend; main-key refused (non-custodial invariant)"`。

---

### Task 2: Follower 登錄與載入

**Files:** Create `src/spark/filet/__init__.py`（docstring）、`src/spark/filet/followers.py`、`tests/test_filet_followers.py`。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_filet_followers.py"""
import json, pytest
from spark.filet.followers import FollowerRef, load_followers

_GOOD = {"followers": [
    {"account_id": "alice", "user_address": "0x"+"a"*40,
     "builder_address": "0x"+"b"*40, "network": "mainnet", "label": "Alice"},
    {"account_id": "bob", "user_address": "0x"+"c"*40,
     "builder_address": "0x"+"b"*40, "network": "testnet"},
]}

def _w(tmp_path, obj):
    p = tmp_path/"followers.json"; p.write_text(json.dumps(obj)); return p

def test_load_two(tmp_path):
    refs = load_followers(_w(tmp_path, _GOOD))
    assert [r.account_id for r in refs] == ["alice", "bob"]
    assert refs[0].label == "Alice" and refs[1].label == ""

def test_frozen(tmp_path):
    r = load_followers(_w(tmp_path, _GOOD))[0]
    with pytest.raises(Exception): r.account_id = "x"

def test_duplicate_rejected(tmp_path):
    dup = {"followers": _GOOD["followers"] + [_GOOD["followers"][0]]}
    with pytest.raises(ValueError): load_followers(_w(tmp_path, dup))

def test_bad_address_rejected(tmp_path):
    bad = {"followers": [{"account_id": "x", "user_address": "0xshort",
        "builder_address": "0x"+"b"*40, "network": "mainnet"}]}
    with pytest.raises(ValueError): load_followers(_w(tmp_path, bad))

def test_non_hex_address_rejected(tmp_path):  # opus O11：驗 hex 字元集
    bad = {"followers": [{"account_id": "x", "user_address": "0x"+"z"*40,
        "builder_address": "0x"+"b"*40, "network": "mainnet"}]}
    with pytest.raises(ValueError): load_followers(_w(tmp_path, bad))

def test_bad_network_rejected(tmp_path):
    bad = {"followers": [{"account_id": "x", "user_address": "0x"+"a"*40,
        "builder_address": "0x"+"b"*40, "network": "devnet"}]}
    with pytest.raises(ValueError): load_followers(_w(tmp_path, bad))

def test_missing_manifest_raises(tmp_path):
    with pytest.raises(FileNotFoundError): load_followers(tmp_path/"nope.json")
```

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作**

```python
"""src/spark/filet/followers.py
Follower 登錄：manifest（JSON）→ list[FollowerRef]。
FollowerRef 是跨 follower 工具（匯總、全域 panic）用的最小身分；
per-follower 完整跟單參數走各自進程的 env（CopySettings.from_env）。"""
import json
from dataclasses import dataclass
from pathlib import Path

_NETWORKS = {"testnet", "mainnet"}
_HEX = set("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class FollowerRef:
    account_id: str
    user_address: str
    builder_address: str
    network: str
    label: str = ""


def _check_addr(field: str, value: str) -> None:
    ok = (isinstance(value, str) and value.startswith("0x") and len(value) == 42
          and all(c in _HEX for c in value[2:]))
    if not ok:
        raise ValueError(f"{field} 不是合法地址（0x + 40 hex）: {value!r}")


def _parse_one(i: int, f: dict, seen: set[str]) -> FollowerRef:
    acct = f.get("account_id", "")
    if not acct:
        raise ValueError(f"followers[{i}] account_id 不得為空")
    if acct in seen:
        raise ValueError(f"followers[{i}] account_id 重複: {acct!r}")
    _check_addr(f"followers[{i}].user_address", f.get("user_address", ""))
    _check_addr(f"followers[{i}].builder_address", f.get("builder_address", ""))
    net = f.get("network", "")
    if net not in _NETWORKS:
        raise ValueError(f"followers[{i}].network 須為 {_NETWORKS}: {net!r}")
    return FollowerRef(acct, f["user_address"], f["builder_address"],
                       net, f.get("label", ""))


def load_followers(manifest_path: str | Path) -> list[FollowerRef]:
    """fail-fast：任一壞條目即 raise。一般載入用這個。"""
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"follower manifest 不存在: {path}")
    data = json.loads(path.read_text())
    refs: list[FollowerRef] = []
    seen: set[str] = set()
    for i, f in enumerate(data.get("followers", [])):
        ref = _parse_one(i, f, seen)
        seen.add(ref.account_id)
        refs.append(ref)
    return refs


def load_followers_tolerant(manifest_path: str | Path) -> tuple[list[FollowerRef], list[str]]:
    """容錯載入：壞條目跳過並收集錯誤訊息，回 (refs, errors)。
    災難工具（panic_all）與日報用這個——單一壞條目不該擋掉救其他 follower。"""
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"follower manifest 不存在: {path}")
    data = json.loads(path.read_text())
    refs: list[FollowerRef] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, f in enumerate(data.get("followers", [])):
        try:
            ref = _parse_one(i, f, seen)
        except ValueError as e:
            errors.append(str(e))
            continue
        seen.add(ref.account_id)
        refs.append(ref)
    return refs, errors
```

測試補：`test_tolerant_skips_bad_entry_keeps_good`（一好一壞 → refs 只含好的、errors 一則）。

- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: follower registry (manifest -> FollowerRef; fail-fast + tolerant loaders)"`。

---

### Task 3: TaggedNotifier

**Files:** Create `src/spark/filet/tagged_notifier.py`、`tests/test_filet_tagged_notifier.py`。先讀 `src/spark/copytrade/notifier.py`（Notifier ABC、RecordingNotifier）。

- [ ] **Step 1: 失敗測試**（用 RecordingNotifier 當 inner）
1. info/warn/critical → inner 對應 level、text 前綴 `[<id>] `。
2. dedup_key 非 None → `<id>:<key>`；None → 傳 None（不去重）。
3. critical 照樣 critical（不改 level）。
4. 兩個不同 id 的 TaggedNotifier 共用同一 inner → 同 raw key 不互相去重（命名空間隔離）。

- [ ] **Step 2–3: 紅→實作**

```python
"""src/spark/filet/tagged_notifier.py"""
from spark.copytrade.notifier import Notifier


class TaggedNotifier(Notifier):
    """多 follower 匯同一頻道時為每則告警加 follower 標籤，並將 dedup_key
    納入 follower 命名空間，使告警可歸屬、跨 follower 不互相去重。"""

    def __init__(self, inner: Notifier, follower_id: str):
        self._inner = inner
        self._tag = follower_id

    def _key(self, k):
        return f"{self._tag}:{k}" if k else None

    def info(self, category, text, dedup_key=None):
        return self._inner.info(category, f"[{self._tag}] {text}", self._key(dedup_key))

    def warn(self, category, text, dedup_key=None):
        return self._inner.warn(category, f"[{self._tag}] {text}", self._key(dedup_key))

    def critical(self, category, text, dedup_key=None):
        return self._inner.critical(category, f"[{self._tag}] {text}", self._key(dedup_key))
```

- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: TaggedNotifier — per-follower alert attribution and dedup namespacing"`。

---

### Task 4: Per-follower 狀態根隔離 ⭐（修 opus B1）

**Files:** Modify `scripts/run_copytrade.py`；Test `tests/test_run_copytrade_state_dir.py`。
先讀：`scripts/run_copytrade.py`（`_REPO_ROOT`、`SHADOW_DIR`、`run_cycle(..., _REPO_ROOT)`、`_append_shadow`）、`src/spark/copytrade/killswitch.py`（`is_tripped(root)`/`trip` 的 ARM/alerts 路徑——確認 root 已是參數）、`src/spark/copytrade/loop.py`（`run_cycle` root 傳遞）。

**問題**：`_REPO_ROOT` 由檔案位置推導，kill switch ARM 檔（`root/var/copytrade/killswitch.tripped`）、alerts.log、shadow、accrued 快照都掛它。多 follower 共用同一 repo → follower A `trip()` 寫的 ARM 檔被 follower B `is_tripped()` 讀到 → B 健康卻停單（連坐）。

**修法**：run_copytrade 讀 env `FILET_STATE_DIR`（預設 `_REPO_ROOT`，**保留 M1 單實例行為不變**）作為狀態根，貫穿 `run_cycle` 的 root 與 `SHADOW_DIR`。killswitch/loop 已把 root 當參數（先讀確認），故只需 run_copytrade 傳對的根。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_run_copytrade_state_dir.py
驗證狀態根由 FILET_STATE_DIR 決定；兩個不同狀態根的 kill switch 互不干擾。"""
import os
from pathlib import Path
import scripts.run_copytrade as rc
from spark.copytrade.killswitch import ARM_FILE_RELPATH, is_tripped

def test_resolve_state_dir_defaults_to_repo(monkeypatch):
    monkeypatch.delenv("FILET_STATE_DIR", raising=False)
    assert rc.resolve_state_dir() == rc._REPO_ROOT

def test_resolve_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path))
    assert rc.resolve_state_dir() == tmp_path

def test_two_state_dirs_isolate_killswitch(tmp_path):
    a, b = tmp_path/"a", tmp_path/"b"
    (a/ARM_FILE_RELPATH).parent.mkdir(parents=True); (a/ARM_FILE_RELPATH).write_text("tripped")
    assert is_tripped(a) is True    # A 已 trip
    assert is_tripped(b) is False   # B 不受影響（隔離）
```

（`resolve_state_dir()` 是本任務要新增的小函式：讀 `FILET_STATE_DIR` env，缺省回 `_REPO_ROOT`，回 `Path`。若 killswitch 的 `ARM_FILE_RELPATH` 常數名不同，讀原始碼對齊。）

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作** —— 加 `resolve_state_dir()`；`main()` 內 `state_root = resolve_state_dir()`，把**三處** `_REPO_ROOT` 使用點全改用 `state_root`（opus 審查點名）：(a) `SHADOW_DIR`（:39，從模組層常數改為 main 內依 state_root 計算）、(b) `_print_status(adapter, user_addr, copy_settings, state_root)`（:149，否則 `--status` 讀共用 repo 根的 ARM 回報錯誤 tripped 狀態）、(c) `run_cycle(..., state_root)`（:185）。**不改 killswitch/loop 簽章**（它們已收 root——opus 已核實 loop.py:62/85 貫穿 is_tripped/trip）。docstring 補一行：`FILET_STATE_DIR` 用途（per-follower 狀態隔離；缺省＝repo 根，保留單實例行為）。測試補一案：`--status` 路徑用注入的 state_root 讀 ARM（monkeypatch 或以 state_dir 有無 ARM 檔驗 tripped 回報）。
- [ ] **Step 4** 全綠（含既有 run_copytrade 相關測試不壞）+ ruff。
- [ ] **Step 5** `git commit -m "feat: per-follower state isolation via FILET_STATE_DIR (kill switch/alerts/shadow)"`。

---

### Task 5: run_copytrade 多實例接線（keystore 選擇 + TaggedNotifier）（修 opus M4-引擎、M5）

**Files:** Modify `scripts/run_copytrade.py`；Test `tests/test_run_copytrade_wiring.py`。

**修法**：(a) keystore 後端依 env `FILET_KEYSTORE` 選擇——`keychain`（預設，Mac 開發）或 `envfile`（VPS，讀 `FILET_KEYS_DIR` 預設 `/etc/filet/keys`）；未設維持 MacKeychainBackend（不改既有行為）。(b) notifier 用 `SPARK_ACCOUNT_ID` 包成 `TaggedNotifier`（account_id 缺省時退回不包裝，避免 dry/shadow 無 account 時炸）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_run_copytrade_wiring.py"""
import scripts.run_copytrade as rc
from spark.keystore.envfile import EnvFileKeyStore
from spark.keystore.keychain import MacKeychainBackend
from spark.filet.tagged_notifier import TaggedNotifier
from spark.copytrade.notifier import NullNotifier

def test_select_keystore_default_keychain(monkeypatch):
    monkeypatch.delenv("FILET_KEYSTORE", raising=False)
    assert isinstance(rc.select_keystore(), MacKeychainBackend)

def test_select_keystore_envfile(monkeypatch, tmp_path):
    monkeypatch.setenv("FILET_KEYSTORE", "envfile")
    monkeypatch.setenv("FILET_KEYS_DIR", str(tmp_path))
    ks = rc.select_keystore()
    assert isinstance(ks, EnvFileKeyStore)

def test_wrap_notifier_tags_when_account(monkeypatch):
    n = rc.wrap_notifier(NullNotifier(), account_id="alice")
    assert isinstance(n, TaggedNotifier)

def test_wrap_notifier_passthrough_without_account(monkeypatch):
    base = NullNotifier()
    assert rc.wrap_notifier(base, account_id=None) is base
```

- [ ] **Step 2–3: 紅→實作** —— 加 `select_keystore()` 與 `wrap_notifier(inner, account_id)` 兩個小函式；`main()` 的 live 分支改用 `select_keystore()`（取代硬編 MacKeychainBackend，run_copytrade.py:161），notifier 建構後 `notifier = wrap_notifier(notifier, account_id)`。keystore import 延後到函式內（保留 import 階段零網路/零 macOS 依賴）。
- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: run_copytrade keystore selection + TaggedNotifier wiring"`。

---

### Task 6: 跨 follower 日報匯總（正確北極星）⭐（修 opus M2、M3）

**Files:** Create `src/spark/filet/aggregate.py`、`scripts/filet_daily_report.py`、`tests/test_filet_aggregate.py`。
先讀：`src/spark/copytrade/report.py`（`DailyReport` 欄位、`build_daily_report` 簽章、`accrued_delta` 語意）、`scripts/copytrade_daily_report.py`（取數管線內嵌在 `main()`——本任務要抽出）、`src/spark/exchange/hyperliquid.py`（`query_builder_accrued(builder)` 是**全域**量、`get_user_fills`）、`src/spark/filet/followers.py`。

**核心修正（紅線 5）**：`query_builder_accrued(builder)` 回 builder 位址的全域累積。M2 全部 follower 共用同一 builder → **北極星＝builder 層級查一次的日增量**（存 builder 層級快照，非 per-follower），**絕不**把各 follower 的 accrued_delta 相加。per-follower 只做 fills 衍生的活動歸屬（taker share、fills 數、名目）。testnet follower 不計入北極星（無真實 fee），但仍在明細列出（標 network）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_filet_aggregate.py"""
from datetime import date
from decimal import Decimal
from spark.filet.followers import FollowerRef
from spark.filet.aggregate import (FollowerSummary, aggregate,
                                    render_aggregate, builder_fee_delta)

def _ref(aid, net="mainnet"):
    return FollowerRef(aid, "0x"+"a"*40, "0x"+"b"*40, net)

def test_north_star_is_builder_level_not_summed():
    # 北極星＝傳入的 builder 層級日增量，與 per-follower summary 無關、不加總
    summaries = [FollowerSummary(_ref("alice"), fills=8, taker_share=Decimal("0.2"),
                                 error=None),
                 FollowerSummary(_ref("bob", "testnet"), fills=3,
                                 taker_share=Decimal("0.1"), error=None)]
    agg = aggregate(date(2026,7,17), summaries,
                    north_star_fee_delta=Decimal("1.84"))
    assert agg.north_star_fee_delta == Decimal("1.84")   # 查一次的值，非相加
    assert agg.follower_count == 2 and agg.ok_count == 2

def test_failed_follower_excluded_from_ok_but_listed():
    summaries = [FollowerSummary(_ref("alice"), fills=8, taker_share=Decimal("0.2"),
                                 error=None),
                 FollowerSummary(_ref("bob"), fills=0, taker_share=Decimal("0"),
                                 error="API timeout")]
    agg = aggregate(date(2026,7,17), summaries, north_star_fee_delta=Decimal("1.0"))
    assert agg.ok_count == 1 and agg.follower_count == 2
    assert any("API timeout" in (s.error or "") for s in agg.summaries)

def test_builder_fee_delta_single_query():
    # builder_fee_delta(today, prev) = today - prev（純函式，查一次的差）
    assert builder_fee_delta(Decimal("5.5"), Decimal("3.66")) == Decimal("1.84")

def test_render_has_single_daily_northstar_line():
    agg = aggregate(date(2026,7,17), [], north_star_fee_delta=Decimal("0"))
    out = render_aggregate(agg)
    assert "單日 builder fee 增量" in out   # opus m6：正名為單日，非「30日日增」

def test_empty_no_crash():
    agg = aggregate(date(2026,7,17), [], north_star_fee_delta=Decimal("0"))
    assert agg.north_star_fee_delta == Decimal("0") and agg.follower_count == 0
```

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作**（`aggregate.py`）

```python
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from spark.filet.followers import FollowerRef


@dataclass(frozen=True)
class FollowerSummary:
    ref: FollowerRef
    fills: int                    # 該 follower 當日成交筆數（fills 衍生）
    taker_share: Decimal          # 該 follower 當日 taker 佔比
    error: str | None = None      # 查詢失敗時記錄，不中斷其他 follower


def collect_follower_summary(ref: FollowerRef, adapter, start: datetime,
                             end: datetime) -> FollowerSummary:
    """對單一 follower 取 fills 算 summary（不查 accrued——避免重複計）。
    任何取數例外捕成 FollowerSummary(error=...)，不外拋（跨 follower 隔離）。
    taker_share = crossed 成交名目 / 總成交名目（總量 0 → 0），沿 report.py 語意。"""
    try:
        fills = adapter.get_user_fills(ref.user_address, start, end)
        ntl = sum((f.sz * f.px for f in fills), Decimal("0"))
        taker_ntl = sum((f.sz * f.px for f in fills if f.crossed), Decimal("0"))
        share = (taker_ntl / ntl) if ntl > 0 else Decimal("0")
        return FollowerSummary(ref, len(fills), share, None)
    except Exception as e:  # noqa: BLE001 — 跨 follower 隔離，錯誤入 summary 不外拋
        return FollowerSummary(ref, 0, Decimal("0"), error=str(e))


@dataclass(frozen=True)
class AggregateReport:
    day: date
    summaries: tuple[FollowerSummary, ...]
    north_star_fee_delta: Decimal  # builder 層級查一次的單日增量（絕不跨 follower 相加）
    follower_count: int
    ok_count: int


def builder_fee_delta(accrued_today: Decimal, accrued_prev: Decimal) -> Decimal:
    """北極星單日增量＝builder 位址全域累積的今昨差（查一次，不加總）。"""
    return accrued_today - accrued_prev


def aggregate(day: date, summaries: list[FollowerSummary], *,
              north_star_fee_delta: Decimal) -> AggregateReport:
    ok = [s for s in summaries if s.error is None]
    return AggregateReport(day, tuple(summaries), north_star_fee_delta,
                           len(summaries), len(ok))


def render_aggregate(agg: AggregateReport) -> str:
    ...  # markdown：頂部單行「單日 builder fee 增量：$X」（北極星，查一次）；
         #  每 follower 一列（label/network/fills/taker_share；失敗列標「查詢失敗」＋error）
```

`scripts/filet_daily_report.py`：
1. 讀 manifest（env `FILET_FOLLOWERS`，預設 `var/filet/followers.json`）——用 `load_followers_tolerant`（一個壞條目不擋整份日報，errors 併入報表）。
2. **北極星**：對 **mainnet** builder 位址查一次 `query_builder_accrued`，減去 builder 層級快照（`var/filet/builder_accrued_snapshot.json`，無檔視為 0）→ `builder_fee_delta`；更新快照。（若有多個相異 mainnet builder 位址，各查一次分別列出——M2 單一 builder，通常一個。）
3. **per-follower summary**：逐 follower `collect_follower_summary(ref, adapter, start, end)`（當日 UTC 0 點～now，UTC 日界對齊 copytrade_daily_report.py）；函式內建錯誤隔離。
4. `aggregate` → `render_aggregate` 印 stdout ＋寫 `var/filet/reports/YYYY-MM-DD.md`。
5. 無 manifest → 用法 + exit 2；import 不觸網。

測試補：`collect_follower_summary` 用 fake adapter——正常 fills 算 taker_share（手算一組 crossed/總名目）、`get_user_fills` 拋例外 → error 入 summary 不外拋、空 fills → fills=0 share=0。

- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: cross-follower aggregate — north-star builder fee queried once (no double-count)"`。

---

### Task 7: systemd 模板與 env 慣例（規格鎖死）（修 opus M4-部署、O10、m7）

**Files:** Create `deploy/filet-follower@.service`、`deploy/follower.env.example`、`deploy/reload_follower.sh`。無單元測試；驗收＝read-back ＋**本地** `systemd-analyze verify deploy/filet-follower@.service`（純本地、不需 root/VPS；若環境無 systemd-analyze 則靜態審並註明）。**絕不** `systemctl enable/start/restart`。

**慣例鎖定（panic_all 依賴）**：systemd 實例名 `%i` **等於 follower 的 `SPARK_ACCOUNT_ID`**（即 account_id）。啟動方式 `systemctl start filet-follower@alice`，其 `FILET_STATE_DIR=/opt/filet/state/alice`、EnvironmentFile `/etc/filet/followers/alice.env`（內含 `SPARK_ACCOUNT_ID=alice`）。Task 8 的 panic_all 依此推導每 follower 的 state root＝`FILET_STATE_BASE/<account_id>`（預設 base `/opt/filet/state`），與引擎讀的同一路徑對齊。

- [ ] **Step 1: `deploy/filet-follower@.service`**

```ini
[Unit]
Description=Filet copytrade follower (%i)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=filet-engine
Group=filet-engine
EnvironmentFile=/etc/filet/followers/%i.env
WorkingDirectory=/opt/filet/spark
# 每 follower 獨立狀態根（修 kill-switch 連坐）
Environment=FILET_STATE_DIR=/opt/filet/state/%i
Environment=FILET_KEYSTORE=envfile
Environment=FILET_KEYS_DIR=/etc/filet/keys
ExecStart=/opt/filet/spark/.venv/bin/python -m scripts.run_copytrade
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
# 引擎只讀 agent key（/etc/filet/keys 於 ProtectSystem=strict 下本就唯讀，不列入 RW）；
# 只有狀態目錄需要寫入
ReadWritePaths=/opt/filet/state/%i
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: `deploy/follower.env.example`**（**不含私鑰**；含 FILET_STATE_DIR 由 unit 注入的說明）

```bash
# /etc/filet/followers/<id>.env — 單一 follower 引擎設定（權限 640，filet-engine 可讀）
# 注意：FILET_STATE_DIR / FILET_KEYSTORE / FILET_KEYS_DIR 由 systemd unit 注入，不放這裡。
# agent key 走 EnvFileKeyStore：/etc/filet/keys/<SPARK_ACCOUNT_ID>/agent.key（600）
SPARK_NETWORK=mainnet
SPARK_ACCOUNT_ID=alice
SPARK_USER_ADDR=0x...
SPARK_BUILDER_ADDR=0x...
COPY_LIVE_TRADING=true
COPY_ALLOCATED_CAPITAL=1000
COPY_MAX_DRAWDOWN_PCT=0.20
COPY_TG_BOT_TOKEN=...
COPY_TG_CHAT_ID=...
```

- [ ] **Step 3: `deploy/reload_follower.sh`**（逐一滾動；註明 sudoers NOPASSWD 需求）

```bash
#!/usr/bin/env bash
# 逐一滾動重啟 follower（拉版後）。單一失敗不中止其餘，最後回報。
# 需求：執行者對 `systemctl restart filet-follower@*` 有 sudo NOPASSWD（見部署文件）。
set -uo pipefail
units=$(systemctl list-units 'filet-follower@*' --no-legend --plain | awk '{print $1}')
fail=0
for u in $units; do
  echo "restarting $u ..."
  sudo systemctl restart "$u" || { echo "  FAILED: $u"; fail=1; }
  sleep 3
done
exit $fail
```

- [ ] **Step 4** read-back + 本地 verify。**Step 5** `git commit -m "feat: systemd template + env convention + rolling reload (per-follower state, keys read-only)"`。

---

### Task 8: 全域 panic（跨 follower）⭐（驗收加 opus 第二意見）（修 opus M4-panic、O8）

**Files:** Create `scripts/panic_all.py`、`tests/test_panic_all.py`。
先讀：`scripts/panic.py`（單 follower：`_exit_code`、`_plan_actions`/`plan_close_actions`、`_AdapterExecutor`、keystore 用法 :144）、`src/spark/copytrade/killswitch.py`（`trip`/`FlattenReport`）、`src/spark/filet/followers.py`、`src/spark/keystore/envfile.py`。

**⭐ load-bearing（opus 交互審查抓出）**：`panic_all` 對每個 follower 呼叫 `trip()` 時，**必須把 ARM 檔寫進該 follower 的 state root**（＝`FILET_STATE_BASE/<account_id>`，對齊 Task 7 unit 的 `/opt/filet/state/%i`）——**不可**用單一 repo 根。否則主網出事跑 `panic_all --yes` 平了倉，但 ARM 落錯地方，引擎下個 cycle `is_tripped()=False` → 依 leader 重新開倉，緊急平倉被一個 cycle 抹掉（違反 killswitch lock-first 設計）。

- [ ] **Step 1: 失敗測試**（fake registry + monkeypatch 假 adapter；不真連網/systemd；用 tmp_path 當 FILET_STATE_BASE）
1. 兩 follower 皆成功平倉 → 全數 report、退出碼 0。
2. **一 follower failures 非空 → 其餘照平、退出碼非 0、失敗 follower 明確標示**。
3. 一 follower 建 adapter/連線拋例外 → 捕捉記錄、不中止其餘、退出碼非 0。
4. **manifest 有壞條目**（opus O8）→ `load_followers_tolerant` 跳過壞條目仍平其餘、退出碼非 0、壞條目記錄。
5. dry（無 `--yes`）→ 每 follower 只列動作、零寫入、**零 ARM 檔**。
6. 退出碼＝各 follower `_exit_code` 語意跨 follower OR（任一 failures/orders_not_cancelled/不可達/壞條目 → 非 0）。
7. keystore 用 envfile（`FILET_KEYSTORE=envfile`）時能正確建；不印任何 key。
8. **⭐ ARM 隔離**：follower `alice` 的 panic（`--yes`）只在 `<FILET_STATE_BASE>/alice/` 下寫 ARM，`<base>/bob/` 不受影響——斷言 alice 的 ARM 檔存在、bob 的不存在。

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作** —— 抽 panic.py 單 follower 執行為可複用函式（若尚非），**簽章收 `state_root: Path` 傳給 `trip()`**（取代 panic.py:162 硬編 `_REPO_ROOT`）；`panic_all` 迭代 registry（`load_followers_tolerant`），per-follower try/except 隔離，每 follower 的 `state_root = Path(os.environ.get("FILET_STATE_BASE", "/opt/filet/state")) / ref.account_id`；keystore 走 `select_keystore`（Task 5）；`--yes` 才真平；network 不擋 mainnet；無 manifest → 用法 + exit 2。
  **附帶（T4 reviewer 指出 panic.py 未隔離）**：panic.py 自己的 `main()`（單 follower）也要 resolve 狀態根——用 `FILET_STATE_DIR` env（與 run_copytrade 同慣例，缺省 `_REPO_ROOT`）傳給抽出的函式，否則 VPS 上對單一 follower 跑 `panic.py` 仍讀共用 repo 根的 ARM。補一案：panic.py main() 用注入 state_root 讀寫該 follower 的 ARM。
- [ ] **Step 4** 全綠 + ruff。**Step 5** `git commit -m "feat: panic_all — global flatten, per-follower isolation, best-effort on bad manifest entries"`。

---

### Task 9: Leaderboard 快照 cron（M3 選人資料）

**Files:** Create `scripts/leaderboard_snapshot.py`、`tests/test_leaderboard_snapshot.py`。

- [ ] **Step 1: 失敗測試**（純函式）
1. `snapshot_from_rows(rows, day)`：每 row 取 address/account_value/pnl（欄位對照 HL leaderboard 回應——實作前查證），Decimal 化、按 pnl 降冪、截前 N（如 200）。
2. `append_snapshot(out_dir, snapshot)`：寫 `<out_dir>/<YYYY-MM-DD>.json`；同日重跑覆寫（冪等）。
3. 空 rows → 空快照、不炸。
4. 缺欄位 row → 大聲跳過並記數（不靜默）。

- [ ] **Step 2–4: 紅→實作→綠** —— 腳本：`fetch_leaderboard()`（SDK/REST，注入以便測）→ `snapshot_from_rows` → `append_snapshot(var/filet/leaderboard/)`。env `SPARK_NETWORK`（預設 mainnet）；import 階段零網路；docstring 附 crontab 每日範例。
- [ ] **Step 5** `git commit -m "feat: daily HL leaderboard snapshot for M3 leader selection"`。

---

### Task 10: 縱深防禦收斂（account_id 路徑安全 + review minor）⭐

**動機**：Task 1 與 Task 2 的兩位 reviewer 獨立指出同一縫——`account_id` 會流進檔案路徑（`EnvFileKeyStore` 的 `<root>/<account_id>/agent.key`、狀態目錄 `/opt/filet/state/<account_id>`、systemd `%i`），但只驗過「非空/不重複」，未驗字元集。含 `..` 或絕對路徑的 account_id 會經 pathlib `/` 逃出 root（路徑穿越）。目前 account_id 由我方 manifest 設定（非攻擊者可控），但 **Phase C onboarding 後端會從使用者輸入生成 account_id**——在該敏感元件（非託管 keystore）上補這道是縱深防禦。順帶收斂 review 的兩個 minor。

**Files:** Modify `src/spark/filet/followers.py`、`src/spark/keystore/envfile.py`、`scripts/run_copytrade.py`、`src/spark/filet/tagged_notifier.py`；Test：對應測試檔補案。
**執行時機**：**最後做**（觸及多個已 commit 檔案，須待 Task 5/8 對 run_copytrade.py/panic.py 的改動全部落地後）。

- [ ] **Step 1: 失敗測試**
1. `followers.py`：新增 `validate_account_id(s)`——僅允許 `^[a-zA-Z0-9_-]{1,64}$`（拒 `..`、`/`、`:`、空、超長）。`_parse_one` 呼叫它。測試：`"a/b"`、`".."`、`"a:b"`、`""`、65 字元 → ValueError；`"alice_1-2"` → 通過。
2. `envfile.py`：`EnvFileKeyStore` 的 `get_agent_signer`/`import_agent_key` 在建路徑前呼叫同一 `validate_account_id`（縱深防禦——即使有人繞過 registry 直接呼叫）。測試：`get_agent_signer("../evil")` → ValueError（非 KeyError），且不觸及檔案系統。
3. `run_copytrade.py`：`resolve_state_dir()` 對相對路徑 `FILET_STATE_DIR` → `.resolve()` 成絕對路徑（避免 CWD 依賴的靜默不同狀態根，T4 reviewer minor）。測試：相對路徑 env → 回絕對路徑。
4. `tagged_notifier.py`：`_key` 改 `if k is not None`（T3 reviewer minor：空字串 dedup_key 應命名空間化而非吞掉，與 TelegramNotifier `is not None` 慣例一致）。測試：`dedup_key=""` → `<tag>:`（非 None）。
5. `followers.py`：補 T2 reviewer 點的容錯 seen-set 迴歸測試（同 account_id「先壞後好再重複」→ 好的留、重複的擋、seen 不被壞條目污染）。

- [ ] **Step 2–4: 紅→實作→綠** —— `validate_account_id` 放 followers.py（單一真相），envfile.py import 它用（或各自定義同 regex——擇一，避免循環 import 就好；建議 followers.py 定義、envfile.py import）。跑各自受影響測試檔 + ruff。
- [ ] **Step 5: Commit** `git commit -m "harden: account_id path-safety validation; absolute state dir; notifier/registry review minors"`（Co-Authored-By footer）。

## 收尾（全 Phase A 完成後）

1. 指揮官親跑 `uv run pytest -q`（全套，M1 456 + 本 Phase 新增全綠）＋ `uv run ruff check src tests scripts`。
2. 更新本計畫頂部「執行狀態」節（任務→commit 對照），commit。
3. 更新 vault `filet/M2-closed-alpha-設計.md` 依賴排序：元件一 ✅。
4. 交付報告：完成清單、雙審戰果、待接續（元件三 VPS+onboarding 後端＝主鑰唯一持有者、資安最高；元件二 dashboard 待原型反饋）。

## 不在本 Phase（各自獨立計畫）

- **元件二 dashboard**（Next.js SIWE + wizard + 績效頁 + admin）——待原型 v1 反饋，可能新 repo `~/projects/filet-dashboard`（與 spark 分離自然強制進程隔離）。
- **元件三 VPS + onboarding 後端**——onboarding 後端是**主鑰唯一觸及點**（生成 agent key 寫入 EnvFileKeyStore、產 ApproveAgent/ApproveBuilderFee 待簽 payload），資安審查等級最高（含 opus 第二意見）。
- M1 收尾（testnet 實測、shadow 3 日、dogfood）——並行推進。
