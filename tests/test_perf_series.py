"""tests/test_perf_series.py
Leader 績效時間序列的採集、冪等、容錯與拼接（spark.filet.perf_series
＋ scripts.perf_series_snapshot）。全離線，portfolio_fn 注入。

本檔盯住的四件事，每一件對應一個「錯了就是永久資料損失或錯誤數字」的失效：
(1) **append 不覆蓋**——序列無法回填，任何重寫整檔的路徑都可能一次抹掉數月歷史。
(2) **重跑冪等**——timer 補跑／手動重跑不得在同一個取樣窗留下兩筆。
(3) **壞行跳過**——一行壞掉不該讓數月資料一起不可用。
(4) **拼接時 pnl 重定基準**——perpDay 的 pnl 是窗內累積，直接串接會在接縫處造出
    一個等於「前一窗全部 PnL」的假跳空，而它會被算進複利裡。
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.perf_series_snapshot import main
from spark.filet.leader_perf import compute_window_performance
from spark.filet.perf_series import (SAMPLE_PERIOD, Segment, append_samples,
                                     build_sample, load_records, sample_bucket,
                                     series_path_for, splice_segments)

_ADDR = "0x" + "ab" * 20
_DAY_MS = 86_400_000


def _at(hour: int, day: int = 19) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc)


def _portfolio(points, period: str = SAMPLE_PERIOD):
    """points = [(ts_ms, av, pnl)] → portfolio() 版型的回應。"""
    return [[period, {
        "accountValueHistory": [[ts, str(av)] for ts, av, _ in points],
        "pnlHistory": [[ts, str(p)] for ts, _, p in points]}]]


# ── 取樣時刻與冪等鍵 ──────────────────────────────────────────────────

def test_sample_bucket_floors_to_12h_windows():
    """⭐ bucket ＝ 冪等的唯一載體：同一個 12 小時窗內的任何時刻都得到同一個鍵。"""
    assert sample_bucket(_at(0)) == "2026-07-19T00"
    assert sample_bucket(_at(11)) == "2026-07-19T00"     # 同窗
    assert sample_bucket(_at(12)) == "2026-07-19T12"     # 換窗
    assert sample_bucket(_at(23)) == "2026-07-19T12"
    assert sample_bucket(_at(0, day=20)) == "2026-07-20T00"


def test_sample_bucket_rejects_naive_datetime():
    """naive 時間會讓 bucket 隨機器時區位移，同一個窗被寫成兩筆。"""
    with pytest.raises(ValueError):
        sample_bucket(datetime(2026, 7, 19, 12, 0))


def test_sample_carries_the_sampling_instant(tmp_path):
    """⭐ 每筆必須帶取樣時刻：沒有時刻的序列無法對齊（拼接與冪等都靠它）。"""
    rec = build_sample(_ADDR, _portfolio([(0, "100", "0")]), _at(13))
    assert rec["sampled_at"] == "2026-07-19T13:00:00+00:00"
    assert rec["bucket"] == "2026-07-19T12"
    assert rec["period"] == SAMPLE_PERIOD
    assert rec["address"] == _ADDR


def test_sample_normalizes_the_sampling_instant_to_utc():
    """非 UTC 的 aware 時間換算成 UTC 再取 bucket（兩端同基準，工程原則 1）。"""
    tz8 = timezone(timedelta(hours=8))
    rec = build_sample(_ADDR, _portfolio([(0, "100", "0")]),
                       datetime(2026, 7, 19, 20, 0, tzinfo=tz8))  # = 12:00 UTC
    assert rec["bucket"] == "2026-07-19T12"


def test_sample_rejects_non_perp_window():
    """⭐ 只抓 perp 窗：預設窗含 spot 與 vault，是客戶複製不到的報酬。"""
    with pytest.raises(ValueError):
        build_sample(_ADDR, _portfolio([(0, "100", "0")], period="month"), _at(0))


def test_sample_rejects_misaligned_series():
    """兩序列時間戳不同步 ⇒ 非同源，拒絕落檔（配錯了每個數字都看起來正常）。"""
    bad = [[SAMPLE_PERIOD, {"accountValueHistory": [[0, "100"], [1, "101"]],
                            "pnlHistory": [[0, "0"]]}]]
    with pytest.raises(ValueError):
        build_sample(_ADDR, bad, _at(0))


# ── append-only 與冪等 ────────────────────────────────────────────────

def test_append_does_not_overwrite_existing_lines(tmp_path):
    """⭐⭐ 第二次 append 必須是**附加**：既有行逐字元不變、檔案只增不減。"""
    p = tmp_path / "s.jsonl"
    first = build_sample(_ADDR, _portfolio([(0, "100", "0")]), _at(0))
    assert append_samples(p, [first]) == 1
    before = p.read_text()

    second = build_sample(_ADDR, _portfolio([(_DAY_MS, "110", "10")]), _at(12))
    assert append_samples(p, [second]) == 1
    after = p.read_text()

    assert after.startswith(before)          # 既有內容原封不動
    assert len(after.splitlines()) == 2
    recs, skipped = load_records(p)
    assert skipped == 0
    assert [r["bucket"] for r in recs] == ["2026-07-19T00", "2026-07-19T12"]


def test_append_is_idempotent_within_the_same_sampling_window(tmp_path):
    """⭐ 同一個取樣窗重跑（timer 補跑／手動重跑）→ 只留一筆。"""
    p = tmp_path / "s.jsonl"
    for hour in (0, 3, 11):                  # 三個時刻，同一個 12 小時窗
        rec = build_sample(_ADDR, _portfolio([(0, "100", "0")]), _at(hour))
        append_samples(p, [rec])
    recs, _ = load_records(p)
    assert len(recs) == 1
    assert recs[0]["bucket"] == "2026-07-19T00"


def test_append_keeps_distinct_addresses_independent(tmp_path):
    """逐 leader 一個檔：A 的取樣窗不會擋掉 B 的同窗取樣。"""
    other = "0x" + "cd" * 20
    pa, pb = series_path_for(tmp_path, _ADDR), series_path_for(tmp_path, other)
    append_samples(pa, [build_sample(_ADDR, _portfolio([(0, "1", "0")]), _at(0))])
    append_samples(pb, [build_sample(other, _portfolio([(0, "2", "0")]), _at(0))])
    assert len(load_records(pa)[0]) == 1 and len(load_records(pb)[0]) == 1


# ── 壞行容錯 ──────────────────────────────────────────────────────────

def test_corrupt_line_is_skipped_not_fatal(tmp_path, caplog):
    """⭐⭐ 一行壞掉只跳過那一行——整檔失效等於數月不可回填的資料一起不可用。

    同時釘住「被跳過的行有被計數」：靜靜跳過等於讓一個持續寫壞資料的 bug 永遠
    沒有人發現（工程原則 3）。
    """
    p = tmp_path / "s.jsonl"
    good1 = build_sample(_ADDR, _portfolio([(0, "100", "0")]), _at(0))
    good2 = build_sample(_ADDR, _portfolio([(_DAY_MS, "110", "10")]), _at(12))
    append_samples(p, [good1])
    with p.open("a") as fh:                  # 模擬半寫／外部工具截斷
        fh.write('{"schema": "filet.perf_series.v1", "address": tru\n')
    append_samples(p, [good2])

    recs, skipped = load_records(p)
    assert skipped == 1
    assert [r["bucket"] for r in recs] == ["2026-07-19T00", "2026-07-19T12"]


def test_structurally_invalid_line_is_skipped(tmp_path):
    """合法 JSON 但結構不符（缺 points／點不是三元組）也跳過——半殘的點會靜默
    算出錯誤報酬率，比少一筆資料危險。"""
    p = tmp_path / "s.jsonl"
    append_samples(p, [build_sample(_ADDR, _portfolio([(0, "100", "0")]), _at(0))])
    with p.open("a") as fh:
        fh.write(json.dumps({"address": _ADDR, "bucket": "x", "sampled_at": "y",
                             "points": [[0, "100"]]}) + "\n")   # 二元組
        fh.write(json.dumps({"address": _ADDR, "bucket": "z"}) + "\n")  # 缺 points
    recs, skipped = load_records(p)
    assert skipped == 2 and len(recs) == 1


def test_missing_file_is_empty_not_error(tmp_path):
    assert load_records(tmp_path / "nope.jsonl") == ([], 0)


# ── 拼接：pnl 重定基準與缺口斷段 ──────────────────────────────────────

def _two_overlapping_windows():
    """兩個重疊 12 小時的 perpDay 窗。第二個窗的 pnl **從該窗起點重新起算**
    （這正是 HL 的真實語意，也是重定基準要處理的東西）。

    帳戶淨值：100 → 110 → 121（連續兩段各 +10%）。
    窗 A（ts 0/12h/24h）pnl 累積：0 / 10 / 21
    窗 B（ts 12h/24h/36h）pnl 從 12h 重新起算：0 / 11 / 23.1
    """
    a = _portfolio([(0, "100", "0"), (_DAY_MS // 2, "110", "10"),
                    (_DAY_MS, "121", "21")])
    b = _portfolio([(_DAY_MS // 2, "110", "0"), (_DAY_MS, "121", "11"),
                    (_DAY_MS + _DAY_MS // 2, "133.1", "23.1")])
    return a, b


def test_splice_rebases_pnl_across_windows():
    """⭐⭐ 接縫處不得出現假跳空：重疊時間戳上的 pnl 差就是精確的偏移量。"""
    a, b = _two_overlapping_windows()
    recs = [build_sample(_ADDR, a, _at(0)), build_sample(_ADDR, b, _at(12))]
    segs = splice_segments(recs)

    assert len(segs) == 1                    # 有重疊 → 一整段
    pnls = [p for _, _, p in segs[0].points]
    # 窗 B 的 0/11/23.1 被重定成 10/21/33.1（偏移量 = 10 − 0），單調遞增、無跳空。
    assert pnls == [Decimal("0"), Decimal("10"), Decimal("21"), Decimal("33.1")]
    avs = [a_ for _, a_, _ in segs[0].points]
    assert avs[-1] == Decimal("133.1")


def _synthetic_equity(steps: int) -> list[Decimal]:
    """每 12 小時 +0.5% 的權益路徑（quantize 到 8 位，讓後續全是精確 Decimal 運算）。"""
    out = [Decimal("100")]
    for _ in range(steps):
        out.append((out[-1] * Decimal("1.005")).quantize(Decimal("0.00000001")))
    return out


def _collected_records(equity: list[Decimal]) -> list[dict]:
    """把權益路徑切成**實際採集會產生的樣子**：每 12 小時一筆、每筆是涵蓋 24 小時
    的 perpDay 窗（3 個點），且窗內 pnl **從該窗起點重新起算**（HL 的真實語意）。"""
    step_ms = _DAY_MS // 2
    recs = []
    for j in range(len(equity) - 2):
        pts = [(ts_k * step_ms, equity[ts_k], equity[ts_k] - equity[j])
               for ts_k in (j, j + 1, j + 2)]
        recs.append(build_sample(_ADDR, _portfolio(pts),
                                 _at(0) + timedelta(hours=12 * j)))
    return recs


def test_spliced_segment_is_consumable_by_leader_perf():
    """⭐⭐ 落檔格式的驗收條件：拼接結果餵給 leader_perf，算出的跨窗指標必須與
    「假如 HL 真的給了一個全解析度長窗」**逐位元組相同**。

    這是本模組存在理由的直接檢驗——perpAllTime 是降採樣的，我們自建序列就是為了
    得到那個拿不到的全解析度長窗。用「與 ground truth 相等」當斷言（而不是比對一個
    自己算的期望值），是因為前者連「拼接漏掉一個點」「接縫多算一段」都會抓到。

    ⭐ 序列刻意跨 31 天：leader_perf 對 < 30 天的資料**結構性不給** twr／max_drawdown
    這些鍵（分級揭露），30 天以下的測試只能驗到 cum_pnl，驗不到跨窗指標。
    """
    steps = 62                                    # 62 × 12h = 31 天
    equity = _synthetic_equity(steps)
    step_ms = _DAY_MS // 2

    seg = splice_segments(_collected_records(equity))
    assert len(seg) == 1
    perf = compute_window_performance(seg[0].portfolio_rows(), "perpAllTime")

    # ground truth：同一條權益路徑，但用「一個從頭到尾的長窗」表達（pnl 從原點累積）。
    truth_rows = _portfolio([(k * step_ms, equity[k], equity[k] - equity[0])
                             for k in range(len(equity))], period="perpAllTime")
    truth = compute_window_performance(truth_rows, "perpAllTime")

    assert perf["status"] == "ok" and truth["status"] == "ok"
    assert perf["sample_count"] == truth["sample_count"] == len(equity)
    assert perf["twr"] == truth["twr"]
    assert perf["max_drawdown"] == truth["max_drawdown"]
    assert perf["cum_pnl"] == truth["cum_pnl"]
    assert perf["twr"] > 0                        # 上漲路徑：方向也要對


def test_unrebased_splice_would_produce_a_false_twr():
    """⚠️ 反面證據：不重定基準會得到什麼。這個測試釘住的是**問題真的存在**——
    沒有它，`test_spliced_segment_is_consumable_by_leader_perf` 可能只是在驗一個
    根本不會出錯的東西。

    實測（本檔的合成路徑，31 天真實報酬 ≈ +36%）：直接串接窗內累積 pnl 得到
    **+1.3%**——低估約 27 倍。之所以不是「負的」而是「被壓平」，是因為每個接縫把
    「從原點累積」換成了「從該窗起點累積」，序列因此退化成一條幾乎沒有斜率的鋸齒。
    這比爆掉更危險：一個明顯是負的數字會有人去查，一個看起來平淡的 +1.3% 不會。
    """
    equity = _synthetic_equity(62)
    step_ms = _DAY_MS // 2
    naive: list[tuple[int, Decimal, Decimal]] = []
    for j in range(len(equity) - 2):
        for k in (j, j + 1, j + 2):
            if not naive or k * step_ms > naive[-1][0]:
                naive.append((k * step_ms, equity[k], equity[k] - equity[j]))
    naive_perf = compute_window_performance(
        _portfolio(naive, period="perpAllTime"), "perpAllTime")
    good = compute_window_performance(
        splice_segments(_collected_records(equity))[0].portfolio_rows(),
        "perpAllTime")
    # 低估一個數量級以上：拿掉重定基準，這條斷言立刻轉紅。
    assert naive_perf["twr"] < good["twr"] / 10
    assert good["twr"] > Decimal("0.3")            # 真實路徑 ≈ +36%


def test_splice_opens_new_segment_on_gap():
    """⚠️ 漏抓（無重疊）→ **開新分段**，不硬接。缺口期間「多少是損益、多少是出入金」
    無法從資料還原；硬接會得到一條每個數字都正常、整體卻是假的序列。"""
    a = _portfolio([(0, "100", "0"), (_DAY_MS, "110", "10")])
    far = 10 * _DAY_MS
    b = _portfolio([(far, "200", "0"), (far + _DAY_MS, "220", "20")])
    recs = [build_sample(_ADDR, a, _at(0)), build_sample(_ADDR, b, _at(0, day=29))]
    segs = splice_segments(recs)
    assert len(segs) == 2                    # 缺口是可見的證據，不是靜默的洞
    assert segs[0].last_ts_ms == _DAY_MS and segs[1].first_ts_ms == far


def test_splice_breaks_segment_when_overlap_disagrees_on_account_value():
    """同一時刻、同一帳戶的 accountValue 必須相同；不同 ⇒ 非同源 ⇒ 斷段。"""
    a = _portfolio([(0, "100", "0"), (_DAY_MS, "110", "10")])
    b = _portfolio([(_DAY_MS, "999", "0"), (2 * _DAY_MS, "1000", "1")])
    recs = [build_sample(_ADDR, a, _at(0)), build_sample(_ADDR, b, _at(12))]
    assert len(splice_segments(recs)) == 2


def test_splice_is_order_independent():
    """記錄依 sampled_at 排序，不靠檔案行序（append-only 檔的行序可能因補跑而亂）。"""
    a, b = _two_overlapping_windows()
    recs = [build_sample(_ADDR, b, _at(12)), build_sample(_ADDR, a, _at(0))]
    segs = splice_segments(recs)
    assert len(segs) == 1 and len(segs[0].points) == 4


def test_segment_covered_days():
    seg = Segment(((0, Decimal("1"), Decimal("0")),
                   (2 * _DAY_MS, Decimal("1"), Decimal("0"))))
    assert seg.covered_days == Decimal("2")


# ── CLI：白名單即單一來源、失敗隔離、冪等 ─────────────────────────────

def _leaders(tmp_path, addresses) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": [{"address": a, "name": a[:6]}
                                         for a in addresses]}))
    return str(p)


def test_cli_collects_whitelisted_leaders(tmp_path):
    """⭐ 白名單新增 leader → 採集自動涵蓋，不必同步任何 env（單一來源）。"""
    new_leader = "0x" + "ee" * 20
    env = {"FILET_LEADERS_PATH": _leaders(tmp_path, [_ADDR, new_leader]),
           "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(portfolio_fn=lambda a: _portfolio([(0, "100", "0")]),
             now=_at(0), env=env)
    assert ei.value.code == 0
    for addr in (_ADDR, new_leader):
        recs, _ = load_records(series_path_for(tmp_path, addr))
        assert len(recs) == 1 and recs[0]["address"] == addr


def test_cli_rerun_in_same_window_adds_nothing(tmp_path):
    """⭐ 重複執行不得產生重複點。"""
    env = {"FILET_LEADERS_PATH": _leaders(tmp_path, [_ADDR]),
           "FILET_DATA_DIR": str(tmp_path)}
    for hour in (0, 5):
        with pytest.raises(SystemExit):
            main(portfolio_fn=lambda a: _portfolio([(0, "100", "0")]),
                 now=_at(hour), env=env)
    recs, _ = load_records(series_path_for(tmp_path, _ADDR))
    assert len(recs) == 1


def test_cli_next_window_appends(tmp_path):
    env = {"FILET_LEADERS_PATH": _leaders(tmp_path, [_ADDR]),
           "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit):
        main(portfolio_fn=lambda a: _portfolio([(0, "100", "0")]), now=_at(0), env=env)
    with pytest.raises(SystemExit):
        main(portfolio_fn=lambda a: _portfolio([(_DAY_MS, "110", "10")]),
             now=_at(12), env=env)
    recs, _ = load_records(series_path_for(tmp_path, _ADDR))
    assert [r["bucket"] for r in recs] == ["2026-07-19T00", "2026-07-19T12"]


def test_cli_isolates_per_leader_failure_and_exits_1(tmp_path):
    """一個 leader 抓不到不連坐其他人；但必須 exit 1（這個窗的點永久消失了）。"""
    bad = "0x" + "ff" * 20
    env = {"FILET_LEADERS_PATH": _leaders(tmp_path, [_ADDR, bad]),
           "FILET_DATA_DIR": str(tmp_path)}

    def portfolio(addr):
        if addr == bad:
            raise ConnectionError("down")
        return _portfolio([(0, "100", "0")])

    with pytest.raises(SystemExit) as ei:
        main(portfolio_fn=portfolio, now=_at(0), env=env)
    assert ei.value.code == 1
    assert len(load_records(series_path_for(tmp_path, _ADDR))[0]) == 1
    assert load_records(series_path_for(tmp_path, bad))[0] == []


def test_cli_broken_whitelist_still_collects_env_targets(tmp_path):
    """白名單壞掉 → 大聲（exit 1）但**不中止**：中止會讓當次所有 leader 的點消失。"""
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    extra = "0x" + "33" * 20
    env = {"FILET_LEADERS_PATH": str(p), "FILET_LEADER_WATCHLIST": extra,
           "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(portfolio_fn=lambda a: _portfolio([(0, "100", "0")]), now=_at(0), env=env)
    assert ei.value.code == 1
    assert len(load_records(series_path_for(tmp_path, extra))[0]) == 1
