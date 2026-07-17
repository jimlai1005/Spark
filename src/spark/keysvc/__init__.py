"""src/spark/keysvc/__init__.py
key-service：不對外的本機 daemon，唯一操作 generate(account_id)——生成 agent keypair、
寫入引擎 keystore、只回 agent 地址。agent 私鑰永不出此進程（非託管拆分雙層的金鑰側）。"""
