"""測試專用：在 Hyperliquid **testnet** 上以 leader 錢包的身分下單，
用來驅動 M1 copytrade 引擎的端到端驗證（leader 動 → follower 跟）。

⚠️ 測試工具，非生產路徑：不進 systemd、不被 src/ 任何模組 import。
⚠️ 僅限 testnet：base url 不含 "testnet" 直接 raise（結構性防呆，見 `_assert_testnet`）。
⚠️ 單筆名目上限 $200（`MAX_NOTIONAL_USD`），超過直接拒絕。

簽章者＝leader 的 agent key（trade-only、無提領權），由 `EnvFileKeyStore` 從
`FILET_KEYS_DIR/<account_id>/agent.key` 讀取（權限 600 硬檢查）。私鑰不進 log/print/例外。

下單一律走既有生產邊界 `HyperliquidAdapter`（→ `ResilientExchange` → SDK），
不自建 SDK 呼叫——重用才是真的在測產品程式碼。

用法：
  uv run python -m scripts.testnet_leader_drive status
  uv run python -m scripts.testnet_leader_drive open ETH buy 100
  uv run python -m scripts.testnet_leader_drive close ETH [0.5]
  uv run python -m scripts.testnet_leader_drive flip ETH 100

可用環境變數覆寫（預設值＝dev testnet 的 Builder3662 錢包）：
  FILET_KEYS_DIR / SPARK_LEADER_ACCOUNT_ID / SPARK_LEADER_ADDR / SPARK_BUILDER_ADDR
"""
import argparse
import os
from decimal import Decimal
from pathlib import Path

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from spark.config import Settings
from spark.copytrade.instrument import _round_size
from spark.exchange.base import BuilderCode
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.keystore.envfile import EnvFileKeyStore

# dev testnet 的 leader（Builder3662）；此錢包同時是本專案的 builder 地址
# （見 ~/filet-dev/followers.json 的 builder_address），故下單帶的 builder 即它自己。
DEFAULT_LEADER_ADDR = "0xbAC652A5Fb611c1BdC3B9D244cc7E0cC03123662"
DEFAULT_KEYS_DIR = "~/filet-dev/keys"
NETWORK = "testnet"

MAX_NOTIONAL_USD = Decimal("200")   # 測試防呆：單筆名目上限
SLIPPAGE = Decimal("0.02")          # 市價單容許滑價（testnet 流動性薄，給寬一點）
MIN_NOTIONAL_USD = Decimal("10")    # HL 協議最小下單名目


def _assert_testnet(api_url: str) -> None:
    """結構性防呆：任何寫入前先確認 base url 指向 testnet。放在建構 Exchange 之前，
    誤設 mainnet 時必須零簽章、零網路寫入即退出。"""
    if "testnet" not in api_url:
        raise RuntimeError(
            f"拒絕執行：本腳本僅限 testnet，偵測到 base url={api_url!r}（不含 'testnet'）")


def _account_id_for(addr: str) -> str:
    """address → keystore account_id 慣例：'f' + 去 0x 的小寫地址
    （對照 ~/filet-dev/keys/ 既有目錄命名）。"""
    return "f" + addr[2:].lower()


class LeaderDriver:
    def __init__(self, adapter: HyperliquidAdapter, signer, address: str,
                 builder: BuilderCode):
        self._adapter = adapter
        self._signer = signer
        self._address = address
        self._builder = builder

    # --- reads ---
    def positions(self):
        return self._adapter.get_positions(self._address)

    def _position_for(self, coin: str):
        for p in self.positions():
            if p.coin == coin:
                return p
        return None

    def print_status(self) -> None:
        snap = self._adapter.get_account_state(self._address)
        print(f"leader={self._address}")
        print(f"  accountValue={snap.account_value} withdrawable={snap.withdrawable} "
              f"totalMarginUsed={snap.total_margin_used} totalNtlPos={snap.total_ntl_pos}")
        positions = self.positions()
        if not positions:
            print("  持倉：（無）")
            return
        for p in positions:
            side = "LONG" if p.szi > 0 else "SHORT"
            print(f"  持倉 {p.coin} {side} szi={p.szi} entry={p.entry_px} "
                  f"uPnL={p.unrealized_pnl} margin={p.margin_used} lev={p.leverage}x")

    # --- sizing ---
    def _size_for_notional(self, coin: str, notional_usd: Decimal) -> Decimal:
        if notional_usd > MAX_NOTIONAL_USD:
            raise SystemExit(
                f"拒絕：名目 ${notional_usd} 超過測試上限 ${MAX_NOTIONAL_USD}")
        if notional_usd < MIN_NOTIONAL_USD:
            raise SystemExit(
                f"拒絕：名目 ${notional_usd} 低於 HL 最小下單名目 ${MIN_NOTIONAL_USD}")
        mids = self._adapter.get_all_mids()
        if coin not in mids:
            raise SystemExit(f"拒絕：{coin} 不在 all_mids（非 perp 幣種或幣名錯誤）")
        size = _round_size(notional_usd / mids[coin], self._adapter.get_size_decimals(coin))
        if size <= 0:
            raise SystemExit(
                f"拒絕：${notional_usd} 換算 {coin} 數量後捨入為 0（mid={mids[coin]}）")
        print(f"  sizing: mid={mids[coin]} notional=${notional_usd} → size={size}")
        return size

    # --- writes ---
    def open(self, coin: str, side: str, notional_usd: Decimal):
        is_buy = side == "buy"
        size = self._size_for_notional(coin, notional_usd)
        res = self._adapter.market_open(self._signer, coin, is_buy, size,
                                        SLIPPAGE, self._builder)
        self._report(f"open {coin} {side} ${notional_usd}", res)
        return res

    def close(self, coin: str, fraction: Decimal):
        pos = self._position_for(coin)
        if pos is None:
            raise SystemExit(f"拒絕：leader 目前無 {coin} 持倉，無可平之倉")
        size = _round_size(abs(pos.szi) * fraction, self._adapter.get_size_decimals(coin))
        if size <= 0:
            raise SystemExit(
                f"拒絕：{coin} 持倉 {abs(pos.szi)} × {fraction} 捨入後為 0")
        # ABC 語意：is_buy 是「平倉下單方向」——多單用賣單平、空單用買單平。
        is_buy = pos.szi < 0
        res = self._adapter.close_reduce_only(self._signer, coin, is_buy, size,
                                              SLIPPAGE, self._builder)
        self._report(f"close {coin} fraction={fraction} (size={size})", res)
        return res

    def flip(self, coin: str, notional_usd: Decimal):
        pos = self._position_for(coin)
        if pos is None:
            raise SystemExit(f"拒絕：leader 目前無 {coin} 持倉，無可反手之倉（請先 open）")
        was_long = pos.szi > 0
        # 先全平：平倉失敗必須停止，不得帶著舊倉直接反向開（否則變成加倉而非反手）。
        res_close = self.close(coin, Decimal("1"))
        if not res_close.ok:
            raise SystemExit(f"flip 中止：平倉未成交，未送出反向開倉單。raw={res_close.raw}")
        return self.open(coin, "sell" if was_long else "buy", notional_usd)

    def _report(self, action: str, res) -> None:
        if res.ok:
            print(f"[OK] {action}: filled_size={res.filled_size} avg_px={res.avg_px}")
        else:
            print(f"[FAIL] {action}: raw={res.raw}")


def _build_driver() -> LeaderDriver:
    address = os.environ.get("SPARK_LEADER_ADDR", DEFAULT_LEADER_ADDR)
    account_id = os.environ.get("SPARK_LEADER_ACCOUNT_ID") or _account_id_for(address)
    builder_addr = os.environ.get("SPARK_BUILDER_ADDR", address.lower())
    keys_dir = Path(os.environ.get("FILET_KEYS_DIR", DEFAULT_KEYS_DIR)).expanduser()

    settings = Settings(builder_address=builder_addr, account_id=account_id,
                        network=NETWORK)
    _assert_testnet(settings.api_url)   # 建構任何 Exchange／簽章者之前

    signer = EnvFileKeyStore(keys_dir).get_agent_signer(account_id)
    info = Info(settings.api_url, skip_ws=True)
    adapter = HyperliquidAdapter(
        NETWORK, info=info,
        exchange=Exchange(signer, settings.api_url, account_address=address))
    return LeaderDriver(adapter, signer, address,
                        BuilderCode(b=settings.builder_address, f=settings.f))


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="testnet_leader_drive",
        description="測試專用：以 leader 身分在 Hyperliquid testnet 下單（僅 testnet）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="印出 leader 帳戶與持倉快照")

    o = sub.add_parser("open", help="市價開倉")
    o.add_argument("coin")
    o.add_argument("side", choices=["buy", "sell"])
    o.add_argument("notional_usd", type=Decimal)

    c = sub.add_parser("close", help="平倉（reduce-only）")
    c.add_argument("coin")
    c.add_argument("fraction", nargs="?", type=Decimal, default=Decimal("1"))

    f = sub.add_parser("flip", help="反手：先全平再反向開")
    f.add_argument("coin")
    f.add_argument("notional_usd", type=Decimal)

    return p.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    driver = _build_driver()

    if args.cmd == "status":
        driver.print_status()
        return

    if args.cmd == "open":
        driver.open(args.coin, args.side, args.notional_usd)
    elif args.cmd == "close":
        if not (Decimal("0") < args.fraction <= Decimal("1")):
            raise SystemExit(f"拒絕：fraction 必須落在 (0, 1]，收到 {args.fraction}")
        driver.close(args.coin, args.fraction)
    elif args.cmd == "flip":
        driver.flip(args.coin, args.notional_usd)

    print("\n--- 動作後持倉快照 ---")
    driver.print_status()


if __name__ == "__main__":
    main()
