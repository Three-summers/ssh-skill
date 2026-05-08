import errno
import hashlib
import json
import os
import socket
import stat
import struct
import tempfile

import pytest

import interactive_protocol
from interactive_protocol import (
    INTERACTIVE_DAEMON_DIR,
    MAX_MESSAGE_BYTES,
    RECV_BUFFER,
    connect_to_daemon,
    get_daemon_id,
    get_daemon_info_path,
    read_daemon_info,
    recv_message,
    remove_daemon_info,
    send_message,
    validate_session_name,
    write_daemon_info,
)


pytestmark = pytest.mark.unit


def test_protocol_constants_match_contract():
    assert INTERACTIVE_DAEMON_DIR == os.path.join(
        tempfile.gettempdir(), "ssh_interactive_daemon"
    )
    assert MAX_MESSAGE_BYTES == 10 * 1024 * 1024
    assert RECV_BUFFER == 65536


def test_session_name_validation_accepts_safe_names():
    assert validate_session_name("gdb-api_1.2") == "gdb-api_1.2"


def test_session_name_validation_accepts_length_and_character_boundaries():
    assert validate_session_name("a" * 64) == "a" * 64
    assert validate_session_name("AZaz09_.-") == "AZaz09_.-"


def test_session_name_validation_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match="session name"):
        validate_session_name("bad;rm -rf")


@pytest.mark.parametrize("name", ["", "a" * 65])
def test_session_name_validation_rejects_length_boundaries(name):
    with pytest.raises(ValueError, match="session name"):
        validate_session_name(name)


def test_daemon_id_is_stable_and_case_insensitive():
    assert get_daemon_id("Prod-Web") == get_daemon_id("prod-web")
    assert len(get_daemon_id("prod-web")) == 12


def test_daemon_id_is_md5_of_lowercased_alias():
    expected = hashlib.md5("prod-web".encode("utf-8")).hexdigest()[:12]
    assert get_daemon_id("Prod-Web") == expected


def test_daemon_info_path_uses_configured_root(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))

    path = get_daemon_info_path("pi")

    assert path.startswith(str(tmp_path))
    assert path.endswith(".json")


def test_daemon_info_path_creates_private_directory(monkeypatch, tmp_path):
    daemon_dir = tmp_path / "daemon"
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(daemon_dir))

    get_daemon_info_path("pi")

    assert stat.S_IMODE(daemon_dir.stat().st_mode) == 0o700


def test_daemon_info_path_rejects_existing_non_directory(monkeypatch, tmp_path):
    daemon_path = tmp_path / "daemon-file"
    daemon_path.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(daemon_path))

    with pytest.raises(RuntimeError, match="daemon metadata directory"):
        get_daemon_info_path("pi")


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="symlinks are not available on this platform"
)
def test_daemon_info_path_rejects_symlink_directory(monkeypatch, tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    daemon_link = tmp_path / "daemon-link"
    os.symlink(target_dir, daemon_link)
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(daemon_link))

    with pytest.raises(RuntimeError, match="daemon metadata directory"):
        get_daemon_info_path("pi")


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership check")
def test_daemon_info_path_rejects_wrong_owner(monkeypatch, tmp_path):
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(daemon_dir))

    original_lstat = os.lstat

    def lstat_with_wrong_owner(path):
        result = original_lstat(path)
        if os.fspath(path) == os.fspath(daemon_dir):
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr("interactive_protocol.os.lstat", lstat_with_wrong_owner)

    with pytest.raises(RuntimeError, match="daemon metadata directory"):
        get_daemon_info_path("pi")


def test_write_daemon_info_uses_restrictive_file_permissions(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    old_umask = os.umask(0)
    try:
        path = write_daemon_info("pi", {"pid": os.getpid(), "port": 41234})
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_write_daemon_info_keeps_existing_metadata_when_write_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = write_daemon_info("pi", {"pid": os.getpid(), "port": 41234})

    def fail_after_partial_write(data, fp, **kwargs):
        fp.write('{"pid":')
        raise RuntimeError("write interrupted")

    monkeypatch.setattr("interactive_protocol.json.dump", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="write interrupted"):
        write_daemon_info("pi", {"pid": os.getpid(), "port": 51234})

    with open(path, encoding="utf-8") as f:
        assert json.load(f)["port"] == 41234


def test_read_daemon_info_returns_metadata_when_process_is_alive(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = get_daemon_info_path("pi")
    metadata = {"pid": 1234, "port": 41234, "alias": "pi"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metadata, f)

    monkeypatch.setattr("interactive_protocol.is_process_alive", lambda pid: True)

    assert read_daemon_info("pi") == metadata


def test_read_daemon_info_removes_stale_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = get_daemon_info_path("pi")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": 999999, "port": 41234, "alias": "pi"}, f)

    monkeypatch.setattr("interactive_protocol.is_process_alive", lambda pid: False)

    assert read_daemon_info("pi") is None
    assert not os.path.exists(path)


def test_read_daemon_info_returns_none_for_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = get_daemon_info_path("pi")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{")

    assert read_daemon_info("pi") is None


def test_read_daemon_info_returns_none_for_non_utf8_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = get_daemon_info_path("pi")
    with open(path, "wb") as f:
        f.write(b"\xff\xfe")

    assert read_daemon_info("pi") is None


def test_write_daemon_info_returns_path_and_overrides_alias(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))

    path = write_daemon_info(
        "pi", {"pid": os.getpid(), "port": 41234, "alias": "wrong"}
    )

    assert path == get_daemon_info_path("pi")
    with open(path, encoding="utf-8") as f:
        metadata = json.load(f)
    assert metadata["alias"] == "pi"
    assert metadata["port"] == 41234


def test_remove_daemon_info_removes_existing_file_and_ignores_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = write_daemon_info("pi", {"pid": os.getpid(), "port": 41234})

    remove_daemon_info("pi")
    remove_daemon_info("pi")

    assert not os.path.exists(path)


def test_daemon_start_lock_prevents_nested_acquisition(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))

    with interactive_protocol.daemon_start_lock("pi", timeout=0.1):
        with pytest.raises(TimeoutError, match="daemon start lock"):
            with interactive_protocol.daemon_start_lock("pi", timeout=0.1):
                pass


def test_daemon_start_lock_removes_stale_lock(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    lock_path = tmp_path / f"{get_daemon_id('pi')}.lock"
    lock_path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr("interactive_protocol.is_process_alive", lambda pid: False)

    with interactive_protocol.daemon_start_lock("pi", timeout=0.1):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_process_liveness_treats_posix_eperm_as_alive(monkeypatch):
    def raise_eperm(pid, signal):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr("interactive_protocol.os.name", "posix")
    monkeypatch.setattr("interactive_protocol.os.kill", raise_eperm)

    from interactive_protocol import is_process_alive

    assert is_process_alive(12345) is True


def test_send_and_recv_message_round_trip():
    left, right = socket.socketpair()
    try:
        send_message(left, {"action": "ping", "value": "ok"})
        assert recv_message(right) == {"action": "ping", "value": "ok"}
    finally:
        left.close()
        right.close()


def test_recv_message_rejects_oversized_payload():
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", MAX_MESSAGE_BYTES + 1))
        with pytest.raises(ValueError, match="message too large"):
            recv_message(right)
    finally:
        left.close()
        right.close()


def test_connect_to_daemon_raises_when_metadata_missing(monkeypatch):
    monkeypatch.setattr("interactive_protocol.read_daemon_info", lambda alias: None)

    with pytest.raises(ConnectionError, match="daemon metadata missing"):
        connect_to_daemon("pi", timeout=1)


@pytest.mark.parametrize("port", [0, -1, 65536, True, False, "bad", 22.9])
def test_connect_to_daemon_rejects_invalid_port_metadata(monkeypatch, port):
    monkeypatch.setattr(
        "interactive_protocol.read_daemon_info",
        lambda alias: {"pid": os.getpid(), "port": port, "alias": alias},
    )
    monkeypatch.setattr(
        "interactive_protocol.socket.create_connection",
        lambda address, timeout: pytest.fail("should not attempt socket connection"),
    )

    with pytest.raises(ConnectionError, match="invalid daemon metadata"):
        connect_to_daemon("pi", timeout=1)


def test_connect_to_daemon_uses_metadata_port(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = None
    accepted = None
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        path = get_daemon_info_path("pi")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "alias": "pi"}, f)

        client = connect_to_daemon("pi", timeout=1)
        accepted, _ = server.accept()
        assert client.getpeername()[1] == port
    finally:
        if client is not None:
            client.close()
        if accepted is not None:
            accepted.close()
        server.close()


def test_connect_to_daemon_removes_metadata_when_port_is_stale(monkeypatch, tmp_path):
    monkeypatch.setattr("interactive_protocol.INTERACTIVE_DAEMON_DIR", str(tmp_path))
    path = write_daemon_info("pi", {"pid": os.getpid(), "port": 41234})

    def refuse_connection(address, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(
        "interactive_protocol.socket.create_connection",
        refuse_connection,
    )

    with pytest.raises(ConnectionError, match="daemon connection failed"):
        connect_to_daemon("pi", timeout=1)

    assert not os.path.exists(path)
