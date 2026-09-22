"""Operator-issued web identities and transactional publication permissions."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mesh_bbs.config import validate_slug
from mesh_bbs.events import HEX_ID, MAX_BODY_BYTES, BBSError
from mesh_bbs.store import Post, Store

WRITE_LIMIT = 30
WRITE_WINDOW = 60.0
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_FIELDS = {"board", "title", "body", "parent_id", "operation"}


class AccessDenied(BBSError):
    def __init__(
        self, message: str = "Invalid or revoked access key", *, status: int = 401
    ) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WebUser:
    actor: str
    editor: bool
    _token_hash: str = field(repr=False)


class WebAccess:
    def __init__(
        self,
        store: Store,
        editors: tuple[str, ...] = (),
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store, self.editors, self.clock = store, frozenset(editors), clock
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS web_users ("
                "name TEXT PRIMARY KEY, actor TEXT UNIQUE NOT NULL, "
                "token_hash TEXT UNIQUE NOT NULL, editor INTEGER NOT NULL, "
                "revoked INTEGER NOT NULL DEFAULT 0, writes TEXT NOT NULL DEFAULT '[]')"
            )

    def create(self, name: str, editor: bool = False) -> str:
        """Return a key once. Revoked names remain reserved to their original owner."""
        validate_slug(name, "contributor name")
        if type(editor) is not bool:
            raise BBSError("Editor permission must be true or false")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM web_users WHERE name=?", (name,)).fetchone():
                raise BBSError("Contributor name already exists; names cannot be reassigned")
            self.store.db.execute(
                "INSERT INTO web_users(name,actor,token_hash,editor) VALUES (?,?,?,?)",
                (name, "web:" + name, digest, int(editor)),
            )
        return token

    def revoke(self, name: str) -> None:
        validate_slug(name, "contributor name")
        with self.store.transaction():
            changed = self.store.db.execute(
                "UPDATE web_users SET revoked=1 WHERE name=?", (name,)
            ).rowcount
            if not changed:
                raise BBSError("Contributor not found")

    def list_users(self) -> list[dict[str, Any]]:
        with self.store.transaction():
            rows = self.store.db.execute(
                "SELECT name,actor,editor,revoked FROM web_users ORDER BY name"
            ).fetchall()
            return [
                {
                    "name": row["name"],
                    "actor": row["actor"],
                    "editor": bool(row["editor"]) or row["actor"] in self.editors,
                    "revoked": bool(row["revoked"]),
                }
                for row in rows
            ]

    def _user(self, digest: str, actor: str | None = None) -> WebUser:
        row = self.store.db.execute(
            "SELECT actor,editor FROM web_users WHERE token_hash=? AND revoked=0", (digest,)
        ).fetchone()
        if row is None or (actor is not None and row["actor"] != actor):
            raise AccessDenied()
        return WebUser(row["actor"], bool(row["editor"]) or row["actor"] in self.editors, digest)

    def authenticate(self, token: str) -> WebUser:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise AccessDenied()
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.store.transaction():
            return self._user(digest)

    def publish(self, user: WebUser, payload: dict[str, Any]) -> Post:
        if not isinstance(payload, dict) or set(payload) != _FIELDS:
            raise BBSError("Expected board, title, body, parent_id, and operation")
        if any(not isinstance(value, str) for value in payload.values()):
            raise BBSError("Post fields must be text")
        board, title, body = payload["board"], payload["title"], payload["body"]
        parent_id, operation = payload["parent_id"], payload["operation"]
        if not _OPERATION.fullmatch(operation):
            raise BBSError("Operation must be 1-64 letters, numbers, underscores, or hyphens")
        if parent_id and not HEX_ID.fullmatch(parent_id):
            raise BBSError("Replies require the full parent post ID")
        try:
            if len(title.encode("utf-8")) > 256 or len(body.encode("utf-8")) > MAX_BODY_BYTES:
                raise BBSError("Title exceeds 256 bytes or post exceeds 64 KiB")
        except UnicodeEncodeError as error:
            raise BBSError("Post must contain valid Unicode text") from error
        if not body.strip() or (not parent_id and not title.strip()):
            raise BBSError("A new thread needs a title, and every post needs text")
        if board not in self.store.boards:
            raise BBSError("Unknown board")
        with self.store.transaction():
            current = self._user(user._token_hash, user.actor)
            if not parent_id and board == "news" and not current.editor:
                raise AccessDenied("Newsletter issues require an editor", status=403)
            operation = "web:" + operation
            previous = self.store.db.execute(
                "SELECT 1 FROM receipts WHERE actor=? AND operation=?",
                (current.actor, "publish:" + operation),
            ).fetchone()
            if previous:
                return self.store.publish(
                    current.actor, operation, board, title, body, parent_id=parent_id
                )
            if parent_id:
                parent = self.store.get_post(parent_id)
                if parent.board != board:
                    raise BBSError("Reply belongs to a different board")
                if parent.deleted:
                    raise BBSError("Cannot reply to a removed post")
            row = self.store.db.execute(
                "SELECT writes FROM web_users WHERE actor=?", (current.actor,)
            ).fetchone()
            now = self.clock()
            writes = [
                timestamp for timestamp in json.loads(row[0]) if timestamp > now - WRITE_WINDOW
            ]
            if len(writes) >= WRITE_LIMIT:
                raise AccessDenied(
                    "Posting limit reached; wait a minute before retrying", status=429
                )
            post = self.store.publish(
                current.actor, operation, board, title, body, parent_id=parent_id
            )
            self.store.db.execute(
                "UPDATE web_users SET writes=? WHERE actor=?",
                (json.dumps([*writes, now]), current.actor),
            )
            return post
