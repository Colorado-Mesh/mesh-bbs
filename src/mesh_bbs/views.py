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
NOMAD_RECENT_LIMIT = 10
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

    def _html(self, title: str, body: str, *, board: str = "") -> bytes:
        title = escape(_text(title))
        name = escape(self.name)
        region = escape(self.store.region)
        boards = "".join(
            f'<a href="/boards/{slug}"'
            + (' aria-current="page"' if slug == board else "")
            + f'><span class="hash">#</span> {slug}</a>'
            for slug in self.store.boards
        )
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} | {name}</title><link rel="stylesheet" href="/assets/bbs.css">'
            '<script src="/assets/bbs.js" defer></script></head><body>'
            '<a class="skip-link" href="#content">Skip to content</a>'
            '<header class="masthead"><a class="brand" href="/">'
            f'<span class="brand-mark" aria-hidden="true">M/B</span><span>{name}'
            "<small>Community bulletin board</small></span></a>"
            '<div class="account"><span id="account-name">Public reading</span>'
            '<button id="signin-open" type="button" hidden>Sign in to post</button>'
            '<button id="signout" type="button" hidden>Sign out</button></div></header>'
            '<div class="workspace"><aside class="sidebar"><p class="eyebrow">Your community</p>'
            f'<p class="region">{region}</p><nav aria-label="Boards"><a href="/">All boards</a>'
            f'{boards}</nav><a class="connect-link" href="/connect">Connect over a mesh</a>'
            '<div class="sidebar-note"><span class="status-dot"></span> A local copy'
            "<p>Saved here first. Shared with connected peers as links become available.</p>"
            '</div></aside><main id="content"><div class="page-heading">'
            f'<p class="eyebrow">Mesh BBS / {region}</p><h1>{title}</h1></div>{body}</main></div>'
            '<footer class="site-footer">Community conversations, across networks.'
            ' <a href="/connect">Posting from a radio</a></footer>'
            '<dialog id="signin-dialog"><form id="signin-form" method="post">'
            '<div class="dialog-heading">'
            '<h2>Sign in to post</h2><button type="button" id="signin-close" '
            'aria-label="Close sign in">Close</button></div>'
            "<p>Use the personal access key your BBS operator gave you.</p>"
            '<label for="access-key">Access key</label><input id="access-key" '
            'type="password" autocomplete="off" required maxlength="128">'
            '<p class="hint">This key stays in this browser tab. Reading is always public.</p>'
            '<p id="signin-error" role="alert"></p><button class="primary" type="submit">'
            "Sign in</button></form></dialog>"
            "</body></html>\n"
        ).encode()

    @staticmethod
    def _meta(post: Post) -> str:
        protocol, separator, sender = post.author.partition(":")
        labels = {
            "meshcore": "MeshCore",
            "meshtastic": "Meshtastic",
            "lxmf": "Reticulum",
            "reticulum": "Reticulum",
            "packet": "Packet",
            "web": "Web",
            "local": "Local",
            "feed": "RSS",
        }
        label = labels.get(protocol, "Peer")
        author = sender if separator else post.author
        short = author if len(author) <= 28 else author[:12] + "…" + author[-8:]
        try:
            stamp = (
                datetime.fromisoformat(post.created_at).astimezone(UTC).strftime("%b %d, %H:%M UTC")
            )
        except (ValueError, OverflowError):
            stamp = post.created_at
        return (
            f'<span class="transport">{label}</span> '
            f'<span class="author" title="{escape(_text(post.author))}">'
            f"{escape(_text(short))}</span>"
            f' <time datetime="{escape(_text(post.created_at))}">{escape(_text(stamp))}</time>'
        )

    def _rows(self, posts: list[Post]) -> str:
        return (
            '<ul class="thread-list">'
            + "".join(
                f'<li><a class="thread-title" href="/threads/{post.post_id}">'
                f'{escape(_text(self._title(post)))}</a><p class="excerpt">'
                f"{escape(_text(post.body[:160]))}</p>"
                f'<div class="post-meta">{self._meta(post)}</div></li>'
                for post in posts
            )
            + "</ul>"
        )

    @staticmethod
    def _title(post: Post) -> str:
        return "Removed post" if post.deleted else post.title or "Untitled post"

    def html_index(self) -> bytes:
        sections = []
        for board in self.store.boards:
            posts = self.store.list_posts(board, limit=3)
            subtitle = (
                "Newsletters and announcements"
                if board == "news"
                else "Conversations from the community"
            )
            sections.append(
                f'<section class="board-section"><div class="section-heading"><div>'
                f'<h2><a href="/boards/{board}"><span class="hash">#</span> {board}</a></h2>'
                f'<p>{subtitle}</p></div><a class="button" href="/new/{board}">New post</a></div>'
                + (
                    self._rows(posts)
                    if posts
                    else '<div class="empty"><h3>A place to start.</h3>'
                    "<p>Share a question, a field report, "
                    "or something your community should know.</p></div>"
                )
                + f'<div class="section-footer"><a href="/boards/{board}">View all threads →</a>'
                f'<a href="/feeds/{board}.xml">RSS feed</a></div></section>'
            )
        return self._html(
            "On the board",
            '<p class="lede">Read the latest. Leave something useful.</p>' + "".join(sections),
        )

    def html_board(self, board: str, after_id: str = "") -> bytes:
        board = self._board(board)
        posts, next_id = self._listing(board, BOARD_LIMIT, after_id)
        body = (
            f'<div class="toolbar"><span>Newest threads first</span>'
            f'<div><a href="/feeds/{board}.xml">Subscribe with RSS</a> '
            f'<a class="button primary" href="/new/{board}">New post</a></div></div>'
        )
        body += (
            self._rows(posts)
            if posts
            else '<div class="empty"><h2>No threads yet.</h2>'
            "<p>Start the conversation with a new post.</p></div>"
        )
        body += f'<p class="hint">Showing up to {BOARD_LIMIT} threads per page.</p>'
        if next_id:
            body += f'<p><a rel="next" href="/boards/{board}?after={next_id}">Next page</a></p>'
        if after_id:
            body += f'<p><a href="/boards/{board}">First page</a></p>'
        return self._html("# " + board, body, board=board)

    def html_thread(self, post_id: str, after_id: str = "") -> bytes:
        selected = self._post(post_id)
        posts, next_id = self._listing(selected.board, THREAD_LIMIT, after_id, selected.thread_id)
        entries = "".join(self._article(post, full=not post.parent_id) for post in posts)
        body = f'<nav><a href="/boards/{selected.board}">Back to {selected.board}</a></nav>'
        body += f'<div class="conversation">{entries}</div>'
        body += f'<p class="hint">Showing up to {THREAD_LIMIT} posts per page, oldest first.</p>'
        if next_id:
            body += (
                f'<p><a rel="next" href="/threads/{selected.post_id}?after={next_id}">'
                "Next page</a></p>"
            )
        if after_id:
            body += f'<p><a href="/threads/{selected.post_id}">First page</a></p>'
        try:
            root = self._post(selected.thread_id)
            title = self._title(root)
        except BBSError:
            title = "Thread arriving from a peer"
        return self._html(title, body, board=selected.board)

    def _article(self, post: Post, *, full: bool = True) -> str:
        text = post.body if full else post.body[:2000]
        body = _body_html("This post was removed." if post.deleted else text)
        parent = (
            f'<a href="/posts/{post.parent_id}">In reply to {post.parent_id[:12]}</a>'
            if post.parent_id
            else "Original post"
        )
        return (
            f'<article class="post" id="post-{post.post_id}"><header><div class="post-meta">'
            f'{self._meta(post)}</div><p class="post-context">{parent}</p>'
            f'<h2><a href="/posts/{post.post_id}">{escape(_text(self._title(post)))}</a></h2>'
            f'</header><div class="post-body">{body}</div><div class="post-actions">'
            + (
                f'<a class="button" href="/reply/{post.post_id}">Reply</a>'
                if not post.deleted
                else ""
            )
            + (
                f'<a href="/posts/{post.post_id}">Read full post</a>'
                if not full and len(post.body) > 2000 and not post.deleted
                else ""
            )
            + f'<a href="/posts/{post.post_id}">Permalink</a></div></article>'
        )

    def html_post(self, post_id: str) -> bytes:
        post = self._post(post_id)
        body = (
            f'<nav><a href="/boards/{post.board}">{post.board}</a> / '
            f'<a href="/threads/{post.post_id}">Thread</a></nav>'
        )
        if post.parent_id:
            body += f'<p>Reply to <a href="/posts/{post.parent_id}">{post.parent_id[:12]}</a></p>'
        body += self._article(post)
        body += (
            f"<details><summary>Post details</summary><dl><dt>Post ID</dt>"
            f"<dd>{post.post_id}</dd><dt>Revision</dt><dd>{post.revision_id}</dd>"
            f"<dt>Created</dt><dd>{escape(_text(post.created_at))}</dd></dl></details>"
        )
        return self._html(self._title(post), body, board=post.board)

    def html_compose(self, board: str = "", parent_id: str = "") -> bytes:
        parent = self._post(parent_id) if parent_id else None
        if parent and parent.deleted:
            raise BBSError("This post was removed")
        board = self._board(parent.board if parent else board)
        title = "Reply to " + self._title(parent) if parent else "New post"
        context = (
            f'<a href="/posts/{parent.post_id}">{escape(_text(self._title(parent)))}</a>'
            if parent
            else f'<a href="/boards/{board}"># {board}</a>'
        )
        body = f'<p class="compose-context">Posting to {context}</p>'
        if parent:
            body += f"<blockquote>{escape(_text(parent.body[:300]))}</blockquote>"
        parent_value = parent.post_id if parent else ""
        title_value = escape(_text("Re: " + parent.title[:60])) if parent else ""
        body += (
            f'<form id="compose-form" method="post" action="/api/posts" '
            f'data-board="{board}" data-parent="{parent_value}">'
            '<p id="compose-access">Sign in with your contributor key to publish.</p>'
            '<label for="post-title">Title</label><input id="post-title" name="title" '
            f'maxlength="256" required value="{title_value}">'
            '<label for="post-body">Message</label><textarea id="post-body" name="body" '
            'rows="13" required placeholder="Write your post. '
            'Long stories and newsletters belong here, too."></textarea>'
            '<div class="composer-meta"><span id="body-count">0 / 65,536 bytes</span>'
            '<span id="draft-state">Draft stays in this browser</span></div>'
            '<div class="compose-buttons"><button id="publish" class="primary" '
            'type="submit" disabled>'
            'Publish post</button><button id="preview-toggle" type="button" hidden>Preview</button>'
            '<button id="draft-clear" type="button" hidden>Discard draft</button></div>'
            '<p id="compose-message" role="status" aria-live="polite"></p>'
            '<section id="post-preview" class="post-body" hidden>'
            "<h2>Preview</h2><div></div></section>"
            '<p class="hint">Your post is public. It saves to this host first, '
            "then syncs to trusted peers.</p>"
            "<noscript>Enable JavaScript to use the composer, "
            "or post using the mesh commands.</noscript></form>"
        )
        return self._html(title, body, board=board)

    def html_connect(self) -> bytes:
        body = (
            '<p class="lede">One board. Different ways in.</p><div class="guide">'
            "<h2>From your browser</h2><p>Read freely. To write, ask your BBS operator for a "
            "personal "
            "access key, choose Sign in to post, and open New post or Reply.</p>"
            "<h2>From MeshCore or Meshtastic</h2><p>Send a direct message to the BBS service node "
            "provided by your operator. Use the same commands on either network.</p>"
            "<h2>From Reticulum</h2><p>Send an LXMF message to the service destination. "
            "You can also browse its NomadNet pages. Ask the operator for the addresses.</p>"
            "<h2>Start with these commands</h2><pre>boards\nthreads general\nread POST_ID\nmore\n"
            "@meetup-1 post general Saturday meetup | Meet at the trailhead at 9.</pre>"
            "<p>Keep the same @operation-id when retrying a post. "
            "Use a new one for a different post.</p>"
            "<h2>Long posts and replies</h2><pre>new general Trip report\n"
            "add DRAFT_ID 1 First part…\n"
            "add DRAFT_ID 2 Next part…\npreview DRAFT_ID\npublish DRAFT_ID\n\n"
            "reply POST_ID Thanks for the report!\npublish REPLY_DRAFT_ID</pre>"
            "<p>The service returns each draft ID. Keep radio messages within your network’s "
            "packet limit; split longer text across numbered parts. "
            "Newsletter threads need editor access.</p>"
            "<h2>Packet radio</h2><p>Your operator can provide a packet terminal gateway with the "
            "same commands. Posting must be enabled for that gateway.</p></div>"
        )
        return self._html("Connect & contribute", body)

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
            except (ValueError, OverflowError):
                pass  # Replicated metadata must not prevent reading the feed.
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _nomad_title(post: Post) -> str:
        title = Views._title(post)
        return "`!`Fcef\n" + _literal(title) + "`f`!\n"

    @staticmethod
    def _nomad_meta(post: Post) -> str:
        try:
            stamp = (
                datetime.fromisoformat(post.created_at)
                .astimezone(UTC)
                .strftime("%Y-%m-%d %H:%M UTC")
            )
        except (ValueError, OverflowError):
            stamp = post.created_at
        return "`F9ab\n" + _literal(f"{stamp} / {post.board}\nFrom: {post.author}") + "`f\n"

    def _nomad_rows(self, posts: list[Post]) -> str:
        return "\n".join(
            self._nomad_title(post)
            + self._nomad_meta(post)
            + _micron_link("Read full post", "post", id=post.post_id).rstrip("\n")
            + "  "
            + _micron_link("Open thread", "thread", id=post.post_id)
            for post in posts
        )

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
            content = "#!c=0\n>BBS / `F7dcCOMMUNITY BULLETIN BOARD`f\n"
            content += "`!\n" + _literal(self.name) + "`!\n"
            content += "`F9ab\n" + _literal("Region: " + self.store.region) + "`f\n\n"
            if path == "/page/index.mu":
                content += ">>Browse boards\n"
                for configured_board in self.store.boards:
                    content += _micron_link(configured_board, "board", board=configured_board)
                recent = [
                    post
                    for configured_board in self.store.boards
                    for post in self.store.list_posts(configured_board, limit=NOMAD_RECENT_LIMIT)
                ]
                recent.sort(key=lambda post: (post.created_at, post.post_id), reverse=True)
                content += "\n>>`F7dcLatest entries`f\nNewest first across all boards.\n\n"
                content += self._nomad_rows(recent[:NOMAD_RECENT_LIMIT])
                if not recent:
                    content += "No entries yet.\n"
                content += "\n>>Take part\nRead an entry in full, or open its thread for replies.\n"
                content += "Send posting commands to the service's LXMF address. Start with help.\n"
            elif path == "/page/board.mu":
                board = self._board(board)
                content += _micron_link("All boards", "index")
                content += _literal("Board: " + board)
                posts, next_id = self._listing(board, BOARD_LIMIT, after_id)
                content += "\n>>`F7dcEntries / newest first`f\n\n"
                content += self._nomad_rows(posts)
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
                    content += (
                        "\n>>`F7dcConversation`f\nOriginal post and replies in reading order.\n\n"
                    )
                    for post in posts:
                        content += self._nomad_title(post) + self._nomad_meta(post)
                        content += _literal("Removed post." if post.deleted else post.body[:240])
                        content += _micron_link("Read full post", "post", id=post.post_id)
                        content += "\n"
                    content += f"\nShowing up to {THREAD_LIMIT} posts per page, oldest first.\n"
                    if next_id:
                        content += _micron_link(
                            "Next page", "thread", id=selected.post_id, after=next_id
                        )
                    if after_id:
                        content += _micron_link("First page", "thread", id=selected.post_id)
                else:
                    content += "\n" + self._nomad_title(selected) + self._nomad_meta(selected)
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
