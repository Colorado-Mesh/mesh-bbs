"""Read published newsletter text linked from a configured feed.

Only an issue already linked by the feed can select a Markdown document under
the operator's configured directory. No repository listing or draft discovery.
"""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urljoin, urlsplit

from markdown_it import MarkdownIt

from mesh_bbs.feeds import FeedError, FeedItem, _canonical_url, _text

MAX_MARKDOWN_BYTES = 128 * 1024
COLORADO_NEWSLETTER_BASE = (
    "https://raw.githubusercontent.com/Colorado-Mesh/advocacy/main/newsletter/issues/"
)


def newsletter_text_url(item: FeedItem, base_url: str) -> str | None:
    base = _canonical_url(base_url)
    matches = set()
    for link in re.findall(r"https?://[^\s<>\"')]+", item.body):
        try:
            link = _canonical_url(link)
        except FeedError:
            continue
        parts = urlsplit(link)
        if parts.netloc == "github.com" and not parts.query:
            github = re.fullmatch(r"/([^/]+)/([^/]+)/raw/(?:refs/heads/)?(.+)", parts.path)
            if github:
                link = "https://raw.githubusercontent.com/" + "/".join(github.groups())
        if not link.startswith(base):
            continue
        # The published PDF identifies the issue; its sibling draft.md is the
        # text source used by the Colorado Mesh newsletter publication tools.
        issue = re.fullmatch(r"(\d{4}-(?:0[1-9]|1[0-2]))/[A-Za-z0-9_.-]+\.pdf", link[len(base) :])
        if issue:
            matches.add(base + issue[1] + "/draft.md")
    if len(matches) > 1:
        raise FeedError("Newsletter entry links multiple issues; cannot select its text source")
    return next(iter(matches), None)


def markdown_text(data: bytes, source_url: str) -> str:
    if len(data) > MAX_MARKDOWN_BYTES:
        raise FeedError("Newsletter Markdown exceeds the 128 KiB source limit")
    try:
        document = data.decode("utf-8-sig")
    except UnicodeError as error:
        raise FeedError("Newsletter Markdown must be UTF-8") from error
    if document.startswith("---\n"):
        _, separator, document = document[4:].partition("\n---\n")
        if not separator:
            raise FeedError("Newsletter Markdown front matter is incomplete")
    parser = MarkdownIt("commonmark", {"html": True, "maxNesting": 20})
    tokens = parser.parse(document)
    for block in tokens:
        for token in block.children or []:
            if token.type == "link_open":
                token.attrSet("href", urljoin(source_url, str(token.attrGet("href") or "")))
            elif token.type == "image":
                # Keep the caption without retrieving the image or shipping a
                # broken image URL to readers on a radio.
                token.type = "html_inline"
                token.content = "[Image: " + escape(token.content) + "]"
    text = _text(parser.renderer.render(tokens, parser.options, {}), "text/html")
    if not text.strip():
        raise FeedError("Newsletter Markdown has no readable text")
    return text
