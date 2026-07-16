"""hl-copytrader vs spark 對帳純函式 parity 交叉比對。

比對對象：
  - spark: `spark.copytrade.orders._plan` / `_orders_match`（OrderSpec/Decimal，
    容忍度由呼叫端注入）。
  - hl-copytrader: `src.orders._plan` / `_orders_match`（dict/float，容忍度來自
    `src.config` 模組常數；價格容忍度 `_prices_equal` 的 `rel=1e-4` 為硬編預設，
    不受 .env 影響——已讀原始碼確認 `_orders_match` 呼叫時未傳 `rel`）。

hl-copytrader 為唯讀依賴：只透過 sys.path 注入唯讀 import 其純函式，不修改、
不呼叫任何有副作用的函式（`_reconcile_orders`/`sync_open_orders`/Trader 等一律不碰）。

安全設計：hl 的 `src/config.py` import 時會 `load_dotenv()`，若讀到真實 .env
可能載入私鑰等敏感值進 os.environ。以下 fixture：
  1. import 完成後立即（不等 teardown）洗掉已知敏感鍵。
  2. teardown 以 clear+update 完整還原 os.environ 快照（快照攝於 load_dotenv 前，
     本身不含 .env 載入的敏感值）。
  3. teardown 清掉 sys.path 與 sys.modules 裡的 `src`/`src.*`，避免汙染其他測試檔
     的模組命名空間（hl 頂層套件名為 `src`，與 spark 自身無關但仍需清乾淨）。
所有斷言訊息只印比對用的正規化 tuple 與 oid，不印 os.environ 或 hl config 的
其他值（SIZE_TOLERANCE 本身非機密，可印）。
"""
import os
import random
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from spark.copytrade.orders import OrderSpec, _orders_match as spark_match, _plan as spark_plan

HL = Path("/Users/jim/projects/hl-copytrader")
pytestmark = pytest.mark.skipif(not HL.exists(), reason="hl-copytrader 不在此機器")

_SENSITIVE_ENV_KEYS = ("WALLET_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
COINS = ("ETH", "BTC", "SOL")
PX_TOL = Decimal("0.0001")  # 對齊 hl `_prices_equal` 硬編預設 rel=1e-4
_QUANT = Decimal("0.000000001")  # 正規化用量化基準（9 位小數，容差 1e-9）


@pytest.fixture(scope="module")
def hl_funcs():
    snapshot = dict(os.environ)
    sys.path.insert(0, str(HL))
    try:
        from src.orders import _plan as hl_plan, _orders_match as hl_match
        from src import config as hl_config
        # import 完成後立即洗掉敏感鍵（不等 teardown）
        for k in _SENSITIVE_ENV_KEYS:
            os.environ.pop(k, None)
        yield hl_plan, hl_match, hl_config
    finally:
        sys.path.remove(str(HL))
        for name in list(sys.modules):
            if name == "src" or name.startswith("src."):
                del sys.modules[name]
        # clear+update 完整還原快照；快照攝於 load_dotenv 之前，故 .env 載入的
        # 敏感值不會經由還原重新進入 os.environ
        os.environ.clear()
        os.environ.update(snapshot)


# ─────────────────────────── 場景產生器 ───────────────────────────


@dataclass(frozen=True)
class Canon:
    """單張掛單的「規格來源」表示——spark(OrderSpec/Decimal) 與 hl(dict/float) 皆由
    同一個 Canon 實例衍生，確保兩邊比較的值同源、同單位（僅型別轉換，數值不重算）。"""

    coin: str
    is_buy: bool
    reduce_only: bool
    is_trigger: bool
    tpsl: str | None
    is_market: bool
    limit_px: Decimal
    trigger_px: Decimal  # 非 trigger 單固定 0，同 hl `_parse_orders`/`_build_desired` 慣例
    sz: Decimal


def _rand_price(rng: random.Random) -> Decimal:
    return Decimal(rng.randint(100_000, 10_000_000)) / Decimal(100)  # 1000.00 ~ 100000.00


def _rand_size(rng: random.Random) -> Decimal:
    return Decimal(rng.randint(1, 10_000)) / Decimal(1000)  # 0.001 ~ 10.000


def _rand_canon(rng: random.Random) -> Canon:
    is_trigger = rng.random() < 0.35
    tpsl = None
    is_market = False
    if is_trigger:
        tpsl = rng.choice(["tp", "sl", None])
        is_market = rng.random() < 0.3
    return Canon(
        coin=rng.choice(COINS),
        is_buy=rng.random() < 0.5,
        reduce_only=rng.random() < 0.3,
        is_trigger=is_trigger,
        tpsl=tpsl,
        is_market=is_market,
        limit_px=_rand_price(rng),
        trigger_px=_rand_price(rng) if is_trigger else Decimal("0"),
        sz=_rand_size(rng),
    )


def _perturb_scalar(rng: random.Random, ref: Decimal, tol: Decimal) -> Decimal | None:
    """回傳 ref 的鄰近擾動值，刻意涵蓋容忍度邊界兩側；回傳 None 表示改用全新隨機值。"""
    mode = rng.random()
    if mode < 0.3:
        return ref  # 完全相同
    if mode < 0.55:
        # 相對差距略小於容忍度 → 應視為相符
        frac = tol * Decimal(str(round(rng.uniform(0.0, 0.95), 6)))
        sign = 1 if rng.random() < 0.5 else -1
        return ref * (Decimal(1) + sign * frac)
    if mode < 0.8:
        # 相對差距略大於容忍度 → 應視為不相符
        frac = tol * Decimal(str(round(rng.uniform(1.05, 4.0), 6)))
        sign = 1 if rng.random() < 0.5 else -1
        result = ref * (Decimal(1) + sign * frac)
        return result if result > 0 else ref
    return None  # 完全隨機


def _perturb_canon(rng: random.Random, base: Canon, px_tol: Decimal, size_tol: Decimal) -> Canon:
    """以 base 為基礎產生鄰近單：價/量刻意命中容忍度邊界，類別欄位小機率翻轉
    （製造 slot 不相符的案例），藉此同時覆蓋 matched/modify/place/cancel 各分支。"""
    limit_px = _perturb_scalar(rng, base.limit_px, px_tol)
    if limit_px is None:
        limit_px = _rand_price(rng)

    is_trigger = base.is_trigger
    trigger_px = base.trigger_px
    if is_trigger:
        trigger_px = _perturb_scalar(rng, base.trigger_px, px_tol)
        if trigger_px is None:
            trigger_px = _rand_price(rng)

    sz = _perturb_scalar(rng, base.sz, size_tol)
    if sz is None or sz <= 0:
        sz = _rand_size(rng)

    is_buy = base.is_buy if rng.random() >= 0.1 else not base.is_buy
    reduce_only = base.reduce_only if rng.random() >= 0.1 else not base.reduce_only
    coin = base.coin if rng.random() >= 0.1 else rng.choice(COINS)
    tpsl = base.tpsl
    is_market = base.is_market
    if is_trigger and rng.random() < 0.1:
        tpsl = rng.choice(["tp", "sl", None])
    if is_trigger and rng.random() < 0.1:
        is_market = not is_market

    return Canon(
        coin=coin,
        is_buy=is_buy,
        reduce_only=reduce_only,
        is_trigger=is_trigger,
        tpsl=tpsl,
        is_market=is_market,
        limit_px=limit_px,
        trigger_px=trigger_px,
        sz=sz,
    )


def _gen_scenario(
    rng: random.Random, px_tol: Decimal, size_tol: Decimal
) -> tuple[list[Canon], list[tuple[int, Canon]]]:
    n_desired = rng.randint(0, 8)
    n_mine = rng.randint(0, 8)
    desired = [_rand_canon(rng) for _ in range(n_desired)]
    mine: list[tuple[int, Canon]] = []
    for i in range(n_mine):
        if desired and rng.random() < 0.5:
            order = _perturb_canon(rng, rng.choice(desired), px_tol, size_tol)
        else:
            order = _rand_canon(rng)
        mine.append((i + 1, order))
    return desired, mine


def _gen_pair(rng: random.Random, px_tol: Decimal, size_tol: Decimal) -> tuple[Canon, Canon]:
    base = _rand_canon(rng)
    other = _perturb_canon(rng, base, px_tol, size_tol) if rng.random() < 0.6 else _rand_canon(rng)
    return base, other


# ─────────────────────────── 型別轉換（同源 → 兩種表示） ───────────────────────────


def _to_spark(c: Canon) -> OrderSpec:
    return OrderSpec(
        coin=c.coin,
        is_buy=c.is_buy,
        sz=c.sz,
        limit_px=c.limit_px,
        reduce_only=c.reduce_only,
        is_trigger=c.is_trigger,
        tpsl=c.tpsl,
        trigger_px=c.trigger_px if c.is_trigger else None,
        is_market=c.is_market,
        tif="Gtc",
    )


def _to_hl(c: Canon) -> dict:
    """對齊 hl `_build_desired`（orders.py:114-126）產出的 dict 形狀（無 oid/dex）。"""
    return {
        "coin": c.coin,
        "is_buy": c.is_buy,
        "size": float(c.sz),
        "limit_px": float(c.limit_px),
        "trigger_px": float(c.trigger_px),
        "reduce_only": c.reduce_only,
        "is_trigger": c.is_trigger,
        "tpsl": c.tpsl,
        "is_market": c.is_market,
        "tif": "Gtc",
        "order_type_name": "Limit",
    }


def _to_hl_mine(oid: int, c: Canon) -> dict:
    """對齊 hl `_parse_orders`（monitor.py:193-207）產出的 dict 形狀（含 oid/dex）。"""
    d = _to_hl(c)
    d["oid"] = oid
    d["dex"] = ""
    return d


# ─────────────────────────── 正規化比對 ───────────────────────────


def _quant(x: Decimal) -> Decimal:
    return x.quantize(_QUANT)


def _norm_spark(o: OrderSpec) -> tuple:
    return (
        o.coin,
        o.is_buy,
        o.reduce_only,
        o.is_trigger,
        o.tpsl,
        o.is_market,
        _quant(o.limit_px),
        _quant(o.trigger_px if o.trigger_px is not None else Decimal(0)),
        _quant(o.sz),
    )


def _norm_hl(d: dict) -> tuple:
    return (
        d["coin"],
        d["is_buy"],
        d["reduce_only"],
        d["is_trigger"],
        d["tpsl"],
        d["is_market"],
        _quant(Decimal(str(d["limit_px"]))),
        _quant(Decimal(str(d["trigger_px"]))),
        _quant(Decimal(str(d["size"]))),
    )


def _assert_plan_matches(
    desired_canons: list[Canon],
    mine_canons: list[tuple[int, Canon]],
    px_tol: Decimal,
    size_tol: Decimal,
    hl_plan,
    ctx: str,
):
    spark_desired = [_to_spark(c) for c in desired_canons]
    spark_mine = [(oid, _to_spark(c)) for oid, c in mine_canons]
    hl_desired = [_to_hl(c) for c in desired_canons]
    hl_mine = [_to_hl_mine(oid, c) for oid, c in mine_canons]

    plan = spark_plan(spark_desired, spark_mine, px_rel_tol=px_tol, size_tol=size_tol)
    h_modifies, h_to_place, h_to_cancel, h_matched = hl_plan(hl_desired, hl_mine)

    spark_cancel_oids = set(plan.to_cancel)
    hl_cancel_oids = {o["oid"] for o in h_to_cancel}
    assert spark_cancel_oids == hl_cancel_oids, (
        f"{ctx}: to_cancel 分歧 spark={spark_cancel_oids} hl={hl_cancel_oids}"
    )

    spark_modifies = {oid: _norm_spark(o) for oid, o in plan.modifies}
    hl_modifies_norm = {oid: _norm_hl(d) for oid, d in h_modifies}
    assert spark_modifies == hl_modifies_norm, f"{ctx}: modifies 分歧 {spark_modifies} vs {hl_modifies_norm}"

    spark_place = Counter(_norm_spark(o) for o in plan.to_place)
    hl_place = Counter(_norm_hl(d) for d in h_to_place)
    assert spark_place == hl_place, f"{ctx}: to_place 分歧 {spark_place} vs {hl_place}"

    # matched：由「全體我方 oid 扣掉 modify 目標與 cancel 目標」反推 hl 實際 matched
    # oid 集合，與 spark 的 frozenset 直接比對；並用 hl 自身回傳的計數做二次校驗
    # （hl 演算法中 used/matched 同步增長，理論上必然自洽，此處當防呆網）。
    all_mine_oids = {oid for oid, _ in mine_canons}
    hl_matched_oids = all_mine_oids - set(hl_modifies_norm) - hl_cancel_oids
    assert hl_matched_oids == plan.matched, f"{ctx}: matched 集合分歧 hl={hl_matched_oids} spark={plan.matched}"
    assert len(hl_matched_oids) == h_matched, f"{ctx}: hl matched 計數 {h_matched} 與其自身回傳不符"
    return plan


# ─────────────────────────── 測試 ───────────────────────────


def test_plan_parity_random_scenarios(hl_funcs):
    hl_plan, _hl_match, hl_config = hl_funcs
    size_tol = Decimal(str(hl_config.SIZE_TOLERANCE))
    rng = random.Random(42)
    n_scenarios = 220
    totals = Counter()
    for i in range(n_scenarios):
        desired, mine = _gen_scenario(rng, PX_TOL, size_tol)
        plan = _assert_plan_matches(desired, mine, PX_TOL, size_tol, hl_plan, ctx=f"scenario#{i}")
        totals["matched"] += len(plan.matched)
        totals["modifies"] += len(plan.modifies)
        totals["to_place"] += len(plan.to_place)
        totals["to_cancel"] += len(plan.to_cancel)
    # 最低覆蓋防呆：任一分支萎縮成（近）零時，parity 斷言會退化成空集合比較而平白通過。
    # 門檻依 seed=42 實測值（matched 75/modifies 200/to_place 511/to_cancel 588）留大幅餘裕。
    for branch in ("matched", "modifies", "to_place", "to_cancel"):
        assert totals[branch] > 10, f"覆蓋防呆：{branch} 累計 {totals[branch]} <= 10，場景產生器已失衡"


def test_plan_parity_edge_cases(hl_funcs):
    hl_plan, _hl_match, hl_config = hl_funcs
    size_tol = Decimal(str(hl_config.SIZE_TOLERANCE))
    rng = random.Random(43)
    some_desired = [_rand_canon(rng) for _ in range(3)]
    some_mine = [(1, _rand_canon(rng)), (2, _rand_canon(rng))]

    for desired, mine, label in (
        ([], [], "雙空"),
        ([], some_mine, "空 desired"),
        (some_desired, [], "空 mine"),
    ):
        _assert_plan_matches(desired, mine, PX_TOL, size_tol, hl_plan, ctx=label)


def test_orders_match_parity_random_pairs(hl_funcs):
    _hl_plan, hl_match, hl_config = hl_funcs
    size_tol = Decimal(str(hl_config.SIZE_TOLERANCE))
    rng = random.Random(42)
    n_pairs = 500
    n_true = n_false = 0
    for i in range(n_pairs):
        d_canon, m_canon = _gen_pair(rng, PX_TOL, size_tol)
        spark_result = spark_match(
            _to_spark(d_canon), _to_spark(m_canon), px_rel_tol=PX_TOL, size_tol=size_tol
        )
        hl_result = hl_match(_to_hl(d_canon), _to_hl_mine(1, m_canon))
        assert spark_result == hl_result, (
            f"pair#{i} 分歧：desired={_norm_spark(_to_spark(d_canon))} "
            f"mine={_norm_spark(_to_spark(m_canon))}"
        )
        if spark_result:
            n_true += 1
        else:
            n_false += 1
    # 最低覆蓋防呆：全 True 或全 False 的配對集無法檢驗另一半分支。
    # 門檻依 seed=42 實測值（True 54/False 446）留餘裕。
    assert n_true > 20, f"覆蓋防呆：相符配對僅 {n_true} 組 <= 20，配對產生器已失衡"
    assert n_false > 20, f"覆蓋防呆：不相符配對僅 {n_false} 組 <= 20，配對產生器已失衡"


def test_hl_size_tolerance_is_default(hl_funcs):
    """記錄 hl 生效的 SIZE_TOLERANCE 是否為原始碼預設值 0.02（供回報用；此數值非機密）。
    無論是否為預設，本身不代表 parity 測試失敗，只是提醒讀者其他測試用的是「生效值」
    而非硬編 0.02——避免誤讀比對結果。"""
    _hl_plan, _hl_match, hl_config = hl_funcs
    print(f"hl SIZE_TOLERANCE 生效值 = {hl_config.SIZE_TOLERANCE}（原始碼預設 0.02）")
