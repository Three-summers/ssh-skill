#!/usr/bin/env python3
"""Local daemon for managing interactive SSH PTY sessions."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
from typing import Any


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(SCRIPT_DIR, "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

from config_v3 import SSHConfigLoaderV3
from interactive_protocol import (
    DEFAULT_WAIT_TIMEOUT,
    read_daemon_info,
    recv_message,
    remove_daemon_info,
    send_message,
    validate_session_name,
    write_daemon_info,
)
from interactive_session import InteractiveSession


IDLE_TIMEOUT = 1800
HEARTBEAT_INTERVAL = 60


def _public_daemon_info(info: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in info.items() if key != "token"}


class InteractiveDaemon:
    def __init__(self, alias: str, idle_timeout: int = IDLE_TIMEOUT):
        self.alias = validate_session_name(alias)
        self.idle_timeout = idle_timeout
        self._last_activity = time.time()
        self._running = False
        self._token: str | None = None
        self._server_socket: socket.socket | None = None
        self._ssh_client: Any = None
        self._connection_params: dict[str, Any] | None = None
        self._sessions: dict[str, InteractiveSession] = {}
        self._sessions_lock = threading.RLock()

    def start(self) -> None:
        self._running = True
        try:
            self._load_config()
            self._connect_ssh()
            self._server_socket = self._bind_server_socket()
            port = self._server_socket.getsockname()[1]
            self._token = secrets.token_urlsafe(32)

            info = {
                "pid": os.getpid(),
                "port": port,
                "host": self._host_info(),
                "started_at": time.time(),
                "token": self._token,
            }
            write_daemon_info(self.alias, info)
            print(
                json.dumps(
                    {"success": True, "alias": self.alias, **_public_daemon_info(info)}
                ),
                flush=True,
            )

            threading.Thread(target=self._idle_loop, daemon=True).start()
            while self._running:
                try:
                    client_sock, _ = self._server_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._running:
                        raise
                    break
                threading.Thread(
                    target=self._handle_client,
                    args=(client_sock,),
                    daemon=True,
                ).start()
        finally:
            self._shutdown()

    def _bind_server_socket(self) -> socket.socket:
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", 0))
        self._server_socket.listen()
        self._server_socket.settimeout(5.0)
        return self._server_socket

    def _load_config(self) -> dict[str, Any]:
        loader = SSHConfigLoaderV3()
        self._connection_params = loader.get_connection_params(self.alias)
        return self._connection_params

    def _connect_ssh(self) -> Any:
        import paramiko

        params = self._connection_params or self._load_config()
        host = params.get("hostname")
        user = params.get("user")
        if not host or not user:
            raise ValueError("interactive PTY requires hostname and user")
        if params.get("proxy_jump"):
            raise ValueError("interactive PTY Paramiko backend does not support ProxyJump")

        key_file = params.get("key_file")
        if key_file:
            key_file = os.path.abspath(os.path.expanduser(str(key_file)))

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": host,
            "port": int(params.get("port", 22)),
            "username": user,
            "timeout": params.get("timeout", 30),
        }
        password = params.get("password")
        if password:
            connect_kwargs.update(
                {
                    "password": password,
                    "look_for_keys": False,
                    "allow_agent": False,
                }
            )
        elif key_file:
            connect_kwargs.update(
                {
                    "key_filename": key_file,
                    "look_for_keys": True,
                    "allow_agent": True,
                }
            )
        else:
            connect_kwargs.update({"look_for_keys": True, "allow_agent": True})

        client.connect(**connect_kwargs)
        self._ssh_client = client
        return self._ssh_client

    def _host_info(self) -> str:
        params = self._connection_params or {}
        user = params.get("user") or ""
        hostname = params.get("hostname") or self.alias
        return f"{user}@{hostname}" if user else str(hostname)

    def _is_ssh_alive(self) -> bool:
        try:
            if self._ssh_client is None:
                return False
            transport = self._ssh_client.get_transport()
            if not transport or not transport.is_active():
                return False
            transport.send_ignore()
            return True
        except Exception:
            return False

    def _handle_client(self, sock: socket.socket) -> None:
        try:
            sock.settimeout(300)
            request = recv_message(sock)
            response = self._handle_request(request)
            send_message(sock, response)
        except Exception as exc:
            try:
                send_message(sock, self._error(str(exc)))
            except Exception:
                pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handle_request(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return self._error("invalid request")

        if self._token is None or request.get("token") != self._token:
            return self._error("unauthorized")

        self._last_activity = time.time()
        action = request.get("action")

        try:
            if action == "ping":
                return {
                    "success": True,
                    "alias": self.alias,
                    "host": self._host_info(),
                    "ssh_alive": self._is_ssh_alive(),
                }
            if action == "start":
                return self._start_session(
                    request.get("session") or request.get("name"),
                    request.get("command"),
                    rows=request.get("rows", 24),
                    cols=request.get("cols", 80),
                )
            if action == "send":
                return self._send_to_session(
                    request.get("session") or request.get("name"),
                    request.get("text", ""),
                    raw=request.get("raw", False),
                    wait_for=request.get("wait_for"),
                    timeout=request.get("timeout"),
                )
            if action == "read":
                return self._read_session(
                    request.get("session") or request.get("name"),
                    since=request.get("since", 0),
                    wait_for=request.get("wait_for"),
                    timeout=request.get("timeout"),
                )
            if action == "control":
                return self._control_session(
                    request.get("session") or request.get("name"),
                    request.get("control") or request.get("key"),
                )
            if action == "resize":
                return self._resize_session(
                    request.get("session") or request.get("name"),
                    rows=request.get("rows", 24),
                    cols=request.get("cols", 80),
                )
            if action == "status":
                return self._status_session(request.get("session") or request.get("name"))
            if action == "list":
                return self._list_sessions()
            if action == "stop":
                return self._stop_session(request.get("session") or request.get("name"))
            if action == "shutdown":
                self._stop_accept_loop()
                return {"success": True}
            return self._error(f"unknown action: {action}")
        except Exception as exc:
            return self._error(str(exc))

    def _start_session(
        self,
        name: str,
        command: str,
        rows: int = 24,
        cols: int = 80,
    ) -> dict[str, Any]:
        try:
            name = validate_session_name(name)
        except ValueError as exc:
            return self._error(str(exc))
        if not command:
            return self._error("command is required")

        with self._sessions_lock:
            if name in self._sessions:
                return self._error(f"session already exists: {name}")
            if self._ssh_client is None:
                return self._error("ssh client is not connected")
            transport = self._ssh_client.get_transport()
            if not transport or not transport.is_active():
                return self._error("ssh transport is not active")

            channel = transport.open_session()
            channel.get_pty(term="xterm-256color", width=int(cols), height=int(rows))
            channel.exec_command(command)
            session = InteractiveSession(
                name=name,
                channel=channel,
                command=command,
                rows=int(rows),
                cols=int(cols),
            )
            session.start_reader()
            self._sessions[name] = session
            self._last_activity = time.time()

        return {"success": True, "session": name, "snapshot": session.snapshot()}

    def _get_session(
        self,
        name: str,
    ) -> tuple[InteractiveSession | None, dict[str, Any] | None]:
        try:
            name = validate_session_name(name)
        except ValueError as exc:
            return None, self._error(str(exc))

        with self._sessions_lock:
            session = self._sessions.get(name)
        if session is None:
            return None, self._error(f"session not found: {name}")
        return session, None

    def _send_to_session(
        self,
        name: str,
        text: str,
        raw: bool = False,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session, error = self._get_session(name)
        if error:
            return error

        assert session is not None
        baseline = None
        if wait_for is not None:
            baseline = session.snapshot()["seq_end"]

        result = session.send_text(text, raw=raw)
        self._last_activity = time.time()
        if wait_for is None:
            return result

        since = result.get("seq_end")
        if since is None:
            since = baseline
        read_result = session.read(
            since=since,
            wait_for=wait_for,
            timeout=self._effective_wait_timeout(wait_for, timeout),
        )
        read_result.update({"bytes": result.get("bytes", 0)})
        if read_result.get("success") is False and read_result.get("error"):
            read_result.setdefault("exit_code", -1)
            read_result.setdefault("stdout", "")
            read_result.setdefault("stderr", read_result["error"])
        return read_result

    def _read_session(
        self,
        name: str,
        since: int = 0,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session, error = self._get_session(name)
        if error:
            return error

        assert session is not None
        self._last_activity = time.time()
        return session.read(
            since=int(since),
            wait_for=wait_for,
            timeout=self._effective_wait_timeout(wait_for, timeout),
        )

    def _control_session(self, name: str, control: str) -> dict[str, Any]:
        session, error = self._get_session(name)
        if error:
            return error

        assert session is not None
        result = session.send_control(control)
        self._last_activity = time.time()
        return result

    def _resize_session(self, name: str, rows: int = 24, cols: int = 80) -> dict[str, Any]:
        session, error = self._get_session(name)
        if error:
            return error

        assert session is not None
        result = session.resize(cols=int(cols), rows=int(rows))
        self._last_activity = time.time()
        return result

    def _status_session(self, name: str) -> dict[str, Any]:
        session, error = self._get_session(name)
        if error:
            return error

        assert session is not None
        return {"success": True, "session": name, "snapshot": session.snapshot()}

    def _list_sessions(self) -> dict[str, Any]:
        with self._sessions_lock:
            self._prune_terminal_sessions_locked()
            sessions = [session.snapshot() for session in self._sessions.values()]
        return {"success": True, "sessions": sessions}

    def _stop_session(self, name: str) -> dict[str, Any]:
        try:
            name = validate_session_name(name)
        except ValueError as exc:
            return self._error(str(exc))

        with self._sessions_lock:
            session = self._sessions.pop(name, None)
        if session is None:
            return self._error(f"session not found: {name}")

        result = session.close()
        self._last_activity = time.time()
        return {"success": True, "session": name, **result}

    def _idle_loop(self) -> None:
        while self._running:
            time.sleep(min(HEARTBEAT_INTERVAL, max(1, self.idle_timeout)))
            with self._sessions_lock:
                self._prune_terminal_sessions_locked()
                has_sessions = bool(self._sessions)
            if has_sessions:
                continue
            if time.time() - self._last_activity >= self.idle_timeout:
                self._stop_accept_loop()
                return

    def _prune_terminal_sessions(self) -> None:
        with self._sessions_lock:
            self._prune_terminal_sessions_locked()

    def _prune_terminal_sessions_locked(self) -> None:
        terminal_names = []
        for name, session in self._sessions.items():
            try:
                state = session.snapshot().get("state")
            except Exception:
                continue
            if state != "running":
                terminal_names.append(name)
        for name in terminal_names:
            self._sessions.pop(name, None)

    @staticmethod
    def _effective_wait_timeout(
        wait_for: str | None,
        timeout: float | None,
    ) -> float | None:
        if wait_for is not None and timeout is None:
            return DEFAULT_WAIT_TIMEOUT
        return timeout

    def _stop_accept_loop(self) -> None:
        self._running = False
        if self._server_socket is not None:
            try:
                wake_sock = socket.create_connection(
                    self._server_socket.getsockname(),
                    timeout=1.0,
                )
                wake_sock.close()
            except Exception:
                pass
            try:
                self._server_socket.close()
            except OSError:
                pass

    def _shutdown(self) -> None:
        self._running = False
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

        if self._ssh_client is not None:
            try:
                self._ssh_client.close()
            except Exception:
                pass
            self._ssh_client = None

        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None

        remove_daemon_info(self.alias)

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": message}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive SSH PTY daemon")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("alias")
    start_parser.add_argument("--idle-timeout", type=int, default=IDLE_TIMEOUT)

    args = parser.parse_args(argv)
    if args.command == "start":
        existing = read_daemon_info(args.alias)
        if existing:
            print(
                json.dumps(
                    {
                        "success": True,
                        "already_running": True,
                        "alias": args.alias,
                        **_public_daemon_info(existing),
                    }
                ),
                flush=True,
            )
            return 0

        daemon = InteractiveDaemon(args.alias, idle_timeout=args.idle_timeout)
        daemon.start()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
