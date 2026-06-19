"""費率換算與金額工具。f 單位為「十分之一個 bp」：f=10 → 1bp → 0.01%。"""
from decimal import Decimal

FEE_CAP_TENTHS_BP = 100  # 協議上限 0.1%


def f_to_percent_str(f: int) -> str:
    """把 builder fee f（十分之一 bp）轉成 ApproveBuilderFee 用的百分比字串。f/1000 (%)。
    會先驗證 f 在協議上限內（0 < f <= 100），否則 raise ValueError。"""
    assert_fee_within_cap(f)
    pct = Decimal(f) / Decimal(1000)
    return f"{pct.normalize()}%"


def assert_fee_within_cap(f: int) -> None:
    if not (0 < f <= FEE_CAP_TENTHS_BP):
        raise ValueError(f"builder fee f={f} 超出協議上限 {FEE_CAP_TENTHS_BP}（0.1%）")
