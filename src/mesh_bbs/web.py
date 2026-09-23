"""Bounded web reading and authenticated posting behind an optional TLS proxy."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from socketserver import TCPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from mesh_bbs import micron
from mesh_bbs.events import SLUG, BBSError
from mesh_bbs.views import Views
from mesh_bbs.web_access import AccessDenied, WebAccess, WebUser

LOG = logging.getLogger(__name__)
MAX_TARGET = 2048
MAX_QUERY = 1024
MAX_POST_BYTES = 400 * 1024


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _write_origins(base_url: str, address: tuple[str, int]) -> tuple[str, ...]:
    parsed = urlsplit(base_url)
    origins: list[str] = []
    if parsed.scheme == "https" or _loopback(parsed.hostname or ""):
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        suffix = f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else ""
        origins.append(f"{parsed.scheme}://{host}{suffix}")
    host, port = address
    if _loopback(host):
        origins.append(f"http://localhost:{port}")
        if ":" in host:
            host = f"[{host}]"
        origins.append(f"http://{host}:{port}")
    return tuple(dict.fromkeys(origins))


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BBSError("Repeated JSON fields are not allowed")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise BBSError("JSON must not contain non-finite numbers")


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

    def server_bind(self) -> None:
        # HTTPServer otherwise performs reverse DNS even for a loopback listener.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

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


def _handler(
    views: Views,
    readiness: Readiness | None,
    access: WebAccess | None = None,
    allowed_origins: tuple[str, ...] = (),
    health_instance: Callable[[], str | None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MeshBBS"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Avoid recording reader post IDs, queries, or transport identities.

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            style_policy = "'self'"
            if content_type.startswith("text/html"):
                nonce = secrets.token_urlsafe(24)
                style_policy += f" 'nonce-{nonce}'"
                body = body.replace(
                    b'<style id="micron-colors">',
                    f'<style id="micron-colors" nonce="{nonce}">'.encode(),
                    1,
                )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; style-src {style_policy}; "
                "script-src 'self'; connect-src 'self'; "
                "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
            )
            if status == 405:
                methods = (
                    "POST"
                    if access is not None
                    and self.path in {"/api/posts", "/api/boards", "/api/preview"}
                    else "GET, HEAD"
                )
                self.send_header("Allow", methods)
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
            if getattr(self, "path", "").startswith("/api/") and access is not None:
                self._json(code, {"error": phrase})
                return
            self._send(code, "text/plain; charset=utf-8", (phrase + "\n").encode())

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def _authenticate(self, *, require_origin: bool) -> WebUser:
            assert access is not None
            for header in ("Authorization", "Origin", "Content-Length", "Content-Type"):
                if len(self.headers.get_all(header, [])) > 1:
                    raise BBSError("Repeated security headers are not allowed")
            if self.headers.get_all("Transfer-Encoding"):
                raise BBSError("Transfer-Encoding is not supported")
            if not allowed_origins:
                raise AccessDenied("Web posting requires HTTPS or a loopback address", status=403)
            origin = self.headers.get("Origin")
            if (require_origin or origin is not None) and origin not in allowed_origins:
                raise AccessDenied("Request origin is not allowed", status=403)
            authorization = self.headers.get("Authorization", "")
            match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", authorization, re.IGNORECASE)
            if match is None:
                raise AccessDenied()
            return access.authenticate(match[1])

        def _session(self) -> None:
            try:
                user = self._authenticate(require_origin=False)
                self._json(200, {"actor": user.actor, "editor": user.editor})
            except AccessDenied as error:
                self._json(error.status, {"error": str(error)})
            except BBSError as error:
                self._json(400, {"error": str(error)})

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
                    health = {"status": "ok"}
                    if health_instance and (instance := health_instance()):
                        health["instance"] = instance
                    self._send(
                        200,
                        "application/json; charset=utf-8",
                        (json.dumps(health, separators=(",", ":")) + "\n").encode(),
                    )
                    return
                if path == "/readyz":
                    ready, report = readiness() if readiness else (True, {"status": "ready"})
                    body = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
                    self._send(200 if ready else 503, "application/json; charset=utf-8", body)
                    return
                if path == "/api/session" and access is not None:
                    self._session()
                    return
                if path in {"/assets/bbs.css", "/assets/bbs.js"}:
                    asset = path.rsplit("/", 1)[1]
                    body = files("mesh_bbs").joinpath("static", asset).read_bytes()
                    content_type = (
                        "text/css; charset=utf-8"
                        if asset.endswith(".css")
                        else "text/javascript; charset=utf-8"
                    )
                    self._send(200, content_type, body)
                    return
                content_type = "text/html; charset=utf-8"
                parts = path.split("/")
                if path == "/":
                    body = views.html_index()
                elif path == "/connect" and access is not None:
                    body = views.html_connect()
                elif path == "/new-board" and access is not None:
                    body = views.html_new_board()
                elif len(parts) == 3 and parts[1] == "new" and access is not None:
                    body = views.html_compose(board=parts[2])
                elif len(parts) == 3 and parts[1] == "reply" and access is not None:
                    body = views.html_compose(parent_id=parts[2])
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
            if access is None:
                self.send_error(405)
                return
            if self.path not in {"/api/posts", "/api/boards", "/api/preview"}:
                self._json(404, {"error": "Not Found"})
                return
            try:
                user = self._authenticate(require_origin=True)
                content_type = self.headers.get("Content-Type", "")
                if not re.fullmatch(
                    r'application/json(?:\s*;\s*charset=(?:utf-8|"utf-8"))?',
                    content_type,
                    re.IGNORECASE,
                ):
                    self._json(415, {"error": "Use application/json with UTF-8 text"})
                    return
                length = self.headers.get("Content-Length", "")
                if not re.fullmatch(r"[0-9]{1,10}", length):
                    self._json(411, {"error": "A single Content-Length is required"})
                    return
                size = int(length)
                if not 0 < size <= MAX_POST_BYTES:
                    self._json(413, {"error": "Request body exceeds the limit or is empty"})
                    return
                deadline = time.monotonic() + 5.0
                chunks = bytearray()
                while len(chunks) < size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(min(65536, size - len(chunks)))
                    if not chunk:
                        raise BBSError("Request body ended before Content-Length")
                    chunks.extend(chunk)
                payload = json.loads(
                    chunks.decode("utf-8"),
                    object_pairs_hook=_json_object,
                    parse_constant=_reject_constant,
                )
                if not isinstance(payload, dict):
                    raise BBSError("Post body must be a JSON object")
                if self.path == "/api/preview":
                    body = payload.get("body")
                    if (
                        set(payload) != {"body"}
                        or not isinstance(body, str)
                        or len(body.encode()) > 65536
                    ):
                        raise BBSError("Preview requires a body up to 65,536 bytes")
                    rendered = micron.html(body)
                    self._json(200, {"html": rendered, "css": micron.stylesheet(rendered)})
                    return
                if self.path == "/api/boards":
                    board = access.create_board(user, payload)
                    self._json(201, {"status": "saved_locally", "board": board})
                    return
                post = access.publish(user, payload)
                self._json(
                    201,
                    {
                        "status": "saved_locally",
                        "post_id": post.post_id,
                        "thread_id": post.thread_id,
                        "parent_id": post.parent_id,
                        "revision_id": post.revision_id,
                    },
                )
            except AccessDenied as error:
                self._json(error.status, {"error": str(error)})
            except TimeoutError:
                self._json(408, {"error": "Request body timed out"})
            except BBSError as error:
                self._json(400, {"error": str(error)})
            except (UnicodeError, ValueError, RecursionError):
                self._json(400, {"error": "Invalid post data"})
            except Exception:
                LOG.error("Could not save a web post")
                self._json(500, {"error": "Could not save the post"})

        def _unsupported_method(self) -> None:
            self.send_error(405)

        do_PUT = _unsupported_method
        do_PATCH = _unsupported_method
        do_DELETE = _unsupported_method
        do_OPTIONS = _unsupported_method
        do_TRACE = _unsupported_method
        do_CONNECT = _unsupported_method

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
        access: WebAccess | None = None,
        health_instance: Callable[[], str | None] | None = None,
    ) -> None:
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError("A valid bind address is required")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Port must be between 0 and 65535")
        self.views, self.host, self.port = views, host, port
        self._readiness = readiness
        self._access = access
        self._health_instance = health_instance
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
        bound_host, bound_port = server.server_address[:2]
        server.RequestHandlerClass = _handler(
            self.views,
            self._readiness,
            self._access,
            _write_origins(self.views.base_url, (str(bound_host), int(bound_port))),
            self._health_instance,
        )
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
