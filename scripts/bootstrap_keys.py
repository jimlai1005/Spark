"""互動式把 main/agent 私鑰匯入 Keychain。私鑰用 getpass 讀取，絕不 echo、絕不 log。
用法: uv run python -m scripts.bootstrap_keys <account_id> <main|agent>"""
import getpass
import sys
from spark.keystore.keychain import MacKeychainBackend


def import_key_interactive(backend, account_id: str, role: str) -> None:
    pk = getpass.getpass(f"貼上 {account_id} 的 {role} 私鑰（不會顯示）: ")
    backend.import_key(account_id, role, pk.strip())
    print(f"已匯入 {account_id}:{role} 至 Keychain")  # 只印身分，不印 key


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("main", "agent"):
        print("用法: python -m scripts.bootstrap_keys <account_id> <main|agent>")
        raise SystemExit(2)
    import_key_interactive(MacKeychainBackend(), sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
