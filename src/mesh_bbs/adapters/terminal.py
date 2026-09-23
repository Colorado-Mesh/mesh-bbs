"""Cleartext command sessions for an operator-managed packet terminal switch."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from typing import BinaryIO

from mesh_bbs.commands import CommandService, byte_prefix
from mesh_bbs.events import BBSError, stable_id

_READ_COMMANDS = frozenset({"", "help", "boards", "threads", "thread", "read", "news", "more"})
_DRAFT_COMMANDS = frozenset({"add", "preview", "publish", "discard"})
_WRITE_COMMANDS = _DRAFT_COMMANDS | {"post", "new", "reply"}
_CALLSIGN = re.compile(r"[A-Z0-9]{2,6}(?:-(?:[0-9]|1[0-5]))?\Z", re.ASCII)


def packet_actor(callsign: str) -> str:
    """Normalize a gateway-attested AX.25 station address, not proof of a license."""
    if not callsign.isascii():
        raise BBSError("Use an AX.25 callsign with an optional SSID from 0 to 15")
    callsign = callsign.upper()
    base = callsign.partition("-")[0]
    if not _CALLSIGN.fullmatch(callsign) or not (
        any(c.isalpha() for c in base) and any(c.isdigit() for c in base)
    ):
        raise BBSError("Use an AX.25 callsign with an optional SSID from 0 to 15")
    return "packet:" + callsign.removesuffix("-0")


def _terminal_text(text: str) -> str:
    return "".join(
        " " if c in "\r\n\t" else c
        for c in text
        if c in "\r\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
    )


class _TerminalCommands(CommandService):
    def __init__(
        self,
        service: CommandService,
        actor: str,
        allowed_boards: tuple[str, ...],
        allow_posts: bool,
        budget: int,
    ) -> None:
        if not allowed_boards or any(board not in service.store.boards for board in allowed_boards):
            raise BBSError("Select at least one configured public board for this terminal")
        super().__init__(service.store, editors=(), guided=False)
        self.actor = actor
        self.allowed_boards = tuple(dict.fromkeys(allowed_boards))
        self.allow_posts = allow_posts
        self.budget = budget
        self.policy = stable_id(*sorted(self.allowed_boards), str(allow_posts), str(budget))
        with self.store.transaction():
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS terminal_cursors ("
                "actor TEXT PRIMARY KEY, policy TEXT NOT NULL)"
            )

    def command(self, text: str) -> str:
        request_id = None
        if text.startswith("@"):
            operation, separator, text = text.partition(" ")
            if not separator or not re.fullmatch(r"@[A-Za-z0-9_-]{1,64}", operation):
                return "Error: use @operation-id COMMAND."
            request_id = "terminal:" + operation[1:]
        verb, _, arguments = text.strip().partition(" ")
        verb, arguments = verb.lower(), arguments.strip()
        if request_id and verb in _READ_COMMANDS | {"preview"}:
            request_id += ":" + self.policy
        try:
            with self.store.transaction():
                self._authorize(verb, arguments)
                policy = self.store.db.execute(
                    "SELECT policy FROM terminal_cursors WHERE actor=?", (self.actor,)
                ).fetchone()
                if not policy or policy[0] != self.policy:
                    self.store.db.execute("DELETE FROM cursors WHERE actor=?", (self.actor,))
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO terminal_cursors VALUES (?,?)",
                        (self.actor, self.policy),
                    )
                return super().handle(
                    self.actor, text, request_id=request_id, max_bytes=self.budget
                )
        except BBSError as exc:
            return byte_prefix("Error: " + _terminal_text(str(exc)), self.budget)

    def _require_board(self, board: str) -> None:
        if board not in self.allowed_boards:
            raise BBSError("Board is not available through this terminal")

    def _require_post(self, post_id: str) -> None:
        try:
            post = self.store.get_post(post_id)
            self._require_board(post.board)
        except BBSError:
            raise BBSError("Post is not available through this terminal") from None

    def _authorize(self, verb: str, arguments: str) -> None:
        if verb not in _READ_COMMANDS | _WRITE_COMMANDS:
            raise BBSError("Unknown command. Send help.")
        if verb in _WRITE_COMMANDS and not self.allow_posts:
            raise BBSError("This terminal is read only")
        target, _, remainder = arguments.partition(" ")
        if verb == "news":
            self._require_board("news")
        elif verb in {"threads", "post", "new"}:
            self._require_board(target)
            if verb == "threads" and remainder:
                self._require_post(remainder)
        elif verb in {"read", "thread", "reply"}:
            self._require_post(target)
            if verb == "thread" and remainder:
                self._require_post(remainder)
        elif verb in _DRAFT_COMMANDS:
            row = self.store.db.execute(
                "SELECT board,parent_id FROM drafts WHERE actor=? AND draft_id=?",
                (self.actor, target),
            ).fetchone()
            if not row:
                raise BBSError("Draft not found for this sender")
            self._require_board(row["board"])
            if row["parent_id"]:
                self._require_post(row["parent_id"])

    def _begin_page(self, actor: str, text: str, revision: str, budget: int) -> str:
        return super()._begin_page(actor, _terminal_text(text), revision, budget)

    def _execute(self, actor: str, text: str, budget: int) -> str:
        verb = text.partition(" ")[0].lower()
        if verb in {"", "help"}:
            help_text = "boards | threads BOARD | thread ID | read ID | more"
            if "news" in self.allowed_boards:
                help_text += " | news [latest]"
            if self.allow_posts:
                help_text += (
                    " | post BOARD TITLE | TEXT"
                    " | new BOARD TITLE | add DRAFT N TEXT | preview DRAFT | publish DRAFT"
                    " | reply ID TEXT | discard DRAFT | @ID COMMAND to retry safely"
                )
            return self._begin_page(actor, help_text + " | quit", "help", budget)
        if verb == "boards":
            return self._begin_page(
                actor, "Boards: " + ", ".join(self.allowed_boards), "boards", budget
            )
        response = super()._execute(actor, text, budget)
        if len(response.encode("utf-8")) > budget:
            return self._begin_page(actor, response, "response", budget)
        return response


def _lines(source: BinaryIO, budget: int) -> Iterator[bytes | None]:
    line = bytearray()
    overflow = False
    after_cr = False
    while chunk := source.read(1):
        if chunk == b"\n" and after_cr:
            after_cr = False
            continue
        after_cr = chunk == b"\r"
        if chunk in {b"\r", b"\n"}:
            yield None if overflow else bytes(line)
            line.clear()
            overflow = False
        elif len(line) < budget:
            line.extend(chunk)
        else:
            overflow = True
    if line or overflow:
        yield None if overflow else bytes(line)


def run_terminal(
    service: CommandService,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    callsign: str,
    allowed_boards: tuple[str, ...],
    max_bytes: int = 256,
    allow_posts: bool = False,
) -> None:
    """Serve an already-associated cleartext session over binary stdin/stdout.

    The caller must authenticate or attest the callsign outside this process and
    limit session duration, connection rate, and radio airtime at the gateway.
    """
    if not 66 <= max_bytes <= 8192:
        raise BBSError("Terminal frame limit must be between 66 and 8192 bytes")
    budget = max_bytes - 2
    actor = packet_actor(callsign)
    commands = _TerminalCommands(service, actor, allowed_boards, allow_posts, budget)

    def write(text: str) -> None:
        frame = byte_prefix(_terminal_text(text), budget).encode("utf-8") + b"\r\n"
        output_stream.write(frame)
        output_stream.flush()

    access = "Posting enabled." if allow_posts else "Read only."
    write(f"{actor} (gateway-attested). {access} Send help.")
    for raw in _lines(input_stream, budget):
        if raw is None:
            write("Error: command exceeds the terminal byte limit.")
            continue
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            write("Error: commands must be valid UTF-8.")
            continue
        if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in text):
            write("Error: control characters are not allowed in commands.")
            continue
        text = text.strip()
        if text.lower() in {"quit", "exit"}:
            write("Goodbye.")
            return
        write(commands.command(text))
