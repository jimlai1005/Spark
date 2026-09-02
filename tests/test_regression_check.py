"""`scripts/filet_regression_check.py` 的純函式離線測試（2026-09-02，plan T4）。

只測不連網、不需要 SSH／HTTP 的部分：sitemap host 解析、systemd 有效環境變數解析、
journal 'Finished' 時間戳解析、磁碟使用率解析、公開 API payload 的欄位/空值判斷，
以及「前端路由清單與 `web/src/app/**/page.tsx` 的一致性」——這條刻意用真實的
`web/src/app` 目錄跑，讓「新增頁面沒被本腳本歸類」在離線測試就會 fail，不必等到
跑 --http 才發現漏了一條路由檢查。
"""
from datetime import datetime, timezone

from scripts.filet_regression_check import (
    KNOWN_FRONTEND_ROUTES,
    REQUIRED_STRATEGY_FIELDS,
    WEB_APP_DIR,
    discover_page_routes,
    explore_is_empty,
    parse_cat_env,
    parse_disk_pct,
    parse_finished_timestamp,
    parse_freshness_output,
    parse_routed_volume,
    parse_show_env_value,
    sitemap_hosts,
    sitemap_locs,
    strategies_missing_fields,
)


class TestSitemapHosts:
    def test_locs_extracted_in_order(self):
        xml = ("<urlset><url><loc>https://trade.filet.app/</loc></url>"
              "<url><loc>https://trade.filet.app/strategies</loc></url></urlset>")
        assert sitemap_locs(xml) == [
            "https://trade.filet.app/", "https://trade.filet.app/strategies"]

    def test_single_consistent_host(self):
        xml = ("<urlset><url><loc>https://trade.filet.app/</loc></url>"
              "<url><loc>https://trade.filet.app/explore</loc></url></urlset>")
        assert sitemap_hosts(xml) == {"trade.filet.app"}

    def test_stale_build_leaves_old_host_behind(self):
        """NEXT_PUBLIC_SITE_ORIGIN 換了但沒重 build 的地雷：sitemap 裡還是舊網域。"""
        xml = "<urlset><url><loc>https://app.filet.trade/</loc></url></urlset>"
        assert sitemap_hosts(xml) == {"app.filet.trade"}

    def test_empty_sitemap_yields_empty_set(self):
        assert sitemap_hosts("<urlset></urlset>") == set()


class TestSystemdEnvParsing:
    def test_show_value_extracts_key(self):
        out = ("FILET_API_TG_BOT_TOKEN=123:abc FILET_API_NETWORK=mainnet "
              "FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json")
        assert parse_show_env_value(out, "FILET_API_NETWORK") == "mainnet"
        assert (parse_show_env_value(out, "FILET_LEADERS_PATH")
               == "/opt/filet/spark/var/filet/leaders.json")

    def test_show_value_missing_key_is_none(self):
        assert parse_show_env_value("FOO=bar", "FILET_API_NETWORK") is None

    def test_cat_env_reads_dropin_merged_output(self):
        """`systemctl cat` 合併輸出：drop-in 補的變數要在 grep 主檔看不到的情況下
        仍被抓到（2026-09-02 實測 filet-api 的 FILET_LEADERS_PATH 就是這樣宣告的）。
        """
        out = (
            "# /etc/systemd/system/filet-api.service\n"
            "[Service]\n"
            "Environment=FILET_API_PORT=8700\n"
            "\n"
            "# /etc/systemd/system/filet-api.service.d/leaders-path.conf\n"
            "[Service]\n"
            "Environment=FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json\n"
        )
        assert (parse_cat_env(out, "FILET_LEADERS_PATH")
               == "/opt/filet/spark/var/filet/leaders.json")

    def test_cat_env_duplicate_key_takes_last_declaration(self):
        out = ("Environment=FILET_LEADERS_PATH=/old/path\n"
              "Environment=FILET_LEADERS_PATH=/new/path\n")
        assert parse_cat_env(out, "FILET_LEADERS_PATH") == "/new/path"

    def test_cat_env_missing_key_is_none(self):
        assert parse_cat_env("Environment=FOO=bar\n", "FILET_LEADERS_PATH") is None


class TestJournalFinishedTimestamp:
    def test_parses_short_iso_line(self):
        line = ("2026-09-02T00:10:31+0000 ip-172-26-5-250 systemd[1]: "
               "Finished Filet daily leaderboard snapshots.")
        got = parse_finished_timestamp(line)
        assert got == datetime(2026, 9, 2, 0, 10, 31, tzinfo=timezone.utc)

    def test_empty_line_is_none(self):
        assert parse_finished_timestamp("") is None
        assert parse_finished_timestamp("   ") is None

    def test_garbage_line_is_none(self):
        assert parse_finished_timestamp("not a timestamp at all") is None


class TestFreshnessOutput:
    def test_finished_within_window(self):
        # journalctl 一行 + `date -u +%s` 一行（伺服器當下時間戳）
        output = ("2026-09-02T00:10:31+0000 host systemd[1]: Finished X.\n"
                  f"{int(datetime(2026, 9, 2, 1, 0, 0, tzinfo=timezone.utc).timestamp())}")
        finished_at, server_now = parse_freshness_output(output)
        assert finished_at == datetime(2026, 9, 2, 0, 10, 31, tzinfo=timezone.utc)
        assert server_now == datetime(2026, 9, 2, 1, 0, 0, tzinfo=timezone.utc)
        hours = (server_now - finished_at).total_seconds() / 3600
        assert abs(hours - (49 * 60 + 29) / 3600) < 1e-6

    def test_no_finished_line_in_window_returns_none(self):
        """兩天內 grep 不到任何 Finished：只剩 `date -u +%s` 一行。"""
        now_epoch = int(datetime(2026, 9, 2, 1, 0, 0, tzinfo=timezone.utc).timestamp())
        finished_at, server_now = parse_freshness_output(f"{now_epoch}")
        assert finished_at is None
        assert server_now == datetime(2026, 9, 2, 1, 0, 0, tzinfo=timezone.utc)

    def test_completely_empty_output_is_none_none(self):
        assert parse_freshness_output("") == (None, None)

    def test_unparsable_epoch_is_none_none(self):
        assert parse_freshness_output("not-a-number") == (None, None)


class TestDiskPct:
    def test_parses_trailing_percent_line(self):
        assert parse_disk_pct("Use%\n 13%") == 13

    def test_ignores_header_only_takes_last_line(self):
        assert parse_disk_pct("Use%\n84%\n") == 84

    def test_unparsable_is_none(self):
        assert parse_disk_pct("garbage") is None

    def test_empty_is_none(self):
        assert parse_disk_pct("") is None


class TestPublicApiPayloads:
    def test_routed_volume_parses_decimal_string(self):
        from decimal import Decimal
        assert parse_routed_volume({"routed_volume_usd_total": "157455.045000"}) == Decimal(
            "157455.045000")

    def test_routed_volume_null_is_none(self):
        assert parse_routed_volume({"routed_volume_usd_total": None}) is None

    def test_routed_volume_missing_key_is_none(self):
        assert parse_routed_volume({}) is None

    def test_routed_volume_garbage_is_none(self):
        assert parse_routed_volume({"routed_volume_usd_total": "not-a-number"}) is None

    def test_explore_empty_rows_is_empty(self):
        assert explore_is_empty({"rows": [], "building": True}) is True

    def test_explore_nonempty_rows_is_not_empty(self):
        assert explore_is_empty({"rows": [{"address": "0x1"}]}) is False

    def test_explore_missing_rows_key_is_empty(self):
        assert explore_is_empty({}) is True

    def test_strategies_missing_fields_detects_gap(self):
        entry = {k: "x" for k in REQUIRED_STRATEGY_FIELDS if k != "as_of"}
        assert strategies_missing_fields(entry) == ["as_of"]

    def test_strategies_missing_fields_empty_when_complete(self):
        entry = dict.fromkeys(REQUIRED_STRATEGY_FIELDS, "x")
        assert strategies_missing_fields(entry) == []


class TestDiscoverPageRoutes:
    def test_root_and_nested_and_dynamic(self, tmp_path):
        (tmp_path / "page.tsx").write_text("x")
        (tmp_path / "admin").mkdir()
        (tmp_path / "admin" / "page.tsx").write_text("x")
        (tmp_path / "strategies" / "[slug]").mkdir(parents=True)
        (tmp_path / "strategies" / "[slug]" / "page.tsx").write_text("x")
        got = discover_page_routes(tmp_path)
        assert got == {"/", "/admin", "/strategies/[slug]"}

    def test_missing_dir_returns_empty_set(self, tmp_path):
        assert discover_page_routes(tmp_path / "does-not-exist") == set()

    def test_new_page_not_covered_by_known_routes_is_detectable(self, tmp_path):
        """示範性測試：模擬「加了新頁面但沒把它歸進任何一類」，證明一致性檢查
        真的會抓到（不是寫假的）。"""
        (tmp_path / "brand-new-feature").mkdir()
        (tmp_path / "brand-new-feature" / "page.tsx").write_text("x")
        got = discover_page_routes(tmp_path)
        assert not got.issubset(KNOWN_FRONTEND_ROUTES)

    def test_real_repo_routes_are_all_known(self):
        """對真實 `web/src/app` 跑：目前每一個 page.tsx 衍生的路由都必須落在
        `KNOWN_FRONTEND_ROUTES`（現行檢查集合 ∪ 動態路由 ∪ redirect 路由 ∪ 已知
        legacy 舊頁）之內——之後有人加新頁面卻忘了把它歸類，這條測試會轉紅。
        """
        real_routes = discover_page_routes(WEB_APP_DIR)
        assert real_routes, "web/src/app 底下應該至少找得到 page.tsx"
        unknown = real_routes - KNOWN_FRONTEND_ROUTES
        assert not unknown, (
            f"新頁面 {unknown} 沒有被 filet_regression_check 歸類——"
            "要嘛加進 FRONTEND_ROUTES 的 200 檢查，要嘛明確歸進 "
            "DYNAMIC_ROUTE_TEMPLATES/REDIRECT_ROUTES/LEGACY_UNLISTED_ROUTES")
