import threading
import time
from types import SimpleNamespace

import pytest

from interactive_session import (
    CONTROL_BYTES,
    InteractiveSession,
    OutputBuffer,
)


pytestmark = pytest.mark.unit


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.resize_calls = []

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def resize_pty(self, width, height):
        self.resize_calls.append((width, height))

    def close(self):
        self.closed = True


class CloseRaceChannel:
    def __init__(self):
        self.closed = False
        self.in_recv_ready = threading.Event()
        self.allow_recv_ready = threading.Event()

    def recv_ready(self):
        self.in_recv_ready.set()
        assert self.allow_recv_ready.wait(timeout=1)
        if self.closed:
            raise OSError("channel closed")
        return False

    def exit_status_ready(self):
        return False

    def close(self):
        self.closed = True
        self.allow_recv_ready.set()


class PartialSendChannel:
    def __init__(self, chunk_size):
        self.chunk_size = chunk_size
        self.sent = []
        self.closed = False

    def send(self, data):
        sent = data[: self.chunk_size]
        self.sent.append(sent)
        return len(sent)

    def close(self):
        self.closed = True


def test_output_buffer_reports_first_retained_sequence_after_truncation():
    buffer = OutputBuffer(max_bytes=10)

    first = buffer.append("abcdef")
    second = buffer.append("ghijkl")
    result = buffer.read_since(0)

    assert first == 1
    assert second == 2
    assert result["seq_start"] == 2
    assert result["seq_end"] == 2
    assert result["truncated"] is True
    assert result["output"] == "ghijkl"


def test_output_buffer_truncated_is_relative_to_requested_sequence():
    buffer = OutputBuffer(max_bytes=10)

    buffer.append("abcdef")
    buffer.append("ghijkl")

    retained_result = buffer.read_since(1)

    assert retained_result["seq_start"] == 2
    assert retained_result["seq_end"] == 2
    assert retained_result["truncated"] is False
    assert retained_result["output"] == "ghijkl"


def test_send_line_writes_newline_by_default():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")

    result = session.send_text("next", raw=False)

    assert result["success"] is True
    assert channel.sent == [b"next\n"]


def test_send_raw_does_not_append_newline():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="python3")

    session.send_text("print(1)", raw=True)

    assert channel.sent == [b"print(1)"]


def test_send_text_writes_all_bytes_when_channel_sends_partially():
    channel = PartialSendChannel(chunk_size=2)
    session = InteractiveSession("dbg", channel, command="gdb ./app")

    result = session.send_text("next")

    assert result["success"] is True
    assert b"".join(channel.sent) == b"next\n"
    assert len(channel.sent) > 1


def test_control_sends_named_control_bytes():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")

    session.send_control("ctrl-c")

    assert channel.sent == [CONTROL_BYTES["ctrl-c"]]


def test_unknown_control_name_fails():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")

    with pytest.raises(ValueError, match="unknown control"):
        session.send_control("ctrl-z")


def test_resize_updates_pty_size():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")

    result = session.resize(cols=120, rows=40)

    assert result["success"] is True
    assert channel.resize_calls == [(120, 40)]
    assert session.snapshot()["cols"] == 120
    assert session.snapshot()["rows"] == 40


@pytest.mark.parametrize(
    "operation",
    [
        lambda session: session.send_text("next"),
        lambda session: session.send_control("ctrl-c"),
        lambda session: session.resize(cols=120, rows=40),
    ],
)
def test_channel_mutations_fail_after_close(operation):
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")
    session.close()

    with pytest.raises(RuntimeError, match="session is not running"):
        operation(session)


def test_read_waits_for_prompt_in_buffer():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")
    session.record_output("started\n(gdb) ")

    result = session.read(since=0, wait_for=r"\(gdb\)", timeout=0.1)

    assert result["success"] is True
    assert result["matched"] is True
    assert "(gdb)" in result["output"]


def test_read_wait_for_matches_output_recorded_by_another_thread():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")
    result = {}

    def wait_for_prompt():
        result["value"] = session.read(since=0, wait_for=r"\(gdb\)", timeout=1)

    thread = threading.Thread(target=wait_for_prompt)
    thread.start()
    time.sleep(0.01)

    session.record_output("started\n(gdb) ")
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert result["value"]["success"] is True
    assert result["value"]["matched"] is True
    assert "(gdb)" in result["value"]["output"]


def test_wait_timeout_returns_captured_output():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")
    session.record_output("running\n")

    result = session.read(since=0, wait_for=r"\(gdb\)", timeout=0.01)

    assert result["success"] is False
    assert result["matched"] is False
    assert result["error"] == "wait-for timeout"
    assert "running" in result["output"]


def test_close_closes_channel_and_marks_state():
    channel = FakeChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")

    result = session.close()

    assert result["success"] is True
    assert channel.closed is True
    assert session.snapshot()["state"] == "closed"


def test_reader_error_after_intentional_close_does_not_override_closed_state():
    channel = CloseRaceChannel()
    session = InteractiveSession("dbg", channel, command="gdb ./app")
    session.start_reader(poll_interval=0.001)
    assert channel.in_recv_ready.wait(timeout=1)

    session.close()
    session._reader_thread.join(timeout=1)

    assert session.snapshot()["state"] == "closed"
    assert session.reader_error is None
