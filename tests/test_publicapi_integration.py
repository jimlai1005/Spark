"""tests/test_publicapi_integration.py
離線端到端：SIWE → keysvc（真 socket + 真 keystore）→ payload → 瀏覽器簽名模擬
（簽名不送後端；上鏈以 FakeHL 狀態翻轉模擬）→ verify → pending → activate。
非託管不變量：主鑰/agent 私鑰/EIP-712 簽名不出現在任何 HTTP 回應、DB、
pending/manifest 檔；API 表面結構上無收簽名欄位。"""
import inspect
import json
import socket
import threading
import uuid
from decimal import Decimal
from pathlib import Path

from eth_account.messages import encode_typed_data
from fastapi.testclient import TestClient

import pytest

from scripts.filet_activate import activate
from spark.filet.followers import load_followers
from spark.keystore.envfile import EnvFileKeyStore
from spark.keysvc.client import KeysvcClient
from spark.keysvc.server import serve_forever
from spark.publicapi.app import create_app
from spark.publicapi.pending import load_pending
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import BUILDER, FakeHL, login, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉（keysvc 慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _connect_when_ready(sock_path):
    """沿 tests/test_keysvc_client.py：重試 connect 避開 bind/listen 窗口 race。"""
    import time
    for _ in range(100):
        c = _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(str(sock_path))
            c.close()
            return
        except (FileNotFoundError, ConnectionRefusedError):
            c.close()
            time.sleep(0.02)
    raise RuntimeError("keysvc 測試: server 未就緒")


def test_full_onboarding_flow_offline(tmp_path):
    # --- 起真 key-service（授權器全放行；peercred 已在 keysvc 測試驗）---
    sock_path = Path(f"/tmp/spark-publicapi-e2e-{uuid.uuid4().hex[:8]}.sock")
    keys_dir = tmp_path / "keys"
    ks = EnvFileKeyStore(keys_dir)
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    _connect_when_ready(sock_path)
    try:
        cfg = make_cfg(tmp_path, keysvc_sock=str(sock_path))
        store = ApiStore(cfg.db_path)
        hl = FakeHL()
        app = create_app(cfg, store, KeysvcClient(str(sock_path)), hl)
        client = TestClient(app, base_url="https://testserver")
        captured: list[str] = []          # 所有 HTTP 回應原文（非託管掃描用）

        def post(path, **kw):
            r = client.post(path, **kw)
            captured.append(r.text)
            return r

        # 1. SIWE 登入（真簽名）
        wallet = login(client)
        account_id = "f" + wallet.address.lower()[2:]
        # 2. 生成 agent（經真 keysvc；私鑰落 keystore、API 只見地址）
        r = post("/api/onboard/agent")
        assert r.status_code == 200
        agent_addr = r.json()["agent_address"]
        assert ks.get_agent_signer(account_id).address.lower() == agent_addr
        # 3. 產兩個 payload → 模擬瀏覽器真簽 typed data（證明可簽）。
        #    簽名「不」送後端——前端直送 HL /exchange（設計定案 1，CORS 已實測）；
        #    本測試以步驟 4 的 FakeHL 狀態翻轉模擬「授權已上鏈」。
        hl.account_values[BUILDER.lower()] = Decimal("150")
        browser_sigs = []
        for kind in ("approve-agent", "approve-builder-fee"):
            r = post(f"/api/onboard/payload/{kind}", json={"chain_id": 42161})
            assert r.status_code == 200
            td = r.json()["typed_data"]
            sm = wallet.sign_message(encode_typed_data(full_message=td))
            browser_sigs.append(hex(sm.r).removeprefix("0x"))
        # 4. 模擬鏈上生效（前端直送的結果）→ verify → pending
        hl.max_fees[(wallet.address.lower(), BUILDER.lower())] = 100
        hl.agents[wallet.address.lower()] = [agent_addr]
        hl.account_values[wallet.address.lower()] = Decimal("150")
        r = post("/api/onboard/verify")
        assert r.json()["state"] == "READY"
        assert len(load_pending(cfg.pending_path)) == 1
        # 5. 人工 activate → 引擎視角：manifest 讀得到、keystore 有 key
        manifest = tmp_path / "followers.json"
        activate(account_id, cfg.pending_path, str(manifest), BUILDER, start=False)
        refs = load_followers(manifest)
        assert refs[0].account_id == account_id
        assert refs[0].user_address == wallet.address.lower()
        assert ks.get_agent_signer(account_id).address.lower() == agent_addr
        # --- 非託管不變量掃描 ---
        master_pk = wallet.key.hex().removeprefix("0x")
        agent_pk = (keys_dir / account_id / "agent.key").read_text().strip() \
            .removeprefix("0x")
        blobs = {
            "http 回應": "\n".join(captured),
            "sqlite DB": Path(cfg.db_path).read_bytes().hex()
                          + Path(cfg.db_path).read_bytes().decode("latin1"),
            "pending/manifest": json.dumps(load_pending(cfg.pending_path))
                                 + manifest.read_text(),
        }
        for name, blob in blobs.items():
            assert master_pk not in blob, f"主鑰出現在 {name}"
            assert agent_pk not in blob, f"agent 私鑰出現在 {name}"
            # EIP-712 授權簽名從未進後端：任何簽名值不得出現在伺服器側任何地方
            for sig_r in browser_sigs:
                assert sig_r not in blob, f"授權簽名出現在 {name}"
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_desync_self_heal_contract_with_real_keysvc(tmp_path):
    """opus 審 I2 要求的真 keysvc desync 契約測試：API DB 遺失後重呼 /agent，
    經 keysvc 唯讀 address op 自癒回填，地址與 keystore 落檔一致（設計定案 12）。"""
    sock_path = Path(f"/tmp/spark-publicapi-heal-{uuid.uuid4().hex[:8]}.sock")
    keys_dir = tmp_path / "keys"
    ks = EnvFileKeyStore(keys_dir)
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    _connect_when_ready(sock_path)
    try:
        cfg = make_cfg(tmp_path, keysvc_sock=str(sock_path))
        store = ApiStore(cfg.db_path)
        app = create_app(cfg, store, KeysvcClient(str(sock_path)), FakeHL())
        client = TestClient(app, base_url="https://testserver")
        wallet = login(client)
        account_id = "f" + wallet.address.lower()[2:]
        first = client.post("/api/onboard/agent").json()["agent_address"]
        # 模擬 API DB 遺失（onboarding 表清空；keystore 的 key 檔仍在）
        with store._lock, store._db:  # noqa: SLF001 — 測試直接清表模擬災難情境
            store._db.execute("DELETE FROM onboarding")
        r = client.post("/api/onboard/agent")
        assert r.status_code == 200
        assert r.json().get("recovered") is True
        assert r.json()["agent_address"] == first          # 同一把 key、同一地址
        assert ks.get_agent_signer(account_id).address.lower() == first  # 與 keystore 一致
        # 回填已持久落 DB（不是每次靠自癒重演）：直接查 store 本身，不透過 API 再觸發自癒。
        assert store.get_agent_address(account_id) == first
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_api_surface_has_no_rsv_signature_fields(tmp_path):
    """紅線 5 的結構性證明：整個 API 的 request model 沒有任何 r/s/v 欄位——
    後端經手 EIP-712 授權簽名在型別層就不可能。唯一的簽名欄位是 SIWE 登入的
    `signature`（EIP-191 身分驗證，性質不同、刻意保留）。"""
    from pydantic import BaseModel

    import spark.publicapi.app as app_mod
    models = [obj for _, obj in inspect.getmembers(app_mod)
              if inspect.isclass(obj) and issubclass(obj, BaseModel)
              and obj is not BaseModel]
    assert models, "app 模組應至少有一個 request model（防呆：抓錯模組會誤判通過）"
    for model in models:
        assert not {"r", "s", "v"} & set(model.model_fields), (
            f"{model.__name__} 含 r/s/v 欄位——後端不得收 EIP-712 簽名（紅線 5）")


def test_cross_user_cannot_touch_others_onboarding(tmp_path):
    """紅線 3 整條驗：B 登入後的所有 onboarding 動作只落在 B 自己的 account。"""
    from tests.publicapi_helpers import make_app
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    ca = TestClient(app, base_url="https://testserver")
    cb = TestClient(app, base_url="https://testserver")
    wa = login(ca)
    login(cb)
    agent_a = ca.post("/api/onboard/agent").json()["agent_address"]
    # B 生成的是 B 自己的 agent，A 的不受影響
    agent_b = cb.post("/api/onboard/agent").json()["agent_address"]
    assert agent_a != agent_b
    acct_a = "f" + wa.address.lower()[2:]
    assert store.get_agent_address(acct_a) == agent_a
