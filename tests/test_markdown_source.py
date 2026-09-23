from dataclasses import replace

import pytest

from mesh_bbs.config import FeedConfig
from mesh_bbs.feeds import FeedError, FeedItem, FetchResult
from mesh_bbs.markdown_source import COLORADO_NEWSLETTER_BASE, markdown_text, newsletter_text_url
from mesh_bbs.newsletters import NewsletterImporter
from mesh_bbs.store import Store

PDF = "https://github.com/Colorado-Mesh/advocacy/raw/main/newsletter/issues/2026-09/issue.pdf"
TEXT = COLORADO_NEWSLETTER_BASE + "2026-09/draft.md"


def item(body):
    return FeedItem("issue-9", "Newsletter", body, "https://example.org/issue", "", False)


@pytest.mark.parametrize(
    "url",
    [
        PDF,
        PDF.replace("raw/main", "raw/refs/heads/main"),
        COLORADO_NEWSLETTER_BASE + "2026-09/issue.pdf",
    ],
)
def test_published_pdf_link_selects_only_its_sibling_markdown(url):
    assert newsletter_text_url(item(f"Read the PDF ({url})"), COLORADO_NEWSLETTER_BASE) == TEXT


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/newsletter/issues/2026-09/issue.pdf",
        PDF.replace("Colorado-Mesh", "Another-Org"),
        PDF.replace("/2026-09/", "/2026-09/../../"),
        PDF.replace("/2026-09/", "/2026-99/"),
        PDF + "?token=secret",
        PDF.replace("issue.pdf", "draft.md"),
        PDF.replace("/raw/", "/blob/"),
    ],
)
def test_unrelated_links_do_not_trigger_document_fetches(url):
    assert newsletter_text_url(item(url), COLORADO_NEWSLETTER_BASE) is None


def test_multiple_issues_do_not_silently_choose_the_wrong_newsletter():
    with pytest.raises(FeedError, match="multiple issues"):
        newsletter_text_url(
            item(PDF + " " + PDF.replace("2026-09", "2026-10")), COLORADO_NEWSLETTER_BASE
        )


def test_markdown_to_plain_text_keeps_headings_links_and_captions_without_markup():
    source = b"""---
layout: post
---
<!-- editorial note -->
# Newsletter
## General News
- **A bold update** and [a reference](https://example.org/article).
![Photo credit](photos/picture.jpg)
<script>hidden()</script><style>hidden css</style>
<a href="file:///private">Unsafe target</a>
"""
    text = markdown_text(source, TEXT)
    assert "Newsletter\n\nGeneral News" in text
    assert "- A bold update and a reference (https://example.org/article)" in text
    assert "[Image: Photo credit]" in text
    assert "Unsafe target" in text
    for removed in ("layout:", "editorial note", "hidden", "file://", "<script", "**"):
        assert removed not in text


@pytest.mark.parametrize(
    "data", [b"\xff", b"x" * (128 * 1024 + 1), b"<!-- empty -->", b"---\nmissing closing delimiter"]
)
def test_invalid_markdown_is_not_published(data):
    with pytest.raises(FeedError):
        markdown_text(data, TEXT)


def test_sync_upgrades_existing_post_and_tracks_text_corrections_without_duplicates(tmp_path):
    feed_url = "https://example.org/feed.xml"
    xml = f"""<rss version="2.0"><channel><title>News</title><item>
    <guid>issue-9</guid><title>September newsletter</title>
    <pubDate>Mon, 21 Sep 2026 00:00:00 GMT</pubDate>
    <description>Read the PDF ({PDF})</description></item></channel></rss>""".encode()
    text = b"# Full newsletter\n\nThe complete first edition."
    requests = []
    broken = False

    def fetch(url, etag=None, last_modified=None):
        requests.append((url, etag, last_modified))
        if url == feed_url:
            return FetchResult(200, xml, '"unchanged-feed"', None)
        assert url == TEXT
        if broken:
            raise FeedError("Markdown temporarily unavailable")
        return FetchResult(200, text, None, None)

    store = Store(tmp_path / "bbs.db", "test")
    try:
        config = FeedConfig("newsletter", feed_url, poll_seconds=60)
        assert NewsletterImporter(store, config, fetcher=fetch).poll_once(now=0).changed == 1
        original = store.list_posts("news")[0]
        config = replace(config, newsletter_markdown_base_url=COLORADO_NEWSLETTER_BASE)
        importer = NewsletterImporter(store, config, fetcher=fetch)
        assert importer.poll_once(now=60).changed == 1
        full = store.list_posts("news")[0]
        assert full.post_id == original.post_id
        assert full.created_at == original.created_at
        assert "complete first edition" in full.body
        assert "Text source: " + TEXT in full.body
        assert "Summary only" not in full.body
        assert requests[-2:] == [(feed_url, None, None), (TEXT, None, None)]
        assert importer.poll_once(now=120).changed == 0
        text = b"# Full newsletter\n\nA corrected second edition."
        assert importer.poll_once(now=180).changed == 1
        corrected = store.list_posts("news")[0]
        assert corrected.post_id == original.post_id
        assert corrected.revision_id != full.revision_id
        broken = True
        assert importer.poll_once(now=240).status == "error"
        assert store.list_posts("news") == [corrected]
    finally:
        store.close()
