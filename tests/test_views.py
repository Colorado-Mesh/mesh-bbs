from __future__ import annotations

import re
from collections.abc import Iterator
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mesh_bbs.events import BBSError
from mesh_bbs.store import Store
from mesh_bbs.views import Views, validate_base_url


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    database = Store(tmp_path / "bbs.db", "colorado-mesh")
    yield database
    database.close()


class Elements(HTMLParser):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.links: list[str] = []
        self.next_links: list[str] = []
        self.feed(content)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.links.extend(value for key, value in attrs if key == "href" and value is not None)
        attributes = dict(attrs)
        if tag == "a" and attributes.get("rel") == "next" and attributes.get("href"):
            self.next_links.append(str(attributes["href"]))


def test_html_escapes_posts_authors_and_service_names(store: Store) -> None:
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    post = store.publish("radio:<svg/onload=alert(3)>", "op1", "news", payload, payload)
    views = Views(store, '<script>alert("name")</script>')
    for document in (
        views.html_index(),
        views.html_board("news"),
        views.html_thread(post.post_id),
        views.html_post(post.post_id),
    ):
        rendered = document.decode()
        parsed = Elements(rendered)
        assert "script" not in parsed.tags
        assert "svg" not in parsed.tags
        assert "img" not in parsed.tags
        assert "main" in parsed.tags
        assert all(link.startswith("/") and not link.startswith("//") for link in parsed.links)
        assert payload not in rendered
    assert "&lt;img" in views.html_post(post.post_id).decode()


def test_posts_replies_and_deleted_parent_remain_navigable(store: Store) -> None:
    post = store.publish("local:alice", "issue", "news", "Newsletter", "Line one\n\nLine two")
    reply = store.publish(
        "local:bob", "reply", "news", "Re: Newsletter", "Thanks", parent_id=post.post_id
    )
    views = Views(store, "Colorado Mesh")
    assert f"/threads/{post.post_id}" in views.html_board("news").decode()
    assert f"/posts/{reply.post_id}" in views.html_thread(post.post_id).decode()
    assert f"/posts/{post.post_id}" in views.html_post(reply.post_id).decode()
    assert "<p>Line one</p><p>Line two</p>" in views.html_post(post.post_id).decode()
    store.revise("local:alice", "remove", post.post_id, "Newsletter", "", remove=True)
    removed = views.html_post(post.post_id).decode()
    assert "This post was removed" in removed
    assert "Line one" not in removed
    assert "Thanks" in views.html_thread(post.post_id).decode()
    assert "Removed post" in views.html_thread(post.post_id).decode()
    assert post.post_id not in views.html_board("news").decode()


def test_rss_full_text_safe_html_and_stable_guid_across_revisions(store: Store) -> None:
    post = store.import_article(
        "newsletter", "issue-7", "news", "A & B", "<script>bad()</script>\nEnd"
    )
    views = Views(store, "Colorado & Mesh", base_url="https://bbs.example.org/")
    root = ET.fromstring(views.rss("news"))
    item = root.find("./channel/item")
    assert item is not None
    expected_guid = f"urn:mesh-bbs:colorado-mesh:{post.post_id}"
    assert item.findtext("guid") == expected_guid
    assert item.find("guid").attrib["isPermaLink"] == "false"  # type: ignore[union-attr]
    assert item.findtext("link") == f"https://bbs.example.org/posts/{post.post_id}"
    assert item.findtext("title") == "A & B"
    description = item.findtext("description", "")
    assert "script" not in Elements(description).tags
    assert "<script>bad()</script>" in unescape(description)
    assert item.findtext("pubDate", "").endswith("GMT")
    revision = item.findtext("{urn:mesh-bbs:feed:1}revision")
    store.import_article("newsletter", "issue-7", "news", "Correction", "New text " * 5000)
    corrected = ET.fromstring(views.rss("news"))
    assert len(corrected.findall("./channel/item")) == 1
    assert corrected.findtext("./channel/item/guid") == expected_guid
    assert corrected.findtext("./channel/item/{urn:mesh-bbs:feed:1}revision") != revision
    assert "New text " * 5000 in corrected.findtext("./channel/item/description", "")


def test_rss_does_not_include_replies_removed_posts_or_other_boards(store: Store) -> None:
    newsletter = store.publish("a", "news", "news", "Issue", "Original")
    store.publish("a", "reply", "news", "Reply", "Thanks", parent_id=newsletter.post_id)
    store.publish("a", "general", "general", "Elsewhere", "Not news")
    views = Views(store, "Mesh")
    assert len(ET.fromstring(views.rss("news")).findall("./channel/item")) == 1
    store.revise("a", "remove", newsletter.post_id, "Issue", "", remove=True)
    assert not ET.fromstring(views.rss("news")).findall("./channel/item")


def test_nomadnet_navigation_uses_full_identifiers_and_request_variables(store: Store) -> None:
    post = store.publish("a", "news", "news", "Issue", "Hello Reticulum")
    views = Views(store, "Colorado Mesh")
    assert b"`[news`:/page/board.mu`board=news]" in views.page("/page/index.mu", {})
    board = views.page("/page/board.mu", {"var_board": "news"}).decode()
    assert f"`[Open thread`:/page/thread.mu`id={post.post_id}]" in board
    thread = views.page("/page/thread.mu", {"var_id": post.post_id}).decode()
    assert f"`[Read full post`:/page/post.mu`id={post.post_id}]" in thread
    document = views.page("/page/post.mu", {"var_id": post.post_id[:8]}).decode()
    assert "Hello Reticulum" in document
    assert "Revision: " + post.revision_id in document


def test_nomadnet_untrusted_markup_stays_inside_literal_blocks(store: Store) -> None:
    attack = "`=\n`[Steal identity`https://evil.invalid]\n`<password`>\n`=\n\\`=\n>Title\n#Hidden\n`{remote}"
    post = store.publish(attack[:200], "attack", "news", attack[:200], attack + "\x1b[31m")
    document = Views(store, attack).page("/page/post.mu", {"var_id": post.post_id}).decode()
    literal = False
    dangerous_lines: list[str] = []
    for line in document.split("\n"):
        if line == "`=":
            literal = not literal
        elif "Steal identity" in line or "password" in line or "remote}" in line:
            assert literal
            dangerous_lines.append(line)
    assert not literal
    assert dangerous_lines
    assert "\n `=\n" in document
    assert "\n \\`=\n" in document
    assert "\x1b" not in document


def test_long_posts_and_listing_limits_are_bounded(store: Store) -> None:
    text = "a\n" * 32768
    post = store.publish("a", "large", "news", "Long newsletter", text)
    views = Views(store, "Mesh")
    assert views.html_post(post.post_id).count(b"a<br>") == 32768
    page = views.page("/page/post.mu", {"var_id": post.post_id})
    assert page.splitlines().count(b" a") == 32768
    assert len(page) < 240 * 1024
    thread = views.page("/page/thread.mu", {"var_id": post.post_id})
    assert len(thread) < 2048


def test_xml_invalid_controls_do_not_break_rss(store: Store) -> None:
    store.publish("a", "control", "news", "Title\x00", "Body\x0b\ufffe")
    root = ET.fromstring(Views(store, "Mesh\uffff").rss("news"))
    assert root.findtext("./channel/item/title") == "Title\ufffd"
    assert "Body\ufffd\ufffd" in root.findtext("./channel/item/description", "")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//evil.invalid",
        "https://user:pass@bbs.example.org",
        "https://bbs.example.org/path",
        "https://bbs.example.org?x=y",
        "https://bbs.example.org#x",
        "https://bbs.example.org\r\nX-Header: yes",
        "https://bbs.example.org:99999",
        'https://bbs.example.org/"><script>',
        "https://bbs.example.org\\@evil.invalid",
    ],
)
def test_feed_base_url_rejects_non_origins_and_injection(url: str) -> None:
    with pytest.raises(ValueError):
        validate_base_url(url)


@pytest.mark.parametrize(
    "url", ["https://bbs.example.org", "http://127.0.0.1:8090", "http://[::1]:8080"]
)
def test_feed_base_url_accepts_explicit_origins(url: str) -> None:
    assert validate_base_url(url + "/") == url


def test_unknown_requests_fail_without_reflecting_input(store: Store) -> None:
    views = Views(store, "Mesh")
    for method in (views.html_board, views.html_post, views.rss):
        with pytest.raises(BBSError):
            method("<script>unknown</script>")
    for path, variables in (
        ("/etc/passwd", {}),
        ("/page/post.mu", {"var_id": "<script>bad</script>"}),
        ("/page/board.mu", {"var_board": "x" * 10000}),
        ("/page/post.mu", {"var_id": ["bad"]}),
    ):
        result = views.page(path, variables)
        assert b"Page unavailable" in result
        assert b"script" not in result
        assert b"passwd" not in result


def test_board_pages_reach_older_threads_without_repeating_after_new_posts(store: Store) -> None:
    for number in range(55):
        store.publish("local:a", f"issue-{number}", "news", f"Issue {number}", "Article")
    views = Views(store, "Mesh")
    original = store.list_posts("news", limit=200)
    cursor = original[49].post_id
    first_html = Elements(views.html_board("news").decode())
    first_micron = views.page("/page/board.mu", {"var_board": "news"}).decode()
    assert first_html.next_links == [f"/boards/news?after={cursor}"]
    assert [link for link in first_html.links if link.startswith("/threads/")] == [
        f"/threads/{post.post_id}" for post in original[:50]
    ]
    assert re.findall(r"Open thread`:/page/thread.mu`id=([0-9a-f]{64})", first_micron) == [
        post.post_id for post in original[:50]
    ]
    assert f"`[Next page`:/page/board.mu`board=news|after={cursor}]" in first_micron

    store.publish("local:a", "new-arrival", "news", "Newer issue", "Arrived while reading")
    second_html = Elements(views.html_board("news", after_id=cursor).decode())
    second_micron = views.page(
        "/page/board.mu", {"var_board": "news", "var_after": cursor}
    ).decode()
    assert [link for link in second_html.links if link.startswith("/threads/")] == [
        f"/threads/{post.post_id}" for post in original[50:]
    ]
    assert re.findall(r"Open thread`:/page/thread.mu`id=([0-9a-f]{64})", second_micron) == [
        post.post_id for post in original[50:]
    ]
    assert not second_html.next_links
    assert "Next page" not in second_micron
    assert "First page" in second_micron


def test_thread_pages_reach_all_replies_including_new_arrivals(store: Store) -> None:
    root = store.publish("local:a", "root", "news", "Discussion", "Root post")
    for number in range(101):
        store.publish(
            "local:a",
            f"reply-{number}",
            "news",
            f"Reply {number}",
            "Text",
            parent_id=root.post_id,
        )
    views = Views(store, "Mesh")
    original = store.list_posts("news", thread_id=root.post_id, limit=200)
    cursor = original[99].post_id
    first_html = Elements(views.html_thread(root.post_id).decode())
    first_micron = views.page("/page/thread.mu", {"var_id": root.post_id}).decode()
    assert first_html.next_links == [f"/threads/{root.post_id}?after={cursor}"]
    assert [link for link in first_html.links if link.startswith("/posts/")] == [
        f"/posts/{post.post_id}" for post in original[:100]
    ]
    assert re.findall(r"Read full post`:/page/post.mu`id=([0-9a-f]{64})", first_micron) == [
        post.post_id for post in original[:100]
    ]
    assert f"`[Next page`:/page/thread.mu`id={root.post_id}|after={cursor}]" in first_micron
    assert len(first_micron.encode()) < 240 * 1024

    latest = store.publish(
        "local:a",
        "new-reply",
        "news",
        "New reply",
        "Arrived while reading",
        parent_id=root.post_id,
    )
    remaining = [post.post_id for post in original[100:]] + [latest.post_id]
    second_html = Elements(views.html_thread(root.post_id, after_id=cursor).decode())
    second_micron = views.page(
        "/page/thread.mu", {"var_id": root.post_id, "var_after": cursor}
    ).decode()
    assert [link for link in second_html.links if link.startswith("/posts/")] == [
        f"/posts/{post_id}" for post_id in remaining
    ]
    assert (
        re.findall(r"Read full post`:/page/post.mu`id=([0-9a-f]{64})", second_micron) == remaining
    )
    assert not second_html.next_links
    assert "Next page" not in second_micron
    assert "First page" in second_micron


def test_exact_page_boundary_does_not_offer_an_empty_next_page(store: Store) -> None:
    for number in range(50):
        store.publish("local:a", f"root-{number}", "news", "Issue", "Article")
    views = Views(store, "Mesh")
    assert not Elements(views.html_board("news").decode()).next_links
    assert b"Next page" not in views.page("/page/board.mu", {"var_board": "news"})
    root = store.list_posts("news")[0]
    for number in range(99):
        store.publish("local:a", f"reply-{number}", "news", "Reply", "Text", parent_id=root.post_id)
    assert not Elements(views.html_thread(root.post_id).decode()).next_links
    assert b"Next page" not in views.page("/page/thread.mu", {"var_id": root.post_id})


def test_pagination_rejects_invalid_and_mismatched_cursors(store: Store) -> None:
    root = store.publish("local:a", "root", "news", "Issue", "Article")
    another = store.publish("local:a", "other", "news", "Other issue", "Article")
    wrong_board = store.publish("local:a", "general", "general", "Other board", "Text")
    views = Views(store, "Mesh")
    for cursor in ("<script>", "a" * 65, "z" * 64, wrong_board.post_id):
        with pytest.raises(BBSError):
            views.html_board("news", after_id=cursor)
        assert b"Page unavailable" in views.page(
            "/page/board.mu", {"var_board": "news", "var_after": cursor}
        )
    with pytest.raises(BBSError):
        views.html_thread(root.post_id, after_id=another.post_id)
    assert b"Page unavailable" in views.page(
        "/page/thread.mu", {"var_id": root.post_id, "var_after": another.post_id}
    )
