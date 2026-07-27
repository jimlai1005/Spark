"""tests/test_api_leader_preview.py — GET /api/leaders/preview（自訂 leader 准入預覽）。

盯住准入檢查（格式／禁止自跟／operator kill-switch）與精選條目優先權：
- 檢查不過 → 4xx，detail 帶**機器可判的 reason code**
  （invalid_format / self_follow / leader_disabled）；
- operator 在精選檔停用的位址**不可**經自訂路徑准入（kill-switch 不可繞過）；
- 已在精選清單且可選 → already_listed=true（後續走既有精選流程，不寫 registry）。

⚠️ 鏈上活動自 2026-07-27 裁決後**不是閘門**：查無活動照樣放行、回 exists=false
（leader 尚未進場時客戶可先完成配置，進場後引擎自動開始跟）。

HL gateway 一律注入 FakeHL（測試全離線）；鏈上 exists 的精確判準（權益 > 0 或有持倉）
以本檔的錨例為準（此旗標只作預覽資訊，不再擋准入）。
"""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from tests.publicapi_helpers import login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（沿 test_api_leaders）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_CURATED = "0x" + "a1" * 20    # 精選、正常營運
_PAUSED = "0x" + "b2" * 20     # 精選、例行下架（accepting_new=false）
_DISABLED = "0x" + "c3" * 20   # 精選、安全撤銷（enabled=false）
_CUSTOM = "0x" + "e5" * 20     # 清單外的自訂位址

_LEADERS = [{"address": _CURATED, "name": "Alpha"},
            {"address": _PAUSED, "name": "Bravo", "accepting_new": False},
            {"address": _DISABLED, "name": "Charlie", "enabled": False}]

# 有 perp 活動的帳戶（權益 > 0 且有一筆持倉）——錨例的期望值均為手算字面值。
_ACTIVE_STATE = {"marginSummary": {"accountValue": "12345.6"},
                 "assetPositions": [{"position": {"coin": "ETH", "szi": "1.5"},
                                     "type": "oneWay"}]}


def _make(tmp_path, entries=None):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    return TestClient(app, base_url="https://testserver"), cfg, hl


def _preview(client, leader):
    return client.get("/api/leaders/preview", params={"leader_address": leader})


def test_requires_session(tmp_path):
    c, cfg, hl = _make(tmp_path)
    assert _preview(c, _CUSTOM).status_code == 401


def test_malformed_address_is_rejected_with_a_reason_code(tmp_path):
    """(1) 格式檢查：非 0x + 40 hex → 400 reason=invalid_format。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    r = _preview(c, "0xnot-an-address")
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "invalid_format"


def test_active_unlisted_address_previews_with_chain_data(tmp_path):
    """(2) 鏈上存在：有 perp 活動的清單外位址 → 200 預覽（位址正規化小寫、
    帳戶權益字串、持倉數、already_listed=false）。期望值＝FakeHL 錨例的手算字面值。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = _preview(c, "0x" + "E5" * 20)          # 大小寫變體 → 正規化
    assert r.status_code == 200, r.text
    assert r.json() == {"address": _CUSTOM, "exists": True,
                        "account_value": "12345.6", "position_count": 1,
                        "already_listed": False, "accepting_new": True}


def test_dead_address_is_admitted_with_exists_false(tmp_path):
    """鏈上無 perp 活動（權益 0 且無持倉，FakeHL 預設）→ 2026-07-27 裁決：**放行**，
    不再 404。leader 可能尚未進場，客戶想提前完成配置、進場後引擎自動開始跟——
    此刻鏈上沒活動不該擋下配置。exists 誠實回報 false、權益與持倉照實回。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    r = _preview(c, _CUSTOM)
    assert r.status_code == 200, r.text
    assert r.json() == {"address": _CUSTOM, "exists": False,
                        "account_value": "0.0", "position_count": 0,
                        "already_listed": False, "accepting_new": True}


def test_equity_without_positions_counts_as_existing(tmp_path):
    """判準錨例：權益 > 0、無持倉（剛入金還沒開倉）→ 存在。原則是擋死地址，
    不是審查品質。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse[_CUSTOM] = {"marginSummary": {"accountValue": "0.01"},
                                 "assetPositions": []}
    r = _preview(c, _CUSTOM)
    assert r.status_code == 200
    assert r.json()["exists"] is True and r.json()["position_count"] == 0


def test_own_login_address_is_rejected_as_self_follow(tmp_path):
    """(3) 禁止自跟：輸入 session 登入位址本身（含大小寫變體）→ 400
    reason=self_follow——自我跟單無意義，且會形成回饋迴圈。"""
    c, cfg, hl = _make(tmp_path)
    wallet = login(c)
    hl.clearinghouse[wallet.address.lower()] = _ACTIVE_STATE   # 即使鏈上活躍也要擋
    r = _preview(c, wallet.address)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "self_follow"


def test_operator_disabled_leader_cannot_be_admitted(tmp_path):
    """⭐ 精選白名單 enabled=false（安全撤銷）的位址 → 400 reason=leader_disabled，
    **即使鏈上活躍**——自訂路徑不得繞過 operator 的安全撤銷（精選條目一律優先）。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse[_DISABLED] = _ACTIVE_STATE
    r = _preview(c, _DISABLED)
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "leader_disabled"


def test_paused_curated_leader_is_admitted_with_accepting_new_false(tmp_path):
    """⭐ 精選 accepting_new=false（例行下架，enabled=true）→ 2026-07-27 使用者裁決：
    **放行**、回 accepting_new=false（前端據此畫警示但不擋）。例行下架只是暫不收
    新客，引擎照跟（is_still_permitted 只看 enabled）；客戶堅持要跟就放行——與
    enabled=false 的安全撤銷（硬擋）分兩支。already_listed 仍為 true（在精選清單）。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse[_PAUSED] = _ACTIVE_STATE
    r = _preview(c, _PAUSED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepting_new"] is False
    assert body["already_listed"] is True


def test_curated_selectable_address_is_flagged_already_listed(tmp_path):
    """已在精選清單且可選 → already_listed=true（同一位址不出現兩種身分：
    前端據此走既有精選流程、不寫 registry）。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse[_CURATED] = _ACTIVE_STATE
    r = _preview(c, _CURATED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_listed"] is True
    assert body["address"] == _CURATED and body["account_value"] == "12345.6"


def test_curated_address_with_empty_chain_account_is_not_404(tmp_path):
    """精選位址即使鏈上讀不到活動也不回 not_found——它是 operator 背書的清單成員，
    「查無活動」的鏈上門檻只施加在未經審核的自訂位址上；exists 誠實回報 false。"""
    c, cfg, hl = _make(tmp_path)
    login(c)                                   # FakeHL 預設空帳戶
    r = _preview(c, _CURATED)
    assert r.status_code == 200, r.text
    assert r.json()["already_listed"] is True and r.json()["exists"] is False


def test_hl_outage_is_a_502_not_a_rejection(tmp_path):
    """HL 查詢 transient 失敗（重試耗盡）→ 502（稍後重試），**不得**誤判成
    not_found——「讀不到鏈」≠「這個位址不存在」（工程原則 2 的分類）。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse_error[_CUSTOM] = ConnectionError("hl down")
    assert _preview(c, _CUSTOM).status_code == 502


def test_broken_allowlist_is_a_503(tmp_path):
    """精選白名單壞掉 → 503（沿 _load_leaders_or_503）：沒有白名單就無法判斷
    精選優先權，不得当成「不在清單」繼續准入。"""
    c, cfg, hl = _make(tmp_path)
    login(c)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    (tmp_path / "leaders.json").write_text("{ not json")
    assert _preview(c, _CUSTOM).status_code == 503
