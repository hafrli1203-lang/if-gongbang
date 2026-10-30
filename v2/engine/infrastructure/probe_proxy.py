"""A small recording HTTP proxy the engine runs a probe script behind.

The probe runner (engine.application.acceptance_probe) starts one proxy
per probe script run on 127.0.0.1, points the script's environment at
it (proxy_env), and after the script exits reads back which upstream
hosts the script's traffic actually went to. The probe sandbox shares
the host network namespace when net=True, so the loopback listener is
reachable from inside it.

Two request shapes are handled:
  - CONNECT host:port (HTTPS through a proxy): the proxy opens the TCP
    connection itself, answers 200, then relays bytes both ways. TLS
    stays end to end; the proxy only sees the host, the port and how
    many bytes went each way.
  - absolute-form requests ("GET http://host/path HTTP/1.1"): the
    request line is rewritten to origin form, "Connection: close" is
    forced, and the rest is relayed the same way.

Every relayed connection becomes one contact record (see
engine.domain.probe_observation for the record shape and for how a
record counts as having reached a target). Nothing here decides pass
or fail.
"""
from __future__ import annotations

import hashlib
import selectors
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from types import TracebackType
from urllib.parse import urlsplit

_HEAD_LIMIT = 64 * 1024
_CONNECT_TIMEOUT_SEC = 15.0
_IDLE_TIMEOUT_SEC = 30.0
_DRAIN_WAIT_SEC = 3.0
_MAX_CONTACTS = 64


@dataclass
class _Contact:
    host: str
    port: int
    method: str
    connected: bool = False
    bytes_up: int = 0
    bytes_down: int = 0
    digest: "hashlib._Hash" = field(default_factory=hashlib.sha256)

    def as_record(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "method": self.method,
                "connected": self.connected, "bytes_up": self.bytes_up,
                "bytes_down": self.bytes_down,
                "response_sha256": self.digest.hexdigest() if self.bytes_down else ""}


def _read_head(sock: socket.socket) -> tuple[bytes, bytes] | None:
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > _HEAD_LIMIT:
            return None
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _split_host_port(value: str, default_port: int) -> tuple[str, int] | None:
    if value.startswith("["):
        host, _, tail = value[1:].partition("]")
        port_text = tail[1:] if tail.startswith(":") else ""
    else:
        host, sep, port_text = value.rpartition(":")
        if not sep:
            host, port_text = value, ""
    if not host:
        return None
    if not port_text:
        return host, default_port
    if not port_text.isdigit():
        return None
    return host, int(port_text)


def _origin_head(method: str, target: str, version: str, header_lines: list[bytes],
                 ) -> tuple[str, int, bytes] | None:
    parts = urlsplit(target)
    if parts.scheme != "http" or not parts.hostname:
        return None
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    kept = [line for line in header_lines
            if not line.lower().startswith((b"connection:", b"proxy-connection:"))]
    head = b"\r\n".join([f"{method} {path} {version}".encode("latin-1"), *kept,
                         b"Connection: close"]) + b"\r\n\r\n"
    return parts.hostname, port, head


def _relay(client: socket.socket, upstream: socket.socket, contact: _Contact) -> None:
    sel = selectors.DefaultSelector()
    sel.register(client, selectors.EVENT_READ, "up")
    sel.register(upstream, selectors.EVENT_READ, "down")
    open_sides = 2
    try:
        while open_sides:
            events = sel.select(timeout=_IDLE_TIMEOUT_SEC)
            if not events:
                return
            for key, _mask in events:
                src = key.fileobj
                if not isinstance(src, socket.socket):
                    continue
                data = src.recv(65536)
                dst = upstream if key.data == "up" else client
                if not data:
                    sel.unregister(src)
                    open_sides -= 1
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                dst.sendall(data)
                if key.data == "up":
                    contact.bytes_up += len(data)
                else:
                    contact.bytes_down += len(data)
                    contact.digest.update(data)
    except OSError:
        return
    finally:
        sel.close()


class _Handler(socketserver.BaseRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        client = self.request
        client.settimeout(_IDLE_TIMEOUT_SEC)
        try:
            parsed = _read_head(client)
        except OSError:
            return
        if parsed is None:
            return
        head, rest = parsed
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin-1", errors="replace").split()
        if len(request_line) != 3:
            return
        method, target, version = request_line
        if method.upper() == "CONNECT":
            self._connect(client, method, target, rest)
        else:
            self._forward(client, method, target, version, lines[1:], rest)

    def _open(self, contact: _Contact) -> socket.socket | None:
        self.server.recorder.add(contact)
        try:
            upstream = socket.create_connection((contact.host, contact.port),
                                                timeout=_CONNECT_TIMEOUT_SEC)
        except OSError:
            return None
        contact.connected = True
        return upstream

    def _connect(self, client: socket.socket, method: str, target: str, rest: bytes) -> None:
        host_port = _split_host_port(target, 443)
        if host_port is None:
            return
        contact = _Contact(host_port[0], host_port[1], method.upper())
        upstream = self._open(contact)
        if upstream is None:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
                contact.bytes_up += len(rest)
            _relay(client, upstream, contact)

    def _forward(self, client: socket.socket, method: str, target: str, version: str,
                 header_lines: list[bytes], rest: bytes) -> None:
        origin = _origin_head(method, target, version, header_lines)
        if origin is None:
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        host, port, head = origin
        contact = _Contact(host, port, method.upper())
        upstream = self._open(contact)
        if upstream is None:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        with upstream:
            upstream.sendall(head + rest)
            contact.bytes_up += len(head) + len(rest)
            _relay(client, upstream, contact)


class _Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contacts: list[_Contact] = []
        self._active = 0

    def add(self, contact: _Contact) -> None:
        with self._lock:
            if len(self._contacts) < _MAX_CONTACTS:
                self._contacts.append(contact)

    def enter(self) -> None:
        with self._lock:
            self._active += 1

    def leave(self) -> None:
        with self._lock:
            self._active -= 1

    def active(self) -> int:
        with self._lock:
            return self._active

    def records(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(c.as_record() for c in self._contacts)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        self.recorder = _Recorder()
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        self.recorder.enter()
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.recorder.leave()


class RecordingProxy:
    """Context manager: a running proxy on 127.0.0.1:<port> for the
    duration of the `with` block. contacts() is the final record list,
    read after the block ends (in-flight relays get a short drain
    window so a just-finished script's last response is counted)."""

    def __init__(self) -> None:
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._final: tuple[dict[str, object], ...] = ()

    def __enter__(self) -> "RecordingProxy":
        self._server = _Server()
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="probe-proxy", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        server = self._server
        if server is None:
            return
        deadline = time.monotonic() + _DRAIN_WAIT_SEC
        while server.recorder.active() and time.monotonic() < deadline:
            time.sleep(0.02)
        server.shutdown()
        server.server_close()
        self._final = server.recorder.records()
        self._server = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("proxy not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def contacts(self) -> tuple[dict[str, object], ...]:
        if self._server is not None:
            return self._server.recorder.records()
        return self._final


def proxy_env(proxy_url: str) -> dict[str, str]:
    """Environment that routes a probe script's HTTP(S) traffic through
    `proxy_url`: both spellings of the proxy variables (tools differ on
    which they read), an empty no-proxy list so loopback and internal
    hosts go through it too, and Node's opt-in switch for honoring
    proxy variables in its built-in fetch/http clients."""
    return {
        "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url, "https_proxy": proxy_url,
        "ALL_PROXY": proxy_url, "all_proxy": proxy_url,
        "NO_PROXY": "", "no_proxy": "",
        "NODE_USE_ENV_PROXY": "1",
    }


__all__ = ["RecordingProxy", "proxy_env"]
