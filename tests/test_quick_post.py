from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mesh_bbs.commands import HELP, CommandService
from mesh_bbs.events import MAX_BODY_BYTES
from mesh_bbs.store import Store

ALICE = "meshcore:" + "a1" * 32
BOB = "meshtastic:11223344"


@pytest.fixture
def service(tmp_path: Path) -> Iterator[CommandService]:
    store = Store(tmp_path / "bbs.db", "colorado-mesh")
    try:
        yield CommandService(store)
    finally:
        store.close()


def test_post_publishes_one_thread_without_an_intermediate_draft(service: CommandService) -> None:
    response = service.handle(ALICE, "post general Saturday meetup | Bring a radio.")
    post = service.store.list_posts("general")[0]
    assert response == f"Saved locally as {post.post_id[:12]}. Replication is pending."
    assert post.title == "Saturday meetup"
    assert post.body == "Bring a radio."
    assert post.author == ALICE
    assert post.thread_id == post.post_id and post.parent_id == ""
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
    assert service.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_body_preserves_remaining_pipes_spaces_and_paragraphs(service: CommandService) -> None:
    body = "Use A | B || C.  Keep spaces.\n\nUTF-8 café あ 🛜.\n  Indented line."
    response = service.handle(ALICE, f"POST general  Field notes   |  {body}  ")
    assert response.startswith("Saved locally")
    post = service.store.list_posts("general")[0]
    assert post.title == "Field notes"
    assert post.body == body


@pytest.mark.parametrize(
    "command",
    [
        "post",
        "post general",
        "post general Title",
        "post general Title |",
        "post general Title | \t\n",
        "post general | Text",
        "post general   | Text",
        "post | Text",
        "post |",
        "post   | Text",
    ],
)
def test_empty_fields_are_rejected_without_any_write(service: CommandService, command: str) -> None:
    assert service.handle(ALICE, "@retry " + command) == "Error: Use post BOARD TITLE | TEXT"
    for table in ("posts", "events", "receipts", "command_receipts", "drafts"):
        assert service.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "title,body",
    [("é" * 129, "text"), ("Title", "é" * (MAX_BODY_BYTES // 2 + 1))],
)
def test_store_utf8_byte_limits_reject_and_roll_back(
    service: CommandService, title: str, body: str
) -> None:
    assert service.handle(ALICE, f"@too-long post general {title} | {body}").startswith("Error:")
    for table in ("posts", "events", "receipts", "command_receipts"):
        assert service.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_exact_store_title_and_body_limits_are_accepted(service: CommandService) -> None:
    title, body = "é" * 128, "あ" * (MAX_BODY_BYTES // 3) + "x"
    assert len(body.encode()) == MAX_BODY_BYTES
    response = service.handle(ALICE, f"post general {title} | {body}", max_bytes=64)
    assert response.startswith("Saved locally") and len(response.encode()) <= 64
    post = service.store.list_posts("general")[0]
    assert (post.title, post.body) == (title, body)


def test_unknown_board_and_news_editor_permissions_are_shared(service: CommandService) -> None:
    assert service.handle(ALICE, "post unknown Title | Text") == "Error: Unknown board"
    assert "editor" in service.handle(ALICE, "post news September | Official issue")
    assert not service.store.list_posts("news")
    assert service.handle("local:operator", "post news September | Official issue").startswith(
        "Saved locally"
    )
    issue = service.store.list_posts("news")[0]
    assert (issue.title, issue.author) == ("September", "local:operator")


def test_explicit_retry_replays_after_restart_with_one_post_and_event(tmp_path: Path) -> None:
    path = tmp_path / "persistent.db"
    command = "@meetup-1 post general Meetup | Saturday at nine"
    store = Store(path, "colorado-mesh")
    try:
        response = CommandService(store).handle(ALICE, command)
        assert response.startswith("Saved locally")
    finally:
        store.close()
    reopened = Store(path, "colorado-mesh")
    try:
        assert CommandService(reopened).handle(ALICE, command) == response
        for table in ("posts", "events", "receipts", "command_receipts"):
            assert reopened.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    finally:
        reopened.close()


def test_manual_retry_id_overrides_changed_transport_ids(service: CommandService) -> None:
    command = "@meetup-1 post general Meetup | Saturday at nine"
    first = service.handle(BOB, command, request_id="packet-1", request_ttl_seconds=60)
    assert service.handle(BOB, command, request_id="packet-2", request_ttl_seconds=60) == first
    receipt = service.store.db.execute("SELECT operation,expires FROM command_receipts").fetchone()
    assert tuple(receipt) == ("user:meetup-1", None)
    assert len(service.store.list_posts("general")) == 1


def test_native_request_id_deduplicates_and_rejects_changed_command(
    service: CommandService,
) -> None:
    command = "post general Meetup | Saturday at nine"
    first = service.handle(BOB, command, request_id="packet-1", request_ttl_seconds=60)
    assert service.handle(BOB, command, request_id="packet-1", request_ttl_seconds=60) == first
    assert "Request ID already used" in service.handle(
        BOB, command + " thirty", request_id="packet-1", request_ttl_seconds=60
    )
    assert len(service.store.list_posts("general")) == 1


def test_equal_text_with_distinct_or_missing_operations_creates_independent_posts(
    service: CommandService,
) -> None:
    command = "post general Repeated announcement | Still meeting Saturday"
    responses = {
        service.handle(ALICE, command),
        service.handle(ALICE, command),
        service.handle(ALICE, "@one " + command),
        service.handle(ALICE, "@two " + command),
        service.handle(BOB, "@one " + command),
    }
    assert len(responses) == 5
    assert len(service.store.list_posts("general")) == 5


def test_failed_receipt_write_rolls_back_post_and_event(service: CommandService) -> None:
    service.store.db.executescript(
        "CREATE TRIGGER receipt_failure BEFORE INSERT ON command_receipts "
        "BEGIN SELECT RAISE(ABORT, 'receipt storage failed'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError, match="receipt storage failed"):
        service.handle(ALICE, "@atomic post general Meetup | Meet Saturday")
    for table in ("posts", "events", "receipts", "command_receipts"):
        assert service.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    service.store.db.execute("DROP TRIGGER receipt_failure")
    assert service.handle(ALICE, "@atomic post general Meetup | Meet Saturday").startswith(
        "Saved locally"
    )


@pytest.mark.parametrize("budget", [64, 160, 4096])
def test_help_keeps_quick_post_syntax_when_paged(service: CommandService, budget: int) -> None:
    page = service.handle(ALICE, "help", max_bytes=budget)
    text = ""
    for _ in range(10):
        assert len(page.encode()) <= budget
        text += page.removesuffix("\n[more]")
        if not page.endswith("\n[more]"):
            break
        page = service.handle(ALICE, "more", max_bytes=budget)
    assert text == HELP
    assert "post BOARD TITLE | TEXT" in text
