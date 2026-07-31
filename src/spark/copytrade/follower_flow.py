"""follower（我方錢包）出入金的回撤基準**永久**校正（Wave 5）。

背景：equity.py 模組 docstring 自承的債——「客戶自行把 perp 資金轉出會被視為
回撤」。prod 的 lifetime 閘（0.40）仍在執法，出金 >40% 會被當成虧損直接平倉
鎖死。本模組把 ledger 流量（入金 +usdc／出金 -usdc）從回撤基準中排除：偵測到
net 流量後，等額平移 7 天滾動樣本與 lifetime peak（讀-改-寫，各自原子）。

語意與 leader 側（leader_flow.py 的 36h 線性衰減）**刻意不同**：leader 中性化
處理的是 scale 分母的暫態跳動（leader 事後會照新 TVL 調倉，分母須收斂回真實值）；
follower 回撤量的是**交易損益**，出入金不是損益——流量要永久排除、不衰減。

exactly-once 邊界：狀態檔 `last_processed_ms` 之前（含）的流量已處理完畢。
crash 語意見 apply_follower_flows 內的「順序鐵則」註解——所有失敗方向都收斂到
「該批流量沒校正」（＝今天的 fail-safe 行為：出金被誤判成虧損），
絕不收斂到「同批流量套兩次」（閘變鈍，fail-open）。
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from pathlib import Path

from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, SAMPLES_RELPATH, _load, _save
from spark.copytrade.notifier import Notifier
from spark.exchange.base import LedgerFlow

STATE_RELPATH = Path("var/copytrade/follower_flow.json")

logger = logging.getLogger(__name__)


def load_marker(root: Path) -> int | None:
    """讀處理標記。不存在／壞檔 → None＋log。

    呼叫端把 None 視為「未初始化」並以 now 重新起算——fail-safe：寧可漏校正
    （回到「出金算虧損」的既有行為），也不猜一個 last 出來冒雙重套用的險。
    """
    path = root / STATE_RELPATH
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("follower_flow 標記讀取失敗（%s）: %r", path, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("follower_flow 標記格式錯誤（%s）: %r", path, raw)
        return None
    try:
        return int(raw["last_processed_ms"])
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("follower_flow 標記欄位無效（%s）: %r", path, e)
        return None


def save_marker(root: Path, last_processed_ms: int) -> bool:
    """原子寫標記（tmp+pid+os.replace，同 costbreaker.save_log 慣例）。

    **絕不拋例外**；回傳是否成功——呼叫端在寫入失敗時必須**放棄本輪調整**：
    標記沒落地就調整，下輪會把同批流量再套一次（fail-open）。
    """
    path = root / STATE_RELPATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"last_processed_ms": int(last_processed_ms)}))
        os.replace(tmp, path)
        return True
    except OSError as e:  # noqa: BLE001 — 狀態留痕失敗不得讓引擎當掉
        logger.warning("follower_flow 標記寫入失敗（%s）: %r", path, e)
        return False


def _shift_samples(root: Path, flows: list[LedgerFlow]) -> None:
    """ts-aware 平移：每筆樣本只吸收**晚於它**的流量（時間戳不動；讀-改-寫，_save 原子）。

    每筆樣本的平移量 = Σ{flow.usdc : flow.time_ms > 樣本 ts×1000}——早於流量的
    樣本尚未含該流量，要平移；晚於流量的樣本（例如 breach 二次確認時，檔內已有
    步驟 2 寫下的「流量之後」樣本）**已經含流量，不得再平移**（2026-08-01 第三批
    審查 F2：全檔平移會把它再平移一次、永久偏離真值 net）。

    單位換算只在此一處做清楚：樣本 ts 是**秒**（float，本機時鐘）、流量 time_ms
    是**毫秒**（交易所時鐘）；比較前把樣本 ts 轉 ms，兩邊同單位再比。
    跨源比較的已知極限（誠實標註）：本機與交易所時鐘的 NTP 偏移可能讓「恰落在
    流量時點附近」的邊界樣本被誤分類；影響上限＝單筆樣本的平移量，且樣本隨
    7 天窗輪替自癒，方向不偏（可偏嚴也可偏鬆一筆樣本），可接受。

    檔案缺失／壞檔（_load 回空）＝無基準可校 → 跳過；之後的樣本由校正後的
    current 重建，天然一致。
    """
    path = root / SAMPLES_RELPATH
    samples = _load(path)
    if not samples:
        return
    shifted: list[tuple[float, str]] = []
    for ts, v in samples:
        ts_ms = ts * 1000.0
        shift = sum((f.usdc for f in flows if f.time_ms > ts_ms), Decimal("0"))
        shifted.append((ts, str(Decimal(v) + shift)))
    _save(path, shifted)


def _shift_lifetime_peak(root: Path, net: Decimal) -> None:
    """lifetime peak `+net` 後 floor 於 0（原子寫，同 update_lifetime_peak 格式）。

    ⭐ 與 _shift_samples 的 ts-aware 不同，peak **維持收 full net**：peak 是單一
    數值、無時間戳，無從按 ts 分段。已接受的過度保守小邊角：入金恰好在縫隙裡
    創出新高時，peak 會被 full net 推高一點點（dd 偏嚴＝fail-safe 方向），
    窗口輪替與下一輪 update_lifetime_peak 自然收斂。

    檔案缺失＝尚無高水位 → 跳過（下一輪 update_lifetime_peak 會以校正後的
    current 重建）。floor 0 的語意：出金超過歷史高點時，「絕對底線」已無參考
    意義，交由 killswitch 的 peak<=0 守衛停用 total dd 判定。
    """
    path = root / LIFETIME_PEAK_RELPATH
    if not path.exists():
        return
    try:
        prev = Decimal(str(json.loads(path.read_text())))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError,
            ValueError, ArithmeticError) as e:
        logger.warning("lifetime peak 讀取失敗（%s），本輪不平移: %r", path, e)
        return
    peak = max(Decimal("0"), prev + net)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(str(peak)))
        os.replace(tmp, path)
    except OSError as e:  # noqa: BLE001 — 出金方向漏平移＝peak 偏高＝dd 偏嚴（fail-safe）
        logger.warning("lifetime peak 寫入失敗（%s）: %r", path, e)


def apply_follower_flows(root: Path, adapter, address: str, notifier: Notifier,
                         *, now_ms: int) -> None:
    """偵測 (last, now_ms] 窗內的出入金並平移回撤基準。每輪呼叫一次（loop 步驟 2 前）。"""
    last = load_marker(root)
    if last is None:
        # 初始化：只落標記、不回溯歷史流量——部署前的流量已烘進既有樣本，
        # 回溯會把「已反映在樣本裡的錢」再平移一次（雙重計算）。誠實不追。
        save_marker(root, now_ms)
        return

    try:
        flows, anomalies = adapter.get_ledger_flows(address, last + 1)
    except Exception as e:  # noqa: BLE001 — 取數失敗是降級不是熔斷
        # 標記不動 → 下輪自 last 重試，事件不丟。這一輪的行為退回今天的
        # fail-safe：出金頂多被誤判成虧損，不會反向鈍化閘門。
        notifier.warn(
            "follower_flow",
            f"follower 出入金取數失敗（{e!r}），本輪不校正；標記不動、下輪重試",
            dedup_key="follower_flow_fetch_failed",
        )
        return

    if anomalies:
        notifier.warn(
            "follower_flow",
            f"follower ledger 出現白名單外型別：{', '.join(str(a) for a in anomalies)}"
            f"——該類流量未計入校正，net 可能不完整，請人工核對",
            dedup_key="follower_flow_unknown_types",
        )

    # 上界 now_ms 是雙重套用防護：時鐘偏差產生的「未來戳」流量若在本輪套用，
    # 標記（now_ms）蓋不住它，下輪 `time_ms > last` 又會撈到同一筆 → 套兩次
    # （fail-open）。留給下輪處理即恰好一次。
    fresh = [f for f in flows if last < f.time_ms <= now_ms]
    net = sum((f.usdc for f in fresh), Decimal("0"))
    if not fresh or net == 0:
        # max(last, now_ms)：時鐘回跳（NTP step）時標記絕不倒退——倒退＝已處理
        # 流量重新落窗＝下輪重套同批（fail-open）。:121 的初始化分支無 last
        # 可比，維持寫 now_ms 不變。
        save_marker(root, max(last, now_ms))
        return

    # ⭐⭐ 順序鐵則：**先寫標記（推進 now_ms）、再做調整**。crash 落在兩步之間的
    # 最壞方向＝該批流量沒校正（出金被視為虧損＝今天的 fail-safe 行為）；反過來
    # （先調整後標記）crash 會讓下輪重套同批流量＝樣本被平移兩次、閘變鈍
    # （fail-open），絕對不可。標記寫失敗同理：沒有標記就不准調整。
    # max(last, now_ms) 同上：時鐘回跳時標記絕不倒退（此分支 fresh 非空 ⇒
    # now_ms > last，max 是防禦性等價寫法，兩處寫點語意一致）。
    if not save_marker(root, max(last, now_ms)):
        notifier.warn(
            "follower_flow",
            "follower_flow 標記寫入失敗，本輪放棄校正（防雙重套用）；下輪重試",
            dedup_key="follower_flow_marker_write_failed",
        )
        return

    try:
        _shift_samples(root, fresh)   # ts-aware：每筆樣本只吸收晚於它的流量
    except OSError as e:
        # 與 _shift_lifetime_peak 的 OSError 語意對稱：標記已推進 → 本批整批跳過
        # ＝漏校正（出金被誤判成虧損，今天的 fail-safe 行為），絕不讓例外穿出
        # run_cycle、也不留「peak 平移了、樣本沒平移」的半套狀態。
        notifier.warn(
            "follower_flow",
            f"樣本平移寫入失敗（{e!r}）——標記已推進，本批校正跳過（fail-safe）",
            dedup_key="follower_flow_samples_write_failed",
        )
        return
    _shift_lifetime_peak(root, net)
    notifier.warn(
        "follower_flow",
        f"偵測到出入金 net={net} USDC（{len(fresh)} 筆），回撤基準已校正"
        f"（滾動樣本與 lifetime peak 平移 {net}）",
        # 事件稀少（人為出入金）；key 帶輪次戳＝實質不去重——固定 key 會把
        # 「同一天兩次出金」的第二則吃掉。
        dedup_key=f"follower_flow_applied:{now_ms}",
    )
