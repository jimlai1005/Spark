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
                        "already_listed": False, "accepting_new": True,
                        "kind": "standard", "vault_checks": None}


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
                        "already_listed": False, "accepting_new": True,
                        "kind": "standard", "vault_checks": None}


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


# ---------- ⭐ vault 自動偵測＋advisory 六項檢查（2026-07-31 Wave 2） ----------
# owner 裁決：自訂 leader 是 vault 也要無痛低門檻上線——准入自動偵測、preflight
# 六項恆等式檢查照跑，但 **FAIL 不擋用戶**（advisory：回應照 200，前端畫警語），
# 同時 notifier.critical 讓營運人工介入。契約欄位：
#   kind: "standard" | "vault"
#   vault_checks: null | {"passed": bool, "failures": [{"name", "detail"}...]}

_VAULT = "0x" + "f6" * 20
_T0, _T1 = 1_700_000_000_000, 1_702_000_000_000

# 全 PASS 錨例（手算）：ΔAV=100、Δpnl=50、淨流量=+50 → 殘差 0；
# withdrawable == maxDistributable == 800；month/perpMonth pnl 終值一致（50）。
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


def _seed_vault(hl, addr=_VAULT, *, details=None, state=None,
                portfolio=None, ledger=None):
    hl.clearinghouse[addr] = state if state is not None else _VAULT_STATE
    hl.vaults[addr] = details if details is not None else _VAULT_DETAILS
    hl.portfolios[addr] = portfolio if portfolio is not None else _VAULT_PORTFOLIO
    hl.ledger_updates[addr] = ledger if ledger is not None else _VAULT_LEDGER


def _make_recording(tmp_path, entries=None):
    """同 _make，另注入 RecordingNotifier（告警可觀測）。"""
    from spark.copytrade.notifier import RecordingNotifier
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries if entries is not None else _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    notifier = RecordingNotifier()
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, notifier=notifier)
    return (TestClient(app, base_url="https://testserver"), cfg, hl, notifier)


def test_vault_address_previews_with_passing_checks(tmp_path):
    """vault 位址、六項全 PASS → kind="vault"、vault_checks.passed=true、
    failures==[]（PASS 不回傳細節），且 notifier **零** critical。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    _seed_vault(hl)
    r = _preview(c, _VAULT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "vault"
    assert body["vault_checks"] == {"passed": True, "failures": []}
    assert [rec for rec in notifier.records if rec[0] == "critical"] == []


def test_vault_check_failure_is_advisory_and_alerts_ops(tmp_path):
    """⭐ 恆等式弄壞（maxDistributable ≠ withdrawable）→ 用戶**不被擋**（200），
    failures 非空且含 name/detail；notifier 收到 critical：訊息含位址、FAIL 項，
    dedup key 含位址（重複告警可去重、不同 vault 不互吞）。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    _seed_vault(hl, details={**_VAULT_DETAILS, "maxDistributable": "700.0"})
    r = _preview(c, _VAULT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "vault"
    assert body["vault_checks"]["passed"] is False
    names = [f["name"] for f in body["vault_checks"]["failures"]]
    assert "perp-resident-tvl" in names
    assert all(f["detail"] for f in body["vault_checks"]["failures"])
    crits = [rec for rec in notifier.records if rec[0] == "critical"]
    assert crits, "advisory FAIL 必須發 critical 告警"
    level, category, text, dedup = crits[0]
    assert _VAULT in text and "perp-resident-tvl" in text
    assert "人工核對" in text
    assert dedup is not None and _VAULT in dedup


def test_flow_stats_warn_does_not_count_as_failure(tmp_path):
    """flow-stats 是資訊性檢查（恆為 PASS，WARN 只進 detail）——單筆流量 > 8% TVL
    不得讓 vault_checks 變 FAIL、不得觸發 critical。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    # 單筆 500（50% TVL）：flow-stats 印 WARN；為維持恆等式，pnl 側同步調整——
    # ΔAV=100、Δpnl=-400、淨流量=+500 → 殘差 0。
    portfolio = [
        ["month", {"accountValueHistory": [[_T0, "900.0"], [_T1, "1000.0"]],
                   "pnlHistory": [[_T0, "0.0"], [_T1, "-400.0"]]}],
        ["perpMonth", {"accountValueHistory": [[_T0, "900.0"], [_T1, "1000.0"]],
                       "pnlHistory": [[_T0, "0.0"], [_T1, "-400.0"]]}],
    ]
    ledger = [{"time": _T0 + 1000, "delta": {"type": "deposit", "usdc": "500.0"}}]
    _seed_vault(hl, portfolio=portfolio, ledger=ledger)
    r = _preview(c, _VAULT)
    assert r.status_code == 200, r.text
    assert r.json()["vault_checks"] == {"passed": True, "failures": []}
    assert [rec for rec in notifier.records if rec[0] == "critical"] == []


def test_non_vault_address_is_standard_with_null_checks(tmp_path):
    """vault_details 回 None（真實 API 對非 vault 的行為，FakeHL 預設）→ 不炸、
    kind="standard"、vault_checks=null，不打 portfolio/ledger、零告警。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    r = _preview(c, _CUSTOM)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "standard"
    assert r.json()["vault_checks"] is None
    assert notifier.records == []


def test_vault_details_missing_name_is_treated_as_standard(tmp_path):
    """vaultDetails 是 dict 但缺 name（形狀漂移）→ 當 standard，不進檢查路徑。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    hl.vaults[_CUSTOM] = {"maxDistributable": "1.0"}   # 無 name
    r = _preview(c, _CUSTOM)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "standard" and r.json()["vault_checks"] is None


def test_vault_details_transient_failure_is_a_502(tmp_path):
    """vault_details transient 例外（重試耗盡）→ 502（照 _chain_activity 同語意
    上拋）——「讀不到鏈」≠「不是 vault」，不得靜默當 standard 放行。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    hl.clearinghouse[_CUSTOM] = _ACTIVE_STATE
    hl.vault_details_error[_CUSTOM] = ConnectionError("hl down")
    assert _preview(c, _CUSTOM).status_code == 502


def test_vault_check_crash_is_advisory_check_error_not_a_500(tmp_path):
    """檢查資料面形狀壞掉（clearinghouseState 缺 withdrawable → run_checks 內
    KeyError）→ advisory 原則不變：**不擋用戶**（200），failures 記 check-error
    ＋critical 告警——檢查掛掉不等於 vault 有問題，但一定要有人看。"""
    c, cfg, hl, notifier = _make_recording(tmp_path)
    login(c)
    state = {"marginSummary": {"accountValue": "1000.0"},
             "assetPositions": []}                     # 無 withdrawable
    _seed_vault(hl, state=state)
    r = _preview(c, _VAULT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "vault"
    assert body["vault_checks"]["passed"] is False
    assert [f["name"] for f in body["vault_checks"]["failures"]] == ["check-error"]
    assert [rec for rec in notifier.records if rec[0] == "critical"]


def test_missing_tg_keys_fall_back_to_log_only_without_crashing(tmp_path):
    """TG 兩鍵缺席（make_cfg 預設）＋未注入 notifier → create_app 落到 log-only
    fallback：FAIL vault 的 preview 照樣 200，告警路徑不炸。"""
    c, cfg, hl = _make(tmp_path)                       # 無 notifier 注入
    login(c)
    _seed_vault(hl, details={**_VAULT_DETAILS, "maxDistributable": "700.0"})
    r = _preview(c, _VAULT)
    assert r.status_code == 200, r.text
    assert r.json()["vault_checks"]["passed"] is False
