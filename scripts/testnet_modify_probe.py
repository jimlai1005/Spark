"""testnet 探針：驗證 modify-first 主動路徑（SDK 0.24.0 modify_order 無 builder 參數）
下單成交後，builder fee 歸屬是否仍然計入。對照兩條路徑：

  A（對照組）：orchestrator.place_marketable_order → 直接下帶 builder 的 IOC 成交單
              （builder 走 "order" action 的 builder 欄位，已知歸屬正確，見
              hyperliquid/utils/signing.py:519-527 order_wires_to_order_action）。
  B（實驗組）：place_order 先建立一筆遠端 GTC 掛單（帶 builder）→ modify_order 把價格
              改到**盤口最佳買價**（SDK modify_order()/bulk_modify_orders_new() 建構的
              batchModify action 結構上無 builder 欄位，見 hyperliquid/exchange.py:190-238）
              → 等該單被吃成交 → 觀察 builder 累計費是否仍增加。

背景：spec T1.3。詳見
docs/superpowers/research/2026-07-16-modify-builder-attribution.md（含判定準則與政策選項）。
本腳本只產生數據，不做政策決定。

── 2026-07-19 testnet 實測結論（f=20、ETH、size=0.01、builder=user 自己）────────────
1. **modify 不會弄丟 builder 歸屬**。控制變因的兩組 maker 成交對照：
     C1 未經 modify 的盤口 GTC 買單 → ratio = 0.9997
     C2 遠端掛單 → modify 到盤口 → ratio = 1.0000（oid 56668340400）
   兩組成交皆 crossed=False（maker）。C1 是必要對照——沒有它就無法排除
   「maker 成交本來就不計 builder fee」這個競爭解釋。
   對照組 A（place taker 路徑）同期 ratio = 0.9998 / 0.9999。
2. **HL 的 batchModify 替換單是 post-only 語意**：modify 成 IOC 或穿價單一律被拒
   （分別回 "Attempted to modify to invalid new order" 與 "Post only order would have
   immediately matched"）。故改單後的訂單只能 rest，不可能在改單當下吃單成交。
3. **modify 會重發新 oid**（實測 56668174536 → 56668192119），而 adapter 的
   modify_order() 只回傳 bool，新 oid 在回應中被丟棄——追蹤改單後的訂單不能沿用舊 oid。
4. **modify 失敗不是原子的**：穿價 modify 被拒那次，原本的遠端掛單也一併消失
   （batchModify 似乎先撤舊單再下新單，新單失敗則兩頭落空）。改單失敗後不可假設
   舊單仍在簿上，必須重抓 get_open_orders 對帳。
─────────────────────────────────────────────────────────────────────

用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\
      [SPARK_NETWORK=testnet] uv run python -m scripts.testnet_modify_probe

keystore 選擇（同 scripts/run_copytrade.py 慣例）:
  FILET_KEYSTORE   缺省／keychain → MacKeychainBackend（M1 Mac 開發行為，不變）；
                    envfile → EnvFileKeyStore(FILET_KEYS_DIR，預設 /etc/filet/keys)。

非託管（M2）模式:
  M2 產品不變量是「我們永遠沒有客戶主鑰」——EnvFileKeyStore.get_main_signer 是結構性
  raise。而本探針的 A/B 探測邏輯**完全不需要主鑰**，只有 onboarding（approve agent +
  approve builder fee）需要。非託管下鏈上授權由客戶瀏覽器錢包完成，腳本不該再做 onboarding：

    PROBE_SKIP_ONBOARD=true  跳過 onboarding，改為**驗證鏈上前置條件**再繼續：
      1) query_agent_addresses(user) 必須包含本地 agent 地址；
      2) query_max_builder_fee(user, builder) 必須 ≥ settings.f。
    任一不滿足即明確錯誤退出（不硬跑——授權缺失下的 0 增量無法與「歸屬遺失」區分）。
  未顯式設定時，若 keystore 無法提供主鑰（PermissionError／NotImplementedError）也會
  自動轉為跳過模式並印警告，而非崩在 get_main_signer 上。

  唯讀查詢（mids／builderRewards／extraAgents／maxBuilderFee）一律走 exchange=None 的
  read_adapter——這些查詢本就不需簽名，跳過模式下也就不相依於根本不存在的 main adapter。
  A/B 探測的寫入路徑仍走 agent adapter，邏輯與 M1 完全相同。

⚠️ 僅限 testnet：SPARK_NETWORK=mainnet 會被拒絕執行（本腳本會下真實測試單，不可對主網跑）。
"""
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from spark.config import Settings
from spark.exchange.base import BuilderCode, Order
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.keystore.base import KeyStore
from spark.onboarding import onboard
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import AccrualTimeout, wait_for_accrual

USAGE = (
    "用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. "
    "[SPARK_NETWORK=testnet] uv run python -m scripts.testnet_modify_probe"
)
REQUIRED_ENV = ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")


def _check_env() -> tuple[str, str, str, str]:
    """驗證必要環境變數並拒絕主網。這必須是 main() 的第一步——缺變數或誤填 mainnet
    時必須零網路呼叫即退出（呼叫端不得在這步之前建構任何 Info/Exchange）。"""
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"缺少環境變數: {', '.join(missing)}")
        print(USAGE)
        raise SystemExit(2)
    network = os.environ.get("SPARK_NETWORK", "testnet")
    if network == "mainnet":
        print("拒絕執行：本探針會下真實測試單，SPARK_NETWORK=mainnet 一律拒跑（僅限 testnet）。")
        raise SystemExit(2)
    return (os.environ["SPARK_ACCOUNT_ID"], os.environ["SPARK_USER_ADDR"],
            os.environ["SPARK_BUILDER_ADDR"], network)


def _env_true(name: str) -> bool:
    """env 旗標解析：容忍行內註解與空白（同 run_copytrade 對 COPY_LIVE_TRADING 的處理）。"""
    return (os.environ.get(name) or "").split("#", 1)[0].strip().lower() == "true"


def select_keystore() -> KeyStore:
    """keystore 後端依 env FILET_KEYSTORE 選擇（1:1 沿用 scripts/run_copytrade.py 的慣例）：
    未設／keychain（預設，Mac 開發）→ MacKeychainBackend；
    envfile（VPS／非託管）→ EnvFileKeyStore(root=FILET_KEYS_DIR，預設 /etc/filet/keys)。"""
    backend = os.environ.get("FILET_KEYSTORE", "keychain")
    if backend == "envfile":
        from spark.keystore.envfile import EnvFileKeyStore
        return EnvFileKeyStore(os.environ.get("FILET_KEYS_DIR", "/etc/filet/keys"))
    from spark.keystore.keychain import MacKeychainBackend
    return MacKeychainBackend()


def _try_main_signer(ks: KeyStore, account_id: str):
    """取主鑰；keystore 結構性不持有主鑰（非託管）時回 None 而非炸開。
    只吞 PermissionError/NotImplementedError——那是「這個後端沒有主鑰」的語意訊號；
    其他例外（KeyError＝有主鑰能力但找不到這個 account、Keychain IO 失敗）照樣拋出，
    不要把「設定錯誤」偽裝成「非託管模式」而靜默降級（工程原則 2：語意 vs 環境失敗）。"""
    try:
        return ks.get_main_signer(account_id)
    except (PermissionError, NotImplementedError) as e:
        print(f"keystore 不持有主鑰（非託管）：{e}")
        return None


def _verify_prerequisites(adapter, user_addr: str, builder_addr: str,
                          agent_address: str, f: int) -> None:
    """跳過 onboarding 時的鏈上前置條件驗證。任一不滿足即 SystemExit——
    絕不硬跑：授權缺失下 builder fee 增量必然為 0，會與「假說 (b) 歸屬遺失」的
    數據落點完全重疊，污染這份要拿去做政策裁決的證據。"""
    agents = adapter.query_agent_addresses(user_addr)
    if agent_address.lower() not in agents:
        raise SystemExit(
            f"前置條件不滿足：本地 agent {agent_address} 未出現在鏈上 extraAgents"
            f"（user={user_addr}，鏈上共 {len(agents)} 個 agent）。"
            "請先用瀏覽器錢包完成 approveAgent 再重跑。")
    max_fee = adapter.query_max_builder_fee(user_addr, builder_addr)
    if max_fee < f:
        raise SystemExit(
            f"前置條件不滿足：maxBuilderFee={max_fee} < 所需 f={f}"
            f"（user={user_addr} builder={builder_addr}）。"
            "請先用瀏覽器錢包完成 approveBuilderFee 再重跑。")
    print(f"前置條件已滿足（鏈上授權由外部完成）：agent={agent_address} 已授權，"
          f"maxBuilderFee={max_fee} ≥ f={f}")


def _confirm_fills(adapter, user_addr: str, coin: str, since: datetime,
                   exclude_oids: frozenset[int] = frozenset(),
                   attempts: int = 10, sleep_s: float = 2.0) -> list:
    """輪詢 get_user_fills 直到查到 `since` 之後的 coin 成交紀錄，回傳**全部**符合的
    UserFill 清單；逾時回傳空清單。

    ⚠️ 為何以「時間窗」而非 oid 比對（2026-07-19 實測修正）：Hyperliquid 的 modify
    會**重新發配 oid**——實測 modify 前 oid=56668174536、modify 後變成 56668192119。
    而 SDK/adapter 的 modify_order() 只回傳 bool，新 oid 在回應中被丟棄（見回報的
    生產碼觀察），腳本拿不到它。若沿用原本「用改單前的 oid 比對 fills」的寫法，成交
    永遠查不到，會把「成交了」誤報成「查無成交」，進而讓 Δ_modify 的解讀失去對照。
    改以 modify 送出前的時間戳為窗起點：對照組 A 的成交早於此時間戳，天然被排除。

    modify_order() 只回傳 bool，需要另外確認「改單後是否真的成交」——不能只靠 builder
    累計費的增量反推：增量為 0 可能是「根本沒成交」，也可能是「成交但歸屬遺失」，兩者
    對應完全不同的政策意涵（前者是腳本/流動性問題，後者才是 T1.3 要驗證的風險），
    必須分開觀察，不能混為一談。

    必須蒐集全部而非第一筆：thin testnet 流動性下，同一張單可能分批成交多筆
    （partial fills）。只取第一筆會低估 notional（ratio 分母），使 ratio 系統性
    偏高——可能把本應人工深查的部分歸屬異常誤判為假說 (a)。故首次查到成交後再多等
    一輪重抓，補齊稍晚入帳的分批成交，回傳完整清單由呼叫端加總 Σ(sz×px)。
    """
    def _match(now):
        # 時間窗左界退 5 秒吸收本機與交易所的時鐘偏差，但**必須**同時用 exclude_oids
        # 排除改單前就已存在的成交——否則這 5 秒會把對照組 A 的 taker 成交吃進來
        # （A 與 B 前後相隔僅數秒；2026-07-19 首次實跑就是這樣把 A 的成交誤記成 B 的，
        # 得出 ratio_b=0.0099 的假陽性「歸屬遺失」結論）。時間窗負責粗篩、oid 負責去重，
        # 兩者缺一不可。
        fills = adapter.get_user_fills(user_addr, since - timedelta(seconds=5), now)
        return [f for f in fills
                if f.coin == coin and f.time >= since - timedelta(seconds=5)
                and f.oid not in exclude_oids]

    for _ in range(attempts):
        matched = _match(datetime.now(timezone.utc))
        if matched:
            # 已有成交：多等一輪再重抓一次，蒐集可能稍晚入帳的分批成交。
            if sleep_s:
                time.sleep(sleep_s)
            return _match(datetime.now(timezone.utc))
        if sleep_s:
            time.sleep(sleep_s)
    return []


def _expected_fee(notional: Decimal, f: int) -> Decimal:
    """f 是「十分之一 bp」（見 spark.config.Settings.f 的欄位註解：20 = 0.02%）。
    fraction = f / 100000（f=20 → 0.0002 = 0.02%）。"""
    return notional * Decimal(f) / Decimal(100000)


def _print_group(label: str, size: Decimal, notional: Decimal, expected_fee: Decimal,
                 delta: Decimal) -> Decimal | None:
    ratio = (delta / expected_fee) if expected_fee > 0 else None
    print(f"[{label}] size={size} notional={notional} expected_fee={expected_fee} "
         f"actual_delta={delta} ratio={ratio if ratio is not None else 'n/a'}")
    return ratio


def main():
    account_id, user_addr, builder_addr, network = _check_env()

    settings = Settings(builder_address=builder_addr, account_id=account_id, network=network)
    ks = select_keystore()
    info = Info(settings.api_url, skip_ws=True)

    # 顯式跳過，或 keystore 結構性無主鑰（非託管）→ 跳過 onboarding。
    skip_onboard = _env_true("PROBE_SKIP_ONBOARD")
    main_signer = None if skip_onboard else _try_main_signer(ks, account_id)
    if not skip_onboard and main_signer is None:
        skip_onboard = True
        print("警告：keystore 無主鑰，自動轉為 PROBE_SKIP_ONBOARD 模式"
              "（改以鏈上前置條件驗證取代 onboarding）。")

    # 唯讀查詢（mids／builderRewards／extraAgents／maxBuilderFee）不需簽名：
    # 統一走 exchange=None 的 adapter，跳過模式下才不會相依於根本不存在的主鑰。
    read_adapter = HyperliquidAdapter(network, info=info, exchange=None)
    try:
        local_agent_address = ks.get_agent_signer(account_id).address
    except KeyError:
        local_agent_address = None

    if skip_onboard:
        # 跳過模式不會產生新 agent key（那需要主鑰簽 approveAgent）——本地必須已有。
        if local_agent_address is None:
            raise SystemExit(
                f"PROBE_SKIP_ONBOARD 模式下本地查無 account {account_id} 的 agent key。"
                "非託管模式不會由本腳本產生／授權 agent，請先備妥 agent key 並完成鏈上授權。")
        _verify_prerequisites(read_adapter, user_addr, settings.builder_address,
                              local_agent_address, settings.f)
    else:
        main_adapter = HyperliquidAdapter(
            network, info=info, exchange=Exchange(main_signer, settings.api_url))
        onboard(main_adapter, settings, main_signer=main_signer, user_address=user_addr,
                local_agent_address=local_agent_address,
                on_agent_key=lambda k: ks.import_key(account_id, "agent", k))
        print("onboarding OK（agent 授權狀態已對照鏈上 extraAgents 查詢驅動）")

    agent = ks.get_agent_signer(account_id)

    agent_adapter = HyperliquidAdapter(
        network, info=info,
        exchange=Exchange(agent, settings.api_url, account_address=user_addr))
    builder = BuilderCode(b=settings.builder_address, f=settings.f)

    # ---------- 對照組 A：place 路徑（builder 走 order action，已知歸屬）----------
    print("\n=== 對照組 A：place_marketable_order ===")
    mid = read_adapter.get_all_mids()[settings.coin]
    baseline_a = read_adapter.query_builder_accrued(settings.builder_address)
    res_a = place_marketable_order(agent_adapter, settings, agent_signer=agent,
                                   is_buy=True, best_opposite_px=mid)
    if not res_a.ok:
        raise SystemExit(
            f"[對照組 A] 下單未成交，已完成步驟：onboarding、baseline_a 查詢。raw={res_a.raw}")
    accrued_a = wait_for_accrual(read_adapter, settings.builder_address, baseline=baseline_a)
    delta_a = accrued_a - baseline_a
    # filled_size/avg_px 出自 HL 回應的 totalSz/avgPx（_parse_order_response），
    # 本身已是跨分批成交的聚合值——不需另行加總 fills（對照實驗組 B 的 Σ(sz×px)）。
    notional_a = res_a.filled_size * res_a.avg_px
    expected_a = _expected_fee(notional_a, settings.f)
    _print_group("A(place)", res_a.filled_size, notional_a, expected_a, delta_a)

    # ---------- 實驗組 B：遠端 GTC 掛單 → modify_order 改可成交 ----------
    print("\n=== 實驗組 B：place 遠端 GTC → modify_order（SDK modify 結構上無 builder 參數）===")
    baseline_b = read_adapter.query_builder_accrued(settings.builder_address)
    mid_b = read_adapter.get_all_mids()[settings.coin]
    resting_order = Order(coin=settings.coin, is_buy=True, size=settings.order_size,
                          limit_px=mid_b * Decimal("0.7"), tif="Gtc")
    res_resting = agent_adapter.place_order(agent, resting_order, builder)
    if not res_resting.ok:
        raise SystemExit(
            f"[實驗組 B] 遠端掛單失敗，已完成步驟：對照組 A 全部完成（Δ_place={delta_a}）。"
            f"raw={res_resting.raw}")
    try:
        status0 = res_resting.raw["response"]["data"]["statuses"][0]
    except (KeyError, IndexError) as e:
        raise SystemExit(
            f"[實驗組 B] 掛單成功但回應無 statuses[0]，已完成步驟：對照組 A 全部完成"
            f"（Δ_place={delta_a}）。raw={res_resting.raw}") from e
    if isinstance(status0, dict) and "filled" in status0:
        raise SystemExit(
            f"[實驗組 B] 遠端掛單意外立即成交（mid×0.7 竟成交，流動性異常），"
            f"實驗組作廢本輪。已完成步驟：對照組 A 全部完成（Δ_place={delta_a}）。"
            f"statuses[0]={status0}")
    try:
        oid = status0["resting"]["oid"]
    except (KeyError, TypeError) as e:
        raise SystemExit(
            f"[實驗組 B] 掛單成功但回應無 resting.oid，已完成步驟：對照組 A 全部完成"
            f"（Δ_place={delta_a}）。raw={res_resting.raw}") from e
    print(f"[實驗組 B] 遠端掛單成功 oid={oid} limit_px={resting_order.limit_px}")

    # modify 目標＝**盤口最佳買價**（非穿價），成交型態必然是 maker。
    # 2026-07-19 實測改設計（原為 mid×1.05 穿價 IOC，在 HL 上結構性不可能成立）：
    #   modify → Ioc          ⇒ "Attempted to modify to invalid new order"
    #   modify → Gtc 穿價     ⇒ "Post only order would have immediately matched, bbo was ..."
    #   modify → Gtc 非穿價   ⇒ True（掛上簿）
    # 即 HL batchModify 的替換單帶 **post-only 語意**：改單後的訂單只能 rest，
    # 永遠不可能在改單當下吃單成交。故「modify 成 taker 立即成交」這個原始設計
    # 無法在 HL 上執行——modify 路徑的成交只會是後續被吃到的 maker 成交。
    # 對照組 A 因此不足以單獨解讀本組（A 是 taker）：若要判斷「歸屬是否因 modify 遺失」，
    # 必須與「未經 modify 的 maker 成交」對照，否則無法排除「maker 本來就不計 builder fee」。
    # 該對照已於 scratchpad 實驗執行並記錄於本檔頂部的實測結論。
    best_bid = Decimal(info.l2_snapshot(settings.coin)["levels"][0][0]["px"])
    modify_target = Order(coin=settings.coin, is_buy=True, size=settings.order_size,
                          limit_px=best_bid, tif="Gtc", reduce_only=False)
    # 改單前先記下已存在的成交 oid（含對照組 A 的），供下面過濾——見 _confirm_fills。
    prior_oids = frozenset(
        f.oid for f in agent_adapter.get_user_fills(
            user_addr, datetime.now(timezone.utc) - timedelta(minutes=30),
            datetime.now(timezone.utc)))
    modify_sent_at = datetime.now(timezone.utc)
    modify_ok = agent_adapter.modify_order(agent, oid, modify_target)
    if not modify_ok:
        raise SystemExit(
            f"[實驗組 B] modify_order 被拒絕（oid={oid}），已完成步驟：對照組 A 全部完成"
            f"（Δ_place={delta_a}）、遠端掛單成功。")

    # 注意：比對用時間窗而非 oid——modify 會重發新 oid（見 _confirm_fills docstring）。
    # attempts 放大到 140（≈7 分鐘）：本組是 maker 成交，要等市場回頭吃掉盤口這張單，
    # 不像 taker 那樣送出即成交；沿用原本 20 秒的窗會系統性地把「還沒被吃」誤報成「查無成交」。
    fills = _confirm_fills(agent_adapter, user_addr, settings.coin, modify_sent_at,
                           exclude_oids=prior_oids, attempts=140)
    if not fills:
        print(f"警告：[實驗組 B] modify 後於輪詢時間窗內查無成交紀錄（改單前 oid={oid}）——"
             "下面的 Δ_modify 若為 0，無法單靠此腳本區分「根本沒成交」與「成交但歸屬遺失」，"
             "須人工核對 get_open_orders / 交易所前端後再解讀。")
        notional_b = settings.order_size * modify_target.limit_px  # 估計值：無實際成交價可用
    else:
        # thin testnet 流動性下同一張單可能分批成交：notional = Σ(sz×px)，
        # 各筆 fee 一併加總輸出作為佐證（fee 是 taker fee，非 builder fee，僅供對照）。
        notional_b = sum((f.sz * f.px for f in fills), Decimal("0"))
        total_sz = sum((f.sz for f in fills), Decimal("0"))
        total_fee = sum((f.fee for f in fills), Decimal("0"))
        print(f"[實驗組 B] 確認成交 fills={len(fills)} 筆 total_sz={total_sz} "
             f"notional={notional_b} total_fee={total_fee} 改單前 oid={oid} "
             f"成交 oid={sorted({f.oid for f in fills})}")
        for f in fills:
            print(f"  - oid={f.oid} sz={f.sz} px={f.px} fee={f.fee} time={f.time.isoformat()}")

    try:
        accrued_b = wait_for_accrual(read_adapter, settings.builder_address, baseline=baseline_b)
        delta_b = accrued_b - baseline_b
    except AccrualTimeout as e:
        # 刻意只在實驗組（B）攔截：逾時本身就是「假說 b：歸屬遺失」的直接證據，不是腳本錯誤，
        # 攔截後仍把訊息印出來（不吞例外），delta 記為 0 讓下面照常產出結構化比較。
        # 對照組（A，已知歸屬正確的路徑）若逾時代表整體設置本身有問題，不攔截、讓它照常炸開。
        print(f"警告：[實驗組 B] {e}")
        delta_b = Decimal("0")

    expected_b = _expected_fee(notional_b, settings.f)
    ratio_b = _print_group("B(modify)", settings.order_size, notional_b, expected_b, delta_b)

    print("\n=== 判定（僅描述數據落點，政策決定見研究報告，本腳本不下結論）===")
    if not fills:
        # 沒有成交就沒有可判定的歸屬問題：Δ=0 只是「這張單還掛著沒被吃」。
        # 這個分支必須擋在 ratio 判讀之前——B 組是 maker 成交，等不到對手單是常態，
        # 若讓它掉進下面 ratio<0.1 的分支，會把「沒成交」直接印成「歸屬遺失」的假陽性
        # （_confirm_fills docstring 早已點名這兩者不可混為一談，此處補上對應的守門）。
        print("無法判定：實驗組 B 的掛單在輪詢時間窗內未成交（maker 單等不到對手單），"
              "Δ_modify=0 反映的是「未成交」而非「歸屬遺失」。請於市場較活躍時重跑。")
    elif ratio_b is None:
        print("無法判定：expected_fee=0（notional 為 0，可能完全沒有成交）。")
    elif ratio_b > Decimal("0.9"):
        print(f"ratio_b={ratio_b} > 0.9 → 支持假說 (a)：modify 後訂單仍歸屬 builder。")
    elif ratio_b < Decimal("0.1"):
        print(f"ratio_b={ratio_b} < 0.1 → 支持假說 (b)：modify 後 builder 歸屬遺失。")
    else:
        print(f"ratio_b={ratio_b} 落在 0.1~0.9 之間 → 異常區間，需人工深查（部分成交／時序競態等）。")


if __name__ == "__main__":
    main()
