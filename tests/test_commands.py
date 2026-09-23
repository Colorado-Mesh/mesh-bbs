from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from mesh_bbs.commands import CommandService
from mesh_bbs.events import MAX_BODY_BYTES, BBSError
from mesh_bbs.store import Store

ALICE = "meshcore:" + "a1" * 32
BOB = "meshtastic:11223344"
EDITOR = "local:operator"


@pytest.fixture
def service(tmp_path: Path) -> Iterator[CommandService]:
    store = Store(tmp_path / "commands.db", "colorado-mesh")
    try:
        yield CommandService(store)
    finally:
        store.close()


def draft_id(response: str) -> str:
    assert not response.startswith("Error:"), response
    match = re.search(r"\b[0-9a-f]{8}\b", response)
    assert match, response
    return match[0]


def collect_pages(service: CommandService, actor: str, first_page: str, budget: int = 160) -> str:
    result = []
    page = first_page
    for _ in range(2048):
        assert len(page.encode("utf-8")) <= budget
        assert "\ufffd" not in page
        if not page.endswith("\n[more]"):
            return "".join([*result, page])
        result.append(page.removesuffix("\n[more]"))
        page = service.handle(actor, "more", max_bytes=budget)
    pytest.fail("paging did not reach the end of a bounded post")


@pytest.mark.parametrize("budget", [64, 160, 8192])
@pytest.mark.parametrize(
    "body",
    [
        "First paragraph. " * 120 + "\nSecond paragraph.\n\nLast line.",
        "あ" * 401 + " 🛜 café " * 40,
        "x" * 700,
    ],
)
def test_read_paging_keeps_all_body_characters_with_utf8_and_footer_budget(
    service: CommandService, budget: int, body: str
) -> None:
    post = service.store.publish(ALICE, "read", "general", "Colorado update", body)
    first = service.handle(ALICE, f"read {post.post_id[:12]}", max_bytes=budget)
    combined = collect_pages(service, ALICE, first, budget)
    assert combined == f"{post.post_id[:12]} {post.title}\n{body}"
    assert service.handle(ALICE, "more", max_bytes=budget).startswith("End.")


def test_maximum_body_is_readable_on_small_radio(service: CommandService) -> None:
    body = "é" * (MAX_BODY_BYTES // 2)
    post = service.store.publish(ALICE, "large", "general", "Long newsletter", body)
    first = service.handle(ALICE, f"read {post.post_id}", max_bytes=64)
    assert collect_pages(service, ALICE, first, 64) == f"{post.post_id[:12]} {post.title}\n{body}"


def test_read_cursor_survives_restart_and_does_not_mix_post_revisions(tmp_path: Path) -> None:
    path = tmp_path / "persistent.db"
    first_store = Store(path, "colorado-mesh")
    first_service = CommandService(first_store)
    body = "Original paragraph. " * 100
    post = first_store.publish(ALICE, "post", "general", "Original title", body)
    first_page = first_service.handle(ALICE, f"read {post.post_id}")
    first_store.revise(ALICE, "correction", post.post_id, "New title", "Replacement body.")
    first_store.close()

    reopened = Store(path, "colorado-mesh")
    try:
        second_service = CommandService(reopened)
        assert collect_pages(second_service, ALICE, first_page) == (
            f"{post.post_id[:12]} Original title\n{body}"
        )
        assert second_service.handle(ALICE, f"read {post.post_id}") == (
            f"{post.post_id[:12]} New title\nReplacement body."
        )
    finally:
        reopened.close()


def test_distinct_actors_keep_independent_reading_positions(service: CommandService) -> None:
    a = service.store.publish(ALICE, "a", "general", "Alice post", "AAAA " * 100)
    b = service.store.publish(BOB, "b", "general", "Bob post", "BBBB " * 100)
    first_a = service.handle(ALICE, f"read {a.post_id}")
    first_b = service.handle(BOB, f"read {b.post_id}")
    assert collect_pages(service, ALICE, first_a) == f"{a.post_id[:12]} Alice post\n{a.body}"
    assert collect_pages(service, BOB, first_b) == f"{b.post_id[:12]} Bob post\n{b.body}"


def test_duplicate_more_replays_after_restart_without_advancing_twice(tmp_path: Path) -> None:
    path = tmp_path / "receipts.db"
    store = Store(path, "colorado-mesh")
    service = CommandService(store)
    post = store.publish(ALICE, "post", "general", "Pages", "Distinct numbered page. " * 100)
    first = service.handle(ALICE, f"read {post.post_id}", request_id="read-1")
    second = service.handle(ALICE, "more", request_id="more-1")
    assert service.handle(ALICE, "more", request_id="more-1") == second
    store.close()
    reopened = Store(path, "colorado-mesh")
    try:
        service = CommandService(reopened)
        assert service.handle(ALICE, "more", request_id="more-1") == second
        assert second.endswith("\n[more]")
        remainder = collect_pages(service, ALICE, service.handle(ALICE, "more"))
        assert first.removesuffix("\n[more]") + second.removesuffix("\n[more]") + remainder == (
            f"{post.post_id[:12]} Pages\n{post.body}"
        )
    finally:
        reopened.close()


def test_new_draft_retry_and_conflicting_operation_id_are_atomic(service: CommandService) -> None:
    first = service.handle(ALICE, "@new-1 new general Original")
    assert service.handle(ALICE, "@new-1 new general Original") == first
    assert service.handle(ALICE, "@new-1 new general Changed").startswith("Error:")
    rows = service.store.db.execute("SELECT title FROM drafts").fetchall()
    assert [row[0] for row in rows] == ["Original"]
    assert draft_id(service.handle(ALICE, "@new-2 new general Original")) != draft_id(first)


def test_duplicate_publish_creates_one_post_and_receipt(service: CommandService) -> None:
    draft = draft_id(service.handle(ALICE, "new general Meetup"))
    service.handle(ALICE, f"add {draft} 1 Meet Saturday at nine.")
    first = service.handle(ALICE, f"publish {draft}", request_id="publish-1")
    assert service.handle(ALICE, f"publish {draft}", request_id="publish-1") == first
    assert service.handle(ALICE, f"publish {draft}", request_id="publish-2") == first
    assert len(service.store.list_posts("general")) == 1
    assert service.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_meshtastic_packet_id_can_be_reused_after_ttl(
    service: CommandService, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr("mesh_bbs.commands.time.time", lambda: now[0])
    options = {"request_id": "0000a123", "request_ttl_seconds": 600}
    first = service.handle(BOB, "new general First", **options)
    assert service.handle(BOB, "new general First", **options) == first
    now[0] = 1599.0
    assert service.handle(BOB, "new general Second", **options).startswith("Error:")
    now[0] = 1601.0
    second = service.handle(BOB, "new general Second", **options)
    assert draft_id(second) != draft_id(first)
    assert service.handle(BOB, "new general Second", **options) == second


def test_explicit_operation_id_does_not_expire_with_transport_receipt(
    service: CommandService, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr("mesh_bbs.commands.time.time", lambda: now[0])
    first = service.handle(
        BOB, "@permanent new general Issue", request_id="radio-1", request_ttl_seconds=60
    )
    now[0] = 2000.0
    assert (
        service.handle(
            BOB, "@permanent new general Issue", request_id="radio-2", request_ttl_seconds=60
        )
        == first
    )
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1


def test_reply_creates_draft_before_publication_and_preserves_parent(
    service: CommandService,
) -> None:
    parent = service.store.publish(ALICE, "root", "general", "Meetup", "Saturday at nine")
    draft = draft_id(service.handle(BOB, f"reply {parent.post_id[:12]} I'll be there."))
    assert service.store.db.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
    preview = service.handle(BOB, f"preview {draft}")
    assert "I'll be there." in preview
    assert service.handle(BOB, f"publish {draft}").startswith("Saved locally")
    thread = service.store.list_posts("general", thread_id=parent.post_id)
    assert len(thread) == 2
    reply = next(post for post in thread if post.post_id != parent.post_id)
    assert reply.parent_id == reply.thread_id == parent.post_id
    assert reply.author == BOB
    nested = draft_id(service.handle(ALICE, f"reply {reply.post_id} Great!"))
    service.handle(ALICE, f"publish {nested}")
    children = service.store.list_posts("general", thread_id=parent.post_id)
    assert any(p.parent_id == reply.post_id and p.thread_id == parent.post_id for p in children)


def test_news_is_reserved_for_imports_including_replies(service: CommandService) -> None:
    issue = service.store.import_article("colorado", "september", "news", "September", "Updates")
    for actor in (BOB, EDITOR):
        for command in (
            "new news Forged",
            "post news Fake | Forged",
            f"reply {issue.post_id} Thanks",
        ):
            assert "read-only" in service.handle(actor, command)
    assert service.store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
    assert "September" in service.handle(BOB, "news")
    assert "Updates" in service.handle(BOB, "news latest")


def test_legacy_news_draft_cannot_publish_after_upgrade(service: CommandService) -> None:
    draft = service.store.new_draft(EDITOR, "general", "Old draft")
    service.store.add_part(EDITOR, draft, 1, "Content")
    service.store.db.execute("UPDATE drafts SET board='news' WHERE draft_id=?", (draft,))
    assert "read-only" in service.handle(EDITOR, f"publish {draft}")
    assert not service.store.list_posts("news")
    assert service.store.draft(EDITOR, draft)[0]["published"] is None


def test_other_actor_cannot_read_modify_publish_or_discard_draft(service: CommandService) -> None:
    draft = draft_id(service.handle(ALICE, "new general Private draft"))
    service.handle(ALICE, f"add {draft} 1 Original")
    for command in (
        f"preview {draft}",
        f"add {draft} 2 Hijack",
        f"publish {draft}",
        f"discard {draft}",
    ):
        assert service.handle(BOB, command).startswith("Error:")
    assert service.store.draft(ALICE, draft)[1] == "Original"
    assert not service.store.list_posts("general")
    assert service.handle(ALICE, f"discard {draft}") == "Draft discarded."
    assert service.handle(ALICE, f"preview {draft}").startswith("Error:")


def test_gapped_draft_can_be_discarded(service: CommandService) -> None:
    draft = draft_id(service.handle(ALICE, "new general Interrupted upload"))
    assert service.handle(ALICE, f"add {draft} 2 Missing first chunk").startswith("Saved part")
    assert service.handle(ALICE, f"preview {draft}").startswith("Error:")
    assert service.handle(ALICE, f"discard {draft}") == "Draft discarded."
    assert service.store.db.execute("SELECT count(*) FROM draft_parts").fetchone()[0] == 0


@pytest.mark.parametrize("budget", [64, 160])
def test_small_budget_supports_complete_create_preview_publish_flow(
    service: CommandService, budget: int
) -> None:
    def command(text: str) -> str:
        reply = service.handle(ALICE, text, max_bytes=budget)
        assert len(reply.encode()) <= budget
        assert not reply.startswith("Error:"), reply
        return reply

    draft = draft_id(command("new general Radio post"))
    command(f"add {draft} 1 UTF-8 café あ 🛜")
    assert "café" in command(f"preview {draft}")
    assert command(f"publish {draft}").startswith("Saved locally")
    post = service.store.list_posts("general")[0]
    assert "Radio post" in command(f"read {post.post_id}")
    denied = service.handle(ALICE, f"discard {draft}", max_bytes=budget)
    assert denied.startswith("Error:")
    assert len(denied.encode()) <= budget
    assert service.store.get_post(post.post_id) == post


def test_thread_command_exposes_reply_ids_and_accepts_nested_reply_id(
    service: CommandService,
) -> None:
    root = service.store.publish(ALICE, "root", "general", "A threaded topic", "Root body")
    reply = service.store.publish(
        BOB, "reply", "general", "A response", "Reply body", parent_id=root.post_id
    )
    nested = service.store.publish(
        ALICE, "nested", "general", "A nested response", "Nested body", parent_id=reply.post_id
    )
    listing = collect_pages(
        service, BOB, service.handle(BOB, f"thread {nested.post_id[:12]}", max_bytes=64), 64
    )
    for post in (root, reply, nested):
        assert post.post_id[:12] in listing
        assert post.title in listing
    assert "Reply body" in service.handle(ALICE, f"read {reply.post_id[:12]}")


def test_removed_post_is_marked_in_thread_and_read_does_not_return_body(
    service: CommandService,
) -> None:
    root = service.store.publish(ALICE, "root", "general", "Topic", "Public body")
    reply = service.store.publish(
        BOB, "reply", "general", "Removed title", "Removed body", parent_id=root.post_id
    )
    service.store.revise(BOB, "remove", reply.post_id, "Removed title", "", remove=True)
    listing = collect_pages(service, ALICE, service.handle(ALICE, f"thread {root.post_id}"))
    assert f"{reply.post_id[:12]} [removed]" in listing
    assert "Removed title" not in listing
    assert service.handle(ALICE, f"read {reply.post_id}") == "This post has been removed."


@pytest.mark.parametrize(
    "command",
    [
        "invalid",
        "new unknown Topic",
        "new general",
        "read xyz",
        "read " + "a" * 64,
        "add",
        "add abc nope text",
        "publish unknown",
        "@bad! help",
        "@missing-command",
        "new general " + "é" * 129,
        "x" * (MAX_BODY_BYTES + 513),
    ],
)
def test_invalid_commands_return_bounded_errors(service: CommandService, command: str) -> None:
    response = service.handle(ALICE, command, max_bytes=64)
    assert response.startswith("Error:")
    assert len(response.encode()) <= 64


@pytest.mark.parametrize("budget", [0, 63, 8193])
def test_invalid_response_budget_is_rejected(service: CommandService, budget: int) -> None:
    with pytest.raises(BBSError, match="Response limit"):
        service.handle(ALICE, "commands", max_bytes=budget)


def test_help_boards_and_empty_states_fit_small_budget(service: CommandService) -> None:
    assert service.handle(ALICE, "more", max_bytes=64).startswith("No open page")
    help_text = collect_pages(service, ALICE, service.handle(ALICE, "commands", max_bytes=64), 64)
    assert "new BOARD TITLE" in help_text and "publish DRAFT" in help_text
    assert "Send one command per DM" in help_text
    assert "Send more for the next page" in help_text
    assert "threads BOARD: newest threads" in help_text
    assert "read ID: full post" in help_text
    assert "general" in service.handle(ALICE, "boards", max_bytes=64)
    assert service.handle(ALICE, "threads general", max_bytes=64) == "No threads yet."
    assert service.handle(ALICE, "news", max_bytes=64).startswith("No posts here yet")


def test_unknown_board_is_an_error_instead_of_an_empty_listing(service: CommandService) -> None:
    assert service.handle(ALICE, "threads misspelled-board").startswith("Error:")
