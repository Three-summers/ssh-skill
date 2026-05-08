"""Core primitives for managing interactive SSH PTY sessions."""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Any


CONTROL_BYTES = {"ctrl-c": b"\x03", "ctrl-d": b"\x04", "enter": b"\n"}


@dataclass
class OutputChunk:
    seq: int
    timestamp: float
    text: str


class OutputBuffer:
    def __init__(self, max_bytes: int = 4 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._chunks: list[OutputChunk] = []
        self._next_seq = 1
        self._byte_count = 0
        self._truncated = False
        self._condition = threading.Condition()

    def append(self, text: str) -> int:
        encoded_len = len(text.encode("utf-8", errors="replace"))

        with self._condition:
            seq = self._next_seq
            self._next_seq += 1
            self._chunks.append(OutputChunk(seq=seq, timestamp=time.time(), text=text))
            self._byte_count += encoded_len

            while self._chunks and self._byte_count > self.max_bytes:
                removed = self._chunks.pop(0)
                self._byte_count -= len(removed.text.encode("utf-8", errors="replace"))
                self._truncated = True

            self._condition.notify_all()
            return seq

    def read_since(self, since: int = 0) -> dict[str, Any]:
        with self._condition:
            return self._read_since_locked(since)

    def wait_for(self, since: int, pattern: str, timeout: float | None) -> dict[str, Any]:
        compiled = re.compile(pattern)
        deadline = None if timeout is None else time.monotonic() + timeout

        with self._condition:
            while True:
                result = self._read_since_locked(since)
                if compiled.search(result["output"]):
                    result.update({"success": True, "matched": True})
                    return result

                if deadline is None:
                    self._condition.wait()
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.update(
                        {
                            "success": False,
                            "matched": False,
                            "error": "wait-for timeout",
                        }
                    )
                    return result

                self._condition.wait(remaining)

    def _read_since_locked(self, since: int) -> dict[str, Any]:
        seq_end = self._next_seq - 1
        chunks = [chunk for chunk in self._chunks if chunk.seq > since]
        output = "".join(chunk.text for chunk in chunks)
        seq_start = chunks[0].seq if chunks else seq_end
        truncated = self._truncated and (
            chunks[0].seq > since + 1 if chunks else seq_end > since
        )

        return {
            "seq_start": seq_start,
            "seq_end": seq_end,
            "output": output,
            "truncated": truncated,
        }


class InteractiveSession:
    def __init__(
        self,
        name: str,
        channel: Any,
        command: str,
        rows: int = 24,
        cols: int = 80,
        max_buffer_bytes: int = 4 * 1024 * 1024,
    ):
        self.name = name
        self.channel = channel
        self.command = command
        self.rows = rows
        self.cols = cols
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.state = "running"
        self.reader_error: str | None = None
        self.output = OutputBuffer(max_bytes=max_buffer_bytes)
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def start_reader(self, poll_interval: float = 0.05) -> None:
        with self._lock:
            if self._reader_thread and self._reader_thread.is_alive():
                return

            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                args=(poll_interval,),
                daemon=True,
            )
            self._reader_thread.start()

    def record_output(self, text: str) -> int:
        seq = self.output.append(text)
        with self._lock:
            self.last_activity = time.time()
        return seq

    def send_text(self, text: str, raw: bool = False) -> dict[str, Any]:
        payload = text if raw else f"{text}\n"
        with self._lock:
            self._ensure_running_locked()
            sent = self._send_all_locked(payload.encode("utf-8", errors="replace"))
            self.last_activity = time.time()
        return {"success": True, "bytes": sent}

    def send_control(self, name: str) -> dict[str, Any]:
        if name not in CONTROL_BYTES:
            raise ValueError(f"unknown control: {name}")

        with self._lock:
            self._ensure_running_locked()
            sent = self._send_all_locked(CONTROL_BYTES[name])
            self.last_activity = time.time()
        return {"success": True, "bytes": sent}

    def resize(self, cols: int, rows: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_running_locked()
            self.channel.resize_pty(width=cols, height=rows)
            self.cols = cols
            self.rows = rows
            self.last_activity = time.time()
        return {"success": True}

    def read(
        self,
        since: int = 0,
        wait_for: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if wait_for is None:
            result = self.output.read_since(since)
            result.update({"success": True})
            return result

        return self.output.wait_for(since, wait_for, timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self.state
            rows = self.rows
            cols = self.cols
            last_activity = self.last_activity

        return {
            "name": self.name,
            "command": self.command,
            "state": state,
            "rows": rows,
            "cols": cols,
            "created_at": self.created_at,
            "last_activity": last_activity,
            "seq_end": self.output.read_since(0)["seq_end"],
        }

    def close(self) -> dict[str, Any]:
        with self._lock:
            self.state = "closed"
            self.last_activity = time.time()
            self.channel.close()
        return {"success": True}

    def _reader_loop(self, poll_interval: float) -> None:
        try:
            while True:
                with self._lock:
                    if self.state in {"closed", "error"}:
                        return

                if self._channel_ready():
                    data = self.channel.recv(4096)
                    if data:
                        self.record_output(data.decode("utf-8", errors="replace"))
                    else:
                        time.sleep(poll_interval)
                elif self._channel_exited():
                    with self._lock:
                        self.state = "closed"
                        self.last_activity = time.time()
                    return
                else:
                    time.sleep(poll_interval)
        except Exception as exc:  # pragma: no cover - depends on channel failures
            with self._lock:
                if self.state == "closed":
                    return
                self.reader_error = str(exc)
                self.state = "error"
                self.last_activity = time.time()

    def _channel_ready(self) -> bool:
        recv_ready = getattr(self.channel, "recv_ready", None)
        return bool(recv_ready and recv_ready())

    def _channel_exited(self) -> bool:
        exit_status_ready = getattr(self.channel, "exit_status_ready", None)
        return bool(exit_status_ready and exit_status_ready())

    def _ensure_running_locked(self) -> None:
        if self.state != "running":
            raise RuntimeError("session is not running")

    def _send_all_locked(self, payload: bytes) -> int:
        sendall = getattr(self.channel, "sendall", None)
        if sendall:
            sendall(payload)
            return len(payload)

        total_sent = 0
        while total_sent < len(payload):
            sent = self.channel.send(payload[total_sent:])
            if sent <= 0:
                raise RuntimeError("channel send returned 0 bytes")
            total_sent += sent
        return total_sent
