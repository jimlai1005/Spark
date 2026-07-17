"""Public API 入口。
用法: FILET_API_NETWORK=testnet FILET_BUILDER_ADDR=0x.. FILET_SIWE_DOMAIN=filet.example \
      FILET_SIWE_URI=https://filet.example FILET_API_DB=var/filet/api.db \
      FILET_KEYSVC_SOCK=/run/filet/keysvc.sock FILET_PENDING_PATH=var/filet/pending.json \
      [FILET_ADMIN_ADDRESSES=0x..,0x..] [FILET_API_PORT=8700] \
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
    from spark.publicapi.hl import HLGateway
    from spark.publicapi.store import ApiStore
    app = create_app(cfg, ApiStore(cfg.db_path), KeysvcClient(cfg.keysvc_sock),
                     HLGateway(cfg.api_url))
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("FILET_API_PORT", "8700")))


if __name__ == "__main__":
    main()
