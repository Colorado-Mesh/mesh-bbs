"""The same DM conversation works on every access protocol, even after restart."""

from pathlib import Path

import pytest

from mesh_bbs.commands import CommandService
from mesh_bbs.store import Store


@pytest.mark.parametrize("replacement", ["menu", "boards", "threads general", "commands"])
def test_replies_does_not_open_a_stale_post(tmp_path, replacement):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        post = store.publish("author", "root", "general", "Running", "Morning run")
        service = CommandService(store)
        assert "Open a post first" in service.handle("reader", "replies")
        service.handle("reader", f"read {post.post_id}")
        service.handle("reader", replacement)
        assert "Open a post first" in service.handle("reader", "replies")
    finally:
        store.close()


def test_legacy_replies_survives_paging_and_restart(tmp_path):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    try:
        post = store.publish("author", "root", "general", "Running", "Morning run " * 100)
        service = CommandService(store)
        service.handle("reader", f"read {post.post_id}")
        service.handle("reader", "more")
    finally:
        store.close()
    store = Store(path, "test")
    try:
        service = CommandService(store)
        assert "Original: Running" in service.handle("reader", "replies")
        for text in ("help", "3", "1", "Title", "replies"):
            service.handle("reader", text)
        assert store.db.execute("SELECT body FROM draft_parts").fetchone()[0] == "replies"
    finally:
        store.close()


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic", "lxmf"])
@pytest.mark.parametrize("opening", ["menu", "number", "hex"])
def test_replies_are_discoverable_and_readable_from_every_post_entry(tmp_path, protocol, opening):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        root = store.publish("author", "root", "general", "Running", "Morning run")
        store.publish(
            "runner", "reply", "general", "Re: Running", "Meet at the park", parent_id=root.post_id
        )
        service = CommandService(store)
        actor = protocol + ":reader"
        if opening == "menu":
            service.handle(actor, "boards")
            service.handle(actor, "1")
            page = service.handle(actor, "1")
        else:
            identifier = (
                f"#{store.post_number(root.post_id)}" if opening == "number" else root.post_id[:12]
            )
            page = service.handle(actor, f"read {identifier}")
        if opening != "hex":
            assert "replies=" in page
        listing = service.handle(actor, "replies")
        assert "Original:" in listing and "Re: Running" in listing
        assert "Meet at the park" in service.handle(actor, "2")
        assert "Re: Running" in service.handle(actor, "back")
    finally:
        store.close()


@pytest.mark.parametrize("budget", [64, 160, 4096])
def test_long_post_offers_replies_before_last_page_without_changing_draft(tmp_path, budget):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        root = store.publish("author", "root", "general", "Long post", "é" * 5000)
        store.publish("runner", "reply", "general", "Reply", "A response", parent_id=root.post_id)
        service = CommandService(store)
        for text in ("help", "3", "1", "My draft", "My text"):
            service.handle("reader", text, max_bytes=budget)
        page = service.handle(
            "reader", f"read #{store.post_number(root.post_id)}", max_bytes=budget
        )
        assert "replies" in page and len(page.encode()) <= budget
        listing = service.handle("reader", "replies", max_bytes=budget)
        assert len(listing.encode()) <= budget
        service.handle("reader", "menu", max_bytes=budget)
        assert "text" in service.handle("reader", "3", max_bytes=budget)
        assert store.db.execute("SELECT body FROM draft_parts").fetchone()[0] == "My text"
    finally:
        store.close()


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic", "lxmf"])
@pytest.mark.parametrize("budget", [64, 160, 4096])
def test_guided_create_board_post_resume_and_publish(tmp_path: Path, protocol: str, budget: int):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    actor = protocol + ":alice"
    service = CommandService(store)
    sequence = 0

    def send(text):
        nonlocal sequence
        sequence += 1
        response = service.handle(actor, text, request_id=str(sequence), max_bytes=budget)
        assert not response.startswith("Error:"), (text, response)
        assert len(response.encode()) <= budget
        # Retransmitting a protocol operation neither advances a page nor appends text twice.
        assert service.handle(actor, text, request_id=str(sequence), max_bytes=budget) == response
        return response

    try:
        assert "1 News" in send("help")
        assert "general" in send("3")
        assert "board name" in send("2")
        assert "title" in send("Trail Reports")
        assert "trail-reports" in store.boards
        assert "text" in send("Saturday hike")
        assert "saved" in send("Meet at nine. ééé")
        assert "Review first" in send("publish")
        assert not store.list_posts("trail-reports")
        store.close()
        store = Store(path, "test")
        service = CommandService(store)
        send("menu")
        assert "text" in send("3")
        assert "saved" in send("Bring water.")
        page = send("done")
        preview = page
        for _ in range(20):
            if "publish |" in page:
                break
            page = send("next")
            preview += page
        else:
            pytest.fail("Preview did not finish")
        assert "Saturday hike" in preview
        assert "Posted" in send("publish")
        assert "Already posted" in send("publish")
        posts = store.list_posts("trail-reports")
        assert len(posts) == 1
        assert posts[0].body == "Meet at nine. ééé\nBring water."
        assert posts[0].author == actor
    finally:
        store.close()


def test_menu_reading_snapshot_back_and_read_only_news(tmp_path):
    with_store = Store(tmp_path / "bbs.db", "test")
    try:
        store = with_store
        issue = store.import_article("colorado", "issue", "news", "Newsletter", "Full article")
        service = CommandService(store)
        actor = "meshtastic:alice"
        assert "Newsletter" in service.handle(actor, "news")
        store.import_article("colorado", "newer", "news", "Newer", "Another article")
        assert "Full article" in service.handle(actor, "1")
        assert "read-only" in service.handle(actor, "reply")
        assert "Newsletter" in service.handle(actor, "back")
        assert "Newer" not in service.handle(actor, "1")
        store.remove("removed", issue.post_id)
        assert "changed or removed" in service.handle(actor, "next")
        service.handle(actor, "menu")
        assert "news" not in service.handle(actor, "3").lower()
    finally:
        with_store.close()


def test_create_board_from_browse_and_keep_other_senders_separate(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        service = CommandService(store)
        assert "Create" in service.handle("a", "boards")
        assert "board name" in service.handle("a", "3")
        assert "title" in service.handle("a", "hiking")
        assert "Welcome" in service.handle("b", "help")
        assert "text" in service.handle("a", "Title")
        service.handle("a", "Body")
        service.handle("a", "cancel")
        assert not store.list_posts("hiking")
        assert store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
    finally:
        store.close()


def test_draft_resume_after_menu_expiry(tmp_path, monkeypatch):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        service = CommandService(store)
        for text in ("help", "3", "1", "Title", "Body"):
            assert not service.handle("a", text).startswith("Error")
        monkeypatch.setattr("mesh_bbs.menus.time.time", lambda: 9999999999)
        service.handle("a", "menu")
        assert "text" in service.handle("a", "3")
        service.handle("a", "done")
        assert "Posted" in service.handle("a", "publish")
    finally:
        store.close()


def test_notice_shortcut_preserves_an_unfinished_post(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        post = store.publish("local:b", "other", "general", "A notice", "Read this")
        number = store.post_number(post.post_id)
        service = CommandService(store)
        for text in ("help", "3", "1", "My title", "My first paragraph"):
            assert not service.handle("a", text).startswith("Error")
        assert "Read this" in service.handle("a", f"read #{number}")
        service.handle("a", "menu")
        assert "text" in service.handle("a", "3")
        service.handle("a", "My second paragraph")
        service.handle("a", "done")
        assert "Posted" in service.handle("a", "publish")
        saved = next(p for p in store.list_posts("general") if p.author == "a")
        assert saved.title == "My title"
        assert saved.body == "My first paragraph\nMy second paragraph"
    finally:
        store.close()
