"""MeshCore retry fields must identify a command across delivery and restart."""

from mesh_bbs.adapters.meshcore import parse_meshcore_message
from mesh_bbs.commands import CommandService
from mesh_bbs.store import Store

KEY = "aabbccddeeff" + "01" * 26
ACTOR = "meshcore:" + KEY


def receive(service, text, timestamp, key=KEY):
    message = parse_meshcore_message(
        dict(
            type="PRIV", txt_type=0, pubkey_prefix=key[:12], sender_timestamp=timestamp, text=text
        ),
        {key: {"public_key": key}},
    )
    return service.handle(
        message.sender, message.text, request_id=message.message_id, request_ttl_seconds=86400
    )


def test_restart_retry_cannot_append_to_draft_or_advance_reader(tmp_path):
    path = tmp_path / "bbs.db"
    store = Store(path, "test")
    try:
        service = CommandService(store)
        post = store.publish(ACTOR, "post", "general", "Testing", "Text " * 100)
        service.handle(ACTOR, f"read #{store.post_number(post.post_id)}")
        prompt = receive(service, "reply", 122)
        saved = receive(service, "You passed the test", 123)
        store.close()
        store = Store(path, "test")
        service = CommandService(store)
        for _ in range(5):
            assert receive(service, "You passed the test", 123) == saved
        assert receive(service, "reply", 122) == prompt
        assert store.db.execute("SELECT count(*) FROM draft_parts").fetchone()[0] == 1
        assert receive(service, "Different text in the same second", 123).startswith("Part 2")
        assert receive(service, "You passed the test", 124).startswith("Part 3")
        # Delayed reads must not reset or advance the current reading position.
        service.handle(ACTOR, "menu")
        first = receive(service, f"read {post.post_id}", 200)
        second = receive(service, "more", 201)
        position = store.db.execute(
            "SELECT position FROM cursors WHERE actor=?", (ACTOR,)
        ).fetchone()[0]
        assert receive(service, "more", 201) == second
        assert receive(service, f"read {post.post_id}", 200) == first
        assert (
            store.db.execute("SELECT position FROM cursors WHERE actor=?", (ACTOR,)).fetchone()[0]
            == position
        )
    finally:
        store.close()


def test_retry_receipt_expiry_uses_arrival_time_not_sender_clock(tmp_path, monkeypatch):
    now = [100000.0]
    monkeypatch.setattr("mesh_bbs.commands.time.time", lambda: now[0])
    store = Store(tmp_path / "bbs.db", "test")
    try:
        service = CommandService(store)
        command = "post general Hello | World"
        saved = receive(service, command, 0)
        assert (
            store.db.execute("SELECT expires FROM command_receipts").fetchone()[0] == now[0] + 86400
        )
        now[0] += 86399
        assert receive(service, command, 0) == saved
        assert len(store.list_posts("general")) == 1
        # A reused timestamp after the bounded window is a new operation.
        now[0] += 2
        assert receive(service, command, 0) != saved
        assert len(store.list_posts("general")) == 2
    finally:
        store.close()


def test_same_text_and_timestamp_from_other_sender_and_explicit_ids(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        service = CommandService(store)
        command = "post general Hello | World"
        receive(service, command, 100)
        receive(service, command, 100, key="11" * 32)
        assert len(store.list_posts("general")) == 2
        first = receive(service, "@one " + command, 100)
        assert receive(service, "@one " + command, 101) == first
        receive(service, "@two " + command, 100)
        assert len(store.list_posts("general")) == 4
    finally:
        store.close()
