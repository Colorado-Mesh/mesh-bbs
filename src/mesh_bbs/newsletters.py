"""Scheduled newsletter imports with durable progress and atomic local saves."""

from __future__ import annotations

import math
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from mesh_bbs.config import FeedConfig
from mesh_bbs.events import MAX_BODY_BYTES, BBSError, stable_id
from mesh_bbs.feeds import FeedError, FeedItem, FetchResult, fetch_feed, parse_feed
from mesh_bbs.store import Store

MAX_BACKOFF_SECONDS = 86400
CLAIM_SECONDS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletter_sources (
    source_id TEXT PRIMARY KEY,
    board TEXT NOT NULL,
    url TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    next_poll_at REAL NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    claim_token TEXT,
    claim_until REAL NOT NULL DEFAULT 0
)
"""

PollStatus = Literal[
    "not_due", "in_progress", "saved_locally", "not_modified", "error", "superseded"
]


@dataclass(frozen=True)
class PollOutcome:
    changed: int
    status: PollStatus
    next_poll_at: float
    error: str | None = None


class FeedFetcher(Protocol):
    def __call__(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult: ...


@dataclass(frozen=True)
class _Claim:
    token: str
    etag: str | None
    last_modified: str | None


class NewsletterImporter:
    """Poll one source; successful results mean saved locally, not replicated.

    The source ID remains bound to its board. Changing its URL is supported and
    clears old HTTP validators. Removing a source from configuration does not
    remove its articles or make the ID available for a different board.
    """

    def __init__(
        self,
        store: Store,
        feed: FeedConfig,
        *,
        fetcher: FeedFetcher = fetch_feed,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store, self.feed, self.fetcher, self.clock = store, feed, fetcher, clock
        if feed.board not in store.boards:
            raise FeedError("Newsletter board is not configured on this host")
        with store.transaction():
            store.db.execute(_SCHEMA)
            source = store.db.execute(
                "SELECT board,url FROM newsletter_sources WHERE source_id=?", (feed.source_id,)
            ).fetchone()
            # Also respect imports made before a poll schedule was created.
            wrong_board = store.db.execute(
                "SELECT 1 FROM posts WHERE json_extract(payload,'$.source_id')=? "
                "AND board<>? LIMIT 1",
                (feed.source_id, feed.board),
            ).fetchone()
            if wrong_board or (source and source["board"] != feed.board):
                raise FeedError("Newsletter source_id is already bound to a different board")
            if source is None:
                store.db.execute(
                    "INSERT INTO newsletter_sources(source_id,board,url) VALUES (?,?,?)",
                    (feed.source_id, feed.board, feed.url),
                )
            elif source["url"] != feed.url:
                store.db.execute(
                    "UPDATE newsletter_sources SET url=?,etag=NULL,last_modified=NULL,"
                    "next_poll_at=0,failures=0,last_error=NULL,claim_token=NULL,claim_until=0 "
                    "WHERE source_id=?",
                    (feed.url, feed.source_id),
                )

    def _claim(self, now: float, force: bool) -> _Claim | PollOutcome:
        with self.store.transaction():
            source = self.store.db.execute(
                "SELECT * FROM newsletter_sources WHERE source_id=?", (self.feed.source_id,)
            ).fetchone()
            if source["board"] != self.feed.board or source["url"] != self.feed.url:
                raise FeedError("Newsletter configuration changed; recreate this importer")
            if source["claim_token"] and source["claim_until"] > now:
                return PollOutcome(0, "in_progress", float(source["claim_until"]))
            if not force and source["next_poll_at"] > now:
                return PollOutcome(0, "not_due", float(source["next_poll_at"]))
            claim = _Claim(secrets.token_hex(16), source["etag"], source["last_modified"])
            self.store.db.execute(
                "UPDATE newsletter_sources SET claim_token=?,claim_until=? WHERE source_id=?",
                (claim.token, now + CLAIM_SECONDS, self.feed.source_id),
            )
            return claim

    def _body(self, item: FeedItem) -> str:
        if len(item.item_id) > 2048:
            raise FeedError("Newsletter item identity exceeds the 2048-character limit")
        if len(item.title.encode("utf-8")) > 256:
            raise FeedError("Newsletter title exceeds the 256-byte limit")
        attribution = [f"Source: {self.feed.source_id}"]
        if item.link:
            attribution.append(f"Original: {item.link}")
        if item.published:
            attribution.append(f"Published: {item.published}")
        # Never include the configured feed URL: it may contain a subscription
        # token. Article links come from the publisher's entry itself.
        parts = ["\n".join(attribution)]
        if item.summary_only:
            parts.append("Summary only: the feed did not provide confirmed full article text.")
        parts.append(item.body or "No article text was included in the feed.")
        body = "\n\n".join(parts)
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise FeedError("Newsletter article including attribution exceeds the 64 KiB limit")
        return body

    def _finish(
        self,
        claim: _Claim,
        response: FetchResult,
        articles: tuple[tuple[FeedItem, str], ...],
        now: float,
    ) -> PollOutcome:
        with self.store.transaction():
            source = self.store.db.execute(
                "SELECT * FROM newsletter_sources WHERE source_id=?", (self.feed.source_id,)
            ).fetchone()
            if source["claim_token"] != claim.token:
                return PollOutcome(0, "superseded", float(source["next_poll_at"]))
            changed = 0
            for item, body in articles:
                post_id = stable_id(self.store.region, "feed", self.feed.source_id, item.item_id)
                before = self.store.db.execute(
                    "SELECT board,json_extract(payload,'$.revision_id') AS revision "
                    "FROM posts WHERE post_id=?",
                    (post_id,),
                ).fetchone()
                if before and before["board"] != self.feed.board:
                    raise FeedError("Newsletter source item already belongs to another board")
                post = self.store.import_article(
                    self.feed.source_id,
                    item.item_id,
                    self.feed.board,
                    item.title,
                    body,
                    published_at=item.published,
                )
                if before is None or before["revision"] != post.revision_id:
                    changed += 1
            etag, modified = response.etag, response.last_modified
            if response.status_code == 304:
                etag = etag if etag is not None else claim.etag
                modified = modified if modified is not None else claim.last_modified
            next_poll_at = now + self.feed.poll_seconds
            # This update shares the outer transaction with every imported
            # event and deduplication receipt. A failed import rolls all back.
            self.store.db.execute(
                "UPDATE newsletter_sources SET etag=?,last_modified=?,next_poll_at=?,"
                "failures=0,last_error=NULL,claim_token=NULL,claim_until=0 WHERE source_id=?",
                (etag, modified, next_poll_at, self.feed.source_id),
            )
            status: PollStatus = "not_modified" if response.status_code == 304 else "saved_locally"
            return PollOutcome(changed, status, next_poll_at)

    def _failed(self, claim: _Claim, error: Exception, now: float) -> PollOutcome:
        message = (
            str(error)[:512]
            if isinstance(error, (FeedError, BBSError))
            else f"Newsletter import failed ({type(error).__name__})"
        )
        with self.store.transaction():
            source = self.store.db.execute(
                "SELECT * FROM newsletter_sources WHERE source_id=?", (self.feed.source_id,)
            ).fetchone()
            if source["claim_token"] != claim.token:
                return PollOutcome(0, "superseded", float(source["next_poll_at"]))
            failures = min(int(source["failures"]) + 1, 32)
            delay = min(self.feed.poll_seconds * 2 ** (failures - 1), MAX_BACKOFF_SECONDS)
            next_poll_at = now + delay
            self.store.db.execute(
                "UPDATE newsletter_sources SET failures=?,last_error=?,next_poll_at=?,"
                "claim_token=NULL,claim_until=0 WHERE source_id=?",
                (failures, message, next_poll_at, self.feed.source_id),
            )
            return PollOutcome(0, "error", next_poll_at, message)

    def poll_once(self, *, now: float | None = None, force: bool = False) -> PollOutcome:
        """Import when due, retaining validators and backoff across restarts.

        ``force`` bypasses the schedule, but does not steal another live poll's
        claim. Each fetch happens with no Store transaction or lock held.
        """
        started_at = self.clock() if now is None else now
        if not math.isfinite(started_at) or started_at < 0:
            raise ValueError("Newsletter poll time must be finite and nonnegative")
        try:
            claim = self._claim(started_at, force)
        except (FeedError, sqlite3.Error) as error:
            message = (
                str(error) if isinstance(error, FeedError) else "Newsletter schedule unavailable"
            )
            return PollOutcome(0, "error", started_at + self.feed.poll_seconds, message)
        if isinstance(claim, PollOutcome):
            return claim
        try:
            response = self.fetcher(self.feed.url, claim.etag, claim.last_modified)
            if response.status_code == 304:
                if claim.etag is None and claim.last_modified is None:
                    raise FeedError("Feed server returned 304 without a conditional request")
                articles: tuple[tuple[FeedItem, str], ...] = ()
            elif response.status_code == 200 and response.content is not None:
                parsed = parse_feed(response.content)
                articles = tuple((item, self._body(item)) for item in parsed.items)
            else:
                raise FeedError("Feed did not return a complete 200 response or a valid 304")
            finished_at = self.clock() if now is None else now
            return self._finish(claim, response, articles, finished_at)
        except Exception as error:
            failed_at = self.clock() if now is None else now
            try:
                return self._failed(claim, error, failed_at)
            except sqlite3.Error:
                return PollOutcome(
                    0,
                    "error",
                    failed_at + self.feed.poll_seconds,
                    "Newsletter import failed and retry state could not be saved",
                )
