import sys
from types import SimpleNamespace

import pytest

import ssh_execute


pytestmark = pytest.mark.unit


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

    def exec_command(self, command):
        stdout = SimpleNamespace(channel=self.channel)
        return None, stdout, None


def test_main_stream_delegates_without_json_output(monkeypatch, capsys):
    calls = []

    def fake_stream_execute(alias, command, timeout):
        calls.append((alias, command, timeout))
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
    assert calls == [("pi", "run-long-task", None)]
    assert capsys.readouterr().out == "live output\n"


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
