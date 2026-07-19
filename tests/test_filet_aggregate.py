"""tests/test_filet_aggregate.py
跨 follower 日報匯總：北極星（builder 層級查一次）不得跨 follower 加總；
per-follower summary 只做 fills 衍生指標（fills 數、taker_share），不查 accrued。
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import scripts.filet_daily_report as fdr
from spark.filet.followers import FollowerRef
from spark.filet.aggregate import (
    FollowerSummary,
    aggregate,
    render_aggregate,
    builder_fee_delta,
    collect_follower_summary,
)


def _ref(aid, net="mainnet"):
    return FollowerRef(aid, "0x" + "a" * 40, "0x" + "b" * 40, net)


def test_north_star_is_builder_level_not_summed():
    # 北極星＝傳入的 builder 層級日增量，與 per-follower summary 無關、不加總
    summaries = [FollowerSummary(_ref("alice"), fills=8, taker_share=Decimal("0.2"),
                                 error=None),
                 FollowerSummary(_ref("bob", "testnet"), fills=3,
                                 taker_share=Decimal("0.1"), error=None)]
    agg = aggregate(date(2026, 7, 17), summaries,
                    north_star_fee_delta=Decimal("1.84"))
    assert agg.north_star_fee_delta == Decimal("1.84")   # 查一次的值，非相加
    assert agg.follower_count == 2 and agg.ok_count == 2


def test_failed_follower_excluded_from_ok_but_listed():
    summaries = [FollowerSummary(_ref("alice"), fills=8, taker_share=Decimal("0.2"),
                                 error=None),
                 FollowerSummary(_ref("bob"), fills=0, taker_share=Decimal("0"),
                                 error="API timeout")]
    agg = aggregate(date(2026, 7, 17), summaries, north_star_fee_delta=Decimal("1.0"))
    assert agg.ok_count == 1 and agg.follower_count == 2
    assert any("API timeout" in (s.error or "") for s in agg.summaries)


def test_builder_fee_delta_single_query():
    # builder_fee_delta(today, prev) = today - prev（純函式，查一次的差）
    assert builder_fee_delta(Decimal("5.5"), Decimal("3.66")) == Decimal("1.84")


def test_render_has_single_daily_northstar_line():
    agg = aggregate(date(2026, 7, 17), [], north_star_fee_delta=Decimal("0"))
    out = render_aggregate(agg)
    assert "單日 builder fee 增量" in out   # opus m6：正名為單日，非「30日日增」


def test_empty_no_crash():
    agg = aggregate(date(2026, 7, 17), [], north_star_fee_delta=Decimal("0"))
    assert agg.north_star_fee_delta == Decimal("0") and agg.follower_count == 0
    # render 也不得炸
    render_aggregate(agg)


# --- collect_follower_summary：三案（正常算 taker_share、例外入 summary、空 fills） ---

class _FakeFill:
    def __init__(self, sz, px, crossed, builder_fee="0"):
        self.sz = Decimal(sz)
        self.px = Decimal(px)
        self.crossed = crossed
        self.builder_fee = Decimal(builder_fee)


class _FakeAdapter:
    def __init__(self, fills=None, raises=None):
        self._fills = fills if fills is not None else []
        self._raises = raises

    def get_user_fills(self, address, start, end):
        if self._raises is not None:
            raise self._raises
        return self._fills


_START = datetime(2026, 7, 17, tzinfo=timezone.utc)
_END = datetime(2026, 7, 17, 23, 59, tzinfo=timezone.utc)


def test_collect_follower_summary_computes_taker_share():
    # 手算：taker(crossed) 名目 = 2*100 + 1*50 = 250；總名目 = 250 + 3*10 = 280
    fills = [
        _FakeFill("2", "100", True),
        _FakeFill("1", "50", True),
        _FakeFill("3", "10", False),
    ]
    adapter = _FakeAdapter(fills=fills)
    s = collect_follower_summary(_ref("alice"), adapter, _START, _END)
    assert s.error is None
    assert s.fills == 3
    assert s.taker_share == Decimal("250") / Decimal("280")


def test_collect_follower_summary_error_captured_not_raised():
    adapter = _FakeAdapter(raises=RuntimeError("boom: connection reset"))
    s = collect_follower_summary(_ref("bob"), adapter, _START, _END)
    assert s.error is not None and "boom" in s.error
    assert s.fills == 0
    assert s.taker_share == Decimal("0")


def test_collect_follower_summary_empty_fills():
    adapter = _FakeAdapter(fills=[])
    s = collect_follower_summary(_ref("carol"), adapter, _START, _END)
    assert s.error is None
    assert s.fills == 0
    assert s.taker_share == Decimal("0")
    assert s.notional == Decimal("0")
    assert s.builder_fee == Decimal("0")


def test_collect_follower_summary_sums_notional_and_builder_fee():
    # 手算：notional = 2*100 + 1*50 + 3*10 = 280；builder_fee = 0.02 + 0.01 + 0（缺鍵視為 0）
    fills = [
        _FakeFill("2", "100", True, builder_fee="0.02"),
        _FakeFill("1", "50", True, builder_fee="0.01"),
        _FakeFill("3", "10", False),  # builder_fee 預設 "0"
    ]
    adapter = _FakeAdapter(fills=fills)
    s = collect_follower_summary(_ref("dave"), adapter, _START, _END)
    assert s.error is None
    assert s.notional == Decimal("280")
    assert s.builder_fee == Decimal("0.03")


def test_collect_follower_summary_error_branch_has_zero_notional_and_fee():
    adapter = _FakeAdapter(raises=RuntimeError("boom: connection reset"))
    s = collect_follower_summary(_ref("eve"), adapter, _START, _END)
    assert s.error is not None and "boom" in s.error
    assert s.notional == Decimal("0")
    assert s.builder_fee == Decimal("0")


def test_north_star_unaffected_by_summaries_builder_fee():
    # 紅線釘樁：即使 summaries 的 builder_fee 加總遠大於／小於 north_star，
    # aggregate() 回傳的 north_star_fee_delta 必須原封不動——不得由 summaries 推導。
    summaries_low = [FollowerSummary(_ref("alice"), fills=1, taker_share=Decimal("1"),
                                      notional=Decimal("100"), builder_fee=Decimal("0.001"))]
    summaries_high = [FollowerSummary(_ref("alice"), fills=1, taker_share=Decimal("1"),
                                       notional=Decimal("999999"), builder_fee=Decimal("999")),
                       FollowerSummary(_ref("bob"), fills=1, taker_share=Decimal("1"),
                                       notional=Decimal("999999"), builder_fee=Decimal("999"))]
    agg_low = aggregate(date(2026, 7, 17), summaries_low, north_star_fee_delta=Decimal("1.84"))
    agg_high = aggregate(date(2026, 7, 17), summaries_high, north_star_fee_delta=Decimal("1.84"))
    assert agg_low.north_star_fee_delta == Decimal("1.84")
    assert agg_high.north_star_fee_delta == Decimal("1.84")
    assert agg_low.north_star_fee_delta == agg_high.north_star_fee_delta


# --- filet_daily_report.main() wiring 整合測試 ---


class _CountingAdapter:
    """記數 query_builder_accrued 呼叫；get_user_fills 回可設定的空/非空 fills。"""

    def __init__(self, accrued, fills=None):
        self._accrued = accrued
        self._fills = fills if fills is not None else []
        self.accrued_calls = []          # 每次呼叫記錄 builder 參數
        self.fills_calls = []            # 每次呼叫記錄 address 參數

    def query_builder_accrued(self, builder):
        self.accrued_calls.append(builder)
        return self._accrued

    def get_user_fills(self, address, start, end):
        self.fills_calls.append(address)
        return list(self._fills)


def _ref_full(aid, builder, net="mainnet"):
    return FollowerRef(aid, "0x" + "a" * 40, builder, net)


def test_wiring_shared_builder_queried_once(tmp_path, monkeypatch):
    # 快照/報表目錄導向 tmp_path，避免碰 repo var/
    monkeypatch.setattr(fdr, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(fdr, "REPORTS_DIR", tmp_path / "reports")

    builder_mixed = "0x" + "B" * 40      # checksum 大小寫
    builder_lower = "0x" + "b" * 40      # 同一位址、小寫
    refs = [
        _ref_full("alice", builder_mixed, "mainnet"),
        _ref_full("bob", builder_lower, "mainnet"),   # 與 alice 同一 builder（大小寫不同）
        _ref_full("carol", "0x" + "c" * 40, "testnet"),  # testnet 不計入北極星
    ]

    # 快照無檔（tmp_path 無 snap.json）→ prev=0；今日 accrued=5.5 → 北極星 delta=5.5（單次）
    adapter = _CountingAdapter(accrued=Decimal("5.5"), fills=[])

    def adapter_for(_network):
        return adapter

    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    report = fdr.generate_report(refs, [], adapter_for, now)

    # ⭐ 共用 builder（含大小寫不同）只查一次
    assert adapter.accrued_calls == ["0x" + "b" * 40]
    # 北極星＝單次 delta，非兩個 follower 相加
    assert report.north_star_fee_delta == Decimal("5.5")
    # 三個 follower 都在明細（含 testnet）
    assert report.follower_count == 3
    # per-follower fills 對三個 follower 位址都查了（含 testnet 的活動歸屬）
    assert len(adapter.fills_calls) == 3
    # 快照寫入單一 builder 位址（小寫）
    snap = json.loads((tmp_path / "snap.json").read_text())
    assert list(snap["builders"].keys()) == ["0x" + "b" * 40]
    assert snap["builders"]["0x" + "b" * 40] == "5.5"


def test_wiring_testnet_only_no_accrued_query(tmp_path, monkeypatch):
    monkeypatch.setattr(fdr, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(fdr, "REPORTS_DIR", tmp_path / "reports")

    refs = [_ref_full("carol", "0x" + "c" * 40, "testnet")]
    adapter = _CountingAdapter(accrued=Decimal("9.9"), fills=[])
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    report = fdr.generate_report(refs, [], lambda _n: adapter, now)

    # testnet builder 完全不觸發 accrued 查詢
    assert adapter.accrued_calls == []
    assert report.north_star_fee_delta == Decimal("0")
    # 快照無 mainnet builder → 不寫檔（維持無檔=0 語意）
    assert not (tmp_path / "snap.json").exists()


def test_wiring_north_star_uses_prev_snapshot(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(
        {"date": "2026-07-16", "builders": {"0x" + "b" * 40: "3.66"}}))
    monkeypatch.setattr(fdr, "SNAPSHOT_PATH", snap_path)
    monkeypatch.setattr(fdr, "REPORTS_DIR", tmp_path / "reports")

    refs = [_ref_full("alice", "0x" + "B" * 40, "mainnet")]
    adapter = _CountingAdapter(accrued=Decimal("5.5"), fills=[])
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    report = fdr.generate_report(refs, [], lambda _n: adapter, now)

    # 北極星＝今日 5.5 - 昨日快照 3.66 = 1.84（查一次的差）
    assert adapter.accrued_calls == ["0x" + "b" * 40]
    assert report.north_star_fee_delta == Decimal("1.84")


# ── ⭐ 換 leader 鏈路對帳：「已寫入但未被套用」（opus 審查 I2）─────────────
# 這條鏈路橫跨兩個進程、兩套權限、兩個目錄，而它的可用性失敗是**靜默**的：API 驗完
# 章、寫檔、回客戶 200「下一個 cycle 生效」，引擎那邊卻可能根本讀不到那個檔（路徑
# 不通、group 權限錯、引擎沒在跑）。三種原因症狀完全一樣——什麼都不會發生，兩邊的
# log 都正常，客戶以為換好了。日報是唯一會讓它浮出來的地方。

_NOW_S = 1_800_000_000.0


def _change_record(account_id, nonce, age_s):
    """一筆落地了 age_s 秒的記錄（issued_at 由 _NOW_S 回推，與判定端同源同單位）。"""
    from spark.filet.leader_change import build_leader_change_record
    issued_at = datetime.fromtimestamp(_NOW_S - age_s, timezone.utc).isoformat()
    return build_leader_change_record(
        account_id=account_id, leader_address="0x" + "d4" * 20, nonce=nonce,
        issued_at=issued_at, signature="0xsig", message="msg")


def _write_changes(tmp_path, *records):
    path = tmp_path / "leader_changes.json"
    path.write_text(json.dumps({"changes": list(records)}))
    return path


def _write_ledger(tmp_path, account_id, redeemed):
    from spark.filet.leader_change_apply import Ledger, save_ledger
    path = tmp_path / account_id / "ledger.json"
    save_ledger(path, Ledger(redeemed=redeemed, applied=None))
    return path


def test_stale_change_never_applied_is_reported(tmp_path):
    """⭐ 記錄逾時未套用 → 告警。這捕捉「客戶簽了、API 收了、引擎從沒套用」整類失敗。"""
    changes = _write_changes(tmp_path, _change_record("alice", "n1", age_s=3600))
    _write_ledger(tmp_path, "alice", {})            # 帳本存在但沒有這顆 nonce

    warns = fdr.leader_change_warnings(
        [_ref("alice")], _NOW_S, changes_path=changes,
        ledger_for=lambda a: tmp_path / a / "ledger.json")

    assert len(warns) == 1
    assert "未被套用" in warns[0] and "alice" in warns[0]


def test_applied_change_is_not_reported(tmp_path):
    """已被套用（帳本有對應的已兌現 nonce）→ 不告警。對帳工具最糟的失效是狼來了。"""
    changes = _write_changes(tmp_path, _change_record("alice", "n1", age_s=3600))
    _write_ledger(tmp_path, "alice", {"n1": "0x" + "d4" * 20})

    assert fdr.leader_change_warnings(
        [_ref("alice")], _NOW_S, changes_path=changes,
        ledger_for=lambda a: tmp_path / a / "ledger.json") == []


def test_fresh_change_is_not_reported(tmp_path):
    """記錄還很新（未逾時）→ 不告警：引擎可能只是還沒跑到下一輪。

    門檻 30 分鐘遠大於 cycle 間隔與任何合理的重啟窗，所以逾時不可能是「還沒輪到」。
    """
    fresh = fdr.LEADER_CHANGE_STALE_S - 60
    changes = _write_changes(tmp_path, _change_record("alice", "n1", age_s=fresh))
    _write_ledger(tmp_path, "alice", {})            # 尚未套用，但還在容忍窗內

    assert fdr.leader_change_warnings(
        [_ref("alice")], _NOW_S, changes_path=changes,
        ledger_for=lambda a: tmp_path / a / "ledger.json") == []


def test_missing_ledger_for_a_stale_change_is_reported(tmp_path):
    """帳本檔根本不存在（引擎從沒跑過／狀態根不對）→ 逾時記錄照樣要報。

    load_ledger 對缺檔回空帳本，於是 nonce 必不在 redeemed 裡 → 落在同一條告警上。
    """
    changes = _write_changes(tmp_path, _change_record("alice", "n1", age_s=3600))

    warns = fdr.leader_change_warnings(
        [_ref("alice")], _NOW_S, changes_path=changes,
        ledger_for=lambda a: tmp_path / a / "nonexistent.json")
    assert len(warns) == 1 and "未被套用" in warns[0]


def test_change_for_an_unactivated_account_is_not_reported(tmp_path):
    """manifest 沒有這個帳號＝尚未 activate，引擎本來就不會套用它——不是鏈路故障。"""
    changes = _write_changes(tmp_path, _change_record("nobody", "n1", age_s=3600))

    assert fdr.leader_change_warnings(
        [_ref("alice")], _NOW_S, changes_path=changes,
        ledger_for=lambda a: tmp_path / a / "ledger.json") == []


def test_unresolvable_exchange_dir_is_itself_the_alert(tmp_path, monkeypatch):
    """⭐ 交換目錄沒設好 ⇒ 這條鏈路必定不通。那**就是**要報的事，不是略過的理由。"""
    monkeypatch.delenv("FILET_EXCHANGE_DIR", raising=False)

    warns = fdr.leader_change_warnings([_ref("alice")], _NOW_S)
    assert len(warns) == 1 and "FILET_EXCHANGE_DIR" in warns[0]


def test_ledger_path_derives_from_the_engine_relpath(monkeypatch):
    """帳本落點由引擎寫入時用的同一個常數推導，不另拼字串——兩份定義漂移會讓對帳
    工具找不到帳本而把**每一筆**記錄都報成未套用（狼來了）。"""
    from spark.filet.leader_change_apply import LEDGER_RELPATH

    monkeypatch.setenv("FILET_STATE_BASE", "/opt/filet/state")
    assert fdr.ledger_path_for("alice") == Path("/opt/filet/state/alice") / LEDGER_RELPATH
