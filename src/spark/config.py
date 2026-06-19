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
    f: int = 20             # 實收 builder fee，十分之一 bp（20 = 0.02%），對應 BuilderCode.f
    max_rate: str = "0.1%"  # ApproveBuilderFee 授權上限（D6：給足協議上限 0.1%，日後調 f 免重簽）
    coin: str = "ETH"
    order_size: Decimal = field(default_factory=lambda: Decimal("0.01"))

    def __post_init__(self):
        if self.network not in API_URLS:
            raise ValueError(f"unknown network: {self.network}")
        assert_fee_within_cap(self.f)
        # 不變量：實收費率（f）不得超過 ApproveBuilderFee 授權上限（max_rate）
        charged_pct = Decimal(self.f) / Decimal(1000)        # f=20 → 0.02
        approved_pct = Decimal(self.max_rate.rstrip("%"))    # "0.1%" → 0.1
        if charged_pct > approved_pct:
            raise ValueError(
                f"builder fee {charged_pct}% 超過授權上限 max_rate={self.max_rate}")

    @property
    def api_url(self) -> str:
        return API_URLS[self.network]

    @property
    def csv_base_url(self) -> str:
        return CSV_BASE_URLS[self.network]
