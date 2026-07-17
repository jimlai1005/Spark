"""tests/test_keysvc_server.py
handle_generate 純函式：request + keystore → response。私鑰不外洩、O_EXCL 已存在、
account_id 校驗（縱深防禦）。socket 接線在 Task 4。"""
from spark.keysvc.server import handle_generate
from spark.keysvc.protocol import GenerateRequest
from spark.keystore.envfile import EnvFileKeyStore


def test_generate_writes_key_returns_address(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    resp = handle_generate(GenerateRequest("alice"), ks)
    assert resp.ok and resp.error is None
    # 回應地址 == keystore 落檔的 agent key 對應地址
    assert ks.get_agent_signer("alice").address == resp.agent_address


def test_generate_private_key_never_in_response(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    resp = handle_generate(GenerateRequest("alice"), ks)
    # 讀出落檔私鑰，確認它不在回應的任何欄位
    pk = (tmp_path / "alice" / "agent.key").read_text().strip()
    blob = f"{resp.ok}{resp.agent_address}{resp.error}"
    assert pk not in blob and pk[2:] not in blob


def test_generate_already_exists_returns_error_not_overwrite(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    first = handle_generate(GenerateRequest("alice"), ks)
    second = handle_generate(GenerateRequest("alice"), ks)  # O_EXCL → 錯誤，不覆寫
    assert second.ok is False and "已" in (second.error or "")
    assert ks.get_agent_signer("alice").address == first.agent_address  # 未被換掉


def test_generate_bad_account_id_rejected(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    resp = handle_generate(GenerateRequest("../evil"), ks)
    assert resp.ok is False and (tmp_path / "..").resolve().joinpath("evil").exists() is False
