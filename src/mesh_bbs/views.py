"""Public, read-only HTML, RSS, and NomadNet representations of local posts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import format_datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from mesh_bbs.events import BBSError
from mesh_bbs.store import Post, Store

BOARD_LIMIT = 50
THREAD_LIMIT = 100
FEED_LIMIT = 20
_INVALID_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
_POST_ID = re.compile(r"[0-9a-f]{8,64}\Z")
_ATOM = "http://www.w3.org/2005/Atom"
_BBS = "urn:mesh-bbs:feed:1"
ET.register_namespace("atom", _ATOM)
ET.register_namespace("bbs", _BBS)


def _text(value: str) -> str:
    return _INVALID_TEXT.sub("\ufffd", value.replace("\r\n", "\n").replace("\r", "\n"))


def _body_html(value: str) -> str:
    return "<p>" + escape(_text(value)).replace("\n\n", "</p><p>").replace("\n", "<br>\n") + "</p>"


def _literal(value: str) -> str:
    """Indent every literal line so user text cannot terminate Micron literal mode.

    NomadNet's MicronParser toggles literal mode on an exact `` `= `` line,
    even within a literal block. A leading space protects both that delimiter
    and the parser's special ``\\`=`` display substitution without losing text.
    Reference: markqvist/NomadNet, MicronParser.parse_line and make_output.
    """
    return "`=\n" + "\n".join(" " + line for line in _text(value).split("\n")) + "\n`=\n"


def _micron_link(label: str, page: str, **variables: str) -> str:
    # Callers supply only constant labels, configured slugs, and validated IDs.
    fields = "|".join(f"{key}={value}" for key, value in variables.items())
    suffix = f"`{fields}" if fields else ""
    return f"`[{label}`:/page/{page}.mu{suffix}]\n"


def validate_base_url(value: str) -> str:
    """Require a configured HTTP origin; never trust an incoming Host header."""
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
        or any(character in value for character in "\\<>\"'`")
    ):
        raise ValueError("Public URL must be an HTTP or HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
        )
    except ValueError as error:
        raise ValueError("Public URL must be an HTTP or HTTPS origin") from error
    if not valid:
        raise ValueError("Public URL must be an HTTP or HTTPS origin")
    return f"{parsed.scheme}://{parsed.netloc}"


class Views:
    """Render only configured public boards, with bounded listing sizes.

    The store currently has no private boards: every configured board is public.
    Individual post pages contain the complete current revision, including long
    newsletters. Thread indexes use excerpts so a large thread stays small.
    """

    def __init__(self, store: Store, name: str, *, base_url: str = "http://127.0.0.1:8080") -> None:
        if not isinstance(name, str) or not name.strip() or len(name.encode()) > 1024:
            raise ValueError("A bounded service name is required")
        self.store = store
        self.name = _text(name)
        self.base_url = validate_base_url(base_url)

    def _board(self, board: str) -> str:
        if board not in self.store.boards:
            raise BBSError("Board is not available on this host")
        return board

    def _post(self, post_id: str) -> Post:
        if not _POST_ID.fullmatch(post_id):
            raise BBSError("Invalid post identifier")
        post = self.store.get_post(post_id)
        self._board(post.board)
        return post

    def _listing(
        self, board: str, limit: int, after_id: str, thread_id: str | None = None
    ) -> tuple[list[Post], str]:
        if not isinstance(after_id, str) or (after_id and not _POST_ID.fullmatch(after_id)):
            raise BBSError("Invalid listing cursor")
        posts = self.store.list_posts(
            board, limit=limit + 1, after_id=after_id, thread_id=thread_id
        )
        next_id = posts[limit - 1].post_id if len(posts) > limit else ""
        return posts[:limit], next_id

    def _html(self, title: str, body: str) -> bytes:
        title = escape(_text(title))
        name = escape(self.name)
        region = escape(self.store.region)
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{title} | {name}</title></head><body>"
            f'<header><p><a href="/">{name}</a> / {region}</p></header>'
            f"<main><h1>{title}</h1>{body}</main>"
            "<footer><p>Local copy. Posts may still be arriving from other hosts. "
            "Browse here; send posting commands to the service over your mesh.</p></footer>"
            "</body></html>\n"
        ).encode()

    @staticmethod
    def _title(post: Post) -> str:
        return "Removed post" if post.deleted else post.title or "Untitled post"

    def html_index(self) -> bytes:
        entries = "".join(
            f'<li><a href="/boards/{board}">{board}</a>'
            + (" — Newsletters and announcements" if board == "news" else "")
            + f' (<a href="/feeds/{board}.xml">RSS feed</a>)</li>'
            for board in self.store.boards
        )
        return self._html("Boards", f"<ul>{entries}</ul>")

    def html_board(self, board: str, after_id: str = "") -> bytes:
        board = self._board(board)
        posts, next_id = self._listing(board, BOARD_LIMIT, after_id)
        entries = "".join(
            f'<li><a href="/threads/{post.post_id}">{escape(_text(self._title(post)))}</a>'
            f" — {escape(_text(post.author))}</li>"
            for post in posts
        )
        body = f'<p><a href="/feeds/{board}.xml">Subscribe with RSS</a></p>'
        body += f"<ul>{entries}</ul>" if posts else "<p>No threads on this page.</p>"
        body += f"<p>Showing up to {BOARD_LIMIT} threads per page, newest first.</p>"
        if next_id:
            body += f'<p><a rel="next" href="/boards/{board}?after={next_id}">Next page</a></p>'
        if after_id:
            body += f'<p><a href="/boards/{board}">First page</a></p>'
        return self._html(board, body)

    def html_thread(self, post_id: str, after_id: str = "") -> bytes:
        selected = self._post(post_id)
        posts, next_id = self._listing(selected.board, THREAD_LIMIT, after_id, selected.thread_id)
        entries = "".join(
            f'<li><article><h2><a href="/posts/{post.post_id}">'
            f"{escape(_text(self._title(post)))}</a></h2>"
            f"<p>{escape(_text(post.author))}</p>"
            + _body_html("Removed post." if post.deleted else post.body[:240])
            + "</article></li>"
            for post in posts
        )
        body = f'<nav><a href="/boards/{selected.board}">Back to {selected.board}</a></nav>'
        body += f"<ol>{entries}</ol><p>Showing up to {THREAD_LIMIT} posts per page, oldest first. "
        body += "Open a post to read its full text.</p>"
        if next_id:
            body += (
                f'<p><a rel="next" href="/threads/{selected.post_id}?after={next_id}">'
                "Next page</a></p>"
            )
        if after_id:
            body += f'<p><a href="/threads/{selected.post_id}">First page</a></p>'
        return self._html("Thread", body)

    def html_post(self, post_id: str) -> bytes:
        post = self._post(post_id)
        body = (
            f'<nav><a href="/boards/{post.board}">{post.board}</a> / '
            f'<a href="/threads/{post.thread_id}">Thread</a></nav>'
            f"<p>From {escape(_text(post.author))}</p>"
        )
        if post.parent_id:
            body += f'<p>Reply to <a href="/posts/{post.parent_id}">{post.parent_id}</a></p>'
        body += _body_html("This post was removed." if post.deleted else post.body)
        body += (
            f"<details><summary>Post details</summary><dl><dt>Post ID</dt>"
            f"<dd>{post.post_id}</dd><dt>Revision</dt><dd>{post.revision_id}</dd>"
            f"<dt>Created</dt><dd>{escape(_text(post.created_at))}</dd></dl></details>"
        )
        return self._html(self._title(post), body)

    def rss(self, board: str) -> bytes:
        board = self._board(board)
        root = ET.Element("rss", version="2.0")
        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "title").text = f"{self.name} / {board}"
        ET.SubElement(channel, "link").text = f"{self.base_url}/boards/{board}"
        ET.SubElement(
            channel, "description"
        ).text = f"{self.store.region}: the {FEED_LIMIT} most recent threads on this host."
        ET.SubElement(
            channel,
            f"{{{_ATOM}}}link",
            href=f"{self.base_url}/feeds/{board}.xml",
            rel="self",
            type="application/rss+xml",
        )
        for post in self.store.list_posts(board, limit=FEED_LIMIT):
            if post.deleted:
                continue
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = _text(self._title(post))
            ET.SubElement(item, "link").text = f"{self.base_url}/posts/{post.post_id}"
            ET.SubElement(
                item, "guid", isPermaLink="false"
            ).text = f"urn:mesh-bbs:{post.region}:{post.post_id}"
            ET.SubElement(item, f"{{{_BBS}}}revision").text = post.revision_id
            # RSS descriptions are HTML. Escape text for that inner HTML layer
            # before ElementTree escapes the outer XML representation.
            ET.SubElement(item, "description").text = _body_html(post.body)
            try:
                created = datetime.fromisoformat(post.created_at)
                if created.tzinfo is not None:
                    ET.SubElement(item, "pubDate").text = format_datetime(
                        created.astimezone(UTC), usegmt=True
                    )
            except ValueError:
                pass  # Replicated metadata must not prevent reading the feed.
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def page(self, path: str, variables: dict[str, Any]) -> bytes:
        """Handle the four registered NomadNet paths and bounded request fields."""
        if (
            not isinstance(path, str)
            or len(path) > 128
            or not isinstance(variables, dict)
            or len(variables) > 16
        ):
            return self._page_error()
        try:
            board = variables.get("var_board", "general")
            post_id = variables.get("var_id", "")
            after_id = variables.get("var_after", "")
            if (
                not isinstance(board, str)
                or not isinstance(post_id, str)
                or not isinstance(after_id, str)
            ):
                return self._page_error()
            if (
                len(board) > 64
                or len(post_id) > 64
                or (after_id and not _POST_ID.fullmatch(after_id))
            ):
                return self._page_error()
            content = ">Mesh BBS\n" + _literal(self.name)
            content += _literal("Region: " + self.store.region)
            if path == "/page/index.mu":
                content += ">Boards\n"
                for configured_board in self.store.boards:
                    content += _micron_link(configured_board, "board", board=configured_board)
                content += "\nSend posting commands to the service's LXMF address.\n"
            elif path == "/page/board.mu":
                board = self._board(board)
                content += _micron_link("All boards", "index")
                content += _literal("Board: " + board)
                posts, next_id = self._listing(board, BOARD_LIMIT, after_id)
                for post in posts:
                    content += _literal(self._title(post) + "\nFrom: " + post.author)
                    content += _micron_link("Open thread", "thread", id=post.post_id)
                content += f"\nShowing up to {BOARD_LIMIT} threads per page, newest first.\n"
                if not posts:
                    content += "No threads on this page.\n"
                if next_id:
                    content += _micron_link("Next page", "board", board=board, after=next_id)
                if after_id:
                    content += _micron_link("First page", "board", board=board)
            elif path in {"/page/thread.mu", "/page/post.mu"}:
                selected = self._post(post_id)
                content += _micron_link("Back to board", "board", board=selected.board)
                if path == "/page/thread.mu":
                    posts, next_id = self._listing(
                        selected.board, THREAD_LIMIT, after_id, selected.thread_id
                    )
                    for post in posts:
                        content += _literal(self._title(post) + "\nFrom: " + post.author)
                        content += _literal("Removed post." if post.deleted else post.body[:240])
                        content += _micron_link("Read full post", "post", id=post.post_id)
                    content += f"\nShowing up to {THREAD_LIMIT} posts per page, oldest first.\n"
                    if next_id:
                        content += _micron_link(
                            "Next page", "thread", id=selected.post_id, after=next_id
                        )
                    if after_id:
                        content += _micron_link("First page", "thread", id=selected.post_id)
                else:
                    content += _literal(self._title(selected) + "\nFrom: " + selected.author)
                    if selected.parent_id:
                        content += _micron_link("Parent post", "post", id=selected.parent_id)
                    content += _literal(
                        "This post was removed." if selected.deleted else selected.body
                    )
                    content += _literal(
                        f"Post ID: {selected.post_id}\nRevision: {selected.revision_id}"
                    )
                    content += _micron_link("Thread", "thread", id=selected.thread_id)
            else:
                return self._page_error()
            encoded = content.encode("utf-8")
            return encoded if len(encoded) <= 240 * 1024 else self._page_error()
        except BBSError:
            return self._page_error()

    @staticmethod
    def _page_error() -> bytes:
        return (
            ">Page unavailable\nThe requested board or post is not available on this host.\n"
            + _micron_link("All boards", "index")
        ).encode("utf-8")
