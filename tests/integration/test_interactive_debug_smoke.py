import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

HOST = os.environ.get("SSH_SKILL_TEST_HOST")
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ssh_interactive.py"
EXECUTE = ROOT / "scripts" / "ssh_execute.py"


def run_json(args, timeout=30):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return json.loads(result.stdout)


def run_raw(args, timeout=30):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.skipif(not HOST, reason="SSH_SKILL_TEST_HOST is not set")
def test_interactive_python_prompt_and_parallel_execute():
    session = f"pytest-py-{os.getpid()}"
    try:
        started = run_json(
            [
                HOST,
                "start",
                session,
                "--command",
                "python3 -q",
            ]
        )
        assert started["success"] is True

        sent = run_json(
            [
                HOST,
                "send",
                session,
                "print(6 * 7)",
                "--wait-for",
                ">>>",
                "--timeout",
                "10",
            ]
        )
        assert sent["success"] is True
        assert "42" in sent["output"]

        normal = subprocess.run(
            [sys.executable, str(EXECUTE), HOST, "printf normal-command"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert normal.returncode == 0
        payload = json.loads(normal.stdout)
        assert payload["stdout"] == "normal-command"

        stopped = run_json([HOST, "stop", session])
        assert stopped["success"] is True
    finally:
        run_raw([HOST, "stop", session], timeout=10)
