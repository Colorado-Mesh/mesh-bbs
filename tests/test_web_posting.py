from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mesh_bbs.commands import CommandService
from mesh_bbs.events import MAX_BODY_BYTES
from mesh_bbs.store import Store
from mesh_bbs.views import Views
from mesh_bbs.web import MAX_POST_BYTES, ReadOnlyWebServer, _write_origins
from mesh_bbs.web_access import WebAccess

ORIGIN = "https://bbs.example.org"


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[ReadOnlyWebServer, WebAccess, str]]:
    store = Store(tmp_path / "bbs.sqlite3", "test")
    access = WebAccess(store)
    token = access.create("alice")
    server = ReadOnlyWebServer(Views(store, "Mesh BBS", base_url=ORIGIN), port=0, access=access)
    server.start()
    yield server, access, token
    server.stop()
    store.close()


def payload(**changes: Any) -> dict[str, Any]:
    return {
        "board": "general",
        "title": "Saturday meetup",
        "body": "Meet at nine.",
        "parent_id": "",
        "operation": "submission-1",
        **changes,
    }


def request(
    server: ReadOnlyWebServer,
    path: str,
    *,
    method: str = "GET",
    token: str = "",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    configured = {}
    if token:
        configured["Authorization"] = "Bearer " + token
    if method == "POST":
        configured.update({"Origin": ORIGIN, "Content-Type": "application/json"})
    configured.update(headers or {})
    connection = http.client.HTTPConnection(*server.address, timeout=5)
    try:
        connection.request(method, path, body=data, headers=configured)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def submit(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    **changes: Any,
) -> tuple[int, dict[str, str], bytes]:
    server, _, token = service
    return request(
        server,
        "/api/posts",
        method="POST",
        token=token,
        data=json.dumps(payload(**changes)).encode(),
    )


def test_session_returns_only_authenticated_public_identity(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, _, token = service
    assert request(server, "/api/session")[0] == 401
    assert request(server, "/api/session", token="x" * 43)[0] == 401
    status, headers, body = request(server, "/api/session", token=token)
    assert status == 200
    assert json.loads(body) == {"actor": "web:alice", "editor": False}
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert "Set-Cookie" not in headers
    assert token.encode() not in body


def test_submission_retries_once_and_is_readable_from_radio_commands(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, access, _ = service
    status, headers, body = submit(service)
    assert status == 201
    saved = json.loads(body)
    assert set(saved) == {"status", "post_id", "thread_id", "parent_id", "revision_id"}
    assert saved["status"] == "saved_locally"
    assert headers["Cache-Control"] == "no-store"
    assert saved["thread_id"] == saved["post_id"]
    assert saved["parent_id"] == ""
    assert json.loads(submit(service)[2]) == saved
    assert len(access.store.list_posts("general")) == 1
    response = CommandService(access.store).handle(
        "meshcore:reader", "read " + saved["post_id"], max_bytes=512
    )
    assert "Meet at nine." in response
    assert b"Meet at nine." in request(server, "/posts/" + saved["post_id"])[2]


def test_radio_post_accepts_web_reply_in_same_thread(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    _, access, _ = service
    radio = CommandService(access.store)
    answer = radio.handle("meshcore:reader", "new general Radio topic")
    draft_id = answer.split()[1].rstrip(".")
    radio.handle("meshcore:reader", f"add {draft_id} 1 Hello from radio")
    radio.handle("meshcore:reader", f"publish {draft_id}")
    original = access.store.list_posts("general")[0]
    status, _, body = submit(service, parent_id=original.post_id, title="", body="Hello from web")
    assert status == 201
    result = json.loads(body)
    assert result["thread_id"] == result["parent_id"] == original.post_id
    assert "Hello from web" in radio.handle("meshcore:reader", "read " + result["post_id"])


def test_http_roles_and_revocation_are_enforced(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, access, token = service
    assert submit(service, board="news")[0] == 403
    editor = access.create("editor", editor=True)
    status, _, body = request(
        server,
        "/api/posts",
        method="POST",
        token=editor,
        data=json.dumps(payload(board="news")).encode(),
    )
    assert status == 403
    issue = access.store.import_article("colorado", "issue", "news", "News", "Text")
    assert submit(service, board="news", parent_id=issue.post_id, title="")[0] == 403
    access.revoke("alice")
    assert request(server, "/api/session", token=token)[0] == 401
    assert submit(service)[0] == 401


@pytest.mark.parametrize(
    "origin", ["https://evil.invalid", "null", "", ORIGIN + "/", "http://bbs.example.org"]
)
def test_cross_origin_requests_fail_without_reflecting_origin(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    origin: str,
) -> None:
    server, access, token = service
    status, headers, body = request(
        server,
        "/api/posts",
        method="POST",
        token=token,
        data=json.dumps(payload()).encode(),
        headers={"Origin": origin},
    )
    assert status == 403
    assert set(json.loads(body)) == {"error"}
    assert "Access-Control-Allow-Origin" not in headers
    assert access.store.list_posts("general") == []
    assert request(server, "/api/session", token=token, headers={"Origin": origin})[0] == 403


def test_actual_bound_loopback_origin_is_allowed_but_forwarded_origin_is_not(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, _, token = service
    local_origin = f"http://{server.address[0]}:{server.address[1]}"
    assert (
        request(
            server,
            "/api/posts",
            method="POST",
            token=token,
            data=json.dumps(payload()).encode(),
            headers={"Origin": local_origin},
        )[0]
        == 201
    )
    assert (
        request(
            server,
            "/api/posts",
            method="POST",
            token=token,
            data=json.dumps(payload()).encode(),
            headers={
                "Origin": "https://evil.invalid",
                "Host": "evil.invalid",
                "X-Forwarded-Host": "evil.invalid",
                "X-Forwarded-Proto": "https",
            },
        )[0]
        == 403
    )


@pytest.mark.parametrize("field", ["actor", "source_id", "thread_id", "region", "post_id"])
def test_request_cannot_supply_identity_or_federation_fields(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    field: str,
) -> None:
    assert submit(service, **{field: "local:operator"})[0] == 400


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b'{"actor":1,"actor":2}',
        b"{",
        b"\xff",
        b'{"value": NaN}',
        json.dumps(payload(body="\ud800")).encode(),
        json.dumps(payload(body="x" * (MAX_BODY_BYTES + 1))).encode(),
    ],
    ids=[
        "array",
        "null",
        "duplicate-fields",
        "truncated",
        "invalid-utf8",
        "nan",
        "surrogate",
        "oversized-text",
    ],
)
def test_invalid_json_and_text_cannot_create_posts(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    body: bytes,
) -> None:
    server, access, token = service
    status, _, response = request(server, "/api/posts", method="POST", token=token, data=body)
    assert status == 400
    assert set(json.loads(response)) == {"error"}
    assert access.store.list_posts("general") == []


def test_maximum_escaped_utf8_body_and_json_charset_are_supported(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, access, token = service
    text = "é" * (MAX_BODY_BYTES // 2)
    status, _, result = request(
        server,
        "/api/posts",
        method="POST",
        token=token,
        data=json.dumps(payload(body=text)).encode(),
        headers={"Content-Type": 'application/json; charset="UTF-8"'},
    )
    assert status == 201
    assert access.store.get_post(json.loads(result)["post_id"]).body == text


@pytest.mark.parametrize("header", ["Authorization", "Origin", "Content-Length", "Content-Type"])
def test_duplicate_security_headers_are_rejected(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    header: str,
) -> None:
    server, access, token = service
    data = json.dumps(payload()).encode()
    headers = {
        "Authorization": "Bearer " + token,
        "Origin": ORIGIN,
        "Content-Length": str(len(data)),
        "Content-Type": "application/json",
    }
    connection = http.client.HTTPConnection(*server.address, timeout=5)
    try:
        connection.putrequest("POST", "/api/posts")
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader(header.lower(), headers[header])
        connection.endheaders(data)
        response = connection.getresponse()
        assert response.status == 400
        assert set(json.loads(response.read())) == {"error"}
        assert access.store.list_posts("general") == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Transfer-Encoding": "chunked"}, 400),
        ({"Content-Length": str(MAX_POST_BYTES + 1)}, 413),
        ({"Content-Length": "-1"}, 411),
        ({"Content-Length": "0"}, 413),
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/json; charset=iso-8859-1"}, 415),
        ({"Content-Type": "application/json; charset=utf-8; charset=utf-8"}, 415),
    ],
)
def test_framing_and_media_types_are_checked_before_reading_body(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    headers: dict[str, str],
    status: int,
) -> None:
    server, access, token = service
    actual, _, body = request(
        server,
        "/api/posts",
        method="POST",
        token=token,
        data=b"{}",
        headers=headers,
    )
    assert actual == status
    assert set(json.loads(body)) == {"error"}
    assert access.store.list_posts("general") == []


def test_short_body_is_rejected_without_waiting_for_timeout(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, _, token = service
    connection = socket.create_connection(server.address, timeout=5)
    try:
        connection.sendall(
            (
                f"POST /api/posts HTTP/1.0\r\nOrigin: {ORIGIN}\r\n"
                f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
                "Content-Length: 20\r\n\r\n{}"
            ).encode()
        )
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 400
        assert set(json.loads(response.read())) == {"error"}
    finally:
        connection.close()


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
def test_other_methods_never_alias_posting(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    method: str,
) -> None:
    server, access, token = service
    assert (
        request(
            server,
            "/api/posts",
            method=method,
            token=token,
            data=json.dumps(payload()).encode(),
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )[0]
        == 405
    )
    assert access.store.list_posts("general") == []


def test_post_errors_do_not_expose_server_exception_details(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, access, _ = service

    def fail(*_args: Any) -> None:
        raise RuntimeError("private_key=secret")

    monkeypatch.setattr(access, "publish", fail)
    status, _, body = submit(service)
    assert status == 500
    assert b"secret" not in body


def test_origin_selection_keeps_public_http_read_only() -> None:
    assert _write_origins("http://bbs.example.org", ("0.0.0.0", 8080)) == ()
    assert _write_origins("https://bbs.example.org:443", ("0.0.0.0", 8080)) == (ORIGIN,)
    assert _write_origins("http://[::1]:8080", ("::1", 1234)) == (
        "http://[::1]:8080",
        "http://localhost:1234",
        "http://[::1]:1234",
    )


def test_localhost_browser_alias_can_publish(service):
    server, _, token = service
    status, _, _ = request(
        server,
        "/api/posts",
        method="POST",
        token=token,
        data=json.dumps(payload()).encode(),
        headers={"Origin": f"http://localhost:{server.address[1]}"},
    )
    assert status == 201


def test_static_files_are_allowlisted_and_browser_routes_have_restrictive_csp(
    service: tuple[ReadOnlyWebServer, WebAccess, str],
) -> None:
    server, _, _ = service
    for path, content_type in (
        ("/assets/bbs.css", "text/css"),
        ("/assets/bbs.js", "text/javascript"),
    ):
        status, headers, body = request(server, path)
        assert status == 200 and body
        assert headers["Content-Type"].startswith(content_type)
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
    assert request(server, "/assets/../config.py")[0] == 404
    assert request(server, "/assets/bbs.js?token=secret")[0] == 400
    assert request(server, "/api/session?token=secret")[0] == 400
    for path in ("/connect", "/new/general"):
        assert request(server, path)[0] == 200


def test_authenticated_board_creation_then_post_and_revocation(service):
    server, access, token = service
    data = b'{"board":"hiking"}'
    for _ in range(2):
        status, _, body = request(server, "/api/boards", method="POST", token=token, data=data)
        assert status == 201 and json.loads(body) == {"board": "hiking", "status": "saved_locally"}
    assert submit(service, board="hiking")[0] == 201
    for content in (
        b'{"board":"news"}',
        b'{"board":"../bad"}',
        b'{"board":"forged","actor":"other"}',
    ):
        assert request(server, "/api/boards", method="POST", token=token, data=content)[0] == 400
    assert request(server, "/api/boards", method="POST", data=data)[0] == 401
    assert (
        request(
            server,
            "/api/boards",
            method="POST",
            token=token,
            data=data,
            headers={"Origin": "https://evil.invalid"},
        )[0]
        == 403
    )
    access.revoke("alice")
    assert request(server, "/api/boards", method="POST", token=token, data=data)[0] == 401
