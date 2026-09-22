from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

import pytest

from mesh_bbs.events import MAX_BODY_BYTES, BBSError, Event
from mesh_bbs.store import Grant, Store
from mesh_bbs.views import Views


class Document(HTMLParser):
    def __init__(self, document: bytes) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.feed(document.decode())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def element(self, element_id: str) -> dict[str, str | None]:
        return next(attrs for _, attrs in self.elements if attrs.get("id") == element_id)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    database = Store(tmp_path / "views.sqlite3", "test-mesh")
    try:
        yield database
    finally:
        database.close()


def test_reply_composer_escapes_parent_content_and_uses_full_parent_identity(store: Store) -> None:
    title = '"><img src=x onerror="alert(1)">'
    body = '</blockquote><script>alert("body")</script><form id="compose-form">'
    parent = store.publish("meshcore:" + "ab" * 32, "parent", "general", title, body)
    document = Document(Views(store, "Community").html_compose(parent_id=parent.post_id[:12]))
    compose = document.element("compose-form")
    assert compose["data-board"] == "general"
    assert compose["data-parent"] == parent.post_id
    assert document.element("post-title")["value"] == "Re: " + title
    assert title in "".join(document.text)
    assert body in "".join(document.text)
    assert "disabled" in document.element("publish")
    assert not any(tag == "img" for tag, _ in document.elements)
    assert [attrs.get("src") for tag, attrs in document.elements if tag == "script"] == [
        "/assets/bbs.js"
    ]
    assert len([attrs for tag, attrs in document.elements if tag == "form"]) == 2
    assert not any(key.startswith("on") for _, attrs in document.elements for key in attrs)


@pytest.mark.parametrize(
    ("author", "label"),
    [
        ("meshcore:" + "ab" * 32, "MeshCore"),
        ("meshtastic:00112233", "Meshtastic"),
        ("reticulum:" + "cd" * 16, "Reticulum"),
        ("lxmf:" + "ef" * 16, "Reticulum"),
        ("packet:N0CALL", "Packet"),
        ("web:alice", "Web"),
        ("local:editor", "Local"),
        ("feed:newsletter", "RSS"),
        ('unknown:<img src=x onerror="boom">', "Peer"),
    ],
)
def test_gui_keeps_full_transport_identity_in_metadata(
    store: Store, author: str, label: str
) -> None:
    post = store.publish(author, "post", "general", "Report", "Body")
    document = Document(Views(store, "Community").html_post(post.post_id))
    assert label in document.text
    assert any(attrs.get("title") == author for _, attrs in document.elements)
    assert not any(tag == "img" for tag, _ in document.elements)


def test_partial_thread_is_reachable_from_reply_permalink_before_parent_arrives(
    tmp_path: Path,
) -> None:
    origin = Store(tmp_path / "origin.sqlite3", "test-mesh")
    reader = Store(tmp_path / "reader.sqlite3", "test-mesh")
    try:
        root = origin.publish("meshcore:author", "root", "general", "Meetup", "Meet Saturday")
        reply = origin.publish(
            "web:reader", "reply", "general", "Re: Meetup", "I'll be there", parent_id=root.post_id
        )
        reader.grants[origin.origin] = Grant(origin.public_key, frozenset({"general"}))
        event = origin.export([reply.revision_id], frozenset({"general"}))[0]
        assert reader.accept(Event.from_dict(event))
        views = Views(reader, "Community")
        permalink = Document(views.html_post(reply.post_id))
        thread_links = [
            str(attrs["href"])
            for tag, attrs in permalink.elements
            if tag == "a" and str(attrs.get("href", "")).startswith("/threads/")
        ]
        assert thread_links
        for link in thread_links:
            thread = views.html_thread(link.removeprefix("/threads/"))
            assert b"Thread arriving from a peer" in thread
            assert "I'll be there" in "".join(Document(thread).text)
        compose = Document(views.html_compose(parent_id=reply.post_id))
        assert compose.element("compose-form")["data-parent"] == reply.post_id
    finally:
        origin.close()
        reader.close()


def test_gui_displays_full_long_reply_but_bounds_thread_excerpt(store: Store) -> None:
    root = store.publish("web:alice", "root", "general", "Field reports", "Add your report")
    content = "あ" * ((MAX_BODY_BYTES - 4) // 3) + " END"
    reply = store.publish(
        "packet:N0CALL", "long-reply", "general", "Report", content, parent_id=root.post_id
    )
    views = Views(store, "Community")
    thread = Document(views.html_thread(root.post_id))
    permalink = Document(views.html_post(reply.post_id))
    assert content in "".join(permalink.text)
    assert content not in "".join(thread.text)
    assert "Read full post" in thread.text
    assert any(attrs.get("href") == f"/posts/{reply.post_id}" for _, attrs in thread.elements)


def test_removed_posts_offer_no_reply_form(store: Store) -> None:
    post = store.publish("web:alice", "root", "general", "Report", "Removed content")
    store.revise("web:alice", "remove", post.post_id, "Report", "", remove=True)
    views = Views(store, "Community")
    document = Document(views.html_post(post.post_id))
    assert "Removed content" not in "".join(document.text)
    assert not any(attrs.get("href") == f"/reply/{post.post_id}" for _, attrs in document.elements)
    with pytest.raises(BBSError, match="removed"):
        views.html_compose(parent_id=post.post_id)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"])
def test_extreme_peer_timestamp_does_not_prevent_gui_metadata(store: Store, stamp: str) -> None:
    post = store.publish("web:alice", "root", "general", "Report", "Content")
    rendered = Views._meta(replace(post, created_at=stamp))
    assert stamp in rendered
