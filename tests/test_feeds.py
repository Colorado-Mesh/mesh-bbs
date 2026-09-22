from __future__ import annotations

import gzip
import traceback
import warnings
from collections.abc import Iterator

import httpx
import pytest

from mesh_bbs import feeds
from mesh_bbs.feeds import FeedError, FeedFetchError, fetch_feed, parse_feed


def rss(items: str) -> bytes:
    return (
        f'<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Colorado Mesh</title><link>https://example.org/</link>"
        f"<description>News</description>{items}</channel></rss>"
    ).encode()


def atom(entries: str) -> bytes:
    return (
        f'<feed xmlns="http://www.w3.org/2005/Atom"><title>Colorado Mesh</title>'
        f"<id>urn:mesh:feed</id><updated>2026-09-22T12:00:00Z</updated>"
        f"{entries}</feed>"
    ).encode()


def test_rss_full_content_preserves_paragraphs_links_and_identity() -> None:
    data = rss("""<item><guid isPermaLink="false">issue:september</guid>
      <title>September &amp; October</title><link>https://example.org/news/9</link>
      <description>A short teaser</description><content:encoded><![CDATA[
      <h2>Network updates</h2><p>First paragraph.</p><p>Second paragraph.</p>
      <a href="https://example.org/events">Events</a><script>doBadThings()</script>
      <img src="https://tracking.example/pixel"/>
      ]]></content:encoded></item>""")
    parsed = parse_feed(data)
    item = parsed.items[0]
    assert parsed.title == "Colorado Mesh"
    assert item.item_id == "issue:september"
    assert item.title == "September & October"
    assert "First paragraph.\n\nSecond paragraph." in item.body
    assert "Events (https://example.org/events)" in item.body
    assert "doBadThings" not in item.body
    assert "tracking.example" not in item.body
    assert item.summary_only is False


def test_atom_plain_content_does_not_mistake_comparison_for_html() -> None:
    item = parse_feed(
        atom("""<entry><id>urn:mesh:issue:1</id><title>Math</title>
      <updated>2026-09-22T13:00:00Z</updated>
      <content type="text">Use x &lt; y and &lt;example&gt; literally.</content></entry>""")
    ).items[0]
    assert item.body == "Use x < y and <example> literally."
    assert item.published == "2026-09-22T13:00:00Z"
    assert item.summary_only is False


@pytest.mark.parametrize(
    "document",
    [
        rss(
            "<item><guid>one</guid>"
            "<description>This might be the whole article.</description></item>"
        ),
        atom("<entry><id>one</id><summary>This might be the whole article.</summary></entry>"),
        atom("""<entry><id>one</id><summary>This might be the whole article.</summary>
         <content src="https://example.org/full" type="text/html"/></entry>"""),
    ],
)
def test_summary_only_is_honest_and_remote_content_is_not_fetched(document: bytes) -> None:
    item = parse_feed(document).items[0]
    assert item.summary_only is True
    assert item.body == "This might be the whole article."


def test_two_summary_fields_do_not_become_full_content() -> None:
    item = parse_feed(
        rss("""<item><guid>one</guid><summary>Teaser one</summary>
      <description>Teaser two</description></item>""")
    ).items[0]
    assert item.summary_only is True


def test_url_fallback_normalizes_host_default_port_fragment_only() -> None:
    item = parse_feed(
        rss("""<item><link>HTTPS://EXAMPLE.ORG:443/news?issue=9#read</link>
      <description>News</description></item>""")
    ).items[0]
    assert item.item_id == "https://example.org/news?issue=9"


def test_corrections_retain_identity_and_equal_text_does_not_collapse_posts() -> None:
    before = parse_feed(rss("<item><guid>A</guid><description>First</description></item>"))
    after = parse_feed(
        rss("""<item><guid>A</guid><description>Corrected</description></item>
      <item><guid>B</guid><description>Corrected</description></item>""")
    )
    assert before.items[0].item_id == after.items[0].item_id
    assert len(after.items) == 2
    assert after.items[0].body == after.items[1].body
    assert after.items[0].item_id != after.items[1].item_id


@pytest.mark.parametrize(
    "item",
    [
        "<item><title>No ID</title><description>Body</description></item>",
        "<item><link>/relative/only</link></item>",
        "<item><link>https://user:password@example.org/</link></item>",
    ],
)
def test_missing_stable_identity_is_reported(item: str) -> None:
    with pytest.raises(FeedError, match="no stable"):
        parse_feed(rss(item))


def test_duplicate_ids_in_one_document_are_not_order_dependent() -> None:
    with pytest.raises(FeedError, match="repeats an identity"):
        parse_feed(rss("<item><guid>A</guid></item><item><guid>A</guid></item>"))


@pytest.mark.parametrize(
    "document",
    [
        b'<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///etc/passwd">]><rss>&secret;</rss>',
        b'<!DOCTYPE rss SYSTEM "https://example.org/external.dtd"><rss/>',
        '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE rss><rss/>'.encode("utf-16"),
        b'<?xml-stylesheet href="https://example.org/style"?><rss/>',
        b'<rss xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///etc/passwd"/></rss>',
    ],
)
def test_unsafe_xml_is_rejected_before_feedparser(
    document: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_parse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unsafe document reached feedparser")

    monkeypatch.setattr(feeds.feedparser, "parse", unexpected_parse)
    with pytest.raises(FeedError):
        parse_feed(document)


def test_size_item_and_depth_limits() -> None:
    with pytest.raises(FeedError, match="bytes"):
        parse_feed(b" " * (feeds.MAX_FEED_BYTES + 1))
    with pytest.raises(FeedError, match="entries"):
        parse_feed(rss("".join(f"<item><guid>{n}</guid></item>" for n in range(201))))
    with pytest.raises(FeedError, match="nesting"):
        parse_feed(b"<rss>" + b"<x>" * 65 + b"</x>" * 65 + b"</rss>")


@pytest.mark.parametrize("document", [b"", b"<rss>", b"<html><title>Not a feed</title></html>"])
def test_invalid_documents_fail(document: bytes) -> None:
    with pytest.raises(FeedError):
        parse_feed(document)


def test_parser_never_accepts_a_url_as_input() -> None:
    with pytest.raises(TypeError, match="bytes"):
        parse_feed("https://example.org/feed")  # type: ignore[arg-type]


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    original = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(feeds.httpx, "Client", client)


def test_conditional_fetch_and_not_modified(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"edition-9"'
        assert request.headers["if-modified-since"] == "Tue, 22 Sep 2026 12:00:00 GMT"
        assert "authorization" not in request.headers
        return httpx.Response(304, headers={"etag": '"edition-9"'})

    mock_http(monkeypatch, handler)
    result = fetch_feed("https://example.org/rss", '"edition-9"', "Tue, 22 Sep 2026 12:00:00 GMT")
    assert result.content is None
    assert result.status_code == 304
    assert result.etag == '"edition-9"'


def test_same_origin_redirect_and_upgrade_do_not_forward_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    content = rss("<item><guid>A</guid></item>")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert "cookie" not in request.headers
        if len(requests) == 1:
            return httpx.Response(
                301, headers={"location": "https://example.org/feed", "set-cookie": "secret=token"}
            )
        if len(requests) == 2:
            return httpx.Response(302, headers={"location": "/new-feed"})
        return httpx.Response(200, stream=httpx.ByteStream(content))

    mock_http(monkeypatch, handler)
    assert fetch_feed("http://example.org/feed").content == content
    assert requests == [
        "http://example.org/feed",
        "https://example.org/feed",
        "https://example.org/new-feed",
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/feed",
        "http://example.org/feed",
        "https://example.org:444/feed",
        "https://user:password@example.org/feed",
        "file:///etc/passwd",
    ],
)
def test_unsafe_redirect_is_not_requested(location: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": location})

    mock_http(monkeypatch, handler)
    with pytest.raises(FeedError):
        fetch_feed("https://example.org/feed")
    assert calls == 1


def test_redirect_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "/again"})

    mock_http(monkeypatch, handler)
    with pytest.raises(FeedFetchError, match="redirect limit"):
        fetch_feed("https://example.org/feed")
    assert calls == feeds.MAX_REDIRECTS + 1


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-length": str(feeds.MAX_FEED_BYTES + 1)}, b""),
        ({}, b"x" * (feeds.MAX_FEED_BYTES + 1)),
        ({"content-encoding": "gzip"}, gzip.compress(b"x" * (feeds.MAX_FEED_BYTES + 1))),
    ],
)
def test_wire_and_decompressed_size_bounds(
    headers: dict[str, str], body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_http(
        monkeypatch,
        lambda _request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body)),
    )
    with pytest.raises(FeedFetchError, match="bytes"):
        fetch_feed("https://example.org/feed")


def test_gzip_response_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    body = rss("<item><guid>A</guid></item>")
    mock_http(
        monkeypatch,
        lambda _request: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(gzip.compress(body))
        ),
    )
    assert fetch_feed("https://example.org/feed").content == body


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/feed",
        "ftp://example.org/feed",
        "https://user:password@example.org/feed",
        "https://example.org/\nfeed",
    ],
)
def test_invalid_fetch_urls_fail_before_network(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid URL reached network")

    mock_http(monkeypatch, handler)
    with pytest.raises(FeedError):
        fetch_feed(url)


def test_invalid_conditional_header_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Injected header reached network")

    mock_http(monkeypatch, handler)
    with pytest.raises(FeedFetchError, match="validator"):
        fetch_feed("https://example.org/feed", "value\r\nAuthorization: secret")


def test_streaming_deadline_stops_slow_trickle(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    received: list[bytes] = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(100):
                clock[0] += 4.0
                received.append(b"x")
                yield b"x"

    monkeypatch.setattr(feeds.time, "monotonic", lambda: clock[0])
    mock_http(monkeypatch, lambda _request: httpx.Response(200, stream=SlowStream()))
    with pytest.raises(FeedFetchError, match="time budget"):
        fetch_feed("https://example.org/feed")
    assert len(received) == 8


@pytest.mark.parametrize("body", [b"not gzip", gzip.compress(b"article")[:-4]])
def test_corrupt_or_truncated_compression_is_rejected(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_http(
        monkeypatch,
        lambda _request: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(body)
        ),
    )
    with pytest.raises(FeedFetchError):
        fetch_feed("https://example.org/feed")


def test_network_errors_do_not_expose_query_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Connection failed for {request.url}", request=request)

    mock_http(monkeypatch, handler)
    with pytest.raises(FeedFetchError) as caught:
        fetch_feed("https://example.org/feed?token=secret-query-token")
    formatted = "".join(traceback.format_exception(caught.value))
    # Traceback contains the test's call site, so inspect only exception messages.
    assert "secret-query-token" not in str(caught.value)
    assert "Connection failed for" not in formatted


def test_original_publication_date_precedes_updated_and_avoids_deprecated_fallback() -> None:
    documents = [
        rss("<item><guid>one</guid><pubDate>Tue, 22 Sep 2026 12:00:00 GMT</pubDate></item>"),
        atom(
            "<entry><id>one</id><published>2026-09-22T12:00:00Z</published>"
            "<updated>2026-09-23T12:00:00Z</updated></entry>"
        ),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert parse_feed(documents[0]).items[0].published == "Tue, 22 Sep 2026 12:00:00 GMT"
        assert parse_feed(documents[1]).items[0].published == "2026-09-22T12:00:00Z"
