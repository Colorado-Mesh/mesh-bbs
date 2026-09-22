from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from mesh_bbs.config import FeedConfig
from mesh_bbs.events import MAX_BODY_BYTES, stable_id
from mesh_bbs.feeds import FeedError, FeedFetchError, FetchResult
from mesh_bbs.newsletters import MAX_BACKOFF_SECONDS, NewsletterImporter
from mesh_bbs.store import Post, Store


def document(
    *entries: tuple[str, str], full: bool = True, published: dict[str, str] | None = None
) -> bytes:
    tag = "content:encoded" if full else "description"
    items = "".join(
        f'<item><guid isPermaLink="false">{identity}</guid><title>September</title>'
        f"<pubDate>{(published or {}).get(identity, '')}</pubDate>"
        f"<link>https://example.org/news/{identity}</link><{tag}>{body}</{tag}></item>"
        for identity, body in entries
    )
    return (
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Colorado Mesh</title>{items}</channel></rss>"
    ).encode()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    database = Store(tmp_path / "bbs.sqlite3", "colorado-mesh")
    yield database
    database.close()


@pytest.fixture
def config() -> FeedConfig:
    return FeedConfig(
        "colorado-newsletter", "https://example.org/feed?token=private", poll_seconds=60
    )


class Fetcher:
    def __init__(self, *responses: FetchResult | Exception) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[str, str | None, str | None]] = []

    def __call__(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.requests.append((url, etag, last_modified))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def result(data: bytes, etag: str | None = '"v1"', modified: str | None = None) -> FetchResult:
    return FetchResult(200, data, etag, modified)


def state(store: Store, config: FeedConfig) -> dict[str, object]:
    with store.transaction():
        row = store.db.execute(
            "SELECT * FROM newsletter_sources WHERE source_id=?", (config.source_id,)
        ).fetchone()
        return dict(row)


def test_import_attribution_and_local_status(store: Store, config: FeedConfig) -> None:
    fetcher = Fetcher(result(document(("issue-9", "First paragraph.\n\nSecond paragraph."))))
    outcome = NewsletterImporter(store, config, fetcher=fetcher).poll_once(now=100)
    assert (outcome.changed, outcome.status, outcome.next_poll_at) == (1, "saved_locally", 160)
    post = store.list_posts("news")[0]
    assert post.source_id == config.source_id
    assert "Original: https://example.org/news/issue-9" in post.body
    assert "First paragraph.\n\nSecond paragraph." in post.body
    assert "token=private" not in post.body
    assert "Summary only" not in post.body
    assert state(store, config)["etag"] == '"v1"'


def test_summary_is_labelled(store: Store, config: FeedConfig) -> None:
    fetcher = Fetcher(result(document(("issue-9", "Only a teaser."), full=False)))
    NewsletterImporter(store, config, fetcher=fetcher).poll_once(now=100)
    body = store.list_posts("news")[0].body
    assert "Summary only: the feed did not provide confirmed full article text." in body
    assert body.endswith("Only a teaser.")


def test_repeated_issue_does_not_create_revisions_but_correction_does(
    store: Store, config: FeedConfig
) -> None:
    first = result(document(("issue-9", "Original text")))
    fetcher = Fetcher(first, first, result(document(("issue-9", "Corrected text")), '"v2"'))
    importer = NewsletterImporter(store, config, fetcher=fetcher)
    assert importer.poll_once(now=0).changed == 1
    post = store.list_posts("news")[0]
    assert importer.poll_once(now=60).changed == 0
    assert store.list_posts("news")[0].revision_id == post.revision_id
    assert importer.poll_once(now=120).changed == 1
    corrected = store.list_posts("news")[0]
    assert corrected.post_id == post.post_id
    assert corrected.revision_id != post.revision_id
    assert corrected.body.endswith("Corrected text")
    assert len(store.list_posts("news")) == 1


def test_equal_text_from_distinct_issues_stays_distinct(store: Store, config: FeedConfig) -> None:
    fetcher = Fetcher(result(document(("issue-9", "Same text"), ("issue-10", "Same text"))))
    outcome = NewsletterImporter(store, config, fetcher=fetcher).poll_once(now=0)
    assert outcome.changed == 2
    assert len(store.list_posts("news")) == 2


def test_conditional_validators_and_schedule_survive_restart(
    tmp_path: Path, config: FeedConfig
) -> None:
    path = tmp_path / "persistent.sqlite3"
    first = Store(path, "colorado-mesh")
    first_fetcher = Fetcher(result(document(("issue-9", "Text")), '"v1"', "Yesterday"))
    NewsletterImporter(first, config, fetcher=first_fetcher).poll_once(now=100)
    first.close()
    reopened = Store(path, "colorado-mesh")
    try:
        fetcher = Fetcher(FetchResult(304, None, None, None))
        importer = NewsletterImporter(reopened, config, fetcher=fetcher)
        skipped = importer.poll_once(now=159)
        assert skipped.status == "not_due"
        assert fetcher.requests == []
        unchanged = importer.poll_once(now=160)
        assert (unchanged.status, unchanged.changed) == ("not_modified", 0)
        assert fetcher.requests == [(config.url, '"v1"', "Yesterday")]
        assert state(reopened, config)["etag"] == '"v1"'
        assert state(reopened, config)["last_modified"] == "Yesterday"
        assert state(reopened, config)["next_poll_at"] == 220
    finally:
        reopened.close()


def test_success_without_validators_clears_old_validators(store: Store, config: FeedConfig) -> None:
    data = document(("issue-9", "Text"))
    fetcher = Fetcher(result(data, '"v1"', "Yesterday"), result(data, None, None))
    importer = NewsletterImporter(store, config, fetcher=fetcher)
    importer.poll_once(now=0)
    importer.poll_once(now=60)
    assert state(store, config)["etag"] is None
    assert state(store, config)["last_modified"] is None


def test_backoff_is_persistent_capped_and_reset_after_success(
    store: Store, config: FeedConfig
) -> None:
    failures = [FeedFetchError("temporarily unavailable") for _ in range(14)]
    fetcher = Fetcher(*failures, result(document(("issue-9", "Recovered"))))
    importer = NewsletterImporter(store, config, fetcher=fetcher)
    now = 0.0
    for number in range(14):
        outcome = importer.poll_once(now=now)
        assert outcome.status == "error"
        assert outcome.next_poll_at - now == min(60 * 2**number, MAX_BACKOFF_SECONDS)
        importer = NewsletterImporter(store, config, fetcher=fetcher)
        assert importer.poll_once(now=now + 1).status == "not_due"
        now = outcome.next_poll_at
    assert importer.poll_once(now=now).status == "saved_locally"
    saved = state(store, config)
    assert saved["failures"] == 0
    assert saved["last_error"] is None
    assert saved["next_poll_at"] == now + 60


def test_failed_batch_rolls_back_posts_receipts_and_validators(
    store: Store, config: FeedConfig
) -> None:
    fetcher = Fetcher(result(document(("one", "First"), ("two", "Second")), '"must-not-save"'))
    importer = NewsletterImporter(store, config, fetcher=fetcher)
    original = store.import_article

    def fail_second(
        source: str, item: str, board: str, title: str, body: str, *, published_at: str = ""
    ) -> Post:
        if item == "two":
            raise FeedError("Second import rejected")
        return original(source, item, board, title, body, published_at=published_at)

    with patch.object(store, "import_article", fail_second):
        outcome = importer.poll_once(now=100)
    assert outcome.status == "error"
    assert outcome.changed == 0
    assert store.list_posts("news") == []
    with store.transaction():
        assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    assert state(store, config)["etag"] is None
    assert state(store, config)["last_error"] == "Second import rejected"


def test_failed_correction_keeps_previous_revision_and_successful_validators(
    store: Store, config: FeedConfig
) -> None:
    fetcher = Fetcher(
        result(document(("one", "Original")), '"original"'),
        result(document(("one", "Correction"), ("two", "Rejected")), '"correction"'),
    )
    importer = NewsletterImporter(store, config, fetcher=fetcher)
    importer.poll_once(now=0)
    original_post = store.list_posts("news")[0]
    import_article = store.import_article

    def fail_second(
        source: str, item: str, board: str, title: str, body: str, *, published_at: str = ""
    ) -> Post:
        if item == "two":
            raise FeedError("Second article rejected")
        return import_article(source, item, board, title, body, published_at=published_at)

    with patch.object(store, "import_article", fail_second):
        assert importer.poll_once(now=60).status == "error"
    assert store.get_post(original_post.post_id) == original_post
    assert state(store, config)["etag"] == '"original"'


def test_oversized_article_is_reported_without_truncation(store: Store, config: FeedConfig) -> None:
    data = document(("one", "Small first article"), ("two", "x" * MAX_BODY_BYTES))
    outcome = NewsletterImporter(store, config, fetcher=Fetcher(result(data))).poll_once(now=0)
    assert outcome.status == "error"
    assert "64 KiB" in (outcome.error or "")
    assert store.list_posts("news") == []
    assert state(store, config)["etag"] is None


def test_size_limit_is_utf8_bytes(store: Store, config: FeedConfig) -> None:
    data = document(("one", "🌐" * (MAX_BODY_BYTES // 4)))
    outcome = NewsletterImporter(store, config, fetcher=Fetcher(result(data))).poll_once(now=0)
    assert outcome.status == "error"
    assert "64 KiB" in (outcome.error or "")


def test_source_binding_rejects_board_move_even_before_first_import(
    store: Store, config: FeedConfig
) -> None:
    NewsletterImporter(store, config)
    with pytest.raises(FeedError, match="different board"):
        NewsletterImporter(store, replace(config, board="general"))


def test_source_binding_checks_articles_imported_before_polling(
    store: Store, config: FeedConfig
) -> None:
    store.import_article(config.source_id, "old", "general", "Old", "Existing article")
    with pytest.raises(FeedError, match="different board"):
        NewsletterImporter(store, config)


def test_url_move_preserves_identity_but_resets_validators(
    store: Store, config: FeedConfig
) -> None:
    old = NewsletterImporter(store, config, fetcher=Fetcher(result(document(("one", "First")))))
    old.poll_once(now=100)
    old_id = store.list_posts("news")[0].post_id
    changed_config = replace(config, url="https://new.example/feed")
    fetcher = Fetcher(result(document(("one", "Corrected"))))
    new = NewsletterImporter(store, changed_config, fetcher=fetcher)
    assert old.poll_once(now=110, force=True).status == "error"
    assert new.poll_once(now=110).changed == 1
    assert fetcher.requests == [(changed_config.url, None, None)]
    assert store.list_posts("news")[0].post_id == old_id


def test_network_has_no_open_store_transaction_and_overlapping_poll_is_skipped(
    store: Store, config: FeedConfig
) -> None:
    calls = 0

    def fetch(url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        nonlocal calls
        calls += 1
        assert store.db.in_transaction is False
        second = NewsletterImporter(store, config, fetcher=fetch).poll_once(now=100, force=True)
        assert second.status == "in_progress"
        return result(document(("one", "Text")))

    outcome = NewsletterImporter(store, config, fetcher=fetch).poll_once(now=100)
    assert outcome.changed == 1
    assert calls == 1


def test_old_fetch_cannot_overwrite_newer_claim(store: Store, config: FeedConfig) -> None:
    def fetch(url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        newer = NewsletterImporter(
            store, config, fetcher=Fetcher(result(document(("one", "Newer text")), '"new"'))
        )
        assert newer.poll_once(now=301).changed == 1
        return result(document(("one", "Stale text")), '"old"')

    old = NewsletterImporter(store, config, fetcher=fetch).poll_once(now=0)
    assert old.status == "superseded"
    assert store.list_posts("news")[0].body.endswith("Newer text")
    assert state(store, config)["etag"] == '"new"'


def test_deleted_article_is_not_restored_by_poll(store: Store, config: FeedConfig) -> None:
    fetcher = Fetcher(
        result(document(("one", "Original"))), result(document(("one", "Correction")))
    )
    importer = NewsletterImporter(store, config, fetcher=fetcher)
    importer.poll_once(now=0)
    post = store.list_posts("news")[0]
    store.revise(post.author, "remove", post.post_id, post.title, "", remove=True)
    assert importer.poll_once(now=60).changed == 0
    assert store.get_post(post.post_id).deleted is True


def test_injected_clock_schedules_after_completion(store: Store, config: FeedConfig) -> None:
    ticks = iter([100.0, 112.0])
    importer = NewsletterImporter(
        store, config, fetcher=Fetcher(result(document(("one", "Text")))), clock=lambda: next(ticks)
    )
    assert importer.poll_once().next_poll_at == 172


def test_unexpected_fetch_error_is_contained_and_redacted(store: Store, config: FeedConfig) -> None:
    fetcher = Fetcher(RuntimeError("secret URL token"))
    outcome = NewsletterImporter(store, config, fetcher=fetcher).poll_once(now=0)
    assert outcome.status == "error"
    assert outcome.error == "Newsletter import failed (RuntimeError)"
    assert "secret" not in str(state(store, config)["last_error"])


def test_unconditional_304_is_not_accepted(store: Store, config: FeedConfig) -> None:
    outcome = NewsletterImporter(
        store, config, fetcher=Fetcher(FetchResult(304, None, None, None))
    ).poll_once(now=0)
    assert outcome.status == "error"
    assert "without a conditional request" in (outcome.error or "")


def test_one_failing_source_does_not_stop_another(store: Store, config: FeedConfig) -> None:
    first = NewsletterImporter(store, config, fetcher=Fetcher(FeedFetchError("Offline")))
    second_config = replace(config, source_id="other-newsletter")
    second = NewsletterImporter(
        store, second_config, fetcher=Fetcher(result(document(("one", "Other article"))))
    )
    outcomes = [first.poll_once(now=0), second.poll_once(now=0)]
    assert [outcome.status for outcome in outcomes] == ["error", "saved_locally"]
    expected = stable_id(store.region, "feed", second_config.source_id, "one")
    assert store.get_post(expected).body.endswith("Other article")


def test_newest_first_feed_keeps_latest_issue_first(store: Store, config: FeedConfig) -> None:
    data = document(
        ("september", "Latest issue"),
        ("august", "Older issue"),
        published={
            "september": "Mon, 21 Sep 2026 12:00:00 GMT",
            "august": "Fri, 21 Aug 2026 12:00:00 GMT",
        },
    )
    assert (
        NewsletterImporter(store, config, fetcher=Fetcher(result(data))).poll_once(now=0).changed
        == 2
    )
    posts = store.list_posts("news")
    assert [post.source_item for post in posts] == ["september", "august"]
    assert posts[0].created_at == "2026-09-21T12:00:00.000000+00:00"


def test_backfilled_old_issue_and_correction_do_not_replace_latest(
    store: Store, config: FeedConfig
) -> None:
    latest = document(
        ("september", "Latest issue"), published={"september": "2026-09-21T12:00:00Z"}
    )
    older = document(("august", "Older issue"), published={"august": "2026-08-21T12:00:00Z"})
    corrected = document(
        ("august", "Corrected older issue"), published={"august": "2026-08-21T12:00:00Z"}
    )
    importer = NewsletterImporter(
        store, config, fetcher=Fetcher(result(latest), result(older), result(corrected))
    )
    for now in (0, 60, 120):
        assert importer.poll_once(now=now).changed == 1
        assert store.list_posts("news", limit=1)[0].source_item == "september"
    old = store.list_posts("news")[1]
    assert old.created_at == "2026-08-21T12:00:00.000000+00:00"
    assert old.body.endswith("Corrected older issue")


def test_publication_timezones_are_normalized_for_ordering(
    store: Store, config: FeedConfig
) -> None:
    data = document(
        ("newest", "Newest, written with a smaller local hour"),
        ("older", "Older, written with a larger local hour"),
        published={
            "newest": "2026-09-22T06:30:00-01:00",
            "older": "2026-09-22T09:00:00+02:00",
        },
    )
    NewsletterImporter(store, config, fetcher=Fetcher(result(data))).poll_once(now=0)
    posts = store.list_posts("news")
    assert [post.source_item for post in posts] == ["newest", "older"]
    assert posts[0].created_at == "2026-09-22T07:30:00.000000+00:00"
    assert posts[1].created_at == "2026-09-22T07:00:00.000000+00:00"
    assert "Published: 2026-09-22T06:30:00-01:00" in posts[0].body


def test_undated_and_invalid_date_issues_stay_behind_dated_issues(
    store: Store, config: FeedConfig
) -> None:
    data = document(
        ("dated", "Published article"),
        ("undated", "No publication date"),
        ("invalid", "Invalid publication date"),
        published={"dated": "2026-09-21T12:00:00Z", "invalid": "not-a-date"},
    )
    outcome = NewsletterImporter(store, config, fetcher=Fetcher(result(data))).poll_once(now=0)
    assert outcome.changed == 3
    posts = store.list_posts("news")
    assert posts[0].source_item == "dated"
    assert all(post.created_at == "1970-01-01T00:00:00.000000+00:00" for post in posts[1:])
