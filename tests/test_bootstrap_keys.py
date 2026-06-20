from scripts.bootstrap_keys import import_key_interactive


class RecordingBackend:
    def __init__(self):
        self.saved = {}
    def import_key(self, account_id, role, private_key):
        self.saved[(account_id, role)] = private_key


def test_import_does_not_print_key(monkeypatch, capsys):
    backend = RecordingBackend()
    monkeypatch.setattr("scripts.bootstrap_keys.getpass.getpass",
                        lambda prompt="": "0xdeadbeef")
    import_key_interactive(backend, account_id="acct1", role="agent")
    out = capsys.readouterr().out
    assert "0xdeadbeef" not in out               # 絕不 echo 私鑰
    assert backend.saved[("acct1", "agent")] == "0xdeadbeef"
