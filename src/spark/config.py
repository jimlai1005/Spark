from dataclasses import dataclass, field
from decimal import Decimal
from spark.money import assert_fee_within_cap

API_URLS = {
    "testnet": "https://api.hyperliquid-testnet.xyz",
    "mainnet": "https://api.hyperliquid.xyz",
}
CSV_BASE_URLS = {
    "testnet": "https://stats-data.hyperliquid.xyz/Testnet/builder_fills",
    "mainnet": "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills",
}
MIN_BUILDER_BALANCE = Decimal("100")  # builder 啟用門檻 USDC


@dataclass(frozen=True)
class Settings:
    builder_address: str
    account_id: str
    network: str
    f: int = 20
    max_rate: str = "0.1%"
    coin: str = "ETH"
    order_size: Decimal = field(default_factory=lambda: Decimal("0.01"))

    def __post_init__(self):
        if self.network not in API_URLS:
            raise ValueError(f"unknown network: {self.network}")
        assert_fee_within_cap(self.f)

    @property
    def api_url(self) -> str:
        return API_URLS[self.network]

    @property
    def csv_base_url(self) -> str:
        return CSV_BASE_URLS[self.network]
