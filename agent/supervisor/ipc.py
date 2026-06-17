"""Inter-process communication for supervisor and worker agents.

Uses Unix Domain Sockets (with TCP loopback fallback on unsupported platforms)
and newline-delimited JSON messages.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Callable

from agent.supervisor.models import IPCMessage

logger = logging.getLogger("agent.supervisor.ipc")


class IPCError(Exception):
    pass


class IPCConnectionClosedError(IPCError):
    pass


def _can_use_unix_socket() -> bool:
    return hasattr(socket, "AF_UNIX")


def _create_socket() -> socket.socket:
    if _can_use_unix_socket():
        return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


class IPCServer:
    """Server side of the supervisor-worker IPC channel.

    Accepts a single client connection and routes incoming messages to a
    handler callback. Outgoing messages can be sent via `send_to_client`.
    """

    def __init__(self, address: str):
        self.address = address
        self._server_socket: socket.socket | None = None
        self._client_socket: socket.socket | None = None
        self._handler: Callable[[IPCMessage], None] | None = None
        self._listen_thread: threading.Thread | None = None
        self._read_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

    def set_handler(self, handler: Callable[[IPCMessage], None]) -> None:
        self._handler = handler

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        if _can_use_unix_socket():
            path = Path(self.address)
            if path.exists():
                path.unlink()
            self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server_socket.bind(self.address)
        else:
            host, port_str = self.address.rsplit(":", 1)
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind((host, int(port_str)))

        self._server_socket.listen(1)
        self._listen_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._listen_thread.start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client_sock, _ = self._server_socket.accept()
            except OSError:
                if self._running:
                    logger.exception("accept loop failed")
                return
            with self._lock:
                if self._client_socket is not None:
                    try:
                        self._client_socket.close()
                    except OSError:
                        pass
                self._client_socket = client_sock
            read_thread = threading.Thread(target=self._read_loop, daemon=True)
            read_thread.start()

    def _read_loop(self) -> None:
        buffer = b""
        sock = self._client_socket
        try:
            while self._running:
                data = sock.recv(4096)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._process_line(line)
        except OSError:
            logger.debug("client connection closed")
        finally:
            with self._lock:
                self._client_socket = None

    def _process_line(self, line: bytes) -> None:
        try:
            payload = json.loads(line.decode("utf-8"))
            msg = IPCMessage(**payload)
        except Exception:
            logger.warning("received invalid IPC message: %s", line)
            return
        if self._handler:
            try:
                self._handler(msg)
            except Exception:
                logger.exception("IPC handler failed for msg %s", msg.msg_id)

    def send_to_client(self, msg: IPCMessage) -> None:
        with self._lock:
            sock = self._client_socket
        if sock is None:
            raise IPCConnectionClosedError("no client connected")
        data = json.dumps(msg.model_dump(), ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            sock.sendall(data)
        except OSError as exc:
            raise IPCConnectionClosedError("failed to send message") from exc

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._client_socket:
                try:
                    self._client_socket.close()
                except OSError:
                    pass
                self._client_socket = None
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        if _can_use_unix_socket():
            path = Path(self.address)
            if path.exists():
                path.unlink(missing_ok=True)


class IPCClient:
    """Client side of the supervisor-worker IPC channel."""

    def __init__(self, address: str):
        self.address = address
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()

    def connect(self, timeout: float = 5.0) -> None:
        sock = _create_socket()
        sock.settimeout(timeout)
        try:
            if _can_use_unix_socket():
                sock.connect(self.address)
            else:
                host, port_str = self.address.rsplit(":", 1)
                sock.connect((host, int(port_str)))
        except OSError as exc:
            sock.close()
            raise IPCError(f"failed to connect to {self.address}") from exc
        sock.settimeout(None)
        self._socket = sock

    def send(self, msg: IPCMessage) -> None:
        with self._lock:
            sock = self._socket
        if sock is None:
            raise IPCConnectionClosedError("not connected")
        data = json.dumps(msg.model_dump(), ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            sock.sendall(data)
        except OSError as exc:
            raise IPCConnectionClosedError("failed to send message") from exc

    def _send_raw(self, data: bytes) -> None:
        """Send raw bytes; used only for testing invalid input handling."""
        with self._lock:
            sock = self._socket
        if sock is None:
            raise IPCConnectionClosedError("not connected")
        sock.sendall(data)

    def receive(self, timeout: float = 5.0) -> IPCMessage | None:
        sock = self._socket
        if sock is None:
            raise IPCConnectionClosedError("not connected")
        sock.settimeout(timeout)
        buffer = b""
        try:
            while b"\n" not in buffer:
                data = sock.recv(4096)
                if not data:
                    return None
                buffer += data
        except socket.timeout:
            return None
        finally:
            sock.settimeout(None)
        line, _ = buffer.split(b"\n", 1)
        return IPCMessage(**json.loads(line.decode("utf-8")))

    def close(self) -> None:
        with self._lock:
            if self._socket:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None
