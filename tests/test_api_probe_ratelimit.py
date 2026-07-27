"""tests/test_api_probe_ratelimit.py — 自訂 leader 探測面的 per-session rate limit
（2026-07-27）。preview 與 select/message 兩個端點：每個 session 位址每
PROBE_RATELIMIT_WINDOW_S 內最多 PROBE_RATELIMIT_MAX 次，超限 429；窗過後恢復；
不同 session 各自獨立；POST select **不**受此限（簽章 gated）。

時鐘走注入的假 now_fn（sliding window 才可測，不 call time.time）。全離線
（autouse socket-ban；本檔另放行 loopback 供 TestClient）。
"""
import json
import socket
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from spark.publicapi.app import PROBE_RATELIMIT_MAX, PROBE_RATELIMIT_WINDOW_S
from tests.publicapi_helpers import make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網（沿 test_api_leaders）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_CURATED = "0x" + "a1" * 20    # 精選可選（POST 走既有精選流程，供不限流的錨定）
_CUSTOM = "0x" + "e5" * 20     # 清單外自訂位址（preview 對 dead 位址亦放行）
_LEADERS = [{"address": _CURATED, "name": "Alpha"}]


class Clock:
    """可變假時鐘：測試推進 .t 模擬時間流逝（滑動窗逐出舊時間戳）。

    起點釘在**真實 wall time**，不是任意 epoch：換 leader 待簽原文的 issued_at
    由 leaders_select_message 以 `datetime.now(timezone.utc)` 產生（真實時鐘），
    而簽章驗證的新鮮度以 now_fn 為準——兩者相差太遠會被判 expired。用真時起點
    讓兩者同源，限流測試推進 60s 仍在新鮮窗內。
    """

    def __init__(self, t: float | None = None):
        self.t = time.time() if t is None else t

    def __call__(self) -> float:
        return self.t


def _make(tmp_path):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": _LEADERS}))
    cfg = make_cfg(tmp_path, leaders_path=str(p))
    clock = Clock()
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, now_fn=clock)
    client = TestClient(app, base_url="https://testserver")
    client._spark_app = app          # introspection seam（Finding 2 reaper 測試用）
    return client, clock


def _login(client, wallet=None):
    wallet = wallet or Account.create()
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return wallet


def _preview(client, addr=_CUSTOM):
    return client.get("/api/leaders/preview", params={"leader_address": addr})


def test_eleventh_preview_within_window_is_429(tmp_path):
    """同 session 前 10 次 preview 放行，第 11 次（窗內）→ 429。"""
    c, clock = _make(tmp_path)
    _login(c)
    for i in range(PROBE_RATELIMIT_MAX):
        assert _preview(c).status_code == 200, i
    assert _preview(c).status_code == 429


def test_window_advance_restores_access(tmp_path):
    """注入 now_fn 前進一個窗長 → 舊時間戳全被逐出，恢復放行。"""
    c, clock = _make(tmp_path)
    _login(c)
    for _ in range(PROBE_RATELIMIT_MAX):
        assert _preview(c).status_code == 200
    assert _preview(c).status_code == 429
    clock.t += PROBE_RATELIMIT_WINDOW_S      # 滑動窗逐出全部舊時間戳
    assert _preview(c).status_code == 200


def test_sessions_are_counted_independently(tmp_path):
    """不同 session（不同登入位址）各自獨立計數：A 打滿 429，B 仍放行。"""
    c, clock = _make(tmp_path)
    _login(c)                                # session A
    for _ in range(PROBE_RATELIMIT_MAX):
        assert _preview(c).status_code == 200
    assert _preview(c).status_code == 429
    _login(c)                                # session B（覆蓋 cookie）→ 自己的計數
    assert _preview(c).status_code == 200


def test_message_endpoint_shares_the_same_limit(tmp_path):
    """select/message 端點同樣受限（與 preview 共用同一個 per-session 計數器）。"""
    c, clock = _make(tmp_path)
    _login(c)
    for _ in range(PROBE_RATELIMIT_MAX):
        r = c.get("/api/leaders/select/message",
                  params={"leader_address": _CUSTOM})
        assert r.status_code == 200, r.text
    r = c.get("/api/leaders/select/message", params={"leader_address": _CUSTOM})
    assert r.status_code == 429


def test_expired_session_keys_are_reaped(tmp_path):
    """⭐ Finding 2：_probe_hits dict 不得隨「輪替 session 位址各 probe 一次」無界
    成長。多個位址各 probe 一次 → dict 有多個 key；時鐘前進一個窗長使全部過期 →
    下一次任一 probe 觸發 reaper，過期 key 被剔除，dict 大小回落到「近窗內 probe
    過的位址數」。"""
    c, clock = _make(tmp_path)
    hits = c._spark_app.state.probe_ratelimit_hits
    for _ in range(5):
        _login(c)                             # 新 wallet → 新 session 位址
        assert _preview(c).status_code == 200
    assert len(hits) == 5                      # 五個位址各留一筆
    clock.t += PROBE_RATELIMIT_WINDOW_S + 1    # 全部時間戳過期
    _login(c)                                  # 第六個位址
    assert _preview(c).status_code == 200      # 觸發 reaper
    assert len(hits) == 1                       # 過期的 5 個 key 已被剔除


def test_post_select_is_not_rate_limited(tmp_path):
    """⭐ POST select **不**受 probe 限流：即使 probe 計數已打滿 429，對精選可選
    leader 的簽章 POST 仍照常受理（簽章 gated、濫用成本高，不套此限）。"""
    c, clock = _make(tmp_path)
    w = _login(c)
    # 先取一份精選 leader 的待簽原文（算 1 次 probe），供稍後 POST 簽名。
    r0 = c.get("/api/leaders/select/message", params={"leader_address": _CURATED})
    assert r0.status_code == 200, r0.text
    m = r0.json()
    # 用滿剩餘額度（9 次 preview → 累計 10），下一次 probe 必 429。
    for _ in range(PROBE_RATELIMIT_MAX - 1):
        assert _preview(c).status_code == 200
    assert _preview(c).status_code == 429
    # probe 已滿，但簽章 POST 不共享此限 → 200。
    sig = w.sign_message(encode_defunct(text=m["message"])).signature.hex()
    body = {"account_id": m["account_id"], "leader_address": m["leader_address"],
            "nonce": m["nonce"], "issued_at": m["issued_at"],
            "signature": sig, "message": m["message"]}
    r = c.post("/api/leaders/select", json=body)
    assert r.status_code == 200, r.text
