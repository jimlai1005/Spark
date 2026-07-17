"""key-service daemon 入口。
用法: FILET_KEYSVC_SOCK=/run/filet/keysvc.sock FILET_KEYS_DIR=/etc/filet/keys \\
      FILET_KEYSVC_ALLOWED_UIDS=1002 uv run python -m scripts.run_keysvc
（生產由 systemd 拉起，跑在 filet-engine；FILET_KEYSVC_ALLOWED_UIDS = filet-api 的 uid）"""
import os
import signal
import threading

USAGE = ("用法: FILET_KEYSVC_SOCK=.. FILET_KEYS_DIR=.. FILET_KEYSVC_ALLOWED_UIDS=<uid[,uid]> "
         "uv run python -m scripts.run_keysvc")


def main() -> None:
    sock = os.environ.get("FILET_KEYSVC_SOCK")
    keys_dir = os.environ.get("FILET_KEYS_DIR")
    uids_raw = os.environ.get("FILET_KEYSVC_ALLOWED_UIDS")
    if not sock or not keys_dir or not uids_raw:
        print(USAGE)
        missing = [k for k, v in [("FILET_KEYSVC_SOCK", sock), ("FILET_KEYS_DIR", keys_dir),
                                  ("FILET_KEYSVC_ALLOWED_UIDS", uids_raw)] if not v]
        print(f"缺少環境變數: {', '.join(missing)}")
        raise SystemExit(2)
    allowed = {int(x) for x in uids_raw.split(",") if x.strip()}
    # 網路依賴延後到 main 內 import（import 階段零副作用）
    from spark.keystore.envfile import EnvFileKeyStore
    from spark.keysvc.peercred import make_peercred_authorizer
    from spark.keysvc.server import serve_forever
    ks = EnvFileKeyStore(keys_dir)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve_forever(sock, ks, make_peercred_authorizer(allowed), stop)


if __name__ == "__main__":
    main()
