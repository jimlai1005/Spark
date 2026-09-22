"""Public API 入口。
用法: FILET_API_NETWORK=testnet FILET_BUILDER_ADDR=0x.. FILET_SIWE_DOMAIN=filet.example \
      FILET_SIWE_URI=https://filet.example FILET_API_DB=var/filet/api.db \
      FILET_KEYSVC_SOCK=/run/filet/keysvc.sock FILET_PENDING_PATH=var/filet/pending.json \
      [FILET_ADMIN_ADDRESSES=0x..,0x..] [FILET_API_PORT=8700] \
      [FILET_STRIPE_SECRET_KEY=sk_test_..（僅測試 key；三個 stripe env 一起設或都不設）] \
      [FILET_STRIPE_WEBHOOK_SECRET=whsec_..] [FILET_STRIPE_PRICE_ID=price_..] \
      uv run python -m scripts.run_api
（生產由 systemd 拉起、跑在 filet-api user；只綁 127.0.0.1，對外經反向代理 TLS。）"""
import os


def main() -> None:
    # 依賴延後到 main 內 import（import 階段零副作用，沿 run_keysvc 慣例）
    from spark.publicapi.config import ApiConfig
    try:
        cfg = ApiConfig.from_env()
    except ValueError as e:
        print(__doc__)
        print(f"設定錯誤: {e}")
        raise SystemExit(2) from e
    import threading
    import time

    import uvicorn

    from spark.keysvc.client import KeysvcClient
    from spark.publicapi import hl_explore
    from spark.publicapi.app import create_app
    from spark.publicapi.billing import StripeGateway
    from spark.publicapi.explore_publisher import ExplorePublisher
    from spark.publicapi.explore_scheduler import ExploreScheduler
    from spark.publicapi.explore_store import ExploreStore
    from spark.publicapi.hl import HLGateway
    from spark.publicapi.hl_budget import WeightLimiter
    from spark.publicapi.store import ApiStore
    billing = (StripeGateway(cfg.stripe_secret_key) if cfg.billing_enabled else None)
    # Task 1.4（spec §5）：同出口 IP 的權重帳本，filet-api 進程內單例，
    # 全域與 explore 子預算上限來自 cfg（環境變數可覆寫，見 config.py）。
    # Task 7.4a/c（2026-09-21 使用者裁決，fills 類別級飢餓修法）：`explore` 底下
    # 再切兩個保留額度子 scope——`explore_base`（state／portfolio／ledger）與
    # `explore_fills`（一頁 fills，保證每分鐘至少一頁）。`scope_parents` 讓子
    # scope 的預留同時計入父與全域、父暫停時子一併暫停（見 hl_budget.py 檔頭）。
    limiter = WeightLimiter(
        global_cap=cfg.hl_global_weight_cap,
        scope_caps={"explore": cfg.hl_explore_weight_cap,
                   "explore_base": cfg.hl_explore_base_weight_cap,
                   "explore_fills": cfg.hl_explore_fills_weight_cap},
        scope_parents={"explore_base": "explore", "explore_fills": "explore"})
    gateway = HLGateway(cfg.api_url, limiter=limiter)
    # Task 2.3（spec P2）：未設 FILET_EXPLORE_DB → 不建 store（None），沿
    # config.py 的 explore_db_path docstring——P2 階段尚無排程／發布消費它。
    explore_store = ExploreStore(cfg.explore_db_path) if cfg.explore_db_path else None
    app = create_app(cfg, ApiStore(cfg.db_path), KeysvcClient(cfg.keysvc_sock),
                     gateway, billing=billing, referral_lookup=gateway.referred_by,
                     hl_limiter=limiter, explore_store=explore_store)

    # Task 3.4（spec P3）：`explore_store` 存在時把 scheduler／publisher 接起來
    # ——建構本身零 IO、不落任何背景 thread（`create_app` 內絕不起 thread，見
    # `hl_explore.ExploreIndex` 類別檔頭）；只有 `EXPLORE_UPSTREAM_REFRESH=1`
    # （D8，__post_init__ 已保證此時 `explore_store` 一定非 None）才真的啟動
    # 排程 thread。`leaderboard_source_fn`／`excluded_fn` 沿用 `create_app` 內
    # 暴露的同一個 `_leaderboard_cache`／精選白名單閉包（`app.state`），不重建
    # 第二份 36MB stats-data 快取。
    stop_event = threading.Event()
    if explore_store is not None:
        publisher = ExplorePublisher(
            store=explore_store, index=app.state.explore_index,
            cfg=hl_explore.ExploreConfig.from_env(), now_fn=time.time,
            snapshot_path=cfg.explore_cache_path)
        scheduler = ExploreScheduler(
            store=explore_store, hl=gateway.scoped("explore"),
            # Task 7.4c：保留額度視圖——有 fills 待處理時基礎類別走 `hl_base`
            # （≤180）、fills 走 `hl_fills`（保證 120）；無 fills 待處理時基礎
            # 類別改走上面的父 scope `hl`（可借滿 300），見 explore_scheduler.py。
            hl_base=gateway.scoped("explore_base"),
            hl_fills=gateway.scoped("explore_fills"),
            leaderboard_source_fn=app.state.leaderboard_get,
            excluded_fn=app.state.explore_excluded_fn,
            cfg=hl_explore.ExploreConfig.from_env(), now_fn=time.time,
            sleep_fn=time.sleep, on_dirty=publisher.mark_dirty,
            on_tick=publisher.maybe_publish,
            # Task 5（2026-09-22，D-B／D-H）：增量週期不再是單一全域值——
            # `cfg.explore_fills_period_s`／`explore_fills_max_period_s` 只覆寫
            # `ExploreScheduler.fills_period_s_for` 逐地址估計的上下界（見該方法
            # 與 config.py 檔頭），也是 `app.py` 詳情頁補排條件讀的同一個下界值。
            fills_min_period_s=cfg.explore_fills_period_s,
            fills_max_period_s=cfg.explore_fills_max_period_s,
            # Task 8（2026-09-22，D-C／D-I）：輔助份額臨時加速，逾期自動恢復
            # 預設 9——解析與 fail-safe 見 ExploreScheduler._special_serve_ratio。
            special_serve_ratio=cfg.explore_special_serve_ratio,
            special_serve_ratio_until=cfg.explore_special_serve_ratio_until)
        app.state.explore_scheduler = scheduler
        app.state.explore_publisher = publisher
        if cfg.explore_upstream_refresh:
            threading.Thread(target=scheduler.run_forever, args=(stop_event,),
                             daemon=True, name="explore-scheduler").start()

    try:
        uvicorn.run(app, host="127.0.0.1",
                   port=int(os.environ.get("FILET_API_PORT", "8700")))
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
