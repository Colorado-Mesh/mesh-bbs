"""SQLite transactions, immutable events, and recoverable local sessions."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mesh_bbs.events import HEX_ID, MAX_BODY_BYTES, SLUG, BBSError, Event, canonical, stable_id


@dataclass(frozen=True)
class Grant:
    public_key: str
    boards: frozenset[str]
    can_moderate: bool = False


@dataclass(frozen=True)
class Post:
    post_id: str
    region: str
    board: str
    thread_id: str
    parent_id: str
    author: str
    title: str
    body: str
    created_at: str
    revision_id: str
    origin: str
    source_id: str
    source_item: str
    deleted: bool = False


def publication_time(value: str) -> str:
    """Undated feed entries sort before dated issues, independent of import order."""
    try:
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        return datetime(1970, 1, 1, tzinfo=UTC).isoformat(timespec="microseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, post_id TEXT NOT NULL, board TEXT NOT NULL,
    origin TEXT NOT NULL, clock INTEGER NOT NULL, moderator INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_post ON events(post_id);
CREATE INDEX IF NOT EXISTS events_board_id ON events(board, event_id);
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY, board TEXT NOT NULL, thread_id TEXT NOT NULL,
    parent_id TEXT NOT NULL, created_at TEXT NOT NULL, deleted INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_board ON posts(board, created_at, post_id);
CREATE INDEX IF NOT EXISTS posts_thread ON posts(thread_id, created_at, post_id);
CREATE TABLE IF NOT EXISTS receipts (
    actor TEXT NOT NULL, operation TEXT NOT NULL, fingerprint TEXT NOT NULL,
    result TEXT NOT NULL, PRIMARY KEY(actor, operation)
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY, actor TEXT NOT NULL, board TEXT NOT NULL,
    title TEXT NOT NULL, parent_id TEXT NOT NULL, published TEXT
);
CREATE TABLE IF NOT EXISTS draft_parts (
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id), number INTEGER NOT NULL,
    body TEXT NOT NULL, PRIMARY KEY(draft_id, number)
);
CREATE TABLE IF NOT EXISTS cursors (
    actor TEXT PRIMARY KEY, body TEXT NOT NULL, position INTEGER NOT NULL,
    revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_receipts (
    actor TEXT NOT NULL, operation TEXT NOT NULL, fingerprint TEXT NOT NULL,
    response TEXT NOT NULL, expires REAL, revision_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(actor,operation)
);
"""


class Store:
    def __init__(
        self,
        path: str | Path,
        region: str,
        boards: tuple[str, ...] = ("general", "news"),
        grants: Mapping[str, Grant] | None = None,
    ) -> None:
        if not SLUG.fullmatch(region) or not boards or any(not SLUG.fullmatch(b) for b in boards):
            raise BBSError("Use lowercase region and board slugs")
        if str(path) != ":memory:":
            file = Path(path)
            file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not stat.S_ISREG(file.lstat().st_mode):
                    raise BBSError("Database must be a regular file, not a symlink") from None
                fd = os.open(file, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        self.region, self.boards = region, boards
        self.grants = dict(grants or {})
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=10
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        if "revision_id" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(command_receipts)")
        }:
            self.db.execute(
                "ALTER TABLE command_receipts ADD COLUMN revision_id TEXT NOT NULL DEFAULT ''"
            )
        try:
            with self.transaction():
                old_region = self._meta("region")
                if old_region and old_region != region:
                    raise BBSError(
                        "This database belongs to another region; use a separate data directory"
                    )
                self._set_meta("region", region)
                private = self._meta("private_key")
                if private is None:
                    private = Ed25519PrivateKey.generate().private_bytes_raw().hex()
                    self._set_meta("private_key", private)
                self.key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private))
                self.public_key = self.key.public_key().public_bytes_raw().hex()
                self.origin = hashlib.sha256(bytes.fromhex(self.public_key)).hexdigest()
        except Exception:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            nested = self.db.in_transaction
            if not nested:
                self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                if not nested:
                    self.db.execute("COMMIT")
            except BaseException:
                if not nested:
                    self.db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))

    def receipt(self, actor: str, operation: str, fingerprint: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT fingerprint,result FROM receipts WHERE actor=? AND operation=?",
                (actor, operation),
            ).fetchone()
            if row and row["fingerprint"] != fingerprint:
                raise BBSError("Operation ID was already used with different content")
            return str(row["result"]) if row else None

    def save_receipt(self, actor: str, operation: str, fingerprint: str, result: str) -> None:
        self.db.execute(
            "INSERT INTO receipts VALUES (?,?,?,?)", (actor, operation, fingerprint, result)
        )

    def _sign(self, **values: Any) -> Event:
        clock = max(int(self._meta("clock") or 0) + 1, time.time_ns() // 1_000_000)
        created_at = values.pop("created_at", datetime.now(UTC).isoformat(timespec="microseconds"))
        event = Event(
            version=1,
            region=self.region,
            origin=self.origin,
            public_key=self.public_key,
            clock=clock,
            created_at=created_at,
            **values,
        )
        data = canonical(event.unsigned())
        return replace(
            event, event_id=hashlib.sha256(data).hexdigest(), signature=self.key.sign(data).hex()
        )

    def _accept(self, event: Event) -> bool:
        event.validate(self.region)
        if event.board not in self.boards:
            raise BBSError("Board is not configured on this host")
        moderator = event.origin == self.origin
        if event.origin != self.origin:
            grant = self.grants.get(event.origin)
            if not grant or grant.public_key != event.public_key or event.board not in grant.boards:
                raise BBSError("Origin is not trusted to write this board")
            moderator = grant.can_moderate
        changed = self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.post_id,
                event.board,
                event.origin,
                event.clock,
                int(moderator),
                canonical(event.to_dict()).decode(),
            ),
        ).rowcount
        if changed:
            self._set_meta("clock", str(max(event.clock, int(self._meta("clock") or 0))))
            self._project(event.post_id)
        return bool(changed)

    def accept(self, event: Event) -> bool:
        with self.transaction():
            return self._accept(event)

    def _project(self, post_id: str) -> None:
        rows = self.db.execute(
            "SELECT payload,moderator FROM events WHERE post_id=?", (post_id,)
        ).fetchall()
        candidates = [(Event.from_dict(json.loads(row[0])), bool(row[1])) for row in rows]
        creates = [e for e, _ in candidates if e.kind == "create"]
        if not creates:
            return  # Retain a revision or removal until its original post arrives.
        original = min(creates, key=lambda e: e.event_id)
        eligible: list[Event] = []
        removed = False
        for event, moderator in candidates:
            same_parent = (event.board, event.thread_id, event.parent_id) == (
                original.board,
                original.thread_id,
                original.parent_id,
            )
            owner = event.origin == original.origin and event.author == original.author
            feed_editor = bool(original.source_id) and (
                event.source_id,
                event.source_item,
                event.author,
            ) == (original.source_id, original.source_item, original.author)
            if not same_parent or not (
                owner or feed_editor or (moderator and event.kind == "remove")
            ):
                continue
            if event.kind == "remove":
                removed = True
            else:
                eligible.append(event)
        current = max(eligible, key=lambda e: (e.clock, e.event_id))
        post = Post(
            post_id,
            original.region,
            original.board,
            original.thread_id,
            original.parent_id,
            original.author,
            current.title,
            "" if removed else current.body,
            original.created_at,
            current.event_id,
            original.origin,
            original.source_id,
            original.source_item,
            removed,
        )
        self.db.execute(
            "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?)",
            (
                post.post_id,
                post.board,
                post.thread_id,
                post.parent_id,
                post.created_at,
                int(post.deleted),
                canonical(asdict(post)).decode(),
            ),
        )
        if removed:
            self.db.execute(
                "DELETE FROM cursors WHERE revision_id IN "
                "(SELECT event_id FROM events WHERE post_id=?)",
                (post_id,),
            )
            self.db.execute(
                "DELETE FROM command_receipts WHERE revision_id IN "
                "(SELECT event_id FROM events WHERE post_id=?)",
                (post_id,),
            )

    def get_post(self, post_id: str) -> Post:
        with self._lock:
            resolved = self.resolve_id(post_id)
            row = self.db.execute(
                "SELECT payload FROM posts WHERE post_id=?", (resolved,)
            ).fetchone()
            if not row:
                raise BBSError("Post is not available on this host yet")
            return Post(**json.loads(row[0]))

    def resolve_id(self, prefix: str) -> str:
        if len(prefix) < 8 or len(prefix) > 64 or any(c not in "0123456789abcdef" for c in prefix):
            raise BBSError("Use a post ID of at least eight hexadecimal characters")
        if HEX_ID.fullmatch(prefix):
            return prefix
        rows = self.db.execute(
            "SELECT post_id FROM posts WHERE post_id LIKE ? LIMIT 2", (prefix + "%",)
        ).fetchall()
        if len(rows) != 1:
            raise BBSError("Unknown or ambiguous post ID; use more characters")
        return str(rows[0][0])

    def list_posts(
        self, board: str, *, thread_id: str | None = None, limit: int = 50, after_id: str = ""
    ) -> list[Post]:
        with self._lock:
            if not 1 <= limit <= 200:
                raise BBSError("List limit must be between 1 and 200")
            anchor = self.get_post(after_id) if after_id else None
            if anchor and (
                anchor.board != board
                or (thread_id and anchor.thread_id != self.resolve_id(thread_id))
            ):
                raise BBSError("Listing cursor belongs to another board or thread")
            condition = ""
            params: tuple[object, ...] = ()
            if anchor:
                operator = ">" if thread_id else "<"
                condition = f" AND (created_at, post_id) {operator} (?, ?)"
                params = (anchor.created_at, anchor.post_id)
            if thread_id:
                rows = self.db.execute(
                    "SELECT payload FROM posts WHERE board=? AND thread_id=? "
                    + condition
                    + " ORDER BY created_at,post_id LIMIT ?",
                    (board, self.resolve_id(thread_id), *params, limit),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT payload FROM posts WHERE board=? AND parent_id='' AND deleted=0 "
                    + condition
                    + " ORDER BY created_at DESC,post_id DESC LIMIT ?",
                    (board, *params, limit),
                ).fetchall()
            return [Post(**json.loads(r[0])) for r in rows]

    def publish(
        self,
        actor: str,
        operation: str,
        board: str,
        title: str,
        body: str,
        *,
        parent_id: str = "",
        source_id: str = "",
        source_item: str = "",
        published_at: str = "",
    ) -> Post:
        if not operation or len(operation) > 256:
            raise BBSError("A bounded operation ID is required")
        fingerprint = stable_id(board, title, body, parent_id, source_id, source_item)
        with self.transaction():
            previous = self.receipt(actor, "publish:" + operation, fingerprint)
            if previous:
                return self.get_post(previous)
            post_id = (
                stable_id(self.region, "feed", source_id, source_item)
                if source_id
                else stable_id(self.region, self.origin, actor, operation)
            )
            thread_id = post_id
            if parent_id:
                parent = self.get_post(parent_id)
                if parent.board != board:
                    raise BBSError("Reply belongs to a different board")
                parent_id, thread_id = parent.post_id, parent.thread_id
            if not body.strip():
                raise BBSError("A post needs text")
            event = self._sign(
                kind="create",
                post_id=post_id,
                board=board,
                thread_id=thread_id,
                parent_id=parent_id,
                author=actor,
                title=title,
                body=body,
                source_id=source_id,
                source_item=source_item,
                post_key=operation,
                created_at=(
                    publication_time(published_at)
                    if source_id
                    else datetime.now(UTC).isoformat(timespec="microseconds")
                ),
            )
            self._accept(event)
            self.save_receipt(actor, "publish:" + operation, fingerprint, post_id)
            return self.get_post(post_id)

    def revise(
        self,
        actor: str,
        operation: str,
        post_id: str,
        title: str,
        body: str,
        *,
        remove: bool = False,
    ) -> Post:
        if not operation or len(operation) > 256:
            raise BBSError("A bounded operation ID is required")
        with self.transaction():
            post = self.get_post(post_id)
            if actor != post.author or (post.origin != self.origin and not post.source_id):
                raise BBSError("Only the originating author can revise this post")
            fingerprint = stable_id(post.post_id, title, body, str(remove))
            old = self.receipt(actor, "revise:" + operation, fingerprint)
            if old:
                return self.get_post(old)
            if post.deleted:
                raise BBSError("Removed posts cannot be restored by a revision")
            event = self._sign(
                kind="remove" if remove else "revise",
                post_id=post.post_id,
                board=post.board,
                thread_id=post.thread_id,
                parent_id=post.parent_id,
                author=actor,
                title=title,
                body="" if remove else body,
                source_id=post.source_id,
                source_item=post.source_item,
            )
            self._accept(event)
            self.save_receipt(actor, "revise:" + operation, fingerprint, post.post_id)
            return self.get_post(post.post_id)

    def import_article(
        self,
        source_id: str,
        item_id: str,
        board: str,
        title: str,
        body: str,
        *,
        published_at: str = "",
    ) -> Post:
        post_id = stable_id(self.region, "feed", source_id, item_id)
        with self.transaction():
            row = self.db.execute(
                "SELECT payload FROM posts WHERE post_id=?", (post_id,)
            ).fetchone()
            actor = "feed:" + source_id
            if row:
                old = Post(**json.loads(row[0]))
                if old.deleted or (old.title, old.body) == (title, body):
                    return old
                # A returning correction must be a new revision even if this text appeared before.
                return self.revise(
                    actor, f"{item_id[:100]}:{secrets.token_hex(16)}", post_id, title, body
                )
            return self.publish(
                actor,
                stable_id(source_id, item_id),
                board,
                title,
                body,
                source_id=source_id,
                source_item=item_id,
                published_at=published_at,
            )

    def remove(self, operation: str, post_id: str) -> Post:
        """Record an operator removal; other hosts need an explicit moderator grant."""
        if not operation or len(operation) > 256:
            raise BBSError("A bounded operation ID is required")
        with self.transaction():
            post = self.get_post(post_id)
            fingerprint = stable_id(post.post_id, "remove")
            previous = self.receipt("local:operator", "remove:" + operation, fingerprint)
            if previous:
                return self.get_post(previous)
            event = self._sign(
                kind="remove",
                post_id=post.post_id,
                board=post.board,
                thread_id=post.thread_id,
                parent_id=post.parent_id,
                author=post.author if post.origin == self.origin else "local:operator",
                title=post.title,
                body="",
            )
            self._accept(event)
            self.save_receipt("local:operator", "remove:" + operation, fingerprint, post.post_id)
            return self.get_post(post.post_id)

    def new_draft(self, actor: str, board: str, title: str, parent_id: str = "") -> str:
        if board not in self.boards or not title or len(title.encode()) > 256:
            raise BBSError("Choose a configured board and a title up to 256 bytes")
        with self.transaction():
            count = self.db.execute(
                "SELECT count(*) FROM drafts WHERE actor=? AND published IS NULL", (actor,)
            ).fetchone()[0]
            if count >= 10:
                raise BBSError("Finish or discard a draft before creating another (limit 10)")
            draft_id = secrets.token_hex(4)
            self.db.execute(
                "INSERT INTO drafts VALUES (?,?,?,?,?,NULL)",
                (draft_id, actor, board, title, parent_id),
            )
            return draft_id

    def draft(self, actor: str, draft_id: str) -> tuple[sqlite3.Row, str]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM drafts WHERE actor=? AND draft_id=?", (actor, draft_id)
            ).fetchone()
            if not row:
                raise BBSError("Draft not found for this sender")
            parts = self.db.execute(
                "SELECT number,body FROM draft_parts WHERE draft_id=? ORDER BY number", (draft_id,)
            ).fetchall()
            if parts and [r[0] for r in parts] != list(range(1, len(parts) + 1)):
                raise BBSError("Draft has missing parts; number them consecutively starting at 1")
            return row, "\n".join(r[1] for r in parts)

    def add_part(self, actor: str, draft_id: str, number: int, body: str) -> None:
        if not 1 <= number <= 1024 or not body or len(body.encode()) > MAX_BODY_BYTES:
            raise BBSError("Invalid draft part")
        with self.transaction():
            draft = self.db.execute(
                "SELECT published FROM drafts WHERE actor=? AND draft_id=?", (actor, draft_id)
            ).fetchone()
            if not draft or draft[0]:
                raise BBSError("No editable draft for this sender")
            old = self.db.execute(
                "SELECT body FROM draft_parts WHERE draft_id=? AND number=?", (draft_id, number)
            ).fetchone()
            if old:
                if old[0] != body:
                    raise BBSError("Part already exists with different text; use a new draft")
                return
            size = self.db.execute(
                "SELECT COALESCE(sum(length(CAST(body AS BLOB))+1),0) "
                "FROM draft_parts WHERE draft_id=?",
                (draft_id,),
            ).fetchone()[0]
            if size + len(body.encode()) > MAX_BODY_BYTES:
                raise BBSError("Draft exceeds 64 KiB")
            self.db.execute("INSERT INTO draft_parts VALUES (?,?,?)", (draft_id, number, body))

    def publish_draft(self, actor: str, draft_id: str) -> Post:
        with self.transaction():
            row, body = self.draft(actor, draft_id)
            if row["published"]:
                return self.get_post(row["published"])
            post = self.publish(
                actor,
                "draft:" + draft_id,
                row["board"],
                row["title"],
                body,
                parent_id=row["parent_id"],
            )
            self.db.execute(
                "UPDATE drafts SET published=? WHERE draft_id=?", (post.post_id, draft_id)
            )
            return post

    def inventory(self, boards: frozenset[str], after: str = "", limit: int = 128) -> list[str]:
        if not boards or not 1 <= limit <= 128:
            return []
        with self._lock:
            placeholders = ",".join("?" for _ in boards)
            return [
                r[0]
                for r in self.db.execute(
                    f"SELECT event_id FROM events WHERE board IN ({placeholders}) "
                    "AND event_id>? ORDER BY event_id LIMIT ?",
                    (*sorted(boards), after, limit),
                ).fetchall()
            ]

    def export(self, ids: list[str], boards: frozenset[str]) -> list[dict[str, Any]]:
        if len(ids) > 32:
            raise BBSError("Request at most 32 events at a time")
        with self._lock:
            result, size = [], 0
            for event_id in ids:
                row = self.db.execute(
                    "SELECT board,payload FROM events WHERE event_id=?", (event_id,)
                ).fetchone()
                if row and row["board"] in boards:
                    size += len(row["payload"].encode())
                    if size > 512 * 1024:
                        break
                    result.append(json.loads(row["payload"]))
            return result

    def missing(self, ids: list[str]) -> list[str]:
        if len(ids) > 128 or any(not isinstance(i, str) or not HEX_ID.fullmatch(i) for i in ids):
            raise BBSError("Invalid inventory")
        with self._lock:
            return [
                i
                for i in ids
                if not self.db.execute("SELECT 1 FROM events WHERE event_id=?", (i,)).fetchone()
            ]

    def backup(self, destination: Path) -> None:
        try:
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise BBSError("Backup destination already exists") from None
        os.close(fd)
        with self._lock, sqlite3.connect(destination) as other:
            self.db.backup(other)
