from __future__ import annotations

import io
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from mesh_bbs.adapters.terminal import packet_actor, run_terminal
from mesh_bbs.commands import CommandService
from mesh_bbs.events import BBSError
from mesh_bbs.store import Store


@pytest.fixture
def service(tmp_path: Path) -> Iterator[CommandService]:
    store = Store(tmp_path / "terminal.sqlite3", "test-mesh", ("general", "news", "other"))
    try:
        yield CommandService(store, editors=("packet:N0CALL",))
    finally:
        store.close()


def session(
    service: CommandService,
    commands: str | bytes,
    *,
    callsign: str = "N0CALL",
    boards: tuple[str, ...] = ("general",),
    max_bytes: int = 256,
    allow_posts: bool = False,
) -> list[str]:
    source = io.BytesIO(commands.encode() if isinstance(commands, str) else commands)
    sink = io.BytesIO()
    run_terminal(
        service,
        source,
        sink,
        callsign=callsign,
        allowed_boards=boards,
        max_bytes=max_bytes,
        allow_posts=allow_posts,
    )
    frames = sink.getvalue().splitlines(keepends=True)
    assert all(frame.endswith(b"\r\n") and len(frame) <= max_bytes for frame in frames)
    assert b"\x1b" not in sink.getvalue()
    return [frame[:-2].decode("utf-8") for frame in frames]


@pytest.mark.parametrize("callsign", ["n0call", "N0CALL-0", "N0CALL"])
def test_station_identity_normalizes_case_and_zero_ssid(callsign: str) -> None:
    assert packet_actor(callsign) == "packet:N0CALL"
    assert packet_actor("N0CALL-15") == "packet:N0CALL-15"


@pytest.mark.parametrize(
    "callsign", ["", "NOCALL", "12345", "N0CALL-16", "N0CALL/1", "x\nN0CALL", "SSß1", "N00CALL"]
)
def test_invalid_station_address_is_not_a_sender(callsign: str) -> None:
    with pytest.raises(BBSError, match="callsign"):
        packet_actor(callsign)


def test_read_only_session_lists_only_selected_boards(service: CommandService) -> None:
    frames = session(service, "boards\r\nhelp\nnew general Title\rquit\nboards\n")
    assert "gateway-attested" in frames[0]
    assert frames[1] == "Boards: general"
    assert "news" not in frames[2]
    assert "new " not in frames[2]
    assert "read only" in frames[3]
    assert frames[4] == "Goodbye."
    assert len(frames) == 5
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0


def test_quick_post_obeys_packet_write_and_board_policy(service: CommandService) -> None:
    command = "@meetup post general Saturday meetup | Bring a radio | and batteries."
    readonly = session(service, command)
    assert "read only" in readonly[1]
    assert not service.store.list_posts("general")

    frames = session(service, command + "\n" + command, allow_posts=True)
    assert frames[1] == frames[2] and frames[1].startswith("Saved locally as")
    post = service.store.list_posts("general")[0]
    assert post.author == "packet:N0CALL"
    assert post.body == "Bring a radio | and batteries."
    assert service.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert "read only" in session(service, command)[1]
    assert "not available" in session(service, command, boards=("other",), allow_posts=True)[1]


def test_packet_quick_post_cannot_publish_news_or_unlisted_board(service: CommandService) -> None:
    frames = session(
        service,
        "@hidden post other Secret | Hidden text\n"
        "@official post news Official newsletter | Unapproved issue\n",
        boards=("general", "news"),
        allow_posts=True,
    )
    assert "not available" in frames[1]
    assert "editor" in frames[2]
    assert service.store.db.execute("SELECT count(*) FROM posts").fetchone()[0] == 0
    assert service.store.db.execute("SELECT count(*) FROM command_receipts").fetchone()[0] == 0


def test_packet_help_advertises_quick_post_only_when_writes_are_allowed(
    service: CommandService,
) -> None:
    writable = session(service, "help", allow_posts=True, max_bytes=1024)[1]
    readonly = session(service, "help", max_bytes=1024)[1]
    assert "post BOARD TITLE | TEXT" in writable
    assert "post BOARD" not in readonly


def test_board_allowlist_covers_posts_threads_news_and_listing_anchors(
    service: CommandService,
) -> None:
    public = service.store.publish("local:operator", "public", "general", "Open", "Open content")
    hidden = service.store.publish("local:operator", "hidden", "other", "Secret", "Secret content")
    issue = service.store.publish("local:operator", "news", "news", "News", "Restricted news")
    commands = [
        "threads other",
        f"read {hidden.post_id}",
        f"thread {hidden.post_id}",
        f"reply {hidden.post_id} Forbidden",
        f"threads general {hidden.post_id}",
        f"thread {public.post_id} {hidden.post_id}",
        f"@retry read {issue.post_id}",
        "news",
        "news latest",
        "new other Forbidden",
    ]
    frames = session(service, "\n".join(commands), allow_posts=True)
    assert all(frame.startswith("Error:") for frame in frames[1:])
    assert not any("Secret" in frame or "Restricted news" in frame for frame in frames)
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0


def test_narrower_later_session_cannot_replay_broad_cursor_or_read_receipts(
    service: CommandService,
) -> None:
    hidden = service.store.publish("local:operator", "hidden", "other", "Secret", "Secret " * 200)
    old = session(
        service,
        f"@read read {hidden.post_id}\n@page more\n@boards boards",
        boards=("general", "other"),
    )
    assert "Secret" in old[1]
    frames = session(service, f"@read read {hidden.post_id}\n@page more\n@boards boards\nmore")
    assert frames[1].startswith("Error:")
    assert "No open page" in frames[2]
    assert frames[3] == "Boards: general"
    assert not any("Secret" in frame or re.search(r"\bother\b", frame) for frame in frames)


def test_terminal_policy_is_rechecked_when_sessions_alternate(service: CommandService) -> None:
    hidden = service.store.publish("local:operator", "hidden", "other", "Secret", "Secret " * 200)
    session(service, f"read {hidden.post_id}", boards=("other",))
    assert "No open page" in session(service, "more", boards=("general",))[1]
    assert "No open page" in session(service, "more", boards=("other",))[1]


def test_drafts_survive_reconnect_and_retry_without_editor_privilege(
    service: CommandService,
) -> None:
    first = session(service, "@draft new general Local event", allow_posts=True)
    draft = re.search(r"\b[0-9a-f]{8}\b", first[1])[0]
    assert session(service, "@draft new general Local event", allow_posts=True)[1] == first[1]
    commands = f"add {draft} 1 Meet Saturday\nadd {draft} 2 Bring water\npublish {draft}"
    frames = session(service, commands, allow_posts=True)
    assert frames[-1].startswith("Saved locally")
    assert session(service, f"publish {draft}", allow_posts=True)[1] == frames[-1]
    posts = service.store.list_posts("general")
    assert len(posts) == 1
    assert posts[0].author == "packet:N0CALL"
    assert posts[0].body == "Meet Saturday\nBring water"
    assert "editor" in session(service, "new news Forged", boards=("news",), allow_posts=True)[1]


def test_draft_validation_rejects_hidden_board_and_foreign_author(
    service: CommandService,
) -> None:
    hidden = service.store.new_draft("packet:N0CALL", "other", "Hidden draft")
    foreign = service.store.new_draft("packet:N1CALL", "general", "Foreign draft")
    for draft in (hidden, foreign):
        frames = session(
            service,
            f"add {draft} 1 Content\npreview {draft}\npublish {draft}\ndiscard {draft}",
            allow_posts=True,
        )
        assert all(frame.startswith("Error:") for frame in frames[1:])
    assert service.store.db.execute("SELECT count(*) FROM draft_parts").fetchone()[0] == 0
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 2


def test_reply_draft_checks_hidden_parent_even_if_board_is_allowed(
    service: CommandService,
) -> None:
    parent = service.store.publish("local:operator", "hidden", "other", "Hidden", "Content")
    draft = service.store.new_draft("packet:N0CALL", "general", "Reply", parent.post_id)
    frames = session(service, f"preview {draft}\npublish {draft}", allow_posts=True)
    assert all("not available through this terminal" in frame for frame in frames[1:])


def test_posting_flag_cannot_be_bypassed_with_operation_prefix_or_cached_preview(
    service: CommandService,
) -> None:
    draft = service.store.new_draft("packet:N0CALL", "general", "Personal draft")
    service.store.add_part("packet:N0CALL", draft, 1, "Unpublished " * 100)
    assert "Unpublished" in session(service, f"@view preview {draft}", allow_posts=True)[1]
    frames = session(service, f"@view preview {draft}\nmore\n@new new general No")
    assert "read only" in frames[1]
    assert "No open page" in frames[2]
    assert "read only" in frames[3]
    assert not any("Unpublished" in frame for frame in frames)


def test_long_utf8_post_can_be_read_one_bounded_page_at_a_time(service: CommandService) -> None:
    post = service.store.publish("local:operator", "large", "general", "Long", "café あ " * 150)
    frames = session(service, f"read {post.post_id[:12]}\n" + "more\n" * 80, max_bytes=66)
    pages = []
    for frame in frames[1:]:
        pages.append(frame.removesuffix(" [more]"))
        if not frame.endswith(" [more]"):
            break
    assert "".join(pages) == f"{post.post_id[:12]} Long {post.body}"


def test_small_frame_acknowledgements_do_not_lose_accepted_draft(service: CommandService) -> None:
    frames = session(service, "new general Title\nmore", max_bytes=66, allow_posts=True)
    assert frames[1].startswith("Draft ")
    assert not any(frame.startswith("Error:") for frame in frames)
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1


def test_control_sequences_never_reach_terminal_and_commands_are_not_repaired(
    service: CommandService,
) -> None:
    post = service.store.publish(
        "local:operator", "controls", "general", "\x1b[31mTitle", "body\n\x07\x1b]0;window\x07"
    )
    frames = session(service, f"read {post.post_id}\nboa\x00rds\nboards\t\nboards\n")
    assert "Title" in frames[1]
    assert "control characters" in frames[2]
    assert "control characters" in frames[3]
    assert frames[4] == "Boards: general"
    assert not any(ord(c) < 32 for frame in frames for c in frame)


def test_invalid_utf8_overflow_and_crlf_recover_at_next_command(service: CommandService) -> None:
    frames = session(service, b"\xff\r\n" + b"x" * 10000 + b"\rboards\n", max_bytes=66)
    assert len(frames) == 4
    assert "UTF-8" in frames[1]
    assert "byte limit" in frames[2]
    assert frames[3] == "Boards: general"


def test_utf8_input_limit_is_bytes_and_a_long_line_is_consumed_in_bounded_reads(
    service: CommandService,
) -> None:
    class SmallReads(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert size == 1
            return super().read(size)

    source = SmallReads(("é" * 33 + "\nboards").encode())
    sink = io.BytesIO()
    run_terminal(
        service, source, sink, callsign="N0CALL", allowed_boards=("general",), max_bytes=66
    )
    assert sink.getvalue().decode().splitlines()[1:] == [
        "Error: command exceeds the terminal byte limit.",
        "Boards: general",
    ]


def test_same_policy_resumes_reading_after_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    store = Store(path, "test-mesh")
    first_service = CommandService(store)
    post = store.publish("local:operator", "post", "general", "Restart", "read " * 150)
    first = session(first_service, f"@read read {post.post_id}")[1]
    expected = session(first_service, "@next more")[1]
    store.close()
    reopened = Store(path, "test-mesh")
    try:
        second_service = CommandService(reopened)
        assert session(second_service, "@next more")[1] == expected
        assert first.endswith(" [more]")
        assert "No open page" not in session(second_service, "more")[1]
    finally:
        reopened.close()


@pytest.mark.parametrize("limit", [65, 8193])
def test_invalid_budget_is_rejected_before_session(service: CommandService, limit: int) -> None:
    with pytest.raises(BBSError, match="frame limit"):
        session(service, "", max_bytes=limit)


@pytest.mark.parametrize("boards", [(), ("missing",)])
def test_empty_or_unknown_allowlist_fails_closed(
    service: CommandService, boards: tuple[str, ...]
) -> None:
    with pytest.raises(BBSError, match="public board"):
        session(service, "", boards=boards)
