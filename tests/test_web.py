from __future__ import annotations

import http.client
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mesh_bbs.store import Store
from mesh_bbs.views import Views
from mesh_bbs.web import ReadOnlyWebServer


@pytest.fixture
def server(tmp_path: Path) -> Iterator[ReadOnlyWebServer]:
    store = Store(tmp_path / "bbs.db", "colorado-mesh")
    store.publish("local:alice", "first", "news", "Newsletter", "Full article")
    instance = ReadOnlyWebServer(
        Views(store, "Colorado Mesh", base_url="https://bbs.example.org"), port=0
    )
    instance.start()
    yield instance
    instance.stop()
    store.close()


def request(
    server: ReadOnlyWebServer,
    target: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*server.address, timeout=5)
    try:
        connection.request(method, target, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_http_routes_serve_only_public_board_content(server: ReadOnlyWebServer) -> None:
    assert server.address[0] == "127.0.0.1"
    post = server.views.store.list_posts("news")[0]
    for target in ("/", "/boards/news", f"/posts/{post.post_id}", f"/threads/{post.post_id}"):
        status, headers, body = request(server, target)
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert b"Colorado Mesh" in body
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert int(headers["Content-Length"]) == len(body)
    assert request(server, "/healthz")[2] == b'{"status":"ok"}\n'


def test_http_feed_never_uses_untrusted_host_header(server: ReadOnlyWebServer) -> None:
    status, headers, body = request(server, "/feeds/news.xml", headers={"Host": "evil.invalid"})
    assert status == 200
    assert headers["Content-Type"].startswith("application/rss+xml")
    root = ET.fromstring(body)
    assert root.findtext("./channel/link") == "https://bbs.example.org/boards/news"
    assert b"evil.invalid" not in body


def test_head_matches_get_headers_without_body(server: ReadOnlyWebServer) -> None:
    status, get_headers, body = request(server, "/boards/news")
    head_status, head_headers, head_body = request(server, "/boards/news", "HEAD")
    assert status == head_status == 200
    assert get_headers["Content-Length"] == head_headers["Content-Length"] == str(len(body))
    assert head_body == b""


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"]
)
def test_write_methods_are_not_available(server: ReadOnlyWebServer, method: str) -> None:
    before = server.views.store.inventory(frozenset({"news"}))
    status, headers, body = request(server, "/posts/12345678", method)
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert body == b"Method Not Allowed\n"
    assert server.views.store.inventory(frozenset({"news"})) == before


@pytest.mark.parametrize(
    "target",
    [
        "/../config.toml",
        "/%2e%2e/config.toml",
        "/.git/config",
        "/boards/missing",
        "/posts/12345678",
        "/feeds/../config.toml",
        "/feeds/missing.xml",
        "/boards/news/other",
        "/<script>alert(1)</script>",
    ],
)
def test_unknown_paths_do_not_serve_files_or_reflect_input(
    server: ReadOnlyWebServer, target: str
) -> None:
    status, _, body = request(server, target)
    assert status == 404
    assert body == b"Not Found\n"


def test_oversized_targets_and_queries_are_rejected(server: ReadOnlyWebServer) -> None:
    assert request(server, "/" + "x" * 2048)[0] == 414
    assert request(server, "/?" + "x" * 1025)[0] == 414
    assert request(server, "/?name=%3Cscript%3E")[0] == 400
    assert request(server, "http://example.org/")[0] == 400


def test_unexpected_render_errors_do_not_disclose_exception_details(
    server: ReadOnlyWebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> bytes:
        raise RuntimeError("private_key=secret /home/operator/config.toml")

    monkeypatch.setattr(server.views, "html_index", fail)
    status, _, body = request(server, "/")
    assert status == 500
    assert body == b"Internal Server Error\n"


def test_service_lifecycle_can_stop_and_restart(server: ReadOnlyWebServer) -> None:
    with pytest.raises(RuntimeError, match="already running"):
        server.start()
    server.stop()
    server.stop()
    with pytest.raises(RuntimeError, match="has not started"):
        _ = server.address
    server.start()
    assert request(server, "/healthz")[0] == 200


def test_shutdown_closes_clients_with_unfinished_requests(server: ReadOnlyWebServer) -> None:
    connection = socket.create_connection(server.address, timeout=5)
    try:
        connection.sendall(b"GET / HTTP/1.0\r\nHost: ")
        # Complete a second request to ensure the first connection was accepted.
        assert request(server, "/healthz")[0] == 200
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 2
    finally:
        connection.close()


@pytest.mark.parametrize(
    "query",
    [
        "after=",
        "after=123",
        "after=abcdefgz",
        "after=" + "a" * 65,
        "after=12345678&after=23456789",
        "after=12345678&other=value",
        "other=12345678",
        "after=%3Cscript%3E",
        "after",
        "after=12345678;other=value",
    ],
)
def test_http_rejects_invalid_pagination_queries(server: ReadOnlyWebServer, query: str) -> None:
    status, _, body = request(server, "/boards/news?" + query)
    assert status == 400
    assert body == b"Bad Request\n"


@pytest.mark.parametrize("path", ["/", "/healthz", "/feeds/news.xml", "/posts/12345678"])
def test_pagination_query_is_only_supported_on_lists(server: ReadOnlyWebServer, path: str) -> None:
    assert request(server, path + "?after=12345678")[0] == 400


def test_http_follows_board_and_thread_cursors(server: ReadOnlyWebServer) -> None:
    store = server.views.store
    for number in range(51):
        store.publish("local:a", f"root-{number}", "news", f"Issue {number}", "Article")
    original = store.list_posts("news", limit=200)
    cursor = original[49].post_id
    status, _, first = request(server, "/boards/news")
    assert status == 200
    assert f'href="/boards/news?after={cursor}"'.encode() in first
    store.publish("local:a", "new-issue", "news", "Newest issue", "New article")
    status, _, second = request(server, f"/boards/news?after={cursor}")
    assert status == 200
    assert all(f"/threads/{post.post_id}".encode() in second for post in original[50:])
    assert all(f"/threads/{post.post_id}".encode() not in second for post in original[:50])

    root = original[0]
    for number in range(101):
        store.publish(
            "local:a",
            f"reply-{number}",
            "news",
            f"Reply {number}",
            "Reply text",
            parent_id=root.post_id,
        )
    replies = store.list_posts("news", thread_id=root.post_id, limit=200)
    after = replies[99].post_id
    status, _, first = request(server, f"/threads/{root.post_id}")
    assert status == 200
    assert f'href="/threads/{root.post_id}?after={after}"'.encode() in first
    status, _, second = request(server, f"/threads/{root.post_id}?after={after}")
    assert status == 200
    assert all(f"/posts/{post.post_id}".encode() in second for post in replies[100:])
    assert all(f"/posts/{post.post_id}".encode() not in second for post in replies[:100])
    assert b"Next page" not in second
