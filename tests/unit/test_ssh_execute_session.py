import json
import sys

import pytest

import ssh_execute


pytestmark = pytest.mark.unit


def test_session_start_command_runs_in_background_and_records_files():
    command = ssh_execute._session_start_remote_command(
        "demo", "cd ~/Script && python3 app.py"
    )

    assert "nohup sh -lc" in command
    assert 'session_dir="$HOME/.ssh-skill/sessions/demo"' in command
    assert 'log_file="$session_dir/session.log"' in command
    assert 'pid_file="$session_dir/pid"' in command
    assert "$HOME/.ssh-skill/sessions/demo/exit_code" in command
    assert "cd ~/Script && python3 app.py" in command


def test_session_name_rejects_shell_metacharacters():
    with pytest.raises(ValueError):
        ssh_execute._session_safe_name("bad; rm -rf /")


def test_session_start_returns_metadata_from_remote_stdout(monkeypatch):
    calls = []

    def fake_direct_execute(alias, command, timeout, use_sudo=False):
        calls.append((alias, command, timeout, use_sudo))
        return {
            "success": True,
            "exit_code": 0,
            "stdout": "pid=123\nremote_dir=$HOME/.ssh-skill/sessions/demo\n"
            "log=$HOME/.ssh-skill/sessions/demo/session.log\n",
            "stderr": "",
        }

    monkeypatch.setattr(ssh_execute, "direct_execute", fake_direct_execute)

    result = ssh_execute.session_start("pi", "python3 app.py", "demo", 30, False)

    assert result["success"] is True
    assert result["session"] == "demo"
    assert result["pid"] == "123"
    assert calls[0][0] == "pi"
    assert calls[0][2] == 30
    assert calls[0][3] is False


def test_session_status_parses_remote_state(monkeypatch):
    monkeypatch.setattr(
        ssh_execute,
        "direct_execute",
        lambda alias, command, timeout, use_sudo=False: {
            "success": True,
            "exit_code": 0,
            "stdout": "session=demo\nstate=running\npid=123\nexit_code=\n"
            "log=$HOME/.ssh-skill/sessions/demo/session.log\n",
            "stderr": "",
        },
    )

    result = ssh_execute.session_status("pi", "demo", 30, False)

    assert result["success"] is True
    assert result["state"] == "running"
    assert result["pid"] == "123"


def test_session_logs_follow_delegates_to_stream(monkeypatch):
    calls = []

    def fake_stream_execute(alias, command, timeout, use_sudo=False):
        calls.append((alias, command, timeout, use_sudo))
        return 0

    monkeypatch.setattr(ssh_execute, "stream_execute", fake_stream_execute)

    assert ssh_execute.session_logs("pi", "demo", lines=20, follow=True, timeout=None) == 0
    assert calls == [
        (
            "pi",
            "tail -n 20 -f $HOME/.ssh-skill/sessions/demo/session.log",
            None,
            False,
        )
    ]


def test_main_session_start_outputs_json(monkeypatch, capsys):
    monkeypatch.setattr(
        ssh_execute,
        "session_start",
        lambda alias, command, session, timeout, use_sudo=False: {
            "success": True,
            "session": session,
            "pid": "123",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ssh_execute.py", "pi", "python3 app.py", "--session", "demo"],
    )

    with pytest.raises(SystemExit) as exc_info:
        ssh_execute.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["session"] == "demo"
