"""Durable, opt-in channel notices from one designated host per radio mesh."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.commands import byte_prefix, display_text
from mesh_bbs.events import BBSError
from mesh_bbs.store import Store, publication_time

LOG = logging.getLogger(__name__)
QUEUE_LIMIT = 32
MAX_AGE = 86400


def _label(text: str, max_bytes: int) -> str:
    text = " ".join(display_text(text).split())
    if len(text.encode()) <= max_bytes:
        return text
    return byte_prefix(text, max_bytes - 3) + "..."


class AnnouncementOutbox:
    def __init__(self, store: Store, protocol: str, *, interval: int = 600) -> None:
        if protocol not in {"meshcore", "meshtastic"} or not 300 <= interval <= 86400:
            raise ValueError("Invalid announcement protocol or interval")
        self.store, self.protocol, self.interval = store, protocol, interval
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS announcement_state ("
                "protocol TEXT PRIMARY KEY, cursor INTEGER NOT NULL, last_attempt REAL NOT NULL)"
            )
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS announcement_seen ("
                "protocol TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(protocol,key))"
            )
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS announcement_queue ("
                "protocol TEXT NOT NULL, key TEXT NOT NULL, board TEXT NOT NULL, "
                "created REAL NOT NULL, PRIMARY KEY(protocol,key))"
            )
            state = store.db.execute(
                "SELECT 1 FROM announcement_state WHERE protocol=?", (protocol,)
            ).fetchone()
            if state is None:
                # Enabling notices or promoting a new host never replays its history.
                cursor = store.db.execute("SELECT coalesce(max(rowid),0) FROM events").fetchone()[0]
                store.db.execute(
                    "INSERT INTO announcement_state VALUES (?,?,0)", (protocol, cursor)
                )
                store.db.execute(
                    "INSERT OR IGNORE INTO announcement_seen SELECT ?,post_id FROM posts",
                    (protocol,),
                )
                store.db.executemany(
                    "INSERT OR IGNORE INTO announcement_seen VALUES (?,?)",
                    [(protocol, "board:" + board) for board in store.boards],
                )

    def _enqueue(self, key: str, board: str, now: float) -> None:
        changed = self.store.db.execute(
            "INSERT OR IGNORE INTO announcement_seen VALUES (?,?)", (self.protocol, key)
        ).rowcount
        if changed:
            self.store.db.execute(
                "INSERT INTO announcement_queue VALUES (?,?,?,?)",
                (self.protocol, key, board, now),
            )

    def poll(self, now: float) -> bool:
        with self.store.transaction():
            for board in self.store.boards:
                self._enqueue("board:" + board, board, now)
            cursor = self.store.db.execute(
                "SELECT cursor FROM announcement_state WHERE protocol=?", (self.protocol,)
            ).fetchone()[0]
            rows = self.store.db.execute(
                "SELECT rowid,payload FROM events WHERE rowid>? ORDER BY rowid LIMIT 1000",
                (cursor,),
            ).fetchall()
            for row in rows:
                event = json.loads(row["payload"])
                if event["kind"] != "create" or event["parent_id"]:
                    continue
                post = self.store.get_post(event["post_id"])
                # Do not advertise old archives arriving in a first federation/feed sync.
                if (
                    not post.deleted
                    and post.board in self.store.boards
                    and now - MAX_AGE
                    <= datetime.fromisoformat(publication_time(post.created_at)).timestamp()
                    <= now + 300
                ):
                    self._enqueue(post.post_id, post.board, now)
            if rows:
                self.store.db.execute(
                    "UPDATE announcement_state SET cursor=? WHERE protocol=?",
                    (rows[-1]["rowid"], self.protocol),
                )
            self.store.db.execute(
                "DELETE FROM announcement_queue WHERE protocol=? AND created<?",
                (self.protocol, now - MAX_AGE),
            )
            dropped = self.store.db.execute(
                "DELETE FROM announcement_queue WHERE protocol=? AND rowid NOT IN "
                "(SELECT rowid FROM announcement_queue WHERE protocol=? "
                "ORDER BY rowid DESC LIMIT ?)",
                (self.protocol, self.protocol, QUEUE_LIMIT),
            ).rowcount
            if dropped:
                LOG.warning(
                    "%s announcement backlog capped; skipped %s notices", self.protocol, dropped
                )
            return bool(
                self.store.db.execute(
                    "SELECT 1 FROM announcement_queue WHERE protocol=? LIMIT 1", (self.protocol,)
                ).fetchone()
            )

    def take(self, now: float, max_bytes: int) -> str | None:
        """Consume before transmission: an uncertain send is never retried as a new notice."""
        if not 120 <= max_bytes <= 233:
            raise ValueError("Announcement packet must allow 120-233 bytes")
        with self.store.transaction():
            last = self.store.db.execute(
                "SELECT last_attempt FROM announcement_state WHERE protocol=?", (self.protocol,)
            ).fetchone()[0]
            if last and now < last + self.interval:
                return None
            rows = self.store.db.execute(
                "SELECT key,board,created FROM announcement_queue WHERE protocol=? ORDER BY rowid",
                (self.protocol,),
            ).fetchall()
            for row in rows:
                key, board = row["key"], row["board"]
                self.store.db.execute(
                    "DELETE FROM announcement_queue WHERE protocol=? AND key=?",
                    (self.protocol, key),
                )
                if board not in self.store.boards or row["created"] < now - MAX_AGE:
                    continue
                if key.startswith("board:"):
                    prefix = "New board: "
                    suffix = "\nTo browse it, send me a private message: boards"
                    text = (
                        prefix + _label(board, max_bytes - len((prefix + suffix).encode())) + suffix
                    )
                else:
                    try:
                        post = self.store.get_post(key)
                    except BBSError:
                        continue
                    if post.deleted:
                        continue
                    number = self.store.post_number(post.post_id)
                    suffix = f'"\nTo read it, send me a private message: read #{number}'
                    board_limit = max_bytes - len(('New post in :\n"' + suffix).encode()) - 16
                    prefix = f'New post in {_label(board, board_limit)}:\n"'
                    text = (
                        prefix
                        + _label(post.title, max_bytes - len((prefix + suffix).encode()))
                        + suffix
                    )
                self.store.db.execute(
                    "UPDATE announcement_state SET last_attempt=? WHERE protocol=?",
                    (now, self.protocol),
                )
                return text
            return None

    def due(self, now: float) -> bool:
        with self.store.transaction():
            last = self.store.db.execute(
                "SELECT last_attempt FROM announcement_state WHERE protocol=?", (self.protocol,)
            ).fetchone()[0]
            return not last or now >= last + self.interval

    async def run(
        self,
        limiter: AirtimeLimiter,
        prepare: Callable[[], Awaitable[int]],
        send: Callable[[str], Awaitable[None]],
        attempts: int = 1,
    ) -> None:
        while True:
            try:
                now = time.time()
                if self.poll(now) and self.due(now):
                    max_bytes = await prepare()  # Verify the channel before consuming a notice.
                    reservation = await limiter.acquire(attempts)
                    try:
                        text = self.take(time.time(), max_bytes)
                        if text is not None:
                            await send(text)
                            LOG.info("%s channel notice accepted by companion", self.protocol)
                    finally:
                        limiter.finish(reservation)
            except Exception:
                LOG.exception(
                    "%s channel announcement failed; no uncertain send retry", self.protocol
                )
                await asyncio.sleep(60)
            await asyncio.sleep(10)
