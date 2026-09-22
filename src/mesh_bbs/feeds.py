"""Bounded RSS/Atom input for operator-configured newsletter sources.

Parsing never retrieves articles, images, stylesheets, or external XML entities.
An entry's identity is independent of its text so corrections keep their ID.
"""

from __future__ import annotations

import io
import re
import time
import unicodedata
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.parsers import expat

import feedparser
import httpx

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ITEMS = 200
MAX_REDIRECTS = 3
FETCH_DEADLINE_SECONDS = 30.0
_CONTENT_TYPES = {"text/plain", "text/html", "application/xhtml+xml"}
_ATOM_NAMESPACES = {"http://www.w3.org/2005/Atom", "http://purl.org/atom/ns#"}


class FeedError(ValueError):
    """The source cannot be imported safely or without losing identity."""


class FeedFetchError(FeedError):
    """A bounded feed request failed."""


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    body: str
    link: str
    published: str
    summary_only: bool


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    items: tuple[FeedItem, ...]


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    content: bytes | None
    etag: str | None
    last_modified: str | None


def _canonical_url(value: str) -> str:
    """Normalize only unambiguous URL parts; preserve query and path identity."""
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise FeedError("Feed URLs must not contain whitespace or control characters")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise FeedError("Feed URLs must be absolute HTTP or HTTPS URLs")
        if parts.username is not None or parts.password is not None:
            raise FeedError("Feed URLs must not contain embedded credentials")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
    except (ValueError, UnicodeError) as error:
        raise FeedError("Invalid feed URL") from error
    scheme = parts.scheme.lower()
    if port is not None and port != (443 if scheme == "https" else 80):
        host += f":{port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


def _inspect_xml(data: bytes) -> list[bool]:
    """Reject unsafe XML before feedparser; record explicit full-content fields."""
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[str] = []
    content_flags: list[bool] = []
    entry_depth: int | None = None

    def reject_construct(*_args: Any) -> None:
        raise FeedError("Feed XML must not contain DTDs, entities, or processing instructions")

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal entry_depth
        local = name.rsplit("}", 1)[-1]
        parent = stack[-1].rsplit("}", 1)[-1] if stack else ""
        stack.append(name)
        if len(stack) > 64:
            raise FeedError("Feed XML nesting exceeds 64 levels")
        if name.startswith("http://www.w3.org/2001/XInclude}"):
            raise FeedError("Feed XML must not contain remote includes")
        if (
            entry_depth is None
            and local in {"entry", "item"}
            and parent in {"feed", "channel", "RDF"}
        ):
            content_flags.append(False)
            entry_depth = len(stack)
            if len(content_flags) > MAX_FEED_ITEMS:
                raise FeedError(f"Feed exceeds {MAX_FEED_ITEMS} entries")
        elif entry_depth is not None and len(stack) == entry_depth + 1:
            namespace = name.rsplit("}", 1)[0] if "}" in name else ""
            atom_content = (
                namespace in _ATOM_NAMESPACES
                and local == "content"
                and "src" not in attributes
                and attributes.get("type", "text") in _CONTENT_TYPES | {"text", "html", "xhtml"}
            )
            rss_content = name in {
                "http://purl.org/rss/1.0/modules/content/}encoded",
                "http://www.w3.org/1999/xhtml}body",
                "body",
                "fullitem",
            }
            if atom_content or rss_content:
                content_flags[-1] = True

    def end(_name: str) -> None:
        nonlocal entry_depth
        if entry_depth == len(stack):
            entry_depth = None
        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject_construct
    parser.EntityDeclHandler = reject_construct
    parser.ProcessingInstructionHandler = reject_construct
    try:
        parser.Parse(data, True)
    except expat.ExpatError as error:
        raise FeedError("Feed is not well-formed XML") from error
    return content_flags


def _clean_text(value: str) -> str:
    safe = "".join(
        character
        for character in value
        if (unicodedata.category(character) != "Cc" or character in "\n\t")
        and character not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    )
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in safe.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _PlainHTML(HTMLParser):
    _hidden = {"script", "style", "iframe", "object", "template", "head"}
    _blocks = {
        "p",
        "div",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_stack: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._hidden:
            self.hidden_stack.append(tag)
        if self.hidden_stack:
            return
        if tag in self._blocks or tag == "br":
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            try:
                self.links.append(_canonical_url(href))
            except FeedError:
                self.links.append("")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden_stack:
            if tag == self.hidden_stack[-1]:
                self.hidden_stack.pop()
            return
        if tag == "a" and self.links:
            link = self.links.pop()
            if link:
                self.parts.append(f" ({link})")
        if tag in self._blocks:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_stack:
            self.parts.append(data)


def _text(value: str, content_type: str = "text/plain") -> str:
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _PlainHTML()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return _clean_text(value)


def parse_feed(data: bytes) -> ParsedFeed:
    """Parse a complete bounded XML feed; fail on entries without stable IDs.

    ``summary_only`` is conservative: only explicit inline full-content fields
    count as a full article. RSS descriptions can contain entire articles, but
    the format gives us no reliable way to distinguish those from teasers.
    """
    if not isinstance(data, bytes):
        raise TypeError("parse_feed accepts bytes, never a URL or file path")
    if len(data) > MAX_FEED_BYTES:
        raise FeedError(f"Feed exceeds {MAX_FEED_BYTES} bytes")
    explicit_content = _inspect_xml(data)
    parsed = feedparser.parse(io.BytesIO(data), sanitize_html=True, resolve_relative_uris=True)
    if not parsed.get("version") or parsed.get("bozo"):
        raise FeedError("Source is not a valid RSS or Atom feed")
    entries = parsed.get("entries", [])
    if len(entries) > MAX_FEED_ITEMS or len(entries) != len(explicit_content):
        raise FeedError("Feed has too many entries or an unsupported entry structure")
    items: list[FeedItem] = []
    seen_ids: set[str] = set()
    for position, entry in enumerate(entries):
        raw_link = entry.get("link", "")
        try:
            link = _canonical_url(raw_link) if raw_link else ""
        except FeedError:
            link = ""
        item_id = str(entry.get("id", "")).strip() or link
        if not item_id:
            raise FeedError(f"Entry {position + 1} has no stable GUID, Atom ID, or absolute link")
        if len(item_id) > 4096 or any(unicodedata.category(c) == "Cc" for c in item_id):
            raise FeedError(f"Entry {position + 1} has an invalid stable identity")
        if item_id in seen_ids:
            raise FeedError(f"Entry {position + 1} repeats an identity within this feed")
        seen_ids.add(item_id)
        content = (
            [
                _text(part.get("value", ""), part.get("type", "text/plain"))
                for part in entry.get("content", [])
                if part.get("type", "text/plain") in _CONTENT_TYPES and not part.get("src")
            ]
            if explicit_content[position]
            else []
        )
        body = "\n\n".join(part for part in content if part)
        summary_only = not bool(body)
        if summary_only:
            body = _text(
                entry.get("summary", ""), entry.get("summary_detail", {}).get("type", "text/plain")
            )
        title = _text(
            entry.get("title", "Untitled"), entry.get("title_detail", {}).get("type", "text/plain")
        )
        items.append(
            FeedItem(
                item_id,
                title or "Untitled",
                body,
                link,
                _clean_text(
                    entry.get("published", "") or (entry["updated"] if "updated" in entry else "")
                ),
                summary_only,
            )
        )
    title = _text(
        parsed.feed.get("title", "Untitled feed"),
        parsed.feed.get("title_detail", {}).get("type", "text/plain"),
    )
    return ParsedFeed(title, tuple(items))


def _header(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 4096 or any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise FeedFetchError("Invalid HTTP cache validator")
    return value


def _redirect_allowed(current: str, target: str) -> bool:
    before, after = urlsplit(current), urlsplit(target)
    if before.netloc == after.netloc and before.scheme == after.scheme:
        return True
    return (
        before.scheme == "http"
        and after.scheme == "https"
        and before.hostname == after.hostname
        and before.port is None
        and after.port is None
    )


def _read_response(response: httpx.Response, deadline: float) -> bytes:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            declared_bytes = int(length)
        except ValueError as error:
            raise FeedFetchError("Invalid feed response Content-Length") from error
        if declared_bytes > MAX_FEED_BYTES or declared_bytes < 0:
            raise FeedFetchError(f"Feed response exceeds {MAX_FEED_BYTES} bytes")
    encoding = response.headers.get("content-encoding", "identity").lower().strip()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise FeedFetchError("Unsupported feed response compression")
    decoder = (
        None
        if encoding == "identity"
        else zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    )
    body = bytearray()
    wire_bytes = 0
    # Inspect each transport read; buffering to a requested chunk size would
    # let a slow trickle hide progress from the total deadline check.
    for chunk in response.iter_raw():
        if time.monotonic() > deadline:
            raise FeedFetchError("Feed request exceeded its total time budget")
        wire_bytes += len(chunk)
        if wire_bytes > MAX_FEED_BYTES:
            raise FeedFetchError(f"Feed response exceeds {MAX_FEED_BYTES} bytes")
        decoded = decoder.decompress(chunk, MAX_FEED_BYTES - len(body) + 1) if decoder else chunk
        body.extend(decoded)
        if len(body) > MAX_FEED_BYTES:
            raise FeedFetchError(f"Decoded feed response exceeds {MAX_FEED_BYTES} bytes")
        if decoder and decoder.unused_data:
            raise FeedFetchError("Feed response has trailing compressed data")
    if decoder and not decoder.eof:
        raise FeedFetchError("Feed response compression is incomplete")
    return bytes(body)


def fetch_feed(url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
    """Fetch an operator-controlled feed with no ambient proxy or credentials.

    Redirects stay on the same origin, except an HTTP-to-HTTPS upgrade on the
    same host. A source moving elsewhere must be changed by its operator.
    Private-network URLs are allowed deliberately for locally hosted feeds.
    """
    current = _canonical_url(url)
    headers = {
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
        "Accept-Encoding": "gzip, deflate, identity",
        "User-Agent": "Mesh-BBS/0.1",
    }
    if etag is not None:
        headers["If-None-Match"] = _header(etag) or ""
    if last_modified is not None:
        headers["If-Modified-Since"] = _header(last_modified) or ""
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    try:
        with httpx.Client(
            timeout=httpx.Timeout(5.0, connect=5.0), follow_redirects=False, trust_env=False
        ) as client:
            for redirects in range(MAX_REDIRECTS + 1):
                if time.monotonic() > deadline:
                    raise FeedFetchError("Feed request exceeded its total time budget")
                client.cookies.clear()
                with client.stream("GET", current, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirects == MAX_REDIRECTS:
                            raise FeedFetchError("Feed exceeded redirect limit")
                        location = response.headers.get("location")
                        if not location:
                            raise FeedFetchError("Feed redirect has no location")
                        target = _canonical_url(urljoin(current, location))
                        if not _redirect_allowed(current, target):
                            raise FeedFetchError("Feed redirect changed origin or downgraded HTTPS")
                        current = target
                        continue
                    validators = (
                        _header(response.headers.get("etag")),
                        _header(response.headers.get("last-modified")),
                    )
                    if response.status_code == 304:
                        return FetchResult(304, None, *validators)
                    if response.status_code != 200:
                        raise FeedFetchError(f"Feed server returned HTTP {response.status_code}")
                    return FetchResult(200, _read_response(response, deadline), *validators)
    except (httpx.HTTPError, zlib.error):
        # Avoid leaking query tokens through the underlying client's URL errors.
        raise FeedFetchError("Feed request failed during connection or response decoding") from None
    raise FeedFetchError("Feed request did not return content")
