"""
Protocol helpers for local interactive SSH daemon control sockets.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
import re
import socket
import stat
import struct
import tempfile
import time
from typing import Any, Iterator


INTERACTIVE_DAEMON_DIR = os.path.join(tempfile.gettempdir(), "ssh_interactive_daemon")
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
RECV_BUFFER = 65536
DEFAULT_WAIT_TIMEOUT = 30.0

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def validate_session_name(name: str) -> str:
    """Validate a local daemon/session alias."""
    if not isinstance(name, str) or not _SESSION_NAME_RE.fullmatch(name):
        raise ValueError("invalid session name")
    return name


def get_daemon_id(alias: str) -> str:
    """Return a stable short id for a daemon alias."""
    validate_session_name(alias)
    return hashlib.md5(alias.lower().encode("utf-8")).hexdigest()[:12]


def get_daemon_info_path(alias: str) -> str:
    """Return the metadata path for an alias, creating the metadata directory."""
    _ensure_daemon_dir()
    return os.path.join(INTERACTIVE_DAEMON_DIR, f"{get_daemon_id(alias)}.json")


def get_daemon_start_lock_path(alias: str) -> str:
    """Return the local daemon start lock path for an alias."""
    _ensure_daemon_dir()
    return os.path.join(INTERACTIVE_DAEMON_DIR, f"{get_daemon_id(alias)}.lock")


@contextmanager
def daemon_start_lock(alias: str, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a simple interprocess lock while starting a daemon."""
    lock_path = get_daemon_start_lock_path(alias)
    deadline = time.monotonic() + timeout
    fd: int | None = None

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.write(fd, str(os.getpid()).encode("ascii"))
            break
        except FileExistsError:
            if _remove_stale_daemon_start_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out acquiring daemon start lock for alias: {alias}"
                )
            time.sleep(0.05)
        except OSError:
            raise

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _remove_stale_daemon_start_lock(lock_path: str) -> bool:
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            pid_text = f.read().strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        pid = -1

    if is_process_alive(pid):
        return False

    try:
        os.remove(lock_path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _ensure_daemon_dir() -> None:
    try:
        os.makedirs(INTERACTIVE_DAEMON_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise RuntimeError("invalid daemon metadata directory") from exc

    try:
        stat_info = os.lstat(INTERACTIVE_DAEMON_DIR)
        if not stat.S_ISDIR(stat_info.st_mode):
            raise RuntimeError("invalid daemon metadata directory")
        if os.name != "nt" and stat_info.st_uid != os.getuid():
            raise RuntimeError("invalid daemon metadata directory")
        os.chmod(INTERACTIVE_DAEMON_DIR, 0o700)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("invalid daemon metadata directory") from exc


def is_process_alive(pid: int) -> bool:
    """Check whether a process id is alive."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
    except (TypeError, ValueError):
        return False

    if os.name == "nt":
        return _is_windows_process_alive(pid)

    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    except ValueError:
        return False


def _is_windows_process_alive(pid: int) -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, PermissionError, ValueError):
        return False


def read_daemon_info(alias: str) -> dict[str, Any] | None:
    """Read daemon metadata, removing stale metadata for dead processes."""
    path = get_daemon_info_path(alias)
    try:
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(info, dict):
        return None

    if not is_process_alive(info.get("pid")):
        remove_daemon_info(alias)
        return None

    return info


def write_daemon_info(alias: str, info: dict[str, Any]) -> str:
    """Write daemon metadata and return the metadata path."""
    validate_session_name(alias)
    path = get_daemon_info_path(alias)
    data = dict(info)
    data["alias"] = alias
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{get_daemon_id(alias)}.",
        suffix=".tmp",
        dir=INTERACTIVE_DAEMON_DIR,
        text=True,
    )
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        _fsync_directory(INTERACTIVE_DAEMON_DIR)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return path


def _fsync_directory(path: str) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def remove_daemon_info(alias: str) -> None:
    """Remove daemon metadata for an alias if it exists."""
    try:
        os.remove(get_daemon_info_path(alias))
    except FileNotFoundError:
        return
    except OSError:
        return


def send_message(sock: socket.socket, data: Any) -> None:
    """Send one length-prefixed JSON message."""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("message too large")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_message(sock: socket.socket, timeout: float | None = None) -> Any:
    """Receive one length-prefixed JSON message."""
    previous_timeout = sock.gettimeout()
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        header = _recv_exact(sock, 4)
        length = struct.unpack("!I", header)[0]
        if length > MAX_MESSAGE_BYTES:
            raise ValueError("message too large")
        payload = _recv_exact(sock, length)
        return json.loads(payload.decode("utf-8"))
    finally:
        if timeout is not None:
            sock.settimeout(previous_timeout)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(min(RECV_BUFFER, remaining))
        if not chunk:
            raise ConnectionError("socket closed while receiving message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def connect_to_daemon(alias: str, timeout: float = 5.0) -> socket.socket:
    """Connect to the daemon control socket for an alias."""
    info = read_daemon_info(alias)
    if not info:
        raise ConnectionError("daemon metadata missing")

    try:
        port = _validate_port(info["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectionError("invalid daemon metadata") from exc

    try:
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError as exc:
        remove_daemon_info(alias)
        raise ConnectionError("daemon connection failed") from exc


def _validate_port(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid port")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.isdigit():
        port = int(value)
    else:
        raise ValueError("invalid port")
    if port < 1 or port > 65535:
        raise ValueError("invalid port")
    return port
