import sys
from types import SimpleNamespace

import pytest

import ssh_execute


pytestmark = pytest.mark.unit


class FakeStdin:
    def __init__(self):
        self.writes = []
        self.flushed = False

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        self.flushed = True


class FakeChannel:
    def __init__(self, stdout_chunks=(), stderr_chunks=(), exit_code=0):
        self.stdout_chunks = [chunk.encode("utf-8") for chunk in stdout_chunks]
        self.stderr_chunks = [chunk.encode("utf-8") for chunk in stderr_chunks]
        self.exit_code = exit_code
        self.closed = False

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv(self, size):
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv_stderr(self, size):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        return not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.closed = True


class FakeSSH:
    def __init__(self, channel):
        self.channel = channel
        self.stdin = FakeStdin()
        self.commands = []

    def exec_command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        stdout = SimpleNamespace(channel=self.channel)
        return self.stdin, stdout, None


def test_main_stream_delegates_without_json_output(monkeypatch, capsys):
    calls = []

    def fake_stream_execute(alias, command, timeout, use_sudo=False):
        calls.append((alias, command, timeout, use_sudo))
        print("live output")
        return 7

    monkeypatch.setattr(ssh_execute, "stream_execute", fake_stream_execute, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ssh_execute.py", "pi", "run-long-task", "--stream"],
    )

    with pytest.raises(SystemExit) as exc_info:
        ssh_execute.main()

    assert exc_info.value.code == 7
    assert calls == [("pi", "run-long-task", None, False)]
    assert capsys.readouterr().out == "live output\n"


def test_main_stream_sudo_delegates_with_sudo_enabled(monkeypatch, capsys):
    calls = []

    def fake_stream_execute(alias, command, timeout, use_sudo=False):
        calls.append((alias, command, timeout, use_sudo))
        print("root output")
        return 0

    monkeypatch.setattr(ssh_execute, "stream_execute", fake_stream_execute, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ssh_execute.py", "petavm", "whoami", "--stream", "--sudo"],
    )

    with pytest.raises(SystemExit) as exc_info:
        ssh_execute.main()

    assert exc_info.value.code == 0
    assert calls == [("petavm", "whoami", None, True)]
    assert capsys.readouterr().out == "root output\n"


def test_stream_paramiko_client_writes_stdout_and_stderr(capsys):
    channel = FakeChannel(
        stdout_chunks=["out-1\n", "out-2\n"],
        stderr_chunks=["warn-1\n"],
        exit_code=3,
    )
    client = SimpleNamespace(_get_connection=lambda: FakeSSH(channel))

    exit_code = ssh_execute._stream_paramiko_client(
        client,
        "run-long-task",
        timeout=None,
        poll_interval=0,
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == "out-1\nout-2\n"
    assert captured.err == "warn-1\n"


def test_sudo_wrap_preserves_shell_command_without_exposing_password():
    wrapped = ssh_execute._sudo_wrap("cd /opt/app && ./install.sh --flag 'two words'")

    assert wrapped.startswith("sudo -S -p '' sh -lc ")
    assert "cd /opt/app && ./install.sh --flag" in wrapped
    assert "secret" not in wrapped


def test_stream_paramiko_client_sends_sudo_password_to_stdin(capsys):
    channel = FakeChannel(stdout_chunks=["root\n"], exit_code=0)
    fake_ssh = FakeSSH(channel)
    client = SimpleNamespace(_get_connection=lambda: fake_ssh)

    exit_code = ssh_execute._stream_paramiko_client(
        client,
        ssh_execute._sudo_wrap("whoami"),
        timeout=None,
        poll_interval=0,
        sudo_password="secret-password",
    )

    assert exit_code == 0
    assert fake_ssh.stdin.writes == ["secret-password\n"]
    assert fake_ssh.stdin.flushed is True
    assert "secret-password" not in fake_ssh.commands[0][0]
    assert capsys.readouterr().out == "root\n"
