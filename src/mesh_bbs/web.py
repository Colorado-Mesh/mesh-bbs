"""Bounded read-only HTTP service; put a TLS reverse proxy in front for public use."""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from mesh_bbs.events import SLUG, BBSError
from mesh_bbs.views import Views

LOG = logging.getLogger(__name__)
MAX_TARGET = 2048
MAX_QUERY = 1024


class _Server(ThreadingHTTPServer):
    request_queue_size = 16
    daemon_threads = False
    block_on_close = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self._slots = threading.BoundedSemaphore(8)
        self._readers: set[socket.socket] = set()
        self._readers_lock = threading.Lock()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        connection.settimeout(5.0)
        with self._readers_lock:
            self._readers.add(connection)
        return connection, address

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            with self._readers_lock:
                self._readers.discard(request)

    def close_readers(self) -> None:
        with self._readers_lock:
            readers = tuple(self._readers)
        for connection in readers:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def handle_error(self, request: Any, client_address: Any) -> None:
        LOG.error("Read-only HTTP connection ended unexpectedly")

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


Readiness = Callable[[], tuple[bool, dict[str, Any]]]


def _handler(views: Views, readiness: Readiness | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MeshBBS"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Avoid recording reader post IDs, queries, or transport identities.

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            if status == 405:
                self.send_header("Allow", "GET, HEAD")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            phrase = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else "Error"
            self._send(code, "text/plain; charset=utf-8", (phrase + "\n").encode())

        def do_GET(self) -> None:
            if len(self.path) > MAX_TARGET:
                self.send_error(414)
                return
            if (
                not self.path.startswith("/")
                or self.path.startswith("//")
                or any(ord(character) <= 32 for character in self.path)
            ):
                self.send_error(400)
                return
            try:
                parsed = urlsplit(self.path)
                if len(parsed.query) > MAX_QUERY:
                    self.send_error(414)
                    return
                if parsed.fragment or parsed.scheme or parsed.netloc:
                    self.send_error(400)
                    return
                path = parsed.path
                after_id = ""
                if parsed.query:
                    query = parse_qsl(
                        parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1
                    )
                    if (
                        len(query) != 1
                        or query[0][0] != "after"
                        or not re.fullmatch(r"[0-9a-f]{8,64}", query[0][1])
                        or not path.startswith(("/boards/", "/threads/"))
                    ):
                        self.send_error(400)
                        return
                    after_id = query[0][1]
                if path == "/healthz":
                    self._send(200, "application/json; charset=utf-8", b'{"status":"ok"}\n')
                    return
                if path == "/readyz":
                    ready, report = readiness() if readiness else (True, {"status": "ready"})
                    body = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
                    self._send(200 if ready else 503, "application/json; charset=utf-8", body)
                    return
                content_type = "text/html; charset=utf-8"
                parts = path.split("/")
                if path == "/":
                    body = views.html_index()
                elif len(parts) == 3 and parts[1] == "boards" and SLUG.fullmatch(parts[2]):
                    body = views.html_board(parts[2], after_id=after_id)
                elif len(parts) == 3 and parts[1] == "posts":
                    body = views.html_post(parts[2])
                elif len(parts) == 3 and parts[1] == "threads":
                    body = views.html_thread(parts[2], after_id=after_id)
                elif len(parts) == 3 and parts[1] == "feeds" and parts[2].endswith(".xml"):
                    board = parts[2][:-4]
                    if not SLUG.fullmatch(board):
                        self.send_error(404)
                        return
                    body = views.rss(board)
                    content_type = "application/rss+xml; charset=utf-8"
                else:
                    self.send_error(404)
                    return
            except BBSError:
                self.send_error(404)
                return
            except ValueError:
                self.send_error(400)
                return
            except Exception:
                LOG.error("Could not render a read-only HTTP response")
                self.send_error(500)
                return
            self._send(200, content_type, body)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_POST(self) -> None:
            self.send_error(405)

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST
        do_TRACE = do_POST
        do_CONNECT = do_POST

    return Handler


class ReadOnlyWebServer:
    """Lifecycle wrapper; start is nonblocking and stop waits for active readers.

    The default listener is local-only. Operators must explicitly choose another
    bind address to expose it. Use ``asyncio.to_thread(server.stop)`` at shutdown
    when running alongside asynchronous adapters.
    """

    def __init__(
        self,
        views: Views,
        host: str = "127.0.0.1",
        port: int = 8080,
        *,
        readiness: Readiness | None = None,
    ) -> None:
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError("A valid bind address is required")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Port must be between 0 and 65535")
        self.views, self.host, self.port = views, host, port
        self._readiness = readiness
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("HTTP service has not started")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("HTTP service is already running")
        server = _Server((self.host, self.port), _handler(self.views, self._readiness))
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="mesh-bbs-http",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            server.server_close()
            raise
        self._server, self._thread = server, thread

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.close_readers()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = None
        self._thread = None
