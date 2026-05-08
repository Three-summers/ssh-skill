import json
import sys
import threading
from types import SimpleNamespace

import pytest

import ssh_interactive
from ssh_interactive_daemon import InteractiveDaemon


pytestmark = pytest.mark.unit


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.closed = False

    def close(self):
        self.closed = True


def test_request_injects_daemon_token_without_mutating_payload(monkeypatch):
    fake = FakeSocket({"success": True, "sessions": []})
    calls = {}
    payload = {"action": "list"}

    monkeypatch.setattr(ssh_interactive, "ensure_daemon", lambda alias: None)
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210, "token": "daemon-token"},
    )
    monkeypatch.setattr(ssh_interactive, "connect_to_daemon", lambda alias: fake)
    monkeypatch.setattr(
        ssh_interactive,
        "send_message",
        lambda sock, data: calls.setdefault("request", data),
    )
    monkeypatch.setattr(
        ssh_interactive,
        "recv_message",
        lambda sock, timeout=None: fake.response,
    )

    response = ssh_interactive.request("pi", payload)

    assert response == {"success": True, "sessions": []}
    assert calls["request"] == {"action": "list", "token": "daemon-token"}
    assert payload == {"action": "list"}
    assert fake.closed is True


def test_request_missing_daemon_token_fails_clearly(monkeypatch):
    monkeypatch.setattr(ssh_interactive, "ensure_daemon", lambda alias: None)
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210},
    )
    monkeypatch.setattr(
        ssh_interactive,
        "connect_to_daemon",
        lambda alias: pytest.fail("request should fail before connecting"),
    )

    with pytest.raises(RuntimeError, match="missing interactive daemon token"):
        ssh_interactive.request("pi", {"action": "list"})


def test_request_retries_once_after_stale_daemon_connection(monkeypatch):
    fake = FakeSocket({"success": True, "sessions": []})
    calls = SimpleNamespace(ensure=[], connect=0, sent=[])

    def fake_connect_to_daemon(alias):
        calls.connect += 1
        if calls.connect == 1:
            raise ConnectionError("daemon connection failed")
        return fake

    monkeypatch.setattr(
        ssh_interactive,
        "ensure_daemon",
        lambda alias: calls.ensure.append(alias),
    )
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210, "token": "daemon-token"},
    )
    monkeypatch.setattr(ssh_interactive, "connect_to_daemon", fake_connect_to_daemon)
    monkeypatch.setattr(
        ssh_interactive,
        "send_message",
        lambda sock, data: calls.sent.append(data),
    )
    monkeypatch.setattr(
        ssh_interactive,
        "recv_message",
        lambda sock, timeout=None: fake.response,
    )

    response = ssh_interactive.request("pi", {"action": "list"})

    assert response == {"success": True, "sessions": []}
    assert calls.ensure == ["pi", "pi"]
    assert calls.connect == 2
    assert calls.sent == [{"action": "list", "token": "daemon-token"}]
    assert fake.closed is True


def test_send_command_builds_request(monkeypatch, capsys):
    fake = FakeSocket({"success": True, "seq_end": 3})
    calls = {}

    monkeypatch.setattr(ssh_interactive, "ensure_daemon", lambda alias: None)
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210, "token": "daemon-token"},
    )
    monkeypatch.setattr(ssh_interactive, "connect_to_daemon", lambda alias: fake)
    monkeypatch.setattr(
        ssh_interactive,
        "send_message",
        lambda sock, data: calls.setdefault("request", data),
    )
    monkeypatch.setattr(ssh_interactive, "recv_message", lambda sock, timeout=None: fake.response)
    monkeypatch.setattr(sys, "argv", ["ssh_interactive.py", "pi", "send", "dbg", "next"])

    with pytest.raises(SystemExit) as exc_info:
        ssh_interactive.main()

    assert exc_info.value.code == 0
    assert calls["request"] == {
        "action": "send",
        "session": "dbg",
        "input": "next",
        "text": "next",
        "raw": False,
        "wait_for": None,
        "timeout": None,
        "token": "daemon-token",
    }
    assert json.loads(capsys.readouterr().out)["seq_end"] == 3


def test_send_payload_is_compatible_with_current_daemon():
    class StubSession:
        def __init__(self):
            self.received = None

        def send_text(self, text, raw=False):
            self.received = (text, raw)
            return {"success": True, "seq_end": 3}

    session = StubSession()
    daemon = object.__new__(InteractiveDaemon)
    daemon._token = "daemon-token"
    daemon._sessions = {"dbg": session}
    daemon._sessions_lock = threading.RLock()
    daemon._last_activity = 0

    args = ssh_interactive.build_parser().parse_args(["pi", "send", "dbg", "next"])
    payload = ssh_interactive.build_payload(args)
    payload["token"] = "daemon-token"
    response = daemon._handle_request(payload)

    assert response["success"] is True
    assert session.received == ("next", False)


def test_read_prints_raw_output_by_default(monkeypatch, capsys):
    fake = FakeSocket({"success": True, "output": "line\n(Pdb) ", "seq_end": 4})

    monkeypatch.setattr(ssh_interactive, "ensure_daemon", lambda alias: None)
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210, "token": "daemon-token"},
    )
    monkeypatch.setattr(ssh_interactive, "connect_to_daemon", lambda alias: fake)
    monkeypatch.setattr(ssh_interactive, "send_message", lambda sock, data: None)
    monkeypatch.setattr(ssh_interactive, "recv_message", lambda sock, timeout=None: fake.response)
    monkeypatch.setattr(sys, "argv", ["ssh_interactive.py", "pi", "read", "dbg", "--since", "0"])

    with pytest.raises(SystemExit) as exc_info:
        ssh_interactive.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "line\n(Pdb) "


def test_read_json_prints_json(monkeypatch, capsys):
    fake = FakeSocket({"success": True, "output": "ok", "seq_end": 1})

    monkeypatch.setattr(ssh_interactive, "ensure_daemon", lambda alias: None)
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210, "token": "daemon-token"},
    )
    monkeypatch.setattr(ssh_interactive, "connect_to_daemon", lambda alias: fake)
    monkeypatch.setattr(ssh_interactive, "send_message", lambda sock, data: None)
    monkeypatch.setattr(ssh_interactive, "recv_message", lambda sock, timeout=None: fake.response)
    monkeypatch.setattr(sys, "argv", ["ssh_interactive.py", "pi", "read", "dbg", "--json"])

    with pytest.raises(SystemExit) as exc_info:
        ssh_interactive.main()

    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["output"] == "ok"


def test_main_uses_client_timeout_longer_than_daemon_wait(monkeypatch, capsys):
    calls = {}

    def fake_request(alias, payload, timeout=300):
        calls["alias"] = alias
        calls["payload"] = payload
        calls["timeout"] = timeout
        return {"success": True, "seq_end": 3}

    monkeypatch.setattr(ssh_interactive, "request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ssh_interactive.py", "pi", "send", "dbg", "next", "--wait-for", "PROMPT", "--timeout", "600"],
    )

    with pytest.raises(SystemExit) as exc_info:
        ssh_interactive.main()

    assert exc_info.value.code == 0
    assert calls["payload"]["timeout"] == 600
    assert calls["timeout"] >= 605
    assert json.loads(capsys.readouterr().out)["seq_end"] == 3


def test_send_wait_for_without_timeout_sets_default_wait_timeout():
    args = ssh_interactive.build_parser().parse_args(
        ["pi", "send", "dbg", "next", "--wait-for", "PROMPT"]
    )

    payload = ssh_interactive.build_payload(args)

    assert payload["wait_for"] == "PROMPT"
    assert payload["timeout"] == ssh_interactive.DEFAULT_WAIT_TIMEOUT


def test_start_daemon_background_waits_past_default_connect_timeout(monkeypatch):
    calls = SimpleNamespace(popen=None, reads=[])
    clock = SimpleNamespace(now=0.0)

    def fake_monotonic():
        return clock.now

    def fake_sleep(interval):
        clock.now += interval

    def fake_read_daemon_info(alias):
        calls.reads.append((alias, clock.now))
        if clock.now >= 30.1:
            return {"pid": 1234, "port": 41234, "alias": alias}
        return None

    def fake_popen(cmd, **kwargs):
        calls.popen = {"cmd": cmd, "kwargs": kwargs}
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr(ssh_interactive.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(ssh_interactive.time, "sleep", fake_sleep)
    monkeypatch.setattr(ssh_interactive, "read_daemon_info", fake_read_daemon_info)
    monkeypatch.setattr(ssh_interactive.subprocess, "Popen", fake_popen)

    assert ssh_interactive.start_daemon_background("pi") is True
    assert calls.popen is not None
    assert max(read_at for _, read_at in calls.reads) >= 30.1


def test_ensure_daemon_rechecks_metadata_inside_start_lock(monkeypatch):
    calls = SimpleNamespace(reads=0, starts=0, lock_aliases=[])

    class FakeLock:
        def __init__(self, alias, timeout=None):
            self.alias = alias
            self.timeout = timeout

        def __enter__(self):
            calls.lock_aliases.append(self.alias)

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_read_daemon_info(alias):
        calls.reads += 1
        if calls.reads == 1:
            return None
        return {"pid": 1234, "port": 41234, "alias": alias}

    def fake_start_daemon_background(alias):
        calls.starts += 1
        return True

    monkeypatch.setattr(ssh_interactive, "read_daemon_info", fake_read_daemon_info)
    monkeypatch.setattr(ssh_interactive, "daemon_start_lock", FakeLock)
    monkeypatch.setattr(
        ssh_interactive,
        "start_daemon_background",
        fake_start_daemon_background,
    )

    ssh_interactive.ensure_daemon("pi")

    assert calls.lock_aliases == ["pi"]
    assert calls.reads == 2
    assert calls.starts == 0


def test_ensure_daemon_start_lock_timeout_covers_daemon_start(monkeypatch):
    calls = SimpleNamespace(lock_timeouts=[])

    class FakeLock:
        def __init__(self, alias, timeout=None):
            assert alias == "pi"
            calls.lock_timeouts.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(ssh_interactive, "read_daemon_info", lambda alias: None)
    monkeypatch.setattr(ssh_interactive, "daemon_start_lock", FakeLock)
    monkeypatch.setattr(ssh_interactive, "start_daemon_background", lambda alias: True)

    ssh_interactive.ensure_daemon("pi")

    assert calls.lock_timeouts
    assert calls.lock_timeouts[0] >= ssh_interactive.DAEMON_START_TIMEOUT


def test_failed_response_exits_nonzero(monkeypatch, capsys):
    fake = FakeSocket({"success": False, "stderr": "session not found: dbg"})

    monkeypatch.setattr(ssh_interactive, "ensure_daemon", lambda alias: None)
    monkeypatch.setattr(
        ssh_interactive,
        "read_daemon_info",
        lambda alias: {"pid": 1234, "port": 43210, "token": "daemon-token"},
    )
    monkeypatch.setattr(ssh_interactive, "connect_to_daemon", lambda alias: fake)
    monkeypatch.setattr(ssh_interactive, "send_message", lambda sock, data: None)
    monkeypatch.setattr(ssh_interactive, "recv_message", lambda sock, timeout=None: fake.response)
    monkeypatch.setattr(sys, "argv", ["ssh_interactive.py", "pi", "status", "dbg"])

    with pytest.raises(SystemExit) as exc_info:
        ssh_interactive.main()

    assert exc_info.value.code == 1
    assert "session not found" in capsys.readouterr().err
