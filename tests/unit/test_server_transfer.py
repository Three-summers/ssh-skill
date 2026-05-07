import pytest

import ssh_server_transfer as transfer


pytestmark = pytest.mark.unit


class FakeResult:
    def __init__(self, success=True, stdout="OK\n", stderr=""):
        self.success = success
        self.stdout = stdout
        self.stderr = stderr


class FakeClient:
    def __init__(self, result=None):
        self.result = result or FakeResult()

    def execute(self, command):
        return self.result


def test_validate_transfer_creates_clients_from_aliases(monkeypatch):
    aliases = []

    def fake_create(alias):
        aliases.append(alias)
        return FakeClient()

    monkeypatch.setattr(transfer, "create_ssh_client", fake_create)

    assert transfer.validate_transfer("source-host", "dest-host") == []
    assert aliases == ["source-host", "dest-host"]


def test_validate_transfer_redacts_sensitive_exception_text(monkeypatch):
    def fake_create(alias):
        raise RuntimeError("password=secret-token metadata={'password': 'secret-token'}")

    monkeypatch.setattr(transfer, "create_ssh_client", fake_create)

    issues = transfer.validate_transfer("source-host", "dest-host")

    joined = "\n".join(issues).lower()
    assert len(issues) == 2
    assert "secret-token" not in joined
    assert "metadata" not in joined
    assert "sensitive connection details redacted" in joined


def test_server_transfer_stream_mode_delegates_with_aliases(monkeypatch):
    calls = []

    monkeypatch.setattr(transfer, "validate_transfer", lambda source, dest: [])

    def fake_stream(source_alias, source_path, dest_alias, dest_path, progress):
        calls.append((source_alias, source_path, dest_alias, dest_path, progress))
        return {"success": True, "mode": "stream"}

    monkeypatch.setattr(transfer, "stream_transfer", fake_stream)

    result = transfer.server_transfer(
        "source-host",
        "/var/log/app.log",
        "dest-host",
        "/backup/app.log",
        mode="stream",
        progress=False,
    )

    assert result == {"success": True, "mode": "stream"}
    assert calls == [
        ("source-host", "/var/log/app.log", "dest-host", "/backup/app.log", False)
    ]
