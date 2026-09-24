import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from mesh_bbs.announcements import MAX_AGE, QUEUE_LIMIT, AnnouncementOutbox
from mesh_bbs.commands import CommandService
from mesh_bbs.config import HostConfig, RadioConfig, load_config, save_config
from mesh_bbs.store import Grant, Store


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic", "lxmf"])
def test_notice_shortcut_reads_same_post_after_new_posts_and_restart(tmp_path, protocol):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    boxes = [AnnouncementOutbox(store, name) for name in ("meshcore", "meshtastic")]
    post = store.publish("local:alice", "original", "general", "Original post", "Hello! " * 50)
    actor = protocol + ":reader"
    for box in boxes:
        box.poll(time.time())
        text = box.take(time.time(), 139)
        assert text.endswith("private message: read #1")
        assert post.post_id[:12] not in text
    store.publish("local:bob", "newer", "general", "Something newer", "Different post")
    assert store.post_number(post.post_id) == 1
    store.close()
    store = Store(path, "test")
    try:
        service = CommandService(store)
        page = service.handle(actor, "read #1", request_id="read-notice")
        assert "Original post" in page and "next=keep reading" in page
        assert "replies=view replies" in page
        assert service.handle(actor, "read #1", request_id="read-notice") == page
        assert "Hello!" in service.handle(actor, "next")
        assert store.get_post("#1").post_id == post.post_id
        assert "Original post" in service.handle(actor, "read " + post.post_id[:12])
        assert "Unknown post number" in service.handle(actor, "read #999")
        store.remove("remove-original", post.post_id)
        assert "removed" in service.handle(actor, "read #1")
        newer = store.list_posts("general")[0]
        assert store.post_number(newer.post_id) != 1
        assert store.get_post("#1").deleted
    finally:
        store.close()


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic"])
@pytest.mark.parametrize("budget", [120, 139, 233])
def test_long_notice_labels_preserve_complete_usable_instructions(tmp_path, protocol, budget):
    board = "b" * 64
    store = Store(tmp_path / "bbs.db", "test", ("general", "news", board))
    try:
        box = AnnouncementOutbox(store, protocol)
        post = store.publish("local:a", "long", board, "🌲" * 60, "Body")
        box.poll(time.time())
        text = box.take(time.time(), budget)
        assert len(text.encode()) <= budget
        assert text.endswith("To read it, send me a private message: read #1")
        assert store.get_post(re.search(r"read (#[0-9]+)$", text)[1]).post_id == post.post_id
        assert "; more" not in text
    finally:
        store.close()


def test_initial_history_is_silent_but_all_new_boards_and_threads_are_noticed(tmp_path):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    store.publish("local:alice", "existing", "general", "Already here", "body")
    outbox = AnnouncementOutbox(store, "meshcore")
    now = time.time()
    assert not outbox.poll(now)
    store.import_article(
        "local:alice",
        "new",
        "news",
        "News issue",
        "body",
        published_at=datetime.now(UTC).isoformat(),
    )
    assert outbox.poll(now)
    text = outbox.take(now, 128)
    assert text == 'New post in news:\n"News issue"\nTo read it, send me a private message: read #1'
    store.close()
    store = Store(path, "test", ("general", "news", "events"))
    outbox = AnnouncementOutbox(store, "meshcore")
    assert outbox.poll(now + 601)
    assert outbox.take(now + 601, 128) == (
        "New board: events\nTo browse it, send me a private message: boards"
    )
    store.publish("local:alice", "event", "events", "Picnic", "body")
    assert outbox.poll(now + 1202)
    assert 'New post in events:\n"Picnic"' in outbox.take(now + 1202, 128)
    store.close()


def test_edits_replies_repeated_sync_and_restart_do_not_repeat_notices(tmp_path):
    a = Store(tmp_path / "a.db", "test")
    b = Store(tmp_path / "b.db", "test")
    b.grants[a.origin] = Grant(a.public_key, frozenset(b.boards))
    box = AnnouncementOutbox(b, "meshcore")
    post = a.publish("local:alice", "first", "general", "First", "body")
    events = a.db.execute("SELECT payload FROM events").fetchall()
    import json

    from mesh_bbs.events import Event

    original = Event.from_dict(json.loads(events[0][0]))
    b.accept(original)
    now = time.time()
    assert box.poll(now)
    assert "read #1" in box.take(now, 128)
    assert b.get_post("#1").post_id == post.post_id
    b.accept(original)
    b.publish("local:bob", "reply", "general", "Reply", "body", parent_id=post.post_id)
    b.close()
    b = Store(tmp_path / "b.db", "test")
    box = AnnouncementOutbox(b, "meshcore")
    assert not box.poll(now + 601)
    assert box.take(now + 601, 128) is None
    a.close()
    b.close()


def test_utf8_title_budget_preserves_read_instructions_and_does_not_announce_edits(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshcore")
    post = store.publish("local:alice", "one", "general", "🌲" * 60, "body")
    now = time.time()
    box.poll(now)
    text = box.take(now, 126)
    assert len(text.encode()) <= 126
    assert text.endswith("To read it, send me a private message: read #1")
    assert '..."' in text
    store.revise("local:alice", "edit", post.post_id, "Edited", "new body")
    assert not box.poll(now + 601)
    store.close()


def test_removed_posts_old_imports_and_expired_backlogs_are_silent(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshtastic")
    now = time.time()
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    store.import_article("feed", "old", "news", "Archived issue", "body", published_at=old)
    post = store.publish("local:alice", "one", "general", "Deleted", "body")
    assert box.poll(now)
    store.remove("remove", post.post_id)
    assert box.take(now, 160) is None
    store.publish("local:alice", "two", "general", "Stale backlog", "body")
    box.poll(time.time())
    assert box.take(now + MAX_AGE + 2, 160) is None
    store.close()


def test_backlog_is_bounded_and_send_attempt_is_durable(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshcore")
    now = time.time()
    for n in range(QUEUE_LIMIT + 4):
        store.publish("local:a", str(n), "general", f"Post {n}", "body")
    box.poll(time.time())
    assert store.db.execute("SELECT count(*) FROM announcement_queue").fetchone()[0] == QUEUE_LIMIT
    assert '"Post 4"\n' in box.take(now, 128)
    box = AnnouncementOutbox(store, "meshcore")
    assert box.take(now + 1, 128) is None
    assert '"Post 5"\n' in box.take(now + 601, 128)
    store.close()


def test_announcement_config_requires_explicit_owner_and_channel(tmp_path):
    with pytest.raises(ValueError):
        RadioConfig(announcement_channel=1)
    with pytest.raises(ValueError):
        RadioConfig(
            announcement_owner="auto", announcement_channel=1, announcement_channel_name="#bbs"
        )
    radio = RadioConfig(
        announcement_owner="a" * 64, announcement_channel=1, announcement_channel_name="#bbs"
    )
    config = HostConfig("Test", "test", tmp_path / "data", meshcore=radio)
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path) == config
    with pytest.raises(ValueError):
        replace(config, meshtastic=replace(radio, announcement_channel=8))
    assert RadioConfig().announcement_owner is None


@pytest.mark.asyncio
async def test_uncertain_channel_send_is_not_replayed_and_reservation_finishes(tmp_path):
    import asyncio

    from mesh_bbs.airtime import AirtimeLimiter

    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshcore")
    store.publish("local:a", "one", "general", "One", "Body")
    limiter = AirtimeLimiter(tmp_path / "airtime.db", "test:meshcore")
    attempted = asyncio.Event()

    async def prepare():
        return 128

    async def send(text):
        attempted.set()
        raise ConnectionError("Serial response was lost")

    task = asyncio.create_task(box.run(limiter, prepare, send))
    try:
        await asyncio.wait_for(attempted.wait(), 1)
        assert not box.poll(time.time())
        assert box.take(time.time() + 601, 128) is None
        assert (
            limiter._db.execute(
                "SELECT count(*) FROM reservations WHERE expires IS NULL"
            ).fetchone()[0]
            == 0
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        limiter.close()
        store.close()


def test_invalid_replicated_timestamp_cannot_stall_later_notices(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshcore")
    bad = store.publish("local:a", "bad", "general", "Bad timestamp", "body")
    # Model a signed peer event whose bounded date string is not parseable.
    import json

    payload = json.loads(
        store.db.execute("SELECT payload FROM posts WHERE post_id=?", (bad.post_id,)).fetchone()[0]
    )
    payload["created_at"] = "not a timestamp"
    store.db.execute(
        "UPDATE posts SET payload=? WHERE post_id=?", (json.dumps(payload), bad.post_id)
    )
    good = store.publish("local:a", "good", "general", "Valid", "body")
    assert box.poll(time.time())
    assert "read #1" in box.take(time.time(), 128)
    assert store.get_post("#1").post_id == good.post_id
    assert box.take(time.time() + 601, 128) is None
    store.close()


def test_same_feed_item_imported_by_two_hosts_announces_once(tmp_path):
    import json

    from mesh_bbs.events import Event

    a = Store(tmp_path / "a.db", "test")
    b = Store(tmp_path / "b.db", "test")
    b.grants[a.origin] = Grant(a.public_key, frozenset(b.boards))
    box = AnnouncementOutbox(b, "meshcore")
    stamp = datetime.now(UTC).isoformat()
    first = b.import_article("blog", "issue", "news", "Issue", "Body", published_at=stamp)
    a.import_article("blog", "issue", "news", "Issue", "Body", published_at=stamp)
    box.poll(time.time())
    assert "read #1" in box.take(time.time(), 128)
    assert b.get_post("#1").post_id == first.post_id
    for row in a.db.execute("SELECT payload FROM events"):
        assert b.accept(Event.from_dict(json.loads(row[0])))
    assert not box.poll(time.time() + 601)
    assert box.take(time.time() + 601, 128) is None
    a.close()
    b.close()
