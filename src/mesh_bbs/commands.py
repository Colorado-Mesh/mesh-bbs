"""A small, persistent command interface shared by every access protocol."""

from __future__ import annotations

import re
import secrets
import time
import unicodedata

from mesh_bbs.events import MAX_BODY_BYTES, BBSError, stable_id
from mesh_bbs.micron import plain
from mesh_bbs.store import Store

HELP = (
    "Send one command per DM. Send more for the next page of any long response.\n"
    "resend: repeat the last reply without advancing or posting again\n"
    "boards: list boards\nthreads BOARD: newest threads\nread ID: full post\n"
    "thread ID: replies\nnews: latest newsletter\nreply ID TEXT\n"
    "post BOARD TITLE | TEXT\n"
    "Long posts: new BOARD TITLE; add DRAFT N TEXT; preview DRAFT; publish DRAFT; discard DRAFT.\n"
    "Replace BOARD with a board name, ID with a listed post ID or this host's #number, "
    "and DRAFT with your draft ID."
)

LAST_REPLY_LIMIT = 4096
LAST_REPLY_TTL = 86400
RESEND_COMMANDS = {"resend", "again", "repeat"}


def display_text(text: str) -> str:
    return "".join(
        c
        for c in text.replace("\r\n", "\n")
        if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
    )


def byte_prefix(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


class CommandService:
    def __init__(
        self, store: Store, editors: tuple[str, ...] = ("local:operator",), *, guided: bool = True
    ) -> None:
        from mesh_bbs.menus import Menus

        self.guided = guided
        self.store = store
        self.editors = frozenset(editors)
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS command_receipts ("
                "actor TEXT NOT NULL, operation TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "response TEXT NOT NULL, expires REAL, revision_id TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY(actor,operation))"
            )
        self.menus = Menus(self)

    def handle(
        self,
        actor: str,
        text: str,
        *,
        request_id: str | None = None,
        max_bytes: int = 160,
        request_ttl_seconds: int | None = None,
    ) -> str:
        if not 64 <= max_bytes <= 8192:
            raise BBSError("Response limit must be between 64 and 8192 bytes")
        if not actor or len(actor.encode()) > 256 or len(text.encode()) > MAX_BODY_BYTES + 512:
            return "Error: command or sender exceeds the limit."
        text = text.strip()
        if text.startswith("@"):
            operation, separator, command = text.partition(" ")
            if not separator or not re.fullmatch(r"@[A-Za-z0-9_-]{1,64}", operation):
                return "Error: use @operation-id COMMAND."
            request_id, request_ttl_seconds, text = "user:" + operation[1:], None, command
        if request_id and len(request_id) > 256:
            return "Error: request ID exceeds the limit."
        fingerprint = stable_id(text, str(max_bytes))
        try:
            with self.store.transaction():
                if request_id:
                    now = time.time()
                    self.store.db.execute(
                        "DELETE FROM command_receipts WHERE expires IS NOT NULL AND expires < ?",
                        (now,),
                    )
                    receipt = self.store.db.execute(
                        "SELECT fingerprint,response FROM command_receipts "
                        "WHERE actor=? AND operation=?",
                        (actor, request_id),
                    ).fetchone()
                    if receipt:
                        if receipt["fingerprint"] != fingerprint:
                            raise BBSError("Request ID already used for another command")
                        return str(receipt["response"])
                now = time.time()
                self.store.db.execute("DELETE FROM last_replies WHERE expires<=?", (now,))
                replay = text.casefold() in RESEND_COMMANDS
                if replay:
                    last = self.store.db.execute(
                        "SELECT response,revision_id FROM last_replies WHERE actor=?", (actor,)
                    ).fetchone()
                    revision = last["revision_id"] if last else ""
                    if last is None:
                        response = "No recent reply to resend. Send help."
                    elif len(last["response"].encode()) > max_bytes:
                        response = "Last reply is too large for this connection. Send help."
                    else:
                        response = str(last["response"])
                else:
                    response = self._execute(actor, text, max_bytes)
                    reading = text.split(" ", 1)[0].lower() in {
                        "read",
                        "more",
                        "news",
                        "next",
                    } or self.menus.reading(actor)
                    cursor = (
                        self.store.db.execute(
                            "SELECT revision_id FROM cursors WHERE actor=?", (actor,)
                        ).fetchone()
                        if reading
                        else None
                    )
                    revision = cursor[0] if cursor else ""
                if len(response.encode()) > max_bytes:
                    raise BBSError("Response exceeds the interface limit")
                if not replay:
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO last_replies VALUES (?,?,?,?)",
                        (actor, response, revision, now + LAST_REPLY_TTL),
                    )
                    self.store.db.execute(
                        "DELETE FROM last_replies WHERE actor IN "
                        "(SELECT actor FROM last_replies ORDER BY expires DESC, rowid DESC "
                        "LIMIT -1 OFFSET ?)",
                        (LAST_REPLY_LIMIT,),
                    )
                if request_id:
                    expiry = time.time() + request_ttl_seconds if request_ttl_seconds else None
                    self.store.db.execute(
                        "INSERT INTO command_receipts VALUES (?,?,?,?,?,?)",
                        (
                            actor,
                            request_id,
                            fingerprint,
                            response,
                            expiry,
                            revision,
                        ),
                    )
                return response
        except (BBSError, ValueError) as exc:
            return byte_prefix("Error: " + display_text(str(exc)), max_bytes)

    def _begin_page(self, actor: str, text: str, revision: str, budget: int) -> str:
        text = display_text(text)
        if len(text.encode()) > MAX_BODY_BYTES + 2048:
            raise BBSError("Listing is too long; choose a smaller board or post")
        self.store.db.execute(
            "INSERT OR REPLACE INTO cursors VALUES (?,?,0,?)",
            (actor, text, revision),
        )
        return self._more(actor, budget)

    def _more(self, actor: str, budget: int) -> str:
        row = self.store.db.execute("SELECT * FROM cursors WHERE actor=?", (actor,)).fetchone()
        if not row:
            return "No open page. Use read ID, threads BOARD, or news."
        body, position = str(row["body"]), int(row["position"])
        rest = body[position:]
        if not rest:
            return "End. Use read ID or news to open another post."
        if len(rest.encode()) <= budget:
            result, consumed = rest, len(rest)
        else:
            footer = "\n[more]"
            content = byte_prefix(rest, budget - len(footer))
            # Prefer a word boundary without discarding any source characters.
            boundary = max(content.rfind("\n"), content.rfind(" "))
            if boundary > len(content) // 2:
                content = content[: boundary + 1]
            consumed = len(content)
            result = content + footer
        self.store.db.execute(
            "UPDATE cursors SET position=? WHERE actor=?", (position + consumed, actor)
        )
        return result

    def _can_start(self, actor: str, board: str) -> None:
        if board not in self.store.boards:
            raise BBSError("Unknown board")
        if board == "news":
            raise BBSError("News is read-only; automatic imports only. Choose another board.")

    def _execute(self, actor: str, text: str, budget: int) -> str:
        guided = self.menus.handle(actor, text, budget) if self.guided else None
        if guided is not None:
            return guided
        verb, _, arguments = text.partition(" ")
        verb = verb.lower()
        arguments = arguments.strip()
        if verb == "commands":
            return self._begin_page(actor, HELP, "help", budget)
        if verb == "boards":
            return self._begin_page(
                actor, "Boards: " + ", ".join(self.store.boards), "boards", budget
            )
        if verb == "more":
            return self._more(actor, budget)
        if verb == "threads":
            board, _, after_id = arguments.partition(" ")
            if board not in self.store.boards:
                raise BBSError("Unknown board")
            posts = self.store.list_posts(board, limit=51, after_id=after_id)
            listing = (
                "\n".join(f"{p.post_id[:12]} {p.title}" for p in posts[:50]) or "No threads yet."
            )
            if len(posts) > 50:
                listing += f"\nNext: threads {board} {posts[49].post_id}"
            return self._begin_page(actor, listing, "threads", budget)
        if verb == "thread":
            thread_id, _, after_id = arguments.partition(" ")
            parent = self.store.get_post(thread_id)
            replies = self.store.list_posts(
                parent.board, thread_id=parent.thread_id, limit=51, after_id=after_id
            )
            listing = "\n".join(
                f"{p.post_id[:12]} {'[removed]' if p.deleted else p.title}" for p in replies[:50]
            )
            if len(replies) > 50:
                listing += f"\nNext: thread {parent.thread_id} {replies[49].post_id}"
            return self._begin_page(actor, listing, "thread", budget)
        if verb == "news":
            posts = self.store.list_posts("news", limit=10)
            if not posts:
                return "No newsletter issues saved on this host yet."
            if arguments == "latest":
                return self._read(actor, posts[0].post_id, budget)
            listing = "\n".join(f"{p.post_id[:12]} {p.title}" for p in posts)
            return self._begin_page(actor, listing, "news", budget)
        if verb == "read":
            return self._read(actor, arguments, budget)
        if verb == "post":
            heading, separator, body = arguments.partition("|")
            board, _, title = heading.strip().partition(" ")
            title, body = title.strip(), body.strip()
            if not separator or not board or not title or not body:
                raise BBSError("Use post BOARD TITLE | TEXT")
            self._can_start(actor, board)
            post = self.store.publish(actor, secrets.token_hex(16), board, title, body)
            return f"Saved locally as {post.post_id[:12]}. Replication is pending."
        if verb == "new":
            board, _, title = arguments.partition(" ")
            self._can_start(actor, board)
            draft = self.store.new_draft(actor, board, title)
            return f"Draft {draft}. Send add {draft} 1 TEXT, then preview {draft}."
        if verb == "reply":
            post_id, _, body = arguments.partition(" ")
            post = self.store.get_post(post_id)
            if not body.strip():
                raise BBSError("Use reply ID TEXT")
            draft = self.store.new_draft(actor, post.board, "Re: " + post.title[:60], post.post_id)
            self.store.add_part(actor, draft, 1, body)
            return f"Reply draft {draft} to {post.post_id[:12]}. Send publish {draft}."
        if verb == "add":
            parts = arguments.split(" ", 2)
            if len(parts) != 3:
                raise BBSError("Use add DRAFT PART_NUMBER TEXT")
            self.store.add_part(actor, parts[0], int(parts[1]), parts[2])
            return f"Saved part {parts[1]}. Use preview {parts[0]} or publish {parts[0]}."
        if verb == "preview":
            draft_row, body = self.store.draft(actor, arguments)
            return self._begin_page(
                actor, f"Draft {arguments}: {draft_row['title']}\n{plain(body)}", "draft", budget
            )
        if verb == "publish":
            draft_row, _ = self.store.draft(actor, arguments)
            if not draft_row["parent_id"]:
                self._can_start(actor, draft_row["board"])
            post = self.store.publish_draft(actor, arguments)
            return f"Saved locally as {post.post_id[:12]}. Replication is pending."
        if verb == "discard":
            draft_row = self.store.db.execute(
                "SELECT published FROM drafts WHERE actor=? AND draft_id=?", (actor, arguments)
            ).fetchone()
            if not draft_row:
                raise BBSError("Draft not found for this sender")
            if draft_row["published"]:
                raise BBSError("Draft was already published")
            self.store.db.execute("DELETE FROM draft_parts WHERE draft_id=?", (arguments,))
            self.store.db.execute("DELETE FROM drafts WHERE draft_id=?", (arguments,))
            return "Draft discarded."
        raise BBSError("Unknown command. Send help.")

    def _read(self, actor: str, post_id: str, budget: int) -> str:
        post = self.store.get_post(post_id)
        if post.deleted:
            return "This post has been removed."
        return self._begin_page(
            actor,
            f"{post.post_id[:12]} {post.title}\n{plain(post.body)}",
            post.revision_id,
            budget,
        )
