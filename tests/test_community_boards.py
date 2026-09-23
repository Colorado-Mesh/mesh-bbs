import json

import pytest

from mesh_bbs.events import MAX_BOARDS, BBSError, Event
from mesh_bbs.federation import FederationService
from mesh_bbs.store import Grant, Store


def test_board_creation_is_persistent_idempotent_and_bounded(tmp_path):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    try:
        assert store.create_board("a", "hiking") == "hiking"
        assert store.create_board("b", "hiking") == "hiking"
        assert store.list_posts("hiking") == []
        assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        for bad in ("", "news", "News", "../hiking", "x" * 65):
            with pytest.raises(BBSError):
                store.create_board("a", bad)
        for number in range(7):
            store.create_board("a", f"board-{number}")
        with pytest.raises(BBSError, match="limit"):
            store.create_board("a", "ninth")
        for number in range(MAX_BOARDS - len(store.boards)):
            store.create_board(f"b-{number}", f"extra-{number}")
        with pytest.raises(BBSError, match="32-board"):
            store.create_board("new-actor", "overflow")
    finally:
        store.close()
    reopened = Store(path, "test")
    assert "hiking" in reopened.boards
    reopened.close()


async def test_new_boards_and_posts_sync_only_with_explicit_community_grant(tmp_path):
    source, target, restricted = [Store(tmp_path / f"{name}.db", "test") for name in "abc"]
    try:
        target.grants[source.origin] = Grant(source.public_key, frozenset({"*"}))
        restricted.grants[source.origin] = Grant(source.public_key, frozenset({"general"}))
        source.create_board("meshcore:a", "hiking")
        source.create_board("meshtastic:b", "empty-board")
        post = source.publish("meshtastic:b", "one", "hiking", "Meetup", "Saturday")
        events = [
            Event.from_dict(json.loads(row[0]))
            for row in source.db.execute("SELECT payload FROM events ORDER BY rowid DESC")
        ]
        # A post may reach a peer before the corresponding board event.
        assert target.accept(events[0])
        for event in events:
            with pytest.raises(BBSError):
                restricted.accept(event)
        upstream = FederationService(source, {"bb" * 16: frozenset({"*"})})
        downstream = FederationService(target, {"aa" * 16: frozenset({"*"})})

        async def request(payload):
            return upstream.handle_request("bb" * 16, payload)

        first = await downstream.pull_peer("aa" * 16, request)
        assert first.accepted_events == 2
        assert (await downstream.pull_peer("aa" * 16, request)).accepted_events == 0
        assert "empty-board" in target.boards
        assert target.get_post(post.post_id) == post
        assert "hiking" not in restricted.boards
        restricted.grants[source.origin] = Grant(source.public_key, frozenset({"*"}))
        service = FederationService(restricted, {"aa" * 16: frozenset({"general"})})
        assert (await service.pull_peer("aa" * 16, request)).accepted_events == 0
        assert "hiking" not in restricted.boards
    finally:
        for store in (source, target, restricted):
            store.close()
