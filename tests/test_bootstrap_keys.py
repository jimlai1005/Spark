import pytest
from unittest.mock import patch
from scripts.bootstrap_keys import import_key_interactive, main


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


def test_main_rejects_wrong_argc():
    with patch("sys.argv", ["bootstrap_keys", "acct1"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 2


def test_main_rejects_invalid_role():
    with patch("sys.argv", ["bootstrap_keys", "acct1", "owner"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 2
