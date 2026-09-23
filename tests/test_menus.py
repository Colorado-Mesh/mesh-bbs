"""The same DM conversation works on every access protocol, even after restart."""

from pathlib import Path

import pytest

from mesh_bbs.commands import CommandService
from mesh_bbs.store import Store


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
