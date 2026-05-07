import pytest

from paramiko_client import ParamikoClient, _format_exception


pytestmark = pytest.mark.unit


class EmptyTextError(Exception):
    def __str__(self):
        return ""


class FakeChannel:
    def recv_exit_status(self):
        return 0


class FakeStream:
    def __init__(self, lines=(), payload=b""):
        self._lines = list(lines)
        self._payload = payload
        self.channel = FakeChannel()

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._payload


class FakeSSH:
    def exec_command(self, command, timeout=None):
        return None, FakeStream(["out\n"]), FakeStream(["err\n"])


def make_client():
    return ParamikoClient("example.test", "deploy", key_file="/tmp/fake-key")


def test_format_exception_uses_class_name_when_message_is_empty():
    assert _format_exception(TimeoutError()) == "TimeoutError"
    assert _format_exception(EmptyTextError()) == "EmptyTextError"


def test_execute_stream_includes_stderr_lines(monkeypatch):
    client = make_client()
    monkeypatch.setattr(client, "_get_connection", lambda: FakeSSH())

    assert list(client.execute_stream("printf test")) == ["out", "[STDERR] err"]


def test_execute_stream_reports_empty_exception_class_name(monkeypatch):
    client = make_client()

    def raise_empty():
        raise EmptyTextError()

    monkeypatch.setattr(client, "_get_connection", raise_empty)

    assert list(client.execute_stream("whoami")) == [
        "[ERROR] Execution error: EmptyTextError"
    ]
