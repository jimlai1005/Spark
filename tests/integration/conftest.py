"""tests/integration/conftest.py
testnet 拋棄式錢包 harness 的 session fixtures（plan T1）。

沒有水龍頭錢包時，任何依賴 `faucet`/`customer`/`leader`/`app` 的測試會經
`harness.faucet_wallet()` 的 `pytest.skip` 自動略過（原因文字含「水龍頭」）——
不需要在這裡另外判斷一次，skip 會沿依賴鏈往上傳播到每個使用到的測試。
"""
import os
import warnings
from dataclasses import dataclass
from decimal import Decimal

import pytest

from tests.integration.harness import (
    KeysvcThread,
    Wallet,
    faucet_wallet,
    flatten,
    fund,
    make_real_app,
    new_wallet,
    sweep,
)


@dataclass
class _Wallets:
    faucet: Wallet
    customer: Wallet
    leader: Wallet


@pytest.fixture(scope="session")
def _wallets():
    faucet = faucet_wallet()  # 缺 Keychain 項目 → pytest.skip，往上傳播給所有依賴者
    customer = new_wallet()
    leader = new_wallet()
    fund(faucet, customer.address, Decimal("150"))
    fund(faucet, leader.address, Decimal("120"))
    yield _Wallets(faucet=faucet, customer=customer, leader=leader)
    # session 收尾順序（plan T1 硬性規定）：flatten(leader) → sweep(customer) →
    # sweep(leader)。每一步 best-effort（harness 函式內部已各自 warn 不拋），
    # 這裡再包一層 try/except 純粹防呆——即使 harness 函式簽名日後改成會拋，
    # 收尾流程也不該因為前一步失敗就跳過後面的清理。
    try:
        flatten(leader)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"session teardown: flatten(leader) 失敗（忽略）: {e}")
    try:
        sweep(customer, faucet)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"session teardown: sweep(customer) 失敗（忽略）: {e}")
    try:
        sweep(leader, faucet)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"session teardown: sweep(leader) 失敗（忽略）: {e}")


@pytest.fixture(scope="session")
def faucet(_wallets: _Wallets) -> Wallet:
    return _wallets.faucet


@pytest.fixture(scope="session")
def customer(_wallets: _Wallets) -> Wallet:
    return _wallets.customer


@pytest.fixture(scope="session")
def leader(_wallets: _Wallets) -> Wallet:
    return _wallets.leader


@pytest.fixture(scope="session")
def builder_address() -> str:
    return os.environ.get("SPARK_BUILDER_ADDR", "0xbAC652a5fb611c1bdc3b9d244cc7e0cc03123662")


@pytest.fixture(scope="session")
def keysvc(tmp_path_factory):
    thread = KeysvcThread(tmp_path_factory.mktemp("keysvc"))
    thread.start()
    yield thread
    thread.stop()


@pytest.fixture(scope="session")
def app(tmp_path_factory, builder_address: str, keysvc: KeysvcThread, leader: Wallet):
    """真 keysvc + 真 HLGateway 組出來的 public API app；白名單只含拋棄式 leader
    （D3：可控、可反手、可平倉，見 plan §4）。"""
    tmp_path = tmp_path_factory.mktemp("app")
    leaders = [{"address": leader.address, "name": "harness-leader",
               "description": "T2 E2E 用拋棄式 leader", "enabled": True,
               "accepting_new": True}]
    return make_real_app(tmp_path, builder=builder_address, leaders=leaders,
                         keysvc_sock=keysvc.sock_path)
