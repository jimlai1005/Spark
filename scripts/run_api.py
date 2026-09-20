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
    import uvicorn

    from spark.keysvc.client import KeysvcClient
    from spark.publicapi.app import create_app
    from spark.publicapi.billing import StripeGateway
    from spark.publicapi.explore_store import ExploreStore
    from spark.publicapi.hl import HLGateway
    from spark.publicapi.hl_budget import WeightLimiter
    from spark.publicapi.store import ApiStore
    billing = (StripeGateway(cfg.stripe_secret_key) if cfg.billing_enabled else None)
    # Task 1.4（spec §5）：同出口 IP 的權重帳本，filet-api 進程內單例，
    # 全域與 explore 子預算上限來自 cfg（環境變數可覆寫，見 config.py）。
    limiter = WeightLimiter(global_cap=cfg.hl_global_weight_cap,
                            scope_caps={"explore": cfg.hl_explore_weight_cap})
    gateway = HLGateway(cfg.api_url, limiter=limiter)
    # Task 2.3（spec P2）：未設 FILET_EXPLORE_DB → 不建 store（None），沿
    # config.py 的 explore_db_path docstring——P2 階段尚無排程／發布消費它。
    explore_store = ExploreStore(cfg.explore_db_path) if cfg.explore_db_path else None
    app = create_app(cfg, ApiStore(cfg.db_path), KeysvcClient(cfg.keysvc_sock),
                     gateway, billing=billing, referral_lookup=gateway.referred_by,
                     hl_limiter=limiter, explore_store=explore_store)
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("FILET_API_PORT", "8700")))


if __name__ == "__main__":
    main()
