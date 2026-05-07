import json
import os
import socket

import pytest

import ssh_tunnel


pytestmark = pytest.mark.unit


def test_tunnel_id_uses_alias_and_local_port():
    assert ssh_tunnel.get_tunnel_id("pi", 10022) == "pi-10022"


def test_tunnel_info_path_is_stable_and_under_tunnel_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_tunnel, "TUNNEL_DIR", str(tmp_path))

    first = ssh_tunnel.get_tunnel_info_path("pi-10022")
    second = ssh_tunnel.get_tunnel_info_path("pi-10022")

    assert first == second
    assert first.startswith(str(tmp_path))
    assert first.endswith(".json")


def test_read_tunnel_info_removes_stale_pid_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_tunnel, "TUNNEL_DIR", str(tmp_path))
    monkeypatch.setattr(ssh_tunnel, "_is_process_alive", lambda pid: False)
    path = ssh_tunnel.get_tunnel_info_path("pi-10022")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": 999999, "tunnel_id": "pi-10022"}, f)

    assert ssh_tunnel.read_tunnel_info("pi-10022") is None
    assert not os.path.exists(path)


def test_find_available_port_skips_occupied_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    occupied = sock.getsockname()[1]
    try:
        assert ssh_tunnel.find_available_port(occupied, occupied + 2) == occupied + 1
    finally:
        sock.close()
