"""scripts/vault_preflight.py
vault leader 上架前的**唯讀** preflight——驗證跟單引擎的兩條假設在「這隻」vault 成立：
(1) vault 的 accountValue 可當 sizing 分母（TVL 100% 躺在 perp 帳戶）；
(2) pnlHistory 流量中性（入金／出金不污染 pnl 序列）。

這兩條對單一 vault（Ultron）已實測成立，但 equity basis 是「錢包形態專屬」的性質
（工程原則 1，事故 #3）：**每隻**新 vault 上架前必須重跑本腳本，不得沿用舊結論。

用法（spec §5、施工計畫 Wave 5）：
  [SPARK_NETWORK=mainnet] uv run python -m scripts.vault_preflight 0xVAULT [--window-days 30]

行為：
  - 唯讀：四份資料全走 HLGateway 的 /info（單一 resilience 邊界，transient 自動重試）；
    本腳本無任何寫入面。
  - 檢查邏輯是純函式 `run_checks(data) -> list[CheckResult]`（離線可測）；
    main() 只負責解析 args、抓資料、印表、exit code。
  - 每檢查印一行 `PASS/FAIL name — detail`；六項全過 exit 0，任一 FAIL exit 1。
  - import 階段零網路（HLGateway 延後 import＋延後建線，照 watchlist_snapshot 慣例）。
"""
import argparse
import os
import time
from dataclasses import dataclass
from decimal import Decimal

from spark.exchange.ledger_flows import flow_anomaly, signed_flow

# ── 閾值常數（來源：spec §3.4；錨例＝Ultron 實測，2026-07-31）──────────────────
# 字串小數位截斷等級的絕對容差：恆等式兩側都是 API 原文數字，理論殘差為 0。
ABS_TOL_USD = Decimal("0.01")
# 流量中性殘差容差：max($1, 0.01% × accountValue)。Ultron 實測殘差 ~$0.08。
FLOW_TOL_MIN_USD = Decimal("1")
FLOW_TOL_REL = Decimal("0.0001")
# 單筆流量佔 TVL 超過 8% 印 WARN（引擎 size_tolerance 預設；僅資訊性，不 FAIL）。
SIZE_TOLERANCE_PCT = Decimal("0.08")
# 恆等式的型別基礎（白名單）唯一定義在 spark.exchange.ledger_flows；
# 檢查 6 取道 flow_anomaly 判定，本檔不得自建型別清單。


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PreflightData:
    """四份 /info 原始回應（gateway 原樣回傳；語意判讀集中在 run_checks）。"""
    clearinghouse_state: dict
    vault_details: dict
    portfolio: list
    ledger_updates: list


def _window(portfolio: list, period: str) -> dict | None:
    """portfolio 形狀 `[[period, {...}], ...]`——按名字挑窗，不靠位置。"""
    for item in portfolio or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == period:
            return item[1]
    return None


# 流量型別映射的唯一定義點：spark.exchange.ledger_flows
_signed_flow = signed_flow


def _pnl_end(window: dict) -> Decimal:
    return Decimal(str(window["pnlHistory"][-1][1]))


def run_checks(data: PreflightData) -> list[CheckResult]:
    """六項檢查（spec §3.4）。純函式：不做 IO，輸入即四份原始回應。"""
    out: list[CheckResult] = []
    vd = data.vault_details if isinstance(data.vault_details, dict) else {}
    chs = data.clearinghouse_state
    account_value = Decimal(str(chs["marginSummary"]["accountValue"]))

    # 1. is-vault：vaultDetails 非空且含 name（非 vault 位址查 vaultDetails 得空回應）。
    is_vault = bool(vd.get("name"))
    out.append(CheckResult(
        "is-vault", is_vault,
        (f"name={vd.get('name')} leaderFraction={vd.get('leaderFraction')} "
         f"followers={len(vd.get('followers') or [])} isClosed={vd.get('isClosed')}")
        if is_vault else "vaultDetails 空或缺 name——位址不是 vault"))

    # 2. perp-resident TVL：withdrawable == maxDistributable ⇒ TVL 100% 躺 perp 帳戶，
    #    accountValue 可當 sizing 分母。兩值都是 API 原文（同一時刻抓），容差只留截斷級。
    withdrawable = Decimal(str(chs["withdrawable"]))
    max_distributable = Decimal(str(vd.get("maxDistributable", "0")))
    tvl_diff = abs(withdrawable - max_distributable)
    out.append(CheckResult(
        "perp-resident-tvl", tvl_diff <= ABS_TOL_USD,
        f"withdrawable={withdrawable} maxDistributable={max_distributable} |diff|={tvl_diff}"))

    # 帳本流量（檢查 3/5/6 共用；白名單外型別不入淨流量，由檢查 6 專責 FAIL）。
    flows = [f for e in data.ledger_updates
             if (f := _signed_flow(e.get("delta") or {})) is not None]
    net_flow = sum(flows, Decimal("0"))

    # 3. flow-neutral pnl 恆等式：|ΔaccountValue − Δpnl − 淨流量| ≤ max($1, 0.01%×AV)。
    #    pnl 取窗內末點減首點（首點依 API 建構為 0.0，相減防禦非零首樣本——與
    #    ΔaccountValue 同窗同算法，工程原則 1 同源）。
    month = _window(data.portfolio, "month")
    if month is None:
        out.append(CheckResult("flow-neutral-pnl", False, "portfolio 缺 month 窗"))
    elif not month.get("accountValueHistory") or not month.get("pnlHistory"):
        # 新 vault：month 窗存在但樣本為空——[-1] 會 IndexError，閘門腳本不得吐
        # traceback；歷史不足無法機器判定恆等式，一律 FAIL 交人工。
        out.append(CheckResult(
            "flow-neutral-pnl", False,
            "月窗樣本為空——vault 歷史不足 30 天，恆等式無法機器判定，人工研判"))
    else:
        avh = month["accountValueHistory"]
        d_av = Decimal(str(avh[-1][1])) - Decimal(str(avh[0][1]))
        d_pnl = _pnl_end(month) - Decimal(str(month["pnlHistory"][0][1]))
        residual = abs(d_av - d_pnl - net_flow)
        tol = max(FLOW_TOL_MIN_USD, FLOW_TOL_REL * account_value)
        out.append(CheckResult(
            "flow-neutral-pnl", residual <= tol,
            f"ΔAV={d_av} Δpnl={d_pnl} 淨流量={net_flow} 殘差={residual} 容差={tol}"))

    # 4. 無 spot 污染：month 與 perpMonth 的 pnl 終值一致 ⇒ pnl 全來自 perp，
    #    spot 桶不存在（equity basis 每桶恰好算一次的前提）。
    perp_month = _window(data.portfolio, "perpMonth")
    if month is None or perp_month is None:
        out.append(CheckResult("no-spot-pollution", False,
                               "portfolio 缺 month 或 perpMonth 窗"))
    elif not month.get("pnlHistory") or not perp_month.get("pnlHistory"):
        out.append(CheckResult(
            "no-spot-pollution", False,
            "月窗樣本為空——vault 歷史不足 30 天，pnl 一致性無法機器判定，人工研判"))
    else:
        pnl_m, pnl_pm = _pnl_end(month), _pnl_end(perp_month)
        out.append(CheckResult(
            "no-spot-pollution", abs(pnl_m - pnl_pm) <= ABS_TOL_USD,
            f"month pnl={pnl_m} perpMonth pnl={pnl_pm} |diff|={abs(pnl_m - pnl_pm)}"))

    # 5. 流量統計：資訊性、恆為 PASS——給人看規模感；單筆 > 8% TVL 印 WARN
    #    （大額進出會放大 sizing 抖動，值得人工看一眼，但不構成 NO-GO）。
    max_single = max((abs(f) for f in flows), default=Decimal("0"))
    if account_value > 0:
        max_pct = max_single / account_value
        net_pct = net_flow / account_value
        warn = "（WARN：單筆超過 8% TVL）" if max_pct > SIZE_TOLERANCE_PCT else ""
        stat = (f"筆數={len(flows)} 最大單筆={max_single} ({max_pct * 100:.2f}% TVL) "
                f"淨流量={net_flow} ({net_pct * 100:.2f}% TVL){warn}")
    else:
        stat = f"筆數={len(flows)} 最大單筆={max_single} 淨流量={net_flow}（accountValue 非正）"
    out.append(CheckResult("flow-stats", True, stat))

    # 6. ledger 型別白名單：白名單外型別、或白名單內缺金額／方向欄位 → FAIL。
    #    恆等式的成立建立在「所有流量都被正確計號」上；未知型別無法計號，
    #    缺欄位的白名單型別會被 signed_flow 靜默漏計（回 None）——兩者同罪。
    #    異常分類取道 ledger_flows.flow_anomaly（唯一定義點）。
    anomalies = sorted({a for e in data.ledger_updates
                        if (a := flow_anomaly(e.get("delta") or {})) is not None})
    out.append(CheckResult(
        "ledger-type-whitelist", not anomalies,
        "全部型別在白名單內且欄位齊全" if not anomalies
        else f"流量異常：{', '.join(anomalies)}——恆等式基礎不成立，需人工研判"))

    return out


def fetch_preflight_data(gateway, address: str, *, window_days: int = 30,
                         clearinghouse_state: dict | None = None,
                         vault_details: dict | None = None) -> PreflightData:
    """四份 /info 資料 → PreflightData（**唯一**的資料抓取定義；2026-07-31 Wave 2）。

    preflight 腳本（main）與 public API 的准入 advisory 檢查共用本函式——同源原則：
    檢查（run_checks）與餵給檢查的資料抓取都只有一份實作，app 端不得自行拼資料面。
    已抓過的 clearinghouse_state／vault_details 可傳入避免重複查詢（app 端准入流程
    在呼叫前已各查過一次）；未傳入則由本函式經同一 gateway 抓。
    ledger 查詢窗＝month 窗首點時間戳（檢查 3 的 ΔAV 與淨流量必須同窗——同源）；
    month 窗缺樣本才回退 now − window_days。transient 例外由 gateway 的 resilience
    邊界重試、耗盡後原樣上拋（呼叫端自行轉譯：腳本吐 traceback、API 轉 502）。
    """
    chs = (clearinghouse_state if clearinghouse_state is not None
           else gateway.clearinghouse_state(address))
    vd = vault_details if vault_details is not None else gateway.vault_details(address)
    pf = gateway.portfolio(address)
    month = _window(pf, "month")
    if month and month.get("accountValueHistory"):
        start_ms = int(month["accountValueHistory"][0][0])
    else:
        start_ms = int(time.time() * 1000) - window_days * 86_400_000
    ledger = gateway.non_funding_ledger_updates(address, start_ms)
    return PreflightData(chs, vd, pf, ledger)


def main(argv=None, gateway=None) -> None:
    """CLI 入口。gateway 可注入（測試離線）；不注入則按 SPARK_NETWORK 建 HLGateway。"""
    ap = argparse.ArgumentParser(
        prog="vault_preflight",
        description="vault leader 上架前唯讀 preflight（六檢查；全過 exit 0，否則 1）")
    ap.add_argument("vault_address", help="vault 位址（0x…）")
    ap.add_argument("--window-days", type=int, default=30,
                    help="ledger 回看天數後備值——僅當 portfolio month 窗為空時使用；"
                         "正常情況查詢窗＝month 窗首點時間戳（預設 30）")
    args = ap.parse_args(argv)

    if gateway is None:  # 延後 import＋延後建線：import 階段零網路（watchlist 慣例）
        from spark.config import API_URLS
        from spark.publicapi.hl import HLGateway
        network = os.environ.get("SPARK_NETWORK", "mainnet")
        if network not in API_URLS:
            raise SystemExit(f"unknown SPARK_NETWORK: {network!r}")
        gateway = HLGateway(API_URLS[network])

    data = fetch_preflight_data(gateway, args.vault_address,
                                window_days=args.window_days)
    results = run_checks(data)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'} {r.name} — {r.detail}")
    raise SystemExit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
