"""tests/test_api_custom_leader_select.py — 自訂 leader 走簽章選擇流程（2026-07-27 spec）。

盯住五件事：
(1) 訊息端點對**可准入**的非精選位址發待簽原文（取代原本的一律拒絕）；
(2) POST select 在驗簽通過後**重新執行全部准入檢查**（不信任客戶端曾呼叫
    preview，防 TOCTOU）；
(3) 通過 → **冪等**寫入 user registry（source:"user"＋added_by 稽核欄位），
    再落簽章換 leader 記錄；registry 寫入失敗 → 5xx 且**不**記錄換 leader；
(4) already_listed 的位址走既有精選流程，**不寫** registry；
(5) 公開目錄 /api/leaders 不含 user-sourced 條目（僅本人可用的可見性）。

簽名用真密碼學（eth_account 本地運算，不觸網，沿 test_api_leader_select）。
"""
import json
import socket
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.filet.leader_change import build_leader_change_message, load_leader_changes
from spark.filet.user_leaders import load_user_leaders
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（沿 test_api_leaders）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_CURATED = "0x" + "a1" * 20    # 精選、正常營運
_DISABLED = "0x" + "c3" * 20   # 精選、安全撤銷（enabled=false）
_CUSTOM = "0x" + "e5" * 20     # 清單外的自訂位址

_LEADERS = [{"address": _CURATED, "name": "Alpha"},
            {"address": _DISABLED, "name": "Charlie", "enabled": False}]

_ACTIVE_STATE = {"marginSummary": {"accountValue": "12345.6"},
                 "assetPositions": [{"position": {"coin": "ETH", "szi": "1.5"},
                                     "type": "oneWay"}]}


def _make(tmp_path, entries=None):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    return TestClient(app, base_url="https://testserver"), cfg, store, hl


def _login(client, wallet):
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return r.json()["account_id"]


def _now_iso() -> str:
    import time
    return datetime.fromtimestamp(time.time(), timezone.utc).isoformat()


def _payload(*, account_id, leader, nonce, wallet, issued_at=None):
    issued_at = issued_at or _now_iso()
    msg = build_leader_change_message(account_id=account_id, leader_address=leader,
                                      nonce=nonce, issued_at=issued_at)
    sig = wallet.sign_message(encode_defunct(text=msg)).signature.hex()
    return {"account_id": account_id, "leader_address": leader, "nonce": nonce,
            "issued_at": issued_at, "signature": sig, "message": msg}


# ---------- 訊息端點：可准入的自訂位址拿得到待簽原文 ----------

def test_message_is_issued_for_an_admissible_custom_address(tmp_path):
    """⭐ 非精選但通過准入（格式合法、非自己、鏈上活躍）→ 200，回正規化位址的
    待簽原文＋nonce（取代原本 is_selectable 的一律拒絕）。"""
    c, cfg, store, hl = _make(tmp_path)
    _login(c, Account.create())
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = c.get("/api/leaders/select/message",
              params={"leader_address": "0x" + "E5" * 20})   # 大小寫變體 → 正規化
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["leader_address"] == _CUSTOM
    assert _CUSTOM in body["message"] and body["nonce"]


# ---------- POST select：驗簽通過 → 冪等寫 registry → 落簽章記錄 ----------

def _select_custom(c, hl, wallet, acct, *, leader=_CUSTOM, chain_state="active"):
    """走完整流程：message 端點取 nonce → 本人簽名 → POST select。"""
    if chain_state == "active":
        hl.clearinghouse[leader.lower()] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": leader})
    assert r0.status_code == 200, r0.text
    body = _payload(account_id=acct, leader=leader, nonce=r0.json()["nonce"],
                    wallet=wallet)
    return c.post("/api/leaders/select", json=body)


def test_custom_select_writes_registry_then_records_the_change(tmp_path):
    """⭐ 自訂 leader happy path：驗簽通過 → 寫入 user registry（source:"user"＋
    added_by=加入者 account_id）→ 落簽章換 leader 記錄。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct)
    assert r.status_code == 200, r.text
    assert r.json()["leader_address"] == _CUSTOM

    (ref,) = load_user_leaders(cfg.user_leaders_path)
    assert ref.address == _CUSTOM and ref.enabled is True
    raw = json.loads(Path(cfg.user_leaders_path).read_text())["leaders"][0]
    assert raw["source"] == "user" and raw["added_by"] == acct

    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _CUSTOM


# ---------- ⭐ POST 獨立重跑准入（不信任 preview／message，防 TOCTOU） ----------

def test_post_admits_even_if_account_has_no_chain_activity(tmp_path):
    """⭐ 2026-07-27 裁決：鏈上無活動不再擋下自訂 leader。訊息端點通過後、提交前
    帳戶被清空（鏈上無 perp 活動）→ POST 仍**放行**（不再 404 not_found），registry
    與換 leader 記錄照常落地——leader 尚未進場時客戶可先完成配置，進場後引擎自動跟。

    （TOCTOU 重跑准入的機制本身仍受 test_post_reruns_admission_operator_disables_
    after_message 覆蓋：operator kill-switch 在提交那一刻仍會擋下。）"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r0.status_code == 200
    del hl.clearinghouse[_CUSTOM]                    # 帳戶清空（鏈上無活動）
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"],
                    wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 200, r.text
    (ref,) = load_user_leaders(cfg.user_leaders_path)
    assert ref.address == _CUSTOM and ref.enabled is True
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _CUSTOM


def test_post_reruns_admission_operator_disables_after_message(tmp_path):
    """⭐ 訊息端點通過後 operator 把該位址列入精選檔並停用 → POST 擋下
    （leader_disabled）——kill-switch 在提交那一刻仍然有效。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r0.status_code == 200
    (tmp_path / "leaders.json").write_text(json.dumps(
        {"leaders": _LEADERS + [{"address": _CUSTOM, "name": "Evil",
                                 "enabled": False}]}))
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"],
                    wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_post_rejects_self_follow_without_needing_preview(tmp_path):
    """⭐ 完全跳過 preview／message、直接 POST 自己的登入位址 → 400 self_follow，
    無任何落地——證明 POST 的准入是獨立的，不依賴客戶端先走過任何檢查。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[w.address.lower()] = _ACTIVE_STATE
    body = _payload(account_id=acct, leader=w.address.lower(), nonce="deadbeef",
                    wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "self_follow"
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_admission_failure_does_not_burn_the_nonce(tmp_path):
    """准入失敗發生在 nonce 消耗之前 → 同一顆 nonce 在准入恢復後仍可完成一次合法
    變更（否則一次准入打嗝就作廢客戶手上的授權，自我 DoS）。

    ⚠️ 觸發准入失敗用 operator kill-switch（把該位址列入精選檔並停用）而非鏈上無
    活動——後者自 2026-07-27 裁決後不再是失敗來源。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    nonce = r0.json()["nonce"]
    lpath = tmp_path / "leaders.json"
    lpath.write_text(json.dumps(                       # operator 停用該位址
        {"leaders": _LEADERS + [{"address": _CUSTOM, "name": "Evil",
                                 "enabled": False}]}))
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=nonce, wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"
    lpath.write_text(json.dumps({"leaders": _LEADERS}))  # operator 撤下停用（恢復）
    assert c.post("/api/leaders/select", json=body).status_code == 200


def test_bad_signature_leaves_registry_untouched(tmp_path):
    """驗簽失敗（另一把私鑰簽的）→ 400，registry 與記錄都不落地：
    寫入只發生在**全部**驗證通過之後（准入通過 ≠ 授權成立）。"""
    c, cfg, store, hl = _make(tmp_path)
    w, attacker = Account.create(), Account.create()
    acct = _login(c, w)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"],
                    wallet=attacker)                 # 簽章來自另一把鑰匙
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert load_user_leaders(cfg.user_leaders_path) == []
    assert load_leader_changes(cfg.leader_changes_path) == []


# ---------- registry 寫入：冪等 ＋ fail loudly ----------

def test_selecting_the_same_custom_leader_twice_writes_one_entry(tmp_path):
    """⭐ 冪等：同一自訂 leader 選兩次（兩顆 nonce、兩次簽名）→ registry 恰一筆，
    第二次照樣 200（重送安全）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct).status_code == 200
    assert _select_custom(c, hl, w, acct).status_code == 200
    assert len(load_user_leaders(cfg.user_leaders_path)) == 1


def test_registry_write_failure_is_5xx_and_no_change_recorded(tmp_path):
    """⭐ registry 寫入失敗（lock 落點被目錄佔住 → OSError）→ 5xx 且**不**記錄換
    leader（fail loudly，工程原則 3）：leader 進不了引擎的驗證來源卻記了換手，
    引擎會永遠拒絕套用一筆客戶已簽章的意圖。注入點選 lock 檔（review F3 之後
    寫入必經之路）：registry 本體仍缺席＝合法空狀態，准入的讀取閘不受影響，
    失敗確實發生在**寫入**那一步。"""
    from spark.filet.user_leaders import registry_lock_path
    c, cfg, store, hl = _make(tmp_path)
    registry_lock_path(cfg.user_leaders_path).mkdir(parents=True)  # 寫入必然失敗
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct)
    assert r.status_code == 500
    assert load_leader_changes(cfg.leader_changes_path) == []


def test_corrupt_registry_makes_admission_a_503_not_an_open_gate(tmp_path):
    """registry 損毀 → 准入端點 503（transient），**不得**降級成空清單放行：
    空清單會讓 operator 的停用位暫時隱形，把一次讀取失敗偽裝成「可准入」。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    _login(c, Account.create())
    p = Path(cfg.user_leaders_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = c.get("/api/leaders/preview", params={"leader_address": _CUSTOM})
    assert r.status_code == 503


def test_already_listed_address_does_not_touch_the_registry(tmp_path):
    """已在精選清單且可選的位址 → 走既有精選流程，**不寫** registry
    （同一位址不出現兩種身分）。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct, leader=_CURATED, chain_state="none")
    assert r.status_code == 200, r.text
    assert not Path(cfg.user_leaders_path).exists()
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _CURATED


# ---------- ⭐ registry 內的 operator kill-switch 對准入可見（review F4） ----------
# operator 對 user-sourced 條目的停用手段是手編 registry（enabled=false，或
# accepting_new=false 停收）。准入若只看精選 refs，這個停用位對 preview／message／
# POST 全部隱形：用戶重選被告知成功（200），意圖卻在引擎端永遠拒絕——「停用了
# 但看起來能用」比「不能用」更糟。三端點都必須拒絕（reason 沿用 leader_disabled，
# 維持「停用 vs 暫停」不可分辨的既有政策）。


def _seed_registry(cfg, address, **over):
    """直接落一筆 registry 條目（模擬 operator 手編後的狀態）。"""
    from pathlib import Path
    entry = {"address": address, "name": address, "description": "",
             "enabled": True, "accepting_new": True,
             "source": "user", "added_by": "f" + "aa" * 20}
    entry.update(over)
    p = Path(cfg.user_leaders_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"leaders": [entry]}))
    return p


def test_preview_rejects_a_registry_revoked_address(tmp_path):
    """preview：registry 條目 enabled=false（安全撤銷）→ 400 leader_disabled，
    即使鏈上活躍。⭐ 2026-07-27 拆旗標：只有 enabled=false（安全撤銷）硬擋；
    accepting_new=false（例行下架）改為放行帶警示（見
    test_preview_admits_a_registry_paused_address）。"""
    c, cfg, store, hl = _make(tmp_path)
    _login(c, Account.create())
    _seed_registry(cfg, _CUSTOM, enabled=False)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = c.get("/api/leaders/preview", params={"leader_address": _CUSTOM})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"


def test_message_rejects_a_registry_revoked_address(tmp_path):
    """訊息端點同閘：enabled=false 的安全撤銷位址連待簽原文都不該給
    （給了就是一份必被拒的簽名陷阱）。"""
    c, cfg, store, hl = _make(tmp_path)
    _login(c, Account.create())
    _seed_registry(cfg, _CUSTOM, enabled=False)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"


def test_post_rejects_a_registry_revoked_address_and_never_reenables_it(tmp_path):
    """⭐ POST：重選一個 registry **安全撤銷**（enabled=false）位址 → 400
    leader_disabled、換 leader 記錄不落地，且 registry 檔**一個位元組都不動**
    ——重選絕不得把撤銷條目改回 enabled（冪等寫入本就不覆寫既有條目，這裡連
    寫入路徑都到不了）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    p = _seed_registry(cfg, _CUSTOM, enabled=False)
    before = p.read_text()
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    body = _payload(account_id=acct, leader=_CUSTOM, nonce="deadbeef", wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"
    assert p.read_text() == before
    assert load_leader_changes(cfg.leader_changes_path) == []


# ---------- ⭐ registry accepting_new=false（例行下架）→ 放行帶警示（2026-07-27） ----------


def test_preview_admits_a_registry_paused_address(tmp_path):
    """⭐ registry 條目 accepting_new=false（例行下架、enabled=true）→ preview
    **放行**、回 accepting_new=false（前端畫警示但不擋）。與 enabled=false 的
    安全撤銷分兩支：例行下架只是暫不收新客，引擎照跟。"""
    c, cfg, store, hl = _make(tmp_path)
    _login(c, Account.create())
    _seed_registry(cfg, _CUSTOM, accepting_new=False)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = c.get("/api/leaders/preview", params={"leader_address": _CUSTOM})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepting_new"] is False
    assert body["already_listed"] is False    # registry 條目不算精選


def test_post_admits_a_registry_paused_address(tmp_path):
    """⭐ registry accepting_new=false（enabled=true）→ POST 放行、落簽章換 leader
    記錄；registry 檔冪等不動（該位址已存在，不覆寫）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    p = _seed_registry(cfg, _CUSTOM, accepting_new=False)
    before = p.read_text()
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r0.status_code == 200, r0.text
    body = _payload(account_id=acct, leader=_CUSTOM, nonce=r0.json()["nonce"], wallet=w)
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 200, r.text
    assert p.read_text() == before             # 冪等：既有條目不覆寫
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _CUSTOM


def test_curated_paused_leader_post_does_not_mirror_into_registry(tmp_path):
    """⭐ Finding 1（kill-switch 破口修復）：精選 accepting_new=false（例行下架、
    enabled=true）→ POST 放行、落簽章換 leader 記錄，但**不**鏡射進 user registry。

    精選位址本就經精選檔 enabled=true 被引擎放行（is_still_permitted 只看 enabled），
    寫進 registry 是多餘且危險的影子條目：日後 operator 用「移除精選條目」撤銷該
    leader（leaders.py 明載＝enabled:false）時，registry 的 enabled:true 影子會壓過
    （merge 沒有精選條目可壓）→ 引擎繼續跟一個已撤銷的 leader。守衛加
    `not already_listed` 後，精選位址（已列）連寫入路徑都到不了。"""
    from pathlib import Path
    _PAUSED = "0x" + "d4" * 20
    c, cfg, store, hl = _make(
        tmp_path,
        entries=_LEADERS + [{"address": _PAUSED, "name": "Delta",
                             "accepting_new": False}])
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct, leader=_PAUSED)
    assert r.status_code == 200, r.text
    assert r.json()["leader_address"] == _PAUSED
    # registry 不含該精選位址（檔根本不會被建＝合法空狀態）
    assert not Path(cfg.user_leaders_path).exists()
    # 換 leader 記錄仍落地
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _PAUSED


def test_removing_curated_paused_entry_revokes_the_leader(tmp_path):
    """⭐ Finding 1 破口修復證明：精選 paused 位址 POST 後，operator 以「移除精選
    條目」撤銷（leaders.py 明載＝enabled:false）→ 引擎 is_still_permitted 轉為
    false（不再放行）。若 POST 錯誤地把 enabled:true 影子寫進 registry，移除精選
    條目後 merge 會讓影子勝出、is_still_permitted 仍 true——那正是破口。"""
    from spark.filet.leaders import is_still_permitted, load_leaders
    from spark.filet.user_leaders import load_user_leaders as _load_ul
    from spark.filet.user_leaders import merge_leaders
    _PAUSED = "0x" + "d4" * 20
    lpath = tmp_path / "leaders.json"
    c, cfg, store, hl = _make(
        tmp_path,
        entries=_LEADERS + [{"address": _PAUSED, "name": "Delta",
                             "accepting_new": False}])
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct, leader=_PAUSED).status_code == 200
    # 跟隨中：精選 enabled=true → 引擎放行
    merged = merge_leaders(load_leaders(cfg.leaders_path),
                           _load_ul(cfg.user_leaders_path))
    assert is_still_permitted(_PAUSED, merged) is True
    # operator 撤銷＝從精選清單移除該條目
    lpath.write_text(json.dumps({"leaders": _LEADERS}))
    merged = merge_leaders(load_leaders(cfg.leaders_path),
                           _load_ul(cfg.user_leaders_path))
    assert is_still_permitted(_PAUSED, merged) is False   # 破口已補


def test_registry_entry_that_is_active_does_not_block_admission(tmp_path):
    """反向錨定：registry 條目 enabled 且 accepting_new → 准入照常通過（閘門只擋
    停用位，不是擋「已存在」）——否則重選自己已准入的 leader 會被自己擋掉。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    _seed_registry(cfg, _CUSTOM)                     # enabled=true, accepting_new=true
    assert _select_custom(c, hl, w, acct).status_code == 200


# ---------- 可見性與兩端接線 ----------

def test_public_directory_excludes_user_sourced_leaders(tmp_path):
    """⭐ /api/leaders 公開目錄**不含** user-sourced 條目：策展門面不被任意位址
    稀釋、平台不為未審核 leader 背書（「僅本人可用」由 API 層可見性實現）。"""
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct).status_code == 200   # registry 已有 _CUSTOM
    assert len(load_user_leaders(cfg.user_leaders_path)) == 1  # 前提確認：真的寫進去了
    r = c.get("/api/leaders")
    assert r.status_code == 200
    assert [x["address"] for x in r.json()["leaders"]] == [_CURATED]


def test_config_registry_path_lives_in_the_exchange_dir(tmp_path):
    """cfg.user_leaders_path＝**交換目錄**下的 user_leaders.json（2026-07-27
    review F2）：不再是精選白名單的 sibling——sibling 佈局迫使 API 取得白名單
    目錄寫權（原子寫需要），破壞「filet-api 對白名單無寫權」的承重不變量。"""
    cfg = make_cfg(tmp_path)
    assert cfg.user_leaders_path == str(tmp_path / "exchange" / "user_leaders.json")
    from pathlib import Path
    assert Path(cfg.user_leaders_path).parent != Path(cfg.leaders_path).parent


# ---------- ⭐ vault 自動偵測：registry 條目寫 kind（2026-07-31 Wave 2） ----------
# registry 條目多了 kind 之後，既有雙層保護（watcher env 注入＋引擎每輪
# apply_vault_policy）對 vault leader 自動生效——這裡盯「寫端真的落了 kind」。

_VAULT = "0x" + "f6" * 20
_T0, _T1 = 1_700_000_000_000, 1_702_000_000_000
_VAULT_STATE = {"marginSummary": {"accountValue": "1000.0"},
                "withdrawable": "800.0",
                "assetPositions": [{"position": {"coin": "ETH", "szi": "1.5"},
                                    "type": "oneWay"}]}
_VAULT_DETAILS = {"name": "Test Vault", "leaderFraction": 0.05,
                  "maxDistributable": "800.0", "followers": [], "isClosed": False}
_VAULT_PORTFOLIO = [
    ["month", {"accountValueHistory": [[_T0, "900.0"], [_T1, "1000.0"]],
               "pnlHistory": [[_T0, "0.0"], [_T1, "50.0"]]}],
    ["perpMonth", {"accountValueHistory": [[_T0, "900.0"], [_T1, "1000.0"]],
                   "pnlHistory": [[_T0, "0.0"], [_T1, "50.0"]]}],
]
_VAULT_LEDGER = [{"time": _T0 + 1000, "delta": {"type": "deposit", "usdc": "50.0"}}]


def _seed_vault(hl, addr=_VAULT, *, details=None):
    hl.clearinghouse[addr] = _VAULT_STATE
    hl.vaults[addr] = details if details is not None else _VAULT_DETAILS
    hl.portfolios[addr] = _VAULT_PORTFOLIO
    hl.ledger_updates[addr] = _VAULT_LEDGER


def test_vault_select_writes_kind_vault_into_the_registry(tmp_path):
    """⭐ vault 位址（六項全 PASS）select 成功 → registry 條目含 "kind":"vault"
    （讀檔斷言原文＋load_user_leaders 的 LeaderRef.kind）——引擎的 vault 雙層
    保護吃的就是這個欄位。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    _seed_vault(hl)
    r = _select_custom(c, hl, w, acct, leader=_VAULT, chain_state="none")
    assert r.status_code == 200, r.text
    raw = json.loads(Path(cfg.user_leaders_path).read_text())["leaders"][0]
    assert raw["kind"] == "vault"
    (ref,) = load_user_leaders(cfg.user_leaders_path)
    assert ref.address == _VAULT and ref.kind == "vault"


def test_standard_select_writes_kind_standard_into_the_registry(tmp_path):
    """非 vault 自訂位址 select → registry 條目 "kind":"standard"（欄位必寫，
    不留缺席讓讀端猜預設）。"""
    from pathlib import Path
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    r = _select_custom(c, hl, w, acct)
    assert r.status_code == 200, r.text
    raw = json.loads(Path(cfg.user_leaders_path).read_text())["leaders"][0]
    assert raw["kind"] == "standard"


def test_vault_check_failure_does_not_block_select_but_alerts(tmp_path):
    """⭐ advisory 裁決的承重測試：恆等式 FAIL 的 vault，用戶仍可完成 select
    （200、registry 落 kind:"vault"、換 leader 記錄照落），notifier 收到 critical
    且訊息含位址——工程判斷交給用戶，營運靠告警人工介入。"""
    from spark.copytrade.notifier import RecordingNotifier
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    notifier = RecordingNotifier()
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, notifier=notifier)
    c = TestClient(app, base_url="https://testserver")
    w = Account.create()
    acct = _login(c, w)
    _seed_vault(hl, details={**_VAULT_DETAILS, "maxDistributable": "700.0"})
    r = _select_custom(c, hl, w, acct, leader=_VAULT, chain_state="none")
    assert r.status_code == 200, r.text
    (ref,) = load_user_leaders(cfg.user_leaders_path)
    assert ref.kind == "vault"
    entries = load_leader_changes(cfg.leader_changes_path)
    assert len(entries) == 1 and entries[0]["leader_address"] == _VAULT
    crits = [rec for rec in notifier.records if rec[0] == "critical"]
    assert crits and _VAULT in crits[0][2]


def test_engine_resolves_the_leader_the_api_admitted(tmp_path):
    """⭐ end-to-end：API 准入寫入 registry 的自訂 leader，引擎 resolve_leader
    從**同一個交換目錄**接到 registry 後放行——寫端與讀端真的接上，
    合法選定的自訂 leader 不會在引擎層被拒（spec user story 18）。"""
    from spark.filet.leader_resolve import resolve_leader
    c, cfg, store, hl = _make(tmp_path)
    w = Account.create()
    acct = _login(c, w)
    assert _select_custom(c, hl, w, acct).status_code == 200
    manifest = tmp_path / "followers.json"
    manifest.write_text(json.dumps({"followers": [
        {"account_id": acct, "user_address": w.address,
         "builder_address": "0x" + "22" * 20, "network": "mainnet",
         "label": "", "leader_address": _CUSTOM}]}))
    res = resolve_leader(account_id=acct, manifest_path=manifest,
                         leaders_path=cfg.leaders_path,
                         user_leaders_path=cfg.user_leaders_path,
                         env_default="", self_address=w.address)
    assert res.address == _CUSTOM
