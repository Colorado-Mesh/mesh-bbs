"""Persistent, numbered DM navigation with one response per reader request."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from mesh_bbs.commands import byte_prefix, display_text
from mesh_bbs.events import BBSError

if TYPE_CHECKING:
    from mesh_bbs.commands import CommandService


class Menus:
    def __init__(self, commands: CommandService) -> None:
        self.commands = commands
        self.store = commands.store
        with self.store.transaction():
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS menu_sessions ("
                "actor TEXT PRIMARY KEY, state TEXT NOT NULL, expires REAL NOT NULL)"
            )

    def reading(self, actor: str) -> bool:
        row = self.store.db.execute(
            "SELECT state FROM menu_sessions WHERE actor=?", (actor,)
        ).fetchone()
        return bool(row and json.loads(row[0]).get("view") == "read")

    def handle(self, actor: str, text: str, budget: int) -> str | None:
        now = time.time()
        self.store.db.execute(
            "DELETE FROM menu_sessions WHERE expires<? AND json_extract(state,'$.draft') IS NULL",
            (now,),
        )
        row = self.store.db.execute(
            "SELECT state FROM menu_sessions WHERE actor=?", (actor,)
        ).fetchone()
        state = json.loads(row[0]) if row else {"view": "home"}
        word = text.casefold().strip()
        response = self._handle(actor, text, word, budget, state)
        if response is None and row:
            # Explicit commands replace the active page. Keep a draft available
            # for menu -> 3, but never apply stale numbered choices to that page.
            state.update(view="legacy", history=[])
            self.store.db.execute(
                "UPDATE menu_sessions SET state=? WHERE actor=?", (json.dumps(state), actor)
            )
        if response is not None:
            self.store.db.execute(
                "INSERT OR REPLACE INTO menu_sessions VALUES (?,?,?)",
                (actor, json.dumps(state), now + 86400),
            )
        return response

    def _home(self, state: dict[str, Any], budget: int) -> str:
        state.update(view="home", history=[])
        if budget < 100:
            return "BBS\n1 News\n2 Boards\n3 Write/resume\nSend a number. menu=start"
        return (
            "Welcome to Mesh BBS!\n1 News & newsletters\n2 Browse boards\n3 Write/resume a post\n"
            "Reply with a number.\nnext=more | back | menu=start"
        )

    def _handle(
        self, actor: str, text: str, word: str, budget: int, state: dict[str, Any]
    ) -> str | None:
        if word in {"", "help", "menu", "?"} or (
            word in {"start", "hi", "hello"}
            and state["view"] not in {"title", "body", "board_name"}
        ):
            return self._home(state, budget)
        if word == "commands":
            return None
        if re.fullmatch(r"read\s+#[1-9][0-9]{0,17}", word):
            post = self.store.get_post(word.split()[1])
            state["history"] = []
            return self._read(actor, state, post.post_id, budget)
        if word == "boards":
            return self._boards(actor, state, budget)
        if word == "news":
            if "news" not in self.store.boards:
                return "No news board here. Send boards to browse."
            return self._posts(state, "news", budget)
        if word == "cancel":
            draft = state.pop("draft", None)
            if draft:
                row, _ = self.store.draft(actor, draft)
                if not row["published"]:
                    self.store.db.execute("DELETE FROM draft_parts WHERE draft_id=?", (draft,))
                    self.store.db.execute("DELETE FROM drafts WHERE draft_id=?", (draft,))
            return self._home(state, budget)
        view = state["view"]
        if word == "back":
            history = state.get("history", [])
            if not history:
                return self._home(state, budget)
            previous = history.pop()
            state.update(previous)
            return self._list_page(state, budget)
        if view == "board_name":
            board = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
            self.store.create_board(actor, board)
            state.update(view="title", board=board)
            return "Board ready. Send a title for your first post. cancel=exit"
        if view == "title":
            self.commands._can_start(actor, state["board"])
            draft = self.store.new_draft(actor, state["board"], text)
            state.update(view="body", draft=draft)
            return self._body_prompt(budget)
        if view == "body":
            if word == "done":
                return self._preview(actor, state, budget)
            if word == "publish":
                return "Review first: send done. Nothing is public yet."
            draft, _ = self.store.draft(actor, state["draft"])
            if draft["published"]:
                raise BBSError("Draft already published. Send menu to start again.")
            number = self.store.db.execute(
                "SELECT COALESCE(max(number),0)+1 FROM draft_parts WHERE draft_id=?",
                (state["draft"],),
            ).fetchone()[0]
            self.store.add_part(actor, state["draft"], number, text)
            return f"Part {number} saved. Send more text, or done to review. cancel=exit"
        if view == "preview":
            if word == "publish":
                draft, _ = self.store.draft(actor, state["draft"])
                if not draft["parent_id"]:
                    self.commands._can_start(actor, draft["board"])
                post = self.store.publish_draft(actor, state["draft"])
                state.update(view="published", post=post.post_id)
                board = byte_prefix(post.board, budget - 39)
                return f"Posted to {board}: {post.post_id[:12]}\nmenu=browse"
            if word in {"next", "more"}:
                return self._page(actor, budget, preview=True)
            if word in {"edit", "add"}:
                state["view"] = "body"
                return self._body_prompt(budget)
            return "Not posted. publish=post | next=preview | add | cancel"
        if view == "published" and word == "publish":
            post = self.store.publish_draft(actor, state["draft"])
            return f"Already posted: {post.post_id[:12]}\nmenu=browse"
        if view == "read":
            if word in {"next", "more"}:
                return self._page(actor, budget)
            if word == "reply":
                self._check_draft(actor, state)
                post = self.store.get_post(state["post"])
                if post.board == "news":
                    return "News is read-only. menu then 3 starts a community discussion."
                if post.deleted:
                    return "This post was removed. Send back or menu."
                draft = self.store.new_draft(
                    actor, post.board, "Re: " + post.title[:60], post.post_id
                )
                state.update(view="body", draft=draft)
                return self._body_prompt(budget)
            if word == "replies":
                post = self.store.get_post(state["post"])
                return self._posts(state, post.board, budget, thread_id=post.thread_id)
        if view in {"boards", "posts", "write_boards"} and word in {"next", "more"}:
            if state.get("stop", 0) < len(state["items"]):
                state["offset"] = state["stop"]
            elif state.get("after"):
                return self._posts(
                    state,
                    state["board"],
                    budget,
                    thread_id=state.get("thread", ""),
                    after=state["after"],
                )
            else:
                return "End of list. Reply with a shown number, back, or menu."
            return self._list_page(state, budget)
        if word.isascii() and word.isdigit() and len(word) <= 3:
            number = int(word)
            if view == "home":
                if number == 1:
                    if "news" not in self.store.boards:
                        return "No news board here yet. Send 2 to browse other boards."
                    return self._posts(state, "news", budget)
                if number == 2:
                    return self._boards(actor, state, budget)
                if number == 3:
                    draft = state.get("draft")
                    if draft:
                        row, _ = self.store.draft(actor, draft)
                        if not row["published"]:
                            state["view"] = "body"
                            return self._body_prompt(budget)
                    state.pop("draft", None)
                    return self._boards(actor, state, budget, writing=True)
            if view in {"boards", "posts", "write_boards"}:
                index = state["offset"] + number - 1
                if number < 1 or index >= state["stop"]:
                    return "Choose a number on this page, next, back, or menu."
                item = state["items"][index]
                self._remember(state)
                if view == "boards" and item[0]:
                    return self._posts(state, item[0], budget)
                if view == "write_boards":
                    self._check_draft(actor, state)
                    if not item[0]:
                        state["view"] = "board_name"
                        return "Send a board name, e.g. hiking. cancel=exit"
                    state.update(view="title", board=item[0])
                    return "Send a short title. Nothing posts until publish. cancel=exit"
                if not item[0]:
                    self._check_draft(actor, state)
                    state["view"] = "board_name"
                    return "Send a board name, e.g. hiking. cancel=exit"
                return self._read(actor, state, item[0], budget)
            return "Send menu to see the numbered choices."
        if word == "next":
            return self.commands._more(actor, budget)
        # Preserve the existing explicit command vocabulary for scripts and links.
        return None

    @staticmethod
    def _body_prompt(budget: int) -> str:
        if budget < 100:
            return "Send post text. done=review | cancel=discard"
        return (
            "Send your text (one or more messages).\n"
            "done=review | cancel=discard\nNothing is public until you send publish."
        )

    def _check_draft(self, actor: str, state: dict[str, Any]) -> None:
        if state.get("draft"):
            draft, _ = self.store.draft(actor, state["draft"])
            if not draft["published"]:
                raise BBSError("You have a draft. menu then 3 resumes it; cancel discards it.")
            state.pop("draft")

    @staticmethod
    def _remember(state: dict[str, Any]) -> None:
        history = state.setdefault("history", [])
        history.append({k: v for k, v in state.items() if k not in {"history", "draft"}})
        del history[:-3]

    def _boards(
        self, actor: str, state: dict[str, Any], budget: int, *, writing: bool = False
    ) -> str:
        boards = [board for board in self.store.boards if not writing or board != "news"]
        state.update(
            view="write_boards" if writing else "boards",
            title="Post to:" if writing else "Boards",
            items=[[board, board] for board in boards] + [["", "+ Create a board"]],
            offset=0,
            after="",
        )
        return self._list_page(state, budget)

    def _posts(
        self,
        state: dict[str, Any],
        board: str,
        budget: int,
        *,
        thread_id: str = "",
        after: str = "",
    ) -> str:
        posts = self.store.list_posts(board, thread_id=thread_id, limit=51, after_id=after)
        state.update(
            view="posts",
            board=board,
            thread=thread_id,
            title="Replies" if thread_id else board,
            items=[[p.post_id, "[removed]" if p.deleted else p.title] for p in posts[:50]],
            offset=0,
            after=posts[49].post_id if len(posts) > 50 else "",
        )
        return self._list_page(state, budget)

    def _list_page(self, state: dict[str, Any], budget: int) -> str:
        items = state["items"]
        if not items:
            state["stop"] = 0
            return "No posts here yet. back=boards | menu=start"
        header = byte_prefix(" ".join(display_text(state["title"]).split()), 20) + "\n"
        footer = "\nReply # | next | back | menu"
        lines: list[str] = []
        start = state["offset"]
        # Never split a menu entry across packets; numbers address the displayed
        # snapshot, even if a feed or another host adds a newer post meanwhile.
        for index in range(start, min(start + 3, len(items))):
            remaining = budget - len((header + "\n".join(lines) + footer).encode())
            if lines:
                remaining -= 1
            if remaining < 12:
                break
            label = " ".join(display_text(items[index][1]).split())
            number = str(index - start + 1) + " "
            limit = min(54, remaining - len(number))
            if len(label.encode()) > limit:
                label = byte_prefix(label, limit - 3) + "..."
            lines.append(number + label)
        state["stop"] = start + len(lines)
        return header + "\n".join(lines) + footer

    def _read(self, actor: str, state: dict[str, Any], post_id: str, budget: int) -> str:
        post = self.store.get_post(post_id)
        if post.deleted:
            return "This post was removed. Send back or menu."
        state.update(view="read", post=post.post_id)
        self.store.db.execute(
            "INSERT OR REPLACE INTO cursors VALUES (?,?,0,?)",
            (actor, display_text(post.title + "\n" + post.body), post.revision_id),
        )
        return self._page(actor, budget)

    def _preview(self, actor: str, state: dict[str, Any], budget: int) -> str:
        row, body = self.store.draft(actor, state["draft"])
        if not body:
            return "Your draft is empty. Send some text first, then done."
        state["view"] = "preview"
        self.store.db.execute(
            "INSERT OR REPLACE INTO cursors VALUES (?,?,0,?)",
            (actor, display_text("DRAFT: " + row["title"] + "\n" + body), "draft"),
        )
        return self._page(actor, budget, preview=True)

    def _page(self, actor: str, budget: int, *, preview: bool = False) -> str:
        row = self.store.db.execute("SELECT * FROM cursors WHERE actor=?", (actor,)).fetchone()
        if row is None:
            return "Page changed or removed. Send back to reopen it, or menu."
        rest = row["body"][row["position"] :]
        end = "\npublish | add | cancel" if preview else "\nEnd. reply | replies | back | menu"
        if not preview:
            post = self.store.db.execute(
                "SELECT board FROM events WHERE event_id=?", (row["revision_id"],)
            ).fetchone()
            if post and post[0] == "news":
                end = "\nEnd. News is read-only. back | menu"
        more = "\nnext=more | cancel" if preview else "\nnext=more | back | menu"
        if not preview and budget >= 100:
            more = "\nSend next to keep reading, or menu for options."
            end = (
                "\nEnd. News is read-only. Send menu for options."
                if post and post[0] == "news"
                else "\nEnd. Send reply to respond, or menu for options."
            )
        if len((rest + end).encode()) <= budget:
            content, footer = rest, end
        else:
            content = byte_prefix(rest, budget - len(more))
            boundary = max(content.rfind("\n"), content.rfind(" "))
            if boundary > len(content) // 2:
                content = content[: boundary + 1]
            footer = more
        self.store.db.execute(
            "UPDATE cursors SET position=position+? WHERE actor=?", (len(content), actor)
        )
        return content + footer
