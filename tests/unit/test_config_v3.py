import sys
import types
from pathlib import Path

import pytest

from config_v3 import SSHConfigLoaderV3


pytestmark = pytest.mark.unit


def write_config(tmp_path):
    config_path = tmp_path / "config"
    config_path.write_text(
        """
# description: Raspberry Pi test host
# environment: lab
# tags: pi, local
# location: desk
# password: redacted-password
Host pi-test
    HostName 192.0.2.10
    User pi
    Port 2222
    ProxyJump jump-host
    ForwardAgent yes

Host key-test
    HostName key.example.test
    User deploy
    IdentityFile ~/.ssh/id_ed25519
    ForwardAgent no
""".lstrip(),
        encoding="utf-8",
    )
    return config_path


def test_get_connection_params_parses_config_and_comment_metadata(tmp_path):
    loader = SSHConfigLoaderV3(config_path=str(write_config(tmp_path)))

    params = loader.get_connection_params("pi-test")

    assert params["alias"] == "pi-test"
    assert params["hostname"] == "192.0.2.10"
    assert params["user"] == "pi"
    assert params["port"] == 2222
    assert params["password"] == "redacted-password"
    assert params["proxy_jump"] == "jump-host"
    assert params["forward_agent"] is True
    assert params["metadata"]["environment"] == "lab"
    assert params["metadata"]["tags"] == ["pi", "local"]


def test_from_alias_uses_paramiko_for_password_hosts(monkeypatch, tmp_path):
    loader = SSHConfigLoaderV3(config_path=str(write_config(tmp_path)))
    created = {}

    class FakeParamikoClient:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "paramiko_client",
        types.SimpleNamespace(ParamikoClient=FakeParamikoClient),
    )

    client = loader.from_alias("pi-test")

    assert isinstance(client, FakeParamikoClient)
    assert created["host"] == "192.0.2.10"
    assert created["password"] == "redacted-password"
    assert client.alias == "pi-test"


def test_from_alias_uses_native_client_for_key_only_hosts(monkeypatch, tmp_path):
    loader = SSHConfigLoaderV3(config_path=str(write_config(tmp_path)))
    created = {}

    class FakeNativeSSHClient:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "native_ssh_client",
        types.SimpleNamespace(NativeSSHClient=FakeNativeSSHClient),
    )

    client = loader.from_alias("key-test")

    assert isinstance(client, FakeNativeSSHClient)
    assert created["host"] == "key.example.test"
    assert created["key_file"] == str(Path.home() / ".ssh/id_ed25519")
    assert created["forward_agent"] is False
    assert client.alias == "key-test"
