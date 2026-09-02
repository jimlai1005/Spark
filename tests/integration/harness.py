"""tests/integration/harness.py
Testnet 拋棄式錢包 harness（golive-regression plan T1，見
docs/superpowers/plans/2026-09-02-golive-regression.md §3 T1）。

**只在這裡碰真實 Hyperliquid testnet**——不進 src/（R5：ExchangeAdapter 不得新增
transfer/withdraw；資金搬運直接用 SDK 的 `Exchange.usd_transfer`，僅限本檔）。

R1（零主網寫入）結構性保證：本模組載入時即斷言 `TESTNET_URL` 含 "testnet"；
所有 `Exchange`/`Info`/`HLGateway` 建構只用這個模組級常數，`run_engine_once`
也強制覆寫 `SPARK_NETWORK=testnet`（呼叫端傳入的 extra_env 不得覆蓋這條）。

R4（私鑰不外洩）：`Wallet.__repr__` 只印位址；私鑰只在 `LocalAccount`/區域變數內
流動，唯一落地點是 `seed-faucet` CLI 寫入 macOS Keychain（`keyring.set_password`），
從不印出、不進例外訊息、不進任何 log。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Callable

import httpx
import keyring
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils import to_hex
from fastapi.testclient import TestClient
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants as hl_constants

from spark.keysvc.client import KeysvcClient
from spark.keysvc.server import serve_forever
from spark.keystore.envfile import EnvFileKeyStore
from spark.publicapi.app import create_app
from spark.publicapi.hl import HLGateway
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import make_cfg

# ---------------------------------------------------------------------------
# R1：零主網寫入的結構性閘門。
# ---------------------------------------------------------------------------


def assert_testnet(url: str) -> None:
    """R1 硬閘門：任何要建構 Exchange/Info/HLGateway 的 URL 都先過這裡。"""
    if "testnet" not in url:
        raise RuntimeError(f"R1 違規：拒絕非 testnet URL（零主網寫入）: {url!r}")


TESTNET_URL: str = hl_constants.TESTNET_API_URL
assert_testnet(TESTNET_URL)


# ---------------------------------------------------------------------------
# Wallet：私鑰只活在 LocalAccount 物件內，repr 只印位址。
# ---------------------------------------------------------------------------


@dataclass(repr=False)
class Wallet:
    account: LocalAccount

    @property
    def address(self) -> str:
        return self.account.address

    def __repr__(self) -> str:  # R4：絕不印私鑰
        return f"<Wallet {self.address}>"

    def sign_text(self, msg: str) -> str:
        """personal_sign，與 tests/publicapi_helpers.login 同法。"""
        return self.account.sign_message(encode_defunct(text=msg)).signature.hex()

    def sign_typed(self, typed_data: dict) -> dict:
        """EIP-712 簽名，回傳格式與 hyperliquid.utils.signing.sign_inner 一致
        （{"r": hex, "s": hex, "v": int}），供 submit_user_signed 直送 HL。"""
        signed = self.account.sign_typed_data(full_message=typed_data)
        return {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}


def new_wallet() -> Wallet:
    """全新拋棄式錢包（Account.create()，os.urandom 亂數；私鑰不落檔）。"""
    return Wallet(Account.create())


def faucet_wallet() -> Wallet:
    """從 Keychain 讀水龍頭主鑰。讀不到 → pytest.skip（缺水龍頭是合法的本機狀態，
    不是測試失敗；exit code 語意見 tests/integration/conftest.py）。"""
    import pytest  # 延後 import：harness 本身可被 CLI 呼叫，不強制依賴 pytest

    name = os.environ.get("FILET_TESTNET_FAUCET_ACCOUNT", "filet-testnet-faucet")
    pk = keyring.get_password("spark", f"{name}:main")
    if pk is None:
        pytest.skip(f"缺 testnet 水龍頭錢包（Keychain spark/{name}:main），見 "
                    "docs/superpowers/plans/2026-09-02-golive-regression.md §2 Q1")
    return Wallet(Account.from_key(pk))


# ---------------------------------------------------------------------------
# 資金搬運：直接用 SDK Exchange（R5——不進 src/）。
# ---------------------------------------------------------------------------


def _faucet_topup_perp(faucet: Wallet, need: Decimal, *, timeout_s: float = 30.0) -> None:
    """若水龍頭 **perp** 保證金不足 `need`，先從 spot 做 usdClassTransfer 補足。

    `Exchange.usd_transfer`（usdSend）是從送款方的 **perp** 保證金送出，但水龍頭的
    資金常態停在 spot（見 `seed-faucet` 播種來源／`faucet-status` 的觀測：2026-09-02
    親查 perp accountValue=0、spot USDC=300）。這裡先把缺口從 spot 轉去 perp
    （水龍頭自己的帳戶內部搬移，`usd_class_transfer` 不觸及任何第三方），
    再由呼叫端的 `usd_transfer` 送出。R5：仍是 harness 內部直接用 SDK，不進 src/。
    """
    info = Info(TESTNET_URL, skip_ws=True)
    exch = Exchange(faucet.account, TESTNET_URL)
    state = info.user_state(faucet.address)
    perp_avail = Decimal(str(state.get("withdrawable", "0")))
    if perp_avail >= need:
        return
    shortfall = need - perp_avail
    spot_avail = _spot_usdc(faucet.address)
    if spot_avail < shortfall:
        raise RuntimeError(
            f"水龍頭資金不足：需要 perp {need}（現有 {perp_avail}），缺口 {shortfall}"
            f"，但水龍頭 spot 只有 {spot_avail}")
    exch.usd_class_transfer(float(shortfall), to_perp=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = info.user_state(faucet.address)
        if Decimal(str(state.get("withdrawable", "0"))) >= need:
            return
        time.sleep(2)
    raise TimeoutError(f"水龍頭 spot→perp 補款未在 {timeout_s}s 內生效"
                       f"（需要 {need}，缺口 {shortfall}）")


def fund(faucet: Wallet, dest: str, usdc: Decimal, *, timeout_s: float = 60.0) -> None:
    """水龍頭 → dest 的 testnet usdSend，輪詢確認到帳（≥ usdc×0.99）。

    `usd_transfer` 從水龍頭的 **perp** 餘額送出（見 `_faucet_topup_perp`）——先確保
    perp 保證金到位，再送款，避免水龍頭資金常態停在 spot 時整個 fund() 靜默失敗
    （HL 對保證金不足的 usdSend 通常回 err，而非拋例外，故不先補足會產生誤導性的
    TimeoutError，看起來像「到帳延遲」而非「來源沒錢」）。
    """
    _faucet_topup_perp(faucet, usdc)
    exch = Exchange(faucet.account, TESTNET_URL)
    info = Info(TESTNET_URL, skip_ws=True)
    exch.usd_transfer(float(usdc), dest)
    target = usdc * Decimal("0.99")
    deadline = time.monotonic() + timeout_s
    last_seen = Decimal("0")
    while time.monotonic() < deadline:
        try:
            last_seen = Decimal(str(info.user_state(dest)["marginSummary"]["accountValue"]))
        except Exception:  # noqa: BLE001 — 查詢失敗視為尚未到帳，繼續輪詢
            last_seen = Decimal("0")
        if last_seen >= target:
            return
        time.sleep(2)
    raise TimeoutError(
        f"fund: {dest} 未在 {timeout_s}s 內收到 ≥{target} USDC（最後查得 {last_seen}）")


def sweep(wallet: Wallet, faucet: Wallet) -> None:
    """把 wallet 的可提餘額掃回水龍頭。best-effort：任何失敗只 warn，不拋——
    這是測試收尾，不該讓 teardown 的清理失敗掩蓋原本的測試結果。"""
    info = Info(TESTNET_URL, skip_ws=True)
    try:
        state = info.user_state(wallet.address)
        withdrawable = Decimal(str(state.get("withdrawable", "0")))
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"sweep: 讀 {wallet.address} withdrawable 失敗（忽略）: {e}")
        return
    if withdrawable <= 1:
        return
    try:
        Exchange(wallet.account, TESTNET_URL).usd_transfer(float(withdrawable), faucet.address)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"sweep: {wallet.address} usd_transfer 回水龍頭失敗（忽略）: {e}")


def flatten(wallet: Wallet) -> None:
    """對 wallet 名下每個非零部位用 market_close 平倉（leader teardown 用）。
    best-effort：單一 coin 平倉失敗只 warn，不影響其餘 coin 的平倉嘗試。"""
    info = Info(TESTNET_URL, skip_ws=True)
    exch = Exchange(wallet.account, TESTNET_URL)
    try:
        positions = info.user_state(wallet.address).get("assetPositions", [])
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"flatten: 讀 {wallet.address} 部位失敗（忽略）: {e}")
        return
    for p in positions:
        item = p.get("position", {})
        coin = item.get("coin")
        try:
            szi = Decimal(str(item.get("szi", "0")))
        except Exception:  # noqa: BLE001
            continue
        if not coin or szi == 0:
            continue
        try:
            exch.market_close(coin)
        except Exception as e:  # noqa: BLE001
            warnings.warn(f"flatten: {wallet.address} 平倉 {coin} 失敗（忽略）: {e}")


def leader_trade(wallet: Wallet, coin: str, is_buy: bool, notional_usd: Decimal):
    """leader 主鑰下市價單（notional ≥ 12 USD 避開 $10 最小名目門檻）。
    size 依 meta() 的 szDecimals 捨入（避免下單被 tick 規則拒絕）。"""
    if notional_usd < Decimal("12"):
        raise ValueError(f"leader_trade: notional_usd={notional_usd} 過低，可能觸發 $10 門檻")
    info = Info(TESTNET_URL, skip_ws=True)
    exch = Exchange(wallet.account, TESTNET_URL)
    mid = Decimal(str(info.all_mids()[coin]))
    meta = info.meta()
    sz_decimals = next(a["szDecimals"] for a in meta["universe"] if a["name"] == coin)
    quantum = Decimal(1).scaleb(-sz_decimals)
    size = (notional_usd / mid).quantize(quantum, rounding=ROUND_DOWN)
    if size <= 0:
        raise ValueError(f"leader_trade: 算出的 size={size} 非正（notional/mid 太小）")
    return exch.market_open(coin, is_buy, float(size))


def submit_user_signed(action: dict, signature: dict) -> dict:
    """前端「直送 HL」路徑的 Python 鏡像：POST {TESTNET_URL}/exchange。
    用來驗證 spark.publicapi.approvals 產生的 typed data 真的能被 HL 接受。"""
    resp = httpx.post(f"{TESTNET_URL}/exchange",
                      json={"action": action, "nonce": action["nonce"], "signature": signature},
                      timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"submit_user_signed 失敗: {data}")
    return data


def wait_until(pred: Callable[[], bool], timeout: float = 60.0, interval: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# ---------------------------------------------------------------------------
# keysvc：真 server，macOS 無 SO_PEERCRED → harness 內直接放行（僅測試用途）。
# ---------------------------------------------------------------------------


class KeysvcThread:
    """在 tmp 目錄起一顆真的 spark.keysvc.server（daemon thread）。

    macOS 沒有 SO_PEERCRED（peercred.py 檔頭），故 `authorize_peer` 在此直接傳入
    恆真的 callable——不透過 `spark.keysvc.peercred`，效果等同「monkeypatch 放行」
    但更直接（`serve_forever` 本就把 authorize_peer 當參數注入，不需要真的
    monkeypatch 任何模組屬性）。真實授權邏輯（SO_PEERCRED uid 比對）已有離線測試
    與 RUNBOOK §8 實機驗收，harness 的目標是驗證協定／keystore／API 三者串接。
    """

    def __init__(self, root: Path):
        self.keys_dir = root / "keys"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        self.sock_path = str(root / "keysvc.sock")
        self._ks = EnvFileKeyStore(str(self.keys_dir))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=serve_forever,
            args=(self.sock_path, self._ks, lambda _conn: True, self._stop),
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if Path(self.sock_path).exists():
                return
            time.sleep(0.05)
        raise TimeoutError(f"keysvc socket 未在時限內就緒: {self.sock_path}")

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)


def make_real_app(tmp_path: Path, *, builder: str, leaders: list[dict],
                  keysvc_sock: str) -> tuple[TestClient, object, ApiStore]:
    """真 KeysvcClient + 真 HLGateway(TESTNET_URL) 組出來的 public API app。
    leaders 是 spark.filet.leaders.load_leaders 接受的 entry dict 清單
    （只含 _ENTRY_ALLOWED_KEYS 內的鍵，否則 loader 會拒載）。

    ⭐ `followers_path` 顯式釘進 tmp_path（T2 修法，T1 遺漏）：`ApiConfig.followers_path`
    預設值是 CWD 相對的 `var/filet/followers.json`——不覆寫的話，凡是讀 manifest 的
    端點（`/api/me/leader`／`/api/me/dashboard` 等）在測試進程 CWD＝repo 根時會去讀
    **repo 工作樹裡的真實檔案**，等同對正式資料造成非預期讀取面（若該檔存在甚至可能
    洩漏真實 follower 清單到測試斷言）。與 leaders_path／exchange_dir／state_base
    同一個「測試必須明講落點」的紀律（make_cfg 既有慣例）。"""
    leaders_path = tmp_path / "leaders.json"
    leaders_path.write_text(json.dumps({"leaders": leaders}))
    cfg = make_cfg(tmp_path, network="testnet", builder_address=builder,
                   keysvc_sock=keysvc_sock, leaders_path=str(leaders_path),
                   followers_path=str(tmp_path / "followers.json"))
    store = ApiStore(cfg.db_path)
    keysvc = KeysvcClient(cfg.keysvc_sock)
    hl = HLGateway(TESTNET_URL)
    app = create_app(cfg, store, keysvc, hl)
    return TestClient(app, base_url="https://testserver"), cfg, store


# ---------------------------------------------------------------------------
# 引擎 subprocess 執行。
# ---------------------------------------------------------------------------


def engine_env_from_file(path: str | Path) -> dict[str, str]:
    """解析 KEY=VALUE 格式 env 檔（沿 filet_auto_activate 產生的區塊格式），
    跳過空行與 `#` 開頭註解行。"""
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = value.strip()
    return env


_REPO_ROOT = Path(__file__).resolve().parents[2]


def run_engine_once(env_file: str | Path, keys_dir: str | Path,
                    extra_env: dict[str, str] | None = None,
                    ) -> subprocess.CompletedProcess:
    """subprocess 跑 `uv run python -m scripts.run_copytrade --once`。

    合併順序（R1 強制項最後套用，任何呼叫端都無法覆蓋成非 testnet）：
    os.environ → env 檔內容 → extra_env → 強制 FILET_KEYS_DIR/SPARK_NETWORK=testnet。
    """
    env = dict(os.environ)
    env.update(engine_env_from_file(env_file))
    if extra_env:
        env.update(extra_env)
    env["FILET_KEYS_DIR"] = str(keys_dir)
    env["SPARK_NETWORK"] = "testnet"
    return subprocess.run(
        ["uv", "run", "python", "-m", "scripts.run_copytrade", "--once"],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


# ---------------------------------------------------------------------------
# CLI：seed-faucet（生水龍頭錢包＋可選一次性播種轉帳）／faucet-status（唯讀）。
# 這支 CLI 本身在使用者裁決 plan §2 Q1 之前不得由任何 agent 執行
# （seed-faucet 會寫 Keychain；--confirm 會動 testnet 真轉帳）。
# ---------------------------------------------------------------------------


def _faucet_entry_name() -> str:
    return os.environ.get("FILET_TESTNET_FAUCET_ACCOUNT", "filet-testnet-faucet")


def _spot_usdc(address: str) -> Decimal:
    info = Info(TESTNET_URL, skip_ws=True)
    raw = info.spot_user_state(address)
    if not isinstance(raw, dict):
        return Decimal("0")
    for b in raw.get("balances") or []:
        if isinstance(b, dict) and b.get("coin") == "USDC":
            try:
                return Decimal(str(b.get("total", "0")))
            except (ValueError, ArithmeticError):
                return Decimal("0")
    return Decimal("0")


def _cmd_seed_faucet(args: argparse.Namespace) -> int:
    if not (args.dry_run or args.confirm):
        print("必須指定 --dry-run 或 --confirm 其中之一（安全預設：兩者皆缺 = 不做任何事）",
              file=sys.stderr)
        return 2
    entry = f"{_faucet_entry_name()}:main"
    if keyring.get_password("spark", entry) is not None:
        print(f"水龍頭錢包已存在（Keychain spark/{entry}），拒絕覆寫", file=sys.stderr)
        return 1
    new = Account.create()
    keyring.set_password("spark", entry, new.key.hex())  # 私鑰只在此行以內存在區域變數
    print(f"已生成水龍頭錢包並存入 Keychain spark/{entry}：位址 {new.address}")
    if args.dry_run:
        print("--dry-run：略過 testnet 轉帳")
        return 0
    src_entry = f"{args.from_account}:main"
    src_pk = keyring.get_password("spark", src_entry)
    if src_pk is None:
        print(f"來源帳戶 Keychain spark/{src_entry} 不存在，無法轉帳", file=sys.stderr)
        return 1
    src_wallet = Wallet(Account.from_key(src_pk))
    print(f"以 {src_wallet.address} 於 testnet 轉帳 {args.amount} USDC 到 {new.address} ...")
    fund(src_wallet, new.address, Decimal(str(args.amount)))
    print("轉帳完成並已確認到帳。")
    return 0


def _cmd_faucet_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    entry = f"{_faucet_entry_name()}:main"
    pk = keyring.get_password("spark", entry)
    if pk is None:
        print(f"找不到水龍頭錢包（Keychain spark/{entry} 不存在）。"
              "請先跑 `seed-faucet`，或見 "
              "docs/superpowers/plans/2026-09-02-golive-regression.md §2 Q1。",
              file=sys.stderr)
        return 1
    wallet = Wallet(Account.from_key(pk))
    info = Info(TESTNET_URL, skip_ws=True)
    try:
        state = info.user_state(wallet.address)
        perp_av = state.get("marginSummary", {}).get("accountValue", "0")
    except Exception as e:  # noqa: BLE001
        perp_av = f"<查詢失敗: {e}>"
    try:
        spot = str(_spot_usdc(wallet.address))
    except Exception as e:  # noqa: BLE001
        spot = f"<查詢失敗: {e}>"
    print(f"水龍頭位址: {wallet.address}")
    print(f"testnet perp accountValue: {perp_av}")
    print(f"testnet spot USDC: {spot}")
    return 0


def _require_scratchpad_path(path: str) -> Path:
    """T6 私鑰檔落地限制（plan §3 T6）：`mint-wallet --pk-file`／`sweep-wallet --pk-file`
    只允許寫進／讀 scratchpad 路徑（路徑任一段落含 "scratchpad"），避免拋棄式錢包
    私鑰意外落進 repo 工作樹或使用者家目錄的常駐位置。"""
    p = Path(path).resolve()
    if "scratchpad" not in p.parts:
        raise SystemExit(f"拒絕：路徑必須落在 scratchpad 底下（收到 {p}）")
    return p


def _cmd_keysvc_serve(args: argparse.Namespace) -> int:
    """T6：前景跑真 keysvc server 直到 SIGTERM/SIGINT（僅測試用途，authorize_peer
    恆真——見本檔 KeysvcThread 檔頭同一份理由：macOS 無 SO_PEERCRED）。"""
    keys_dir = Path(args.keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    ks = EnvFileKeyStore(str(keys_dir))
    stop = threading.Event()

    def _on_signal(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    print(f"keysvc-serve: 監聽 {args.sock}（keys_dir={keys_dir}），等待 SIGTERM/SIGINT 停止")
    serve_forever(args.sock, ks, lambda _conn: True, stop)
    print("keysvc-serve: 已停止")
    return 0


def _cmd_mint_wallet(args: argparse.Namespace) -> int:
    """T6：從水龍頭建拋棄式錢包並 fund，私鑰寫入 --pk-file（O_EXCL/mode 600，
    僅限 scratchpad 路徑）；stdout 只印位址（R4：私鑰不外洩）。

    先確認水龍頭 perp+spot 合計 ≥160（T2 可能正在同時消耗同一個水龍頭），
    不足就每 60s 輪詢，最多 45 分鐘。"""
    pk_file = _require_scratchpad_path(args.pk_file)
    if pk_file.exists():
        print(f"pk 檔已存在，拒絕覆寫: {pk_file}", file=sys.stderr)
        return 1
    usdc = Decimal(str(args.usdc))
    need_total = Decimal("160")
    faucet = faucet_wallet()
    info = Info(TESTNET_URL, skip_ws=True)
    deadline = time.monotonic() + 45 * 60
    while True:
        try:
            perp = Decimal(str(
                info.user_state(faucet.address).get("marginSummary", {})
                    .get("accountValue", "0")))
        except Exception:  # noqa: BLE001
            perp = Decimal("0")
        try:
            spot = _spot_usdc(faucet.address)
        except Exception:  # noqa: BLE001
            spot = Decimal("0")
        if perp + spot >= need_total:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"mint-wallet: 水龍頭餘額不足（perp {perp} + spot {spot} < {need_total}），"
                "45 分鐘輪詢逾時")
        print(f"mint-wallet: 水龍頭餘額不足（perp {perp} + spot {spot} < {need_total}），"
              "60s 後重試…", file=sys.stderr)
        time.sleep(60)
    wallet = new_wallet()
    fund(faucet, wallet.address, usdc)
    fd = os.open(str(pk_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("0x" + wallet.account.key.hex().removeprefix("0x"))
    print(wallet.address)
    return 0


def _cmd_sweep_wallet(args: argparse.Namespace) -> int:
    """T6：把 --pk-file 指向的拋棄式錢包 withdrawable 掃回水龍頭；成功才刪除 pk 檔
    （sweep() 本身是 best-effort/只 warn 不拋，這裡用 warnings 攔截判定是否真的成功，
    避免資金卡在錢包裡卻把唯一的復原憑證刪掉）。"""
    pk_file = _require_scratchpad_path(args.pk_file)
    if not pk_file.exists():
        print(f"pk 檔不存在: {pk_file}", file=sys.stderr)
        return 1
    pk = pk_file.read_text().strip()
    wallet = Wallet(Account.from_key(pk))
    faucet = faucet_wallet()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sweep(wallet, faucet)
    failures = [w for w in caught if str(w.message).startswith("sweep:")]
    if failures:
        for w in failures:
            print(f"sweep 失敗（保留 pk 檔以便重試）: {w.message}", file=sys.stderr)
        return 1
    pk_file.unlink()
    print(f"已掃回水龍頭並刪除 pk 檔：{wallet.address}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.integration.harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    seed = sub.add_parser("seed-faucet",
                          help="生成新水龍頭錢包並（--confirm 才會）從主鑰轉帳播種")
    seed.add_argument("--from-account", required=True,
                      help="Keychain 主鑰帳戶名（spark/<name>:main），播種來源")
    seed.add_argument("--amount", type=float, default=300.0, help="轉帳 USDC 數量")
    seed.add_argument("--dry-run", action="store_true",
                      help="只生成＋存 Keychain，不轉帳")
    seed.add_argument("--confirm", action="store_true",
                      help="確認執行真的 testnet 轉帳（動真錢的水龍頭來源）")
    seed.set_defaults(func=_cmd_seed_faucet)

    status = sub.add_parser("faucet-status", help="唯讀：印水龍頭位址與 testnet 餘額")
    status.set_defaults(func=_cmd_faucet_status)

    keysvc_serve = sub.add_parser(
        "keysvc-serve", help="T6：前景跑真 keysvc server 直到 SIGTERM/SIGINT（僅測試用途）")
    keysvc_serve.add_argument("--sock", required=True, help="unix socket 路徑")
    keysvc_serve.add_argument("--keys-dir", required=True, help="agent key 存放目錄")
    keysvc_serve.set_defaults(func=_cmd_keysvc_serve)

    mint_wallet = sub.add_parser(
        "mint-wallet", help="T6：從水龍頭建拋棄式錢包並 fund，私鑰寫入 --pk-file")
    mint_wallet.add_argument("--usdc", type=float, default=150.0, help="fund 的 USDC 數量")
    mint_wallet.add_argument("--pk-file", required=True,
                             help="私鑰輸出檔（必須在 scratchpad 路徑內，O_EXCL/mode 600）")
    mint_wallet.set_defaults(func=_cmd_mint_wallet)

    sweep_wallet = sub.add_parser(
        "sweep-wallet", help="T6：把 --pk-file 錢包 withdrawable 掃回水龍頭，成功後刪除 pk 檔")
    sweep_wallet.add_argument("--pk-file", required=True,
                              help="私鑰輸入檔（必須在 scratchpad 路徑內）")
    sweep_wallet.set_defaults(func=_cmd_sweep_wallet)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
