import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


HOST = os.environ.get("SSH_SKILL_SUDO_TEST_HOST")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not HOST,
        reason="set SSH_SKILL_SUDO_TEST_HOST to run sudo integration tests",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _redact(text):
    redacted = []
    for line in text.splitlines():
        if "password" in line.lower() or "passphrase" in line.lower():
            redacted.append("[redacted sensitive line]")
        else:
            redacted.append(line)
    return "\n".join(redacted)


def _failure(result):
    return (
        f"exit={result.returncode}\n"
        f"stdout={_redact(result.stdout)}\n"
        f"stderr={_redact(result.stderr)}"
    )


def test_sudo_execute_returns_json_result():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "ssh_execute.py"),
            HOST,
            "whoami; id -u",
            "--sudo",
            "--timeout",
            "15",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, _failure(result)
    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "root\n0\n"
    assert "password" not in result.stdout.lower()
    assert "password" not in result.stderr.lower()


def test_sudo_stream_outputs_raw_root_identity():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "ssh_execute.py"),
            HOST,
            "whoami; id -u",
            "--sudo",
            "--stream",
            "--timeout",
            "15",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, _failure(result)
    assert result.stdout == "root\n0\n"
    assert result.stderr == ""
