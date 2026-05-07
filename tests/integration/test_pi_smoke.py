import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

HOST = os.environ.get("SSH_SKILL_TEST_HOST")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not HOST,
        reason="set SSH_SKILL_TEST_HOST to run real SSH tests",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _redact(text):
    redacted = []
    for line in text.splitlines():
        lowered = line.lower()
        if "password" in lowered or "passphrase" in lowered:
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


def _run_script(script_name, args, timeout=90, check=True):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script_name), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None

    if check:
        assert result.returncode == 0, _failure(result)
        assert payload is not None, _failure(result)
        assert payload.get("success") is True, _failure(result)

    return payload, result


@pytest.fixture
def remote_root():
    root = f"/tmp/ssh-skill-test-{uuid.uuid4().hex}"
    _run_script(
        "ssh_execute.py",
        [HOST, f"mkdir -p {shlex.quote(root)} && printf ready", "--no-daemon"],
    )
    try:
        yield root
    finally:
        _run_script(
            "ssh_execute.py",
            [HOST, f"rm -rf {shlex.quote(root)}", "--no-daemon"],
            check=False,
        )


def test_execute_command_against_real_host():
    payload, _ = _run_script(
        "ssh_execute.py",
        [HOST, "printf ssh-skill-ok", "--no-daemon"],
    )

    assert payload["stdout"] == "ssh-skill-ok"
    assert payload["exit_code"] == 0


def test_upload_and_download_small_file(remote_root, tmp_path):
    content = "hello from ssh skill integration\n"
    local_file = tmp_path / "upload.txt"
    local_file.write_text(content, encoding="utf-8")
    remote_file = f"{remote_root}/uploaded.txt"

    _run_script(
        "ssh_upload.py",
        [HOST, str(local_file), remote_file, "--no-progress"],
    )
    payload, _ = _run_script(
        "ssh_execute.py",
        [HOST, f"cat {shlex.quote(remote_file)}", "--no-daemon"],
    )
    assert payload["stdout"] == content

    downloaded = tmp_path / "downloaded.txt"
    _run_script(
        "ssh_download.py",
        [HOST, remote_file, str(downloaded), "--no-progress"],
    )
    assert downloaded.read_text(encoding="utf-8") == content


def test_recursive_upload_and_download_directory(remote_root, tmp_path):
    local_dir = tmp_path / "tree"
    nested_dir = local_dir / "nested"
    nested_dir.mkdir(parents=True)
    (local_dir / "root.txt").write_text("root-file\n", encoding="utf-8")
    (nested_dir / "child.txt").write_text("child-file\n", encoding="utf-8")

    remote_dir = f"{remote_root}/tree"
    _run_script(
        "ssh_upload.py",
        [HOST, str(local_dir), remote_dir, "--recursive", "--no-progress"],
        timeout=120,
    )

    payload, _ = _run_script(
        "ssh_execute.py",
        [
            HOST,
            f"cat {shlex.quote(remote_dir + '/root.txt')} "
            f"{shlex.quote(remote_dir + '/nested/child.txt')}",
            "--no-daemon",
        ],
    )
    assert payload["stdout"] == "root-file\nchild-file\n"

    downloaded_dir = tmp_path / "downloaded-tree"
    _run_script(
        "ssh_download.py",
        [HOST, remote_dir, str(downloaded_dir), "--recursive", "--no-progress"],
        timeout=120,
    )
    assert (downloaded_dir / "root.txt").read_text(encoding="utf-8") == "root-file\n"
    assert (downloaded_dir / "nested" / "child.txt").read_text(
        encoding="utf-8"
    ) == "child-file\n"


def test_server_transfer_stream_mode_on_real_host(remote_root):
    content = "server-transfer-content\n"
    source_file = f"{remote_root}/source.txt"
    dest_file = f"{remote_root}/dest.txt"

    _run_script(
        "ssh_execute.py",
        [
            HOST,
            f"printf %s {shlex.quote(content)} > {shlex.quote(source_file)}",
            "--no-daemon",
        ],
    )
    payload, _ = _run_script(
        "ssh_server_transfer.py",
        [HOST, source_file, HOST, dest_file, "--mode", "stream", "--no-progress"],
        timeout=120,
    )

    assert payload["mode"] == "stream"
    assert payload["files_transferred"] == 1
    check, _ = _run_script(
        "ssh_execute.py",
        [HOST, f"cat {shlex.quote(dest_file)}", "--no-daemon"],
    )
    assert check["stdout"] == content
