"""tests/test_api_onboard.py"""
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.publicapi_helpers import BUILDER, login, make_app

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def test_generate_agent_returns_address(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    agent = r.json()["agent_address"]
    assert agent.startswith("0x") and len(agent) == 42 and agent == agent.lower()
    account_id = "f" + wallet.address.lower()[2:]
    assert keysvc.generated[account_id].lower() == agent
    assert store.get_agent_address(account_id) == agent


def test_generate_agent_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).post("/api/onboard/agent").status_code == 401


def test_generate_agent_twice_409(tmp_path):
    """防重生：已有 agent 拒絕 rotate（避免作廢既有鏈上授權，沿 M1 語意）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 200
    r = client.post("/api/onboard/agent")
    assert r.status_code == 409
    # 成功義的 409（前端視為成功）帶機器可判別 code——與自癒失敗的 409 區分
    # （2026-08-29 M3 round2 Task 3：兩種 409 先前無法區分，是隱藏 bug）。
    assert r.json()["detail"]["code"] == "agent_exists"
    assert "不重生" in r.json()["detail"]["message"]


def test_keysvc_down_502(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    keysvc.fail = ConnectionRefusedError("keysvc down")
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 502


def test_keysvc_truncated_response_502(tmp_path):
    """opus 必修 1 的 app 層閉環：keysvc client 把截斷/中斷回應轉譯成 ConnectionError
    後，app 既有的 except ConnectionError handler（或 onboard_agent 的 except OSError）
    必須接得住、回 502（而不是通用 500）——證明轉譯後不需要改 app.py。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    keysvc.fail = ConnectionError("keysvc 回應中斷")
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 502


def test_status_hl_down_502(tmp_path):
    """HL transient 失敗（resilience 重試耗盡後上拋 ConnectionError）在 status 端點
    統一轉譯成 502，而非通用 500（app 層 exception handler，工程原則 5）。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    login(client)

    def _boom(*a, **kw):
        raise ConnectionError("HL unreachable")
    hl.max_builder_fee = _boom
    r = client.get("/api/onboard/status")
    assert r.status_code == 502


def test_payload_builder_fee_hl_timeout_502(tmp_path):
    """同上，TimeoutError 版本，走 payload/approve-builder-fee 端點的
    builder 餘額門檻查詢路徑。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    login(client)

    def _boom(*a, **kw):
        raise TimeoutError("HL timeout")
    hl.get_account_value = _boom
    r = client.post("/api/onboard/payload/approve-builder-fee", json={"chain_id": 42161})
    assert r.status_code == 502


def test_desync_self_heals_via_address_op(tmp_path):
    """keysvc 有 key 但 DB 無地址（DB 遺失/回應遺失殘局）→ 唯讀 address op 自癒回填
    （設計定案 12），照常 200，回應帶 recovered=true 供觀測。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = "f" + wallet.address.lower()[2:]
    keysvc.generated[account_id] = "0x" + "EE" * 20  # 預塞：keystore 有、DB 無
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    assert r.json()["recovered"] is True
    assert r.json()["agent_address"] == "0x" + "ee" * 20   # normalize 後回填
    assert store.get_agent_address(account_id) == "0x" + "ee" * 20  # DB 已回填


def test_desync_and_address_also_fails_409(tmp_path):
    """自癒也失敗（address op 打不通）才 409，訊息明確要求人工介入。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = "f" + wallet.address.lower()[2:]
    keysvc.generated[account_id] = "0x" + "ee" * 20
    keysvc.address_fail = ConnectionRefusedError("keysvc down")
    r = client.post("/api/onboard/agent")
    assert r.status_code == 409
    # 失敗義的 409：code 與「已有 agent」的成功義 409 不同，前端才分得出兩者
    # （2026-08-29 M3 round2 Task 3）。
    assert r.json()["detail"]["code"] == "agent_conflict"
    assert "無法自動復原" in r.json()["detail"]["message"]
    assert store.get_agent_address(account_id) is None  # 未寫入半套狀態


def _make_ready(hl, wallet_addr: str, agent: str):
    hl.max_fees[(wallet_addr.lower(), BUILDER.lower())] = 100
    hl.agents[wallet_addr.lower()] = [agent]
    hl.account_values[wallet_addr.lower()] = Decimal("150")


def test_status_progresses_to_ready(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    s0 = client.get("/api/onboard/status").json()
    assert s0["agent_generated"] is False and s0["state"] == "IN_PROGRESS"
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    s1 = client.get("/api/onboard/status").json()
    assert s1["agent_generated"] and s1["builder_fee_approved"]
    assert s1["agent_approved"] and s1["funded"]
    assert s1["state"] == "READY"
    assert s1["agent_address"] == agent


def test_status_funding_below_floor_not_ready(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    hl.account_values[wallet.address.lower()] = Decimal("99")  # < 100 USDC 門檻
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is False and s["state"] == "IN_PROGRESS"


# ── ⭐ 入金判定值外流（2026-07-30）：`funded=False` 單獨出現時客戶無法自我診斷，
# 而最常見的原因是「錢在 spot、perp 是 0」。判定用的 perp 淨值與門檻必須一起回，
# 且必須是**同一次讀取的同一個值**（工程原則 1）——否則畫面寫 105、系統說不足。


def test_status_exposes_the_perp_value_that_decided_funded(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    hl.account_values[wallet.address.lower()] = Decimal("99.25")
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is False
    # 顯示值 == 判定值（不是另一個欄位、不是另一次讀取）
    assert s["perp_account_value"] == "99.25"
    assert s["min_deposit"] == str(cfg.min_user_deposit)
    assert s["deposit_shortfall"] == "0.75"


def test_status_shortfall_is_zero_once_funded(tmp_path):
    """已達標 ⇒ 差額為 "0"（不是負數——前端會把它當成一句話印出來）。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)   # 150 USDC
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is True
    assert s["perp_account_value"] == "150"
    assert s["deposit_shortfall"] == "0"


def test_perp_value_read_failure_is_502_not_a_zero_balance(tmp_path):
    """⭐ 讀不到錢 ≠ 錢不存在。餘額查詢失敗必須讓整個端點 502（既有行為），
    **不得**被吞成 0 而讓 funded=False——那會把「我們查不到」偽裝成「你沒錢」，
    客戶照著畫面再存一次錢也不會有任何改變。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    login(client)

    def _boom(*a, **kw):
        raise ConnectionError("HL unreachable")
    hl.get_account_value = _boom
    r = client.get("/api/onboard/status")
    assert r.status_code == 502
    assert "perp_account_value" not in r.text


def test_status_isolated_between_users(tmp_path):
    """紅線 3：account 由 session 衍生——另一個使用者看不到、也影響不了你的進度。"""
    app, *_ = make_app(tmp_path)
    c1, c2 = _client(app), _client(app)
    login(c1)
    login(c2)
    c1.post("/api/onboard/agent")
    assert c2.get("/api/onboard/status").json()["agent_generated"] is False


# ── ⭐ spot「卡住資金」偵測（2026-07-19 入金體驗）──────────────────────────
# 我方只鏡像 perp。客戶從 CEX 提幣或走第三方橋入金時錢會落在 spot，perp 仍是 0 →
# funded 判 False，而畫面上只寫「尚未入金」。劃轉是 user-signed action，我方結構上
# 無法代做（需要主鑰，違反非託管不變量）——所以只能偵測＋提示。


def test_spot_balance_surfaces_transfer_hint(tmp_path):
    """spot 有錢 → 回提示資訊，且**足以讓前端寫出金額**。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.spot_usdc[wallet.address.lower()] = Decimal("250.5")
    s = client.get("/api/onboard/status").json()
    assert s["spot_stranded"] is not None
    assert s["spot_stranded"]["usdc"] == "250.5"
    assert s["spot_stranded"]["action_required"] == "manual_transfer_spot_to_perp"
    assert "250.5" in s["spot_stranded"]["note"]


def test_spot_hint_never_offers_to_transfer_on_the_customers_behalf(tmp_path):
    """⭐ 非託管不變量（紅線 3）：提示只能說明，不得提供或暗示代客戶劃轉的能力。
    劃轉需要主鑰簽章，我方結構上不持有——承諾一個做不到的動作比不提示更糟。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.spot_usdc[wallet.address.lower()] = Decimal("250.5")
    hint = client.get("/api/onboard/status").json()["spot_stranded"]
    # 沒有任何代簽 payload／劃轉入口的欄位
    assert set(hint) == {"usdc", "threshold", "action_required", "note"}
    for k in ("typed_data", "transfer_payload", "transfer_url", "auto_transfer"):
        assert k not in hint
    # 文案必須明說「我們無法代為操作」
    assert "無法" in hint["note"] and "主鑰" in hint["note"]


def test_no_spot_balance_no_hint(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.get("/api/onboard/status").json()["spot_stranded"] is None


def test_dust_spot_balance_does_not_nag(tmp_path):
    """塵埃餘額不提示：每次登入都跳一個處理不掉的提示，會讓客戶學會忽略所有提示。
    邊界：門檻值本身要提示（>= 而非 >），否則剛好 1 USDC 的人永遠看不到說明。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    assert cfg.spot_stranded_min_usdc == Decimal("1")
    hl.spot_usdc[wallet.address.lower()] = Decimal("0.9999")
    assert client.get("/api/onboard/status").json()["spot_stranded"] is None
    hl.spot_usdc[wallet.address.lower()] = Decimal("1")
    assert client.get("/api/onboard/status").json()["spot_stranded"] is not None


def test_funded_customer_never_sees_stranded_spot_warning(tmp_path):
    """⭐ 已入金 ＋ spot 留零頭 → **不提示**（變異測試靶：拿掉 funded 閘必轉紅）。

    本提示的適用範圍只有一種情形——「perp 還沒錢，而錢在 spot」。perp 已達
    min_user_deposit 的客戶正在正常跟單，對他顯示「你的錢卡在 spot…請劃轉到 perp」
    是純粹的困惑與客服成本，而本函式的 docstring 自己就把「假警報比沒提示糟」
    定為失效方向（查詢失敗時 fail-silent 即據此）。先前 fail-silent 只擋住了
    「查詢失敗」那一路，成功查到時沒有任何 funded 閘。

    觸發面不是邊角：門檻僅 1 USDC，劃轉留零頭就會中。
    """
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.account_values[wallet.address.lower()] = Decimal("5000")   # perp 足額
    hl.spot_usdc[wallet.address.lower()] = Decimal("3")           # spot 只剩零頭
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is True
    assert s["spot_stranded"] is None, "已入金的客戶不得看到『錢卡在 spot』假警報"


def test_funded_customer_with_large_spot_balance_still_no_warning(tmp_path):
    """已入金但同時持有大額 spot（拿去做別的用途）→ 一樣不提示。

    這一條說明為什麼修法是 funded 閘而不是「把門檻改成相對量」：相對門檻只是把
    假警報的邊界往上挪，這個客戶照樣中。產品本來就預期客戶有 spot 餘額與操作。
    """
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.account_values[wallet.address.lower()] = Decimal("5000")
    hl.spot_usdc[wallet.address.lower()] = Decimal("20000")
    assert client.get("/api/onboard/status").json()["spot_stranded"] is None


def test_unfunded_customer_with_spot_balance_still_gets_the_hint(tmp_path):
    """既有行為不變：未入金 ＋ spot 有錢 → 照常提示（這才是本提示存在的理由）。

    與上面兩條成對——funded 閘必須只關掉假警報，不得把真警報一起關掉。
    """
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.account_values[wallet.address.lower()] = Decimal("0")      # perp 空的
    hl.spot_usdc[wallet.address.lower()] = Decimal("250.5")
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is False
    assert s["spot_stranded"] is not None
    assert s["spot_stranded"]["usdc"] == "250.5"


def test_spot_balance_not_queried_at_all_once_funded(tmp_path):
    """funded 閘排在餘額查詢**之前**：已入金就不該再打一次 spot 查詢。

    不只是省一次呼叫——閘門若排在查詢之後，一個已入金客戶的 spot 查詢失敗
    仍會走進 except 分支，把「不適用」與「查不到」混成同一件事。
    """
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.account_values[wallet.address.lower()] = Decimal("5000")

    calls: list[str] = []
    real = hl.spot_usdc_balance

    def _counting(address):
        calls.append(address)
        return real(address)

    hl.spot_usdc_balance = _counting
    assert client.get("/api/onboard/status").json()["spot_stranded"] is None
    assert calls == [], "已入金仍查了 spot 餘額 → funded 閘排在查詢之後"


def test_spot_query_failure_shows_no_hint_and_does_not_break_status(tmp_path):
    """⭐ 失效方向刻意與 builder 合規監控**相反**：查不到 → 不提示。
    對一個已經劃轉好、正在正常跟單的客戶顯示「你的錢卡住了」是純粹的困惑；
    漏掉一次提示的代價只是他下次載入才看到。假警報比沒提示糟。

    ⚠️ 同時釘住：這個輔助查詢的失敗**不得**把整個 onboarding 狀態頁打成 502
    （本 app 有 ConnectionError/TimeoutError → 502 的全域 handler）。
    """
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    for exc in (ConnectionError("spot down"), TimeoutError("slow"),
                ValueError("schema drift")):
        hl.spot_error[wallet.address.lower()] = exc
        r = client.get("/api/onboard/status")
        assert r.status_code == 200, f"{exc!r} 打掉了狀態頁: {r.text}"
        assert r.json()["spot_stranded"] is None


def test_spot_hint_is_isolated_between_users(tmp_path):
    """⭐ 只回自己的：address 出自 session，端點無 account 參數（沿既有結構）。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    c1, c2 = _client(app), _client(app)
    w1 = login(c1)
    login(c2)
    hl.spot_usdc[w1.address.lower()] = Decimal("500")
    assert c1.get("/api/onboard/status").json()["spot_stranded"] is not None
    assert c2.get("/api/onboard/status").json()["spot_stranded"] is None
