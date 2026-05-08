import json
import sys
from types import SimpleNamespace

import pytest

import ssh_interactive_daemon
from ssh_interactive_daemon import InteractiveDaemon


pytestmark = pytest.mark.unit


class FakeTransport:
    def __init__(self):
        self.channels = []

    def is_active(self):
        return True

    def send_ignore(self):
        return None

    def open_session(self):
        channel = FakeChannel()
        self.channels.append(channel)
        return channel


class FakeSSHClient:
    def __init__(self):
        self.transport = FakeTransport()
        self.closed = False

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self):
        self.pty_calls = []
        self.exec_commands = []
        self.sent = []
        self.closed = False

    def get_pty(self, term="xterm-256color", width=80, height=24):
        self.pty_calls.append((term, width, height))

    def exec_command(self, command):
        self.exec_commands.append(command)

    def recv_ready(self):
        return False

    def exit_status_ready(self):
        return False

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def close(self):
        self.closed = True


def daemon_with_fake_client():
    client = FakeSSHClient()
    daemon = InteractiveDaemon("pi")
    daemon._ssh_client = client
    daemon._connection_params = {
        "alias": "pi",
        "user": "pi",
        "hostname": "192.0.2.10",
        "port": 22,
    }
    return daemon, client


def test_request_without_token_is_rejected_before_routing(monkeypatch):
    daemon, _ = daemon_with_fake_client()
    daemon._token = "correct-token"
    calls = []

    def fake_start_session(*args, **kwargs):
        calls.append((args, kwargs))
        return {"success": True}

    monkeypatch.setattr(daemon, "_start_session", fake_start_session)

    result = daemon._handle_request(
        {"action": "start", "session": "dbg", "command": "gdb ./app"}
    )

    assert result == {
        "success": False,
        "exit_code": -1,
        "stdout": "",
        "stderr": "unauthorized",
    }
    assert calls == []


def test_request_without_configured_daemon_token_is_rejected():
    daemon, _ = daemon_with_fake_client()

    result = daemon._handle_request({"action": "list"})

    assert result["success"] is False
    assert result["stderr"] == "unauthorized"


def test_request_with_correct_token_can_list_sessions():
    daemon, _ = daemon_with_fake_client()
    daemon._token = "correct-token"

    result = daemon._handle_request({"action": "list", "token": "correct-token"})

    assert result == {"success": True, "sessions": []}


def test_start_writes_daemon_metadata_with_token_without_printing_it(
    monkeypatch,
    capsys,
):
    daemon = InteractiveDaemon("pi")
    written = {}

    class FakeServerSocket:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def accept(self):
            daemon._running = False
            raise OSError("accept stopped")

        def close(self):
            pass

    monkeypatch.setattr(daemon, "_load_config", lambda: None)
    monkeypatch.setattr(daemon, "_connect_ssh", lambda: None)
    monkeypatch.setattr(daemon, "_bind_server_socket", lambda: FakeServerSocket())
    monkeypatch.setattr(ssh_interactive_daemon.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(ssh_interactive_daemon, "remove_daemon_info", lambda alias: None)

    def fake_write_daemon_info(alias, info):
        written["alias"] = alias
        written["info"] = dict(info)

    monkeypatch.setattr(ssh_interactive_daemon, "write_daemon_info", fake_write_daemon_info)

    daemon.start()

    assert written["alias"] == "pi"
    assert isinstance(written["info"].get("token"), str)
    assert len(written["info"]["token"]) > 20
    output = json.loads(capsys.readouterr().out)
    assert "token" not in output


def test_existing_daemon_status_does_not_print_token(monkeypatch, capsys):
    monkeypatch.setattr(
        ssh_interactive_daemon,
        "read_daemon_info",
        lambda alias: {
            "pid": 1234,
            "port": 43210,
            "host": "pi@192.0.2.10",
            "token": "daemon-token",
        },
    )

    result = ssh_interactive_daemon._main(["start", "pi"])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["already_running"] is True
    assert "token" not in output


def test_connect_ssh_uses_paramiko_for_key_only_alias(monkeypatch, tmp_path):
    calls = SimpleNamespace(policy=None, connect_kwargs=None)
    key_file = tmp_path / "id_ed25519"

    class FakeSSHClientForConnect:
        def set_missing_host_key_policy(self, policy):
            calls.policy = policy

        def connect(self, **kwargs):
            calls.connect_kwargs = kwargs

    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: FakeSSHClientForConnect(),
        AutoAddPolicy=lambda: "auto-add-policy",
    )

    class FakeLoader:
        def get_connection_params(self, alias):
            assert alias == "keyhost"
            return {
                "hostname": "192.0.2.20",
                "user": "deploy",
                "port": 2200,
                "timeout": 30,
                "key_file": str(key_file),
            }

        def from_alias(self, alias):
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(ssh_interactive_daemon, "SSHConfigLoaderV3", lambda: FakeLoader())

    daemon = InteractiveDaemon("keyhost")
    result = daemon._connect_ssh()

    assert result is daemon._ssh_client
    assert calls.policy == "auto-add-policy"
    assert calls.connect_kwargs == {
        "hostname": "192.0.2.20",
        "port": 2200,
        "username": "deploy",
        "timeout": 30,
        "key_filename": str(key_file),
        "look_for_keys": True,
        "allow_agent": True,
    }
    assert daemon._connection_params["key_file"] == str(key_file)


def test_connect_ssh_uses_paramiko_for_password_alias(monkeypatch):
    calls = SimpleNamespace(connect_kwargs=None)

    class FakeSSHClientForConnect:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            calls.connect_kwargs = kwargs

    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: FakeSSHClientForConnect(),
        AutoAddPolicy=lambda: object(),
    )

    class FakeLoader:
        def get_connection_params(self, alias):
            assert alias == "passhost"
            return {
                "hostname": "192.0.2.21",
                "user": "root",
                "port": 22,
                "timeout": 12,
                "password": "secret",
            }

    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(ssh_interactive_daemon, "SSHConfigLoaderV3", lambda: FakeLoader())

    daemon = InteractiveDaemon("passhost")
    daemon._connect_ssh()

    assert calls.connect_kwargs == {
        "hostname": "192.0.2.21",
        "port": 22,
        "username": "root",
        "timeout": 12,
        "password": "secret",
        "look_for_keys": False,
        "allow_agent": False,
    }


def test_start_session_opens_distinct_pty_channel():
    daemon, client = daemon_with_fake_client()

    result = daemon._start_session("dbg", "gdb ./app", rows=30, cols=100)

    assert result["success"] is True
    assert result["session"] == "dbg"
    assert len(client.transport.channels) == 1
    channel = client.transport.channels[0]
    assert channel.pty_calls == [("xterm-256color", 100, 30)]
    assert channel.exec_commands == ["gdb ./app"]


def test_start_existing_session_fails():
    daemon, _ = daemon_with_fake_client()
    daemon._start_session("dbg", "gdb ./app", rows=24, cols=80)

    result = daemon._start_session("dbg", "python3 -m pdb app.py", rows=24, cols=80)

    assert result["success"] is False
    assert "already exists" in result["stderr"]


def test_send_delegates_to_named_session_only():
    daemon, client = daemon_with_fake_client()
    daemon._start_session("gdb", "gdb ./app", rows=24, cols=80)
    daemon._start_session("pdb", "python3 -m pdb app.py", rows=24, cols=80)

    result = daemon._send_to_session("gdb", "next", raw=False, wait_for=None, timeout=0)

    assert result["success"] is True
    assert client.transport.channels[0].sent == [b"next\n"]
    assert client.transport.channels[1].sent == []


def test_send_with_wait_for_reads_from_send_result_seq_end():
    daemon, _ = daemon_with_fake_client()
    calls = SimpleNamespace(read=[])

    class StubSession:
        def send_text(self, text, raw=False):
            assert text == "next"
            assert raw is False
            return {"success": True, "seq_end": 7}

        def read(self, since=0, wait_for=None, timeout=None):
            calls.read.append(
                {"since": since, "wait_for": wait_for, "timeout": timeout}
            )
            return {"success": True, "output": "PROMPT", "seq_end": 9}

        def snapshot(self):
            return {"seq_end": 3}

    daemon._sessions["gdb"] = StubSession()

    result = daemon._send_to_session(
        "gdb",
        "next",
        raw=False,
        wait_for="PROMPT",
        timeout=1.5,
    )

    assert result["success"] is True
    assert calls.read == [{"since": 7, "wait_for": "PROMPT", "timeout": 1.5}]


def test_send_with_wait_for_without_send_seq_uses_pre_send_baseline():
    daemon, _ = daemon_with_fake_client()
    calls = SimpleNamespace(read=[])

    class StubSession:
        def __init__(self):
            self.seq_end = 3

        def send_text(self, text, raw=False):
            assert text == "next"
            assert raw is False
            self.seq_end = 4
            return {"success": True, "bytes": 5}

        def read(self, since=0, wait_for=None, timeout=None):
            calls.read.append(
                {"since": since, "wait_for": wait_for, "timeout": timeout}
            )
            return {"success": True, "output": "PROMPT", "seq_end": self.seq_end}

        def snapshot(self):
            current = self.seq_end
            if current == 4:
                self.seq_end = 5
            return {"seq_end": self.seq_end}

    daemon._sessions["gdb"] = StubSession()

    result = daemon._send_to_session(
        "gdb",
        "next",
        raw=False,
        wait_for="PROMPT",
        timeout=1.5,
    )

    assert result["success"] is True
    assert calls.read == [{"since": 3, "wait_for": "PROMPT", "timeout": 1.5}]


def test_send_with_wait_for_without_timeout_uses_default_wait_timeout():
    daemon, _ = daemon_with_fake_client()
    calls = SimpleNamespace(read=[])

    class StubSession:
        def send_text(self, text, raw=False):
            return {"success": True, "seq_end": 7}

        def read(self, since=0, wait_for=None, timeout=None):
            calls.read.append(
                {"since": since, "wait_for": wait_for, "timeout": timeout}
            )
            return {"success": True, "output": "PROMPT", "seq_end": 9}

        def snapshot(self):
            return {"seq_end": 3}

    daemon._sessions["gdb"] = StubSession()

    result = daemon._send_to_session(
        "gdb",
        "next",
        raw=False,
        wait_for="PROMPT",
        timeout=None,
    )

    assert result["success"] is True
    assert calls.read == [
        {
            "since": 7,
            "wait_for": "PROMPT",
            "timeout": ssh_interactive_daemon.DEFAULT_WAIT_TIMEOUT,
        }
    ]


def test_read_with_wait_for_without_timeout_uses_default_wait_timeout():
    daemon, _ = daemon_with_fake_client()
    calls = SimpleNamespace(read=[])

    class StubSession:
        def read(self, since=0, wait_for=None, timeout=None):
            calls.read.append(
                {"since": since, "wait_for": wait_for, "timeout": timeout}
            )
            return {"success": True, "output": "PROMPT", "seq_end": 9}

    daemon._sessions["gdb"] = StubSession()

    result = daemon._read_session("gdb", since=2, wait_for="PROMPT", timeout=None)

    assert result["success"] is True
    assert calls.read == [
        {
            "since": 2,
            "wait_for": "PROMPT",
            "timeout": ssh_interactive_daemon.DEFAULT_WAIT_TIMEOUT,
        }
    ]


def test_handle_client_sets_finite_socket_timeout_before_recv(monkeypatch):
    daemon, _ = daemon_with_fake_client()
    daemon._token = "correct-token"
    calls = SimpleNamespace(sent=[])

    class FakeSocket:
        def __init__(self):
            self.timeouts = []
            self.closed = False

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def close(self):
            self.closed = True

    sock = FakeSocket()

    def fake_recv_message(receiving_sock):
        assert receiving_sock is sock
        assert sock.timeouts == [300]
        return {"action": "ping", "token": "correct-token"}

    def fake_send_message(sending_sock, data):
        assert sending_sock is sock
        calls.sent.append(data)

    monkeypatch.setattr(ssh_interactive_daemon, "recv_message", fake_recv_message)
    monkeypatch.setattr(ssh_interactive_daemon, "send_message", fake_send_message)

    daemon._handle_client(sock)

    assert sock.closed is True
    assert calls.sent[0]["success"] is True


def test_bind_server_socket_sets_accept_timeout(monkeypatch):
    daemon, _ = daemon_with_fake_client()

    class FakeServerSocket:
        def __init__(self):
            self.options = []
            self.bound = None
            self.listened = False
            self.timeouts = []

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

        def bind(self, address):
            self.bound = address

        def listen(self):
            self.listened = True

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def getsockname(self):
            return ("127.0.0.1", 43210)

    server_socket = FakeServerSocket()
    monkeypatch.setattr(
        ssh_interactive_daemon.socket,
        "socket",
        lambda *args, **kwargs: server_socket,
    )

    result = daemon._bind_server_socket()

    assert result is server_socket
    assert server_socket.bound == ("127.0.0.1", 0)
    assert server_socket.listened is True
    assert server_socket.timeouts == [5.0]
    assert daemon._server_socket is server_socket


def test_shutdown_request_wakes_accept_loop_with_self_connect(monkeypatch):
    daemon, _ = daemon_with_fake_client()
    calls = SimpleNamespace(create_connection=[])

    class FakeServerSocket:
        def __init__(self):
            self.closed = False

        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            self.closed = True

    class FakeWakeSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def fake_create_connection(address, timeout=None):
        calls.create_connection.append({"address": address, "timeout": timeout})
        return FakeWakeSocket()

    server_socket = FakeServerSocket()
    monkeypatch.setattr(
        ssh_interactive_daemon.socket,
        "create_connection",
        fake_create_connection,
    )
    daemon._running = True
    daemon._server_socket = server_socket

    daemon._token = "correct-token"
    result = daemon._handle_request({"action": "shutdown", "token": "correct-token"})

    assert result["success"] is True
    assert daemon._running is False
    assert calls.create_connection == [
        {"address": ("127.0.0.1", 43210), "timeout": 1.0}
    ]
    assert server_socket.closed is True


def test_send_wait_for_timeout_uses_daemon_error_format():
    daemon, _ = daemon_with_fake_client()

    class StubSession:
        def send_text(self, text, raw=False):
            return {"success": True, "seq_end": 7, "bytes": 5}

        def read(self, since=0, wait_for=None, timeout=None):
            return {
                "success": False,
                "matched": False,
                "error": "wait-for timeout",
                "seq_start": 7,
                "seq_end": 8,
                "output": "partial",
                "truncated": False,
            }

        def snapshot(self):
            return {"seq_end": 3}

    daemon._sessions["gdb"] = StubSession()

    result = daemon._send_to_session(
        "gdb",
        "next",
        raw=False,
        wait_for="PROMPT",
        timeout=0.1,
    )

    assert result["success"] is False
    assert result["exit_code"] == -1
    assert result["stdout"] == ""
    assert result["stderr"] == "wait-for timeout"
    assert result["output"] == "partial"
    assert result["matched"] is False
    assert result["seq_start"] == 7
    assert result["seq_end"] == 8
    assert result["truncated"] is False


def test_stop_session_closes_only_named_channel():
    daemon, client = daemon_with_fake_client()
    daemon._start_session("gdb", "gdb ./app", rows=24, cols=80)
    daemon._start_session("pdb", "python3 -m pdb app.py", rows=24, cols=80)

    result = daemon._stop_session("gdb")

    assert result["success"] is True
    assert client.transport.channels[0].closed is True
    assert client.transport.channels[1].closed is False
    assert "pdb" in daemon._sessions


def test_prune_terminal_sessions_removes_closed_and_error_sessions_only():
    daemon, _ = daemon_with_fake_client()

    class StubSession:
        def __init__(self, state):
            self.state = state

        def snapshot(self):
            return {"state": self.state}

    daemon._sessions = {
        "closed": StubSession("closed"),
        "error": StubSession("error"),
        "running": StubSession("running"),
    }

    daemon._prune_terminal_sessions()

    assert list(daemon._sessions) == ["running"]


def test_list_sessions_prunes_terminal_sessions_from_output():
    daemon, _ = daemon_with_fake_client()

    class StubSession:
        def __init__(self, state):
            self.state = state

        def snapshot(self):
            return {"state": self.state}

    daemon._sessions = {
        "closed": StubSession("closed"),
        "running": StubSession("running"),
    }

    result = daemon._list_sessions()

    assert result == {"success": True, "sessions": [{"state": "running"}]}


def test_unknown_session_returns_clear_error():
    daemon, _ = daemon_with_fake_client()

    result = daemon._read_session("missing", since=0, wait_for=None, timeout=0)

    assert result["success"] is False
    assert result["stderr"] == "session not found: missing"
