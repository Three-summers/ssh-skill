#!/usr/bin/env python3
"""User-facing CLI for interactive SSH PTY sessions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(SCRIPT_DIR, "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

from interactive_protocol import (
    DEFAULT_WAIT_TIMEOUT,
    connect_to_daemon,
    daemon_start_lock,
    read_daemon_info,
    recv_message,
    send_message,
)


DAEMON_START_TIMEOUT = 35.0
DAEMON_START_LOCK_MARGIN = 5.0


def start_daemon_background(alias: str) -> bool:
    """Start the local daemon for an alias and wait briefly for metadata."""
    daemon_path = os.path.join(SCRIPT_DIR, "ssh_interactive_daemon.py")
    cmd = [sys.executable, daemon_path, "start", alias]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)
    deadline = time.monotonic() + DAEMON_START_TIMEOUT
    while time.monotonic() < deadline:
        if read_daemon_info(alias):
            return True
        time.sleep(0.1)
    return bool(read_daemon_info(alias))


def ensure_daemon(alias: str) -> None:
    """Ensure the local daemon is running for an alias."""
    if read_daemon_info(alias):
        return
    lock_timeout = DAEMON_START_TIMEOUT + DAEMON_START_LOCK_MARGIN
    with daemon_start_lock(alias, timeout=lock_timeout):
        if read_daemon_info(alias):
            return
        if not start_daemon_background(alias):
            raise RuntimeError(f"failed to start interactive daemon for alias: {alias}")


def request(alias: str, payload: dict[str, Any], timeout: float | None = 300) -> Any:
    """Send a daemon request and return its response."""
    last_error: ConnectionError | None = None
    for attempt in range(2):
        ensure_daemon(alias)
        info = read_daemon_info(alias)
        token = info.get("token") if isinstance(info, dict) else None
        if not token:
            raise RuntimeError(f"missing interactive daemon token for alias: {alias}")
        request_payload = dict(payload)
        request_payload["token"] = token
        try:
            sock = connect_to_daemon(alias)
        except ConnectionError as exc:
            last_error = exc
            if attempt == 0:
                continue
            raise
        try:
            send_message(sock, request_payload)
            return recv_message(sock, timeout=timeout)
        finally:
            sock.close()

    assert last_error is not None
    raise last_error


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2))


def handle_response(
    response: dict[str, Any],
    raw_output: bool = False,
    json_output: bool = False,
) -> int:
    if response.get("success"):
        if raw_output and not json_output:
            sys.stdout.write(str(response.get("output", "")))
            return 0
        print_json(response)
        return 0

    message = response.get("stderr") or response.get("error") or "request failed"
    print(message, file=sys.stderr)
    if json_output:
        print_json(response)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage interactive SSH PTY sessions")
    parser.add_argument("alias")
    subparsers = parser.add_subparsers(dest="action", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("session")
    start_parser.add_argument("--command", required=True)
    start_parser.add_argument("--rows", type=int, default=24)
    start_parser.add_argument("--cols", type=int, default=80)

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("session")
    send_parser.add_argument("input")
    send_parser.add_argument("--raw", action="store_true")
    send_parser.add_argument("--wait-for")
    send_parser.add_argument("--timeout", type=float)

    read_parser = subparsers.add_parser("read")
    read_parser.add_argument("session")
    read_parser.add_argument("--since", type=int, default=0)
    read_parser.add_argument("--wait-for")
    read_parser.add_argument("--timeout", type=float)
    read_parser.add_argument("--json", action="store_true", dest="json_output")

    control_parser = subparsers.add_parser("control")
    control_parser.add_argument("session")
    control_parser.add_argument("control")

    resize_parser = subparsers.add_parser("resize")
    resize_parser.add_argument("session")
    resize_parser.add_argument("--cols", type=int, required=True)
    resize_parser.add_argument("--rows", type=int, required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("session")

    subparsers.add_parser("list")

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("session")

    return parser


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "start":
        return {
            "action": "start",
            "session": args.session,
            "command": args.command,
            "rows": args.rows,
            "cols": args.cols,
        }
    if args.action == "send":
        timeout = args.timeout
        if args.wait_for is not None and timeout is None:
            timeout = DEFAULT_WAIT_TIMEOUT
        return {
            "action": "send",
            "session": args.session,
            "input": args.input,
            "text": args.input,
            "raw": args.raw,
            "wait_for": args.wait_for,
            "timeout": timeout,
        }
    if args.action == "read":
        timeout = args.timeout
        if args.wait_for is not None and timeout is None:
            timeout = DEFAULT_WAIT_TIMEOUT
        return {
            "action": "read",
            "session": args.session,
            "since": args.since,
            "wait_for": args.wait_for,
            "timeout": timeout,
        }
    if args.action == "control":
        return {
            "action": "control",
            "session": args.session,
            "control": args.control,
        }
    if args.action == "resize":
        return {
            "action": "resize",
            "session": args.session,
            "cols": args.cols,
            "rows": args.rows,
        }
    if args.action == "status":
        return {"action": "status", "session": args.session}
    if args.action == "list":
        return {"action": "list"}
    if args.action == "stop":
        return {"action": "stop", "session": args.session}
    raise ValueError(f"unknown action: {args.action}")


def client_timeout_for(args: argparse.Namespace) -> float:
    timeout = getattr(args, "timeout", None)
    wait_for = getattr(args, "wait_for", None)
    if timeout is not None:
        return max(300, timeout + 5)
    if wait_for is not None:
        return 300
    return 300


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    response = request(args.alias, build_payload(args), timeout=client_timeout_for(args))
    return handle_response(
        response,
        raw_output=args.action == "read",
        json_output=getattr(args, "json_output", False),
    )


def main() -> None:
    try:
        raise SystemExit(_main())
    except SystemExit:
        raise
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
