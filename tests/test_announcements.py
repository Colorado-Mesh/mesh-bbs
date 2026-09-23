import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from mesh_bbs.announcements import MAX_AGE, QUEUE_LIMIT, AnnouncementOutbox
from mesh_bbs.config import HostConfig, RadioConfig, load_config, save_config
from mesh_bbs.store import Grant, Store


def test_initial_history_is_silent_but_all_new_boards_and_threads_are_noticed(tmp_path):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    store.publish("local:alice", "existing", "general", "Already here", "body")
    outbox = AnnouncementOutbox(store, "meshcore")
    now = time.time()
    assert not outbox.poll(now)
    post = store.import_article(
        "local:alice",
        "new",
        "news",
        "News issue",
        "body",
        published_at=datetime.now(UTC).isoformat(),
    )
    assert outbox.poll(now)
    text = outbox.take(now, 128)
    assert text == f"New [news] News issue\nDM this node: read {post.post_id[:12]}; more"
    store.close()
    store = Store(path, "test", ("general", "news", "events"))
    outbox = AnnouncementOutbox(store, "meshcore")
    assert outbox.poll(now + 601)
    assert outbox.take(now + 601, 128) == "New BBS board. DM this node: threads events; more"
    store.publish("local:alice", "event", "events", "Picnic", "body")
    assert outbox.poll(now + 1202)
    assert "New [events] Picnic" in outbox.take(now + 1202, 128)
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
    assert post.post_id[:12] in box.take(now, 128)
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
    assert text.endswith(f"DM this node: read {post.post_id[:12]}; more")
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
    assert "Post 4\n" in box.take(now, 128)
    box = AnnouncementOutbox(store, "meshcore")
    assert box.take(now + 1, 128) is None
    assert "Post 5\n" in box.take(now + 601, 128)
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
    assert good.post_id[:12] in box.take(time.time(), 128)
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
    assert first.post_id[:12] in box.take(time.time(), 128)
    for row in a.db.execute("SELECT payload FROM events"):
        assert b.accept(Event.from_dict(json.loads(row[0])))
    assert not box.poll(time.time() + 601)
    assert box.take(time.time() + 601, 128) is None
    a.close()
    b.close()
