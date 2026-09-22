from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mesh_bbs.events import MAX_BODY_BYTES, BBSError, Event, canonical
from mesh_bbs.store import Grant, Post, Store

BOARDS = frozenset({"general", "news"})
ALICE = "meshcore:alice"


@pytest.fixture
def hosts(tmp_path: Path) -> Iterator[tuple[Store, Store, Store]]:
    stores = tuple(Store(tmp_path / f"host-{number}.db", "colorado-mesh") for number in range(3))
    for destination in stores:
        destination.grants = {
            source.origin: Grant(source.public_key, BOARDS)
            for source in stores
            if source is not destination
        }
    yield stores
    for store in stores:
        store.close()


def events(store: Store) -> list[Event]:
    ids = store.inventory(BOARDS)
    result = []
    for offset in range(0, len(ids), 32):
        result.extend(
            Event.from_dict(value) for value in store.export(ids[offset : offset + 32], BOARDS)
        )
    return result


def event_for(store: Store, post: Post, kind: str) -> Event:
    matches = [e for e in events(store) if e.post_id == post.post_id and e.kind == kind]
    assert len(matches) == 1
    return matches[0]


def resign(store: Store, event: Event, **changes: Any) -> Event:
    changed = replace(event, **changes)
    data = canonical(changed.unsigned())
    return replace(
        changed, event_id=hashlib.sha256(data).hexdigest(), signature=store.key.sign(data).hex()
    )


def test_restart_preserves_identity_content_and_operation_receipts(tmp_path: Path) -> None:
    path = tmp_path / "host.db"
    first = Store(path, "colorado-mesh")
    post = first.publish(ALICE, "op-1", "general", "A title", "A body")
    origin, public_key = first.origin, first.public_key
    first.close()
    restarted = Store(path, "colorado-mesh")
    try:
        assert (restarted.origin, restarted.public_key) == (origin, public_key)
        assert restarted.publish(ALICE, "op-1", "general", "A title", "A body") == post
        assert len(events(restarted)) == 1
        assert restarted.get_post(post.post_id[:8]) == post
    finally:
        restarted.close()


def test_region_mismatch_does_not_reassign_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "host.db"
    store = Store(path, "colorado-mesh")
    origin = store.origin
    store.close()
    with pytest.raises(BBSError, match="another region"):
        Store(path, "other-mesh")
    reopened = Store(path, "colorado-mesh")
    try:
        assert reopened.origin == origin
    finally:
        reopened.close()


def test_concurrent_operation_retry_creates_one_event(hosts: tuple[Store, Store, Store]) -> None:
    store, _, _ = hosts
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.publish(ALICE, "lost-ack", "general", "Update", "Hello"), range(32)
            )
        )
    assert len({post.post_id for post in results}) == 1
    assert len(events(store)) == 1
    assert len(store.list_posts("general")) == 1


def test_concurrent_connections_share_dedupe_transaction(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    first, second = Store(path, "colorado-mesh"), Store(path, "colorado-mesh")
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda number: (first if number % 2 else second).publish(
                        ALICE, "same-request", "news", "Issue 9", "Network updates"
                    ),
                    range(32),
                )
            )
        assert len({post.post_id for post in results}) == 1
        assert len(events(first)) == 1
    finally:
        first.close()
        second.close()


def test_same_body_distinct_operations_and_actors_are_not_deduplicated(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    posts = [
        store.publish(ALICE, "one", "general", "Thanks", "Thank you"),
        store.publish(ALICE, "two", "general", "Thanks", "Thank you"),
        store.publish("meshtastic:bob", "one", "general", "Thanks", "Thank you"),
    ]
    assert len({post.post_id for post in posts}) == 3
    assert len(events(store)) == 3


def test_operation_id_reuse_with_changed_content_is_rejected(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    original = store.publish(ALICE, "one", "general", "Title", "Original")
    with pytest.raises(BBSError, match="different content"):
        store.publish(ALICE, "one", "general", "Title", "Changed")
    assert store.get_post(original.post_id) == original
    assert len(events(store)) == 1


def test_publication_and_receipt_roll_back_together(
    hosts: tuple[Store, Store, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, _ = hosts
    original_save = store.save_receipt

    def fail_after_receipt(*args: str) -> None:
        original_save(*args)
        raise sqlite3.OperationalError("simulated disk error")

    monkeypatch.setattr(store, "save_receipt", fail_after_receipt)
    with pytest.raises(sqlite3.OperationalError, match="disk error"):
        store.publish(ALICE, "retry", "general", "Title", "Body")
    assert events(store) == []
    assert store.list_posts("general") == []
    assert store.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    monkeypatch.setattr(store, "save_receipt", original_save)
    post = store.publish(ALICE, "retry", "general", "Title", "Body")
    assert store.get_post(post.post_id) == post
    assert len(events(store)) == 1


def test_replication_requires_trusted_signing_key_and_board_grant(
    hosts: tuple[Store, Store, Store],
) -> None:
    source, target, _ = hosts
    source.publish(ALICE, "one", "general", "Title", "Body")
    event = events(source)[0]
    for grant in (None, Grant("ff" * 32, BOARDS), Grant(source.public_key, frozenset({"news"}))):
        target.grants = {source.origin: grant} if grant else {}
        with pytest.raises(BBSError, match="not trusted"):
            target.accept(event)
        assert events(target) == []
    target.grants = {source.origin: Grant(source.public_key, frozenset({"general"}))}
    assert target.accept(event)
    assert not target.accept(event)
    assert target.get_post(event.post_id).body == "Body"


def test_invalid_signature_cannot_change_replica(hosts: tuple[Store, Store, Store]) -> None:
    source, target, _ = hosts
    source.publish(ALICE, "one", "general", "Title", "Body")
    event = events(source)[0]
    with pytest.raises(BBSError, match="signature"):
        target.accept(replace(event, signature="00" * 64))
    assert events(target) == []


def test_reply_before_parent_keeps_thread_links(hosts: tuple[Store, Store, Store]) -> None:
    source, target, _ = hosts
    root = source.publish(ALICE, "root", "general", "Saturday cleanup", "Meet at nine")
    reply = source.publish(
        "lxmf:bob", "reply", "general", "", "I'll be there", parent_id=root.post_id
    )
    target.accept(event_for(source, reply, "create"))
    received = target.get_post(reply.post_id)
    assert received.parent_id == received.thread_id == root.post_id
    with pytest.raises(BBSError, match="not available"):
        target.get_post(root.post_id)
    target.accept(event_for(source, root, "create"))
    assert [post.post_id for post in target.list_posts("general", thread_id=root.post_id)] == [
        root.post_id,
        reply.post_id,
    ]


def test_reply_to_reply_stays_in_original_thread(hosts: tuple[Store, Store, Store]) -> None:
    store, _, _ = hosts
    root = store.publish(ALICE, "root", "general", "Meetup", "Saturday")
    reply = store.publish(ALICE, "reply", "general", "", "Where?", parent_id=root.post_id)
    nested = store.publish(ALICE, "nested", "general", "", "Park", parent_id=reply.post_id)
    assert nested.thread_id == root.post_id
    assert nested.parent_id == reply.post_id
    with pytest.raises(BBSError, match="different board"):
        store.publish(ALICE, "wrong-board", "news", "", "Body", parent_id=root.post_id)


def test_removal_before_creation_is_retained_and_cannot_resurrect(
    hosts: tuple[Store, Store, Store],
) -> None:
    source, target, _ = hosts
    post = source.publish(ALICE, "post", "general", "Title", "Original")
    source.revise(ALICE, "correction", post.post_id, "Title", "Corrected")
    source.revise(ALICE, "remove", post.post_id, "Title", "", remove=True)
    creation = event_for(source, post, "create")
    revision = event_for(source, post, "revise")
    removal = event_for(source, post, "remove")
    assert target.accept(removal)
    target.accept(creation)
    assert target.get_post(post.post_id).deleted
    target.accept(revision)
    target.accept(resign(source, revision, clock=removal.clock + 1, body="Resurrection"))
    assert target.get_post(post.post_id).deleted
    assert target.get_post(post.post_id).body == ""
    assert target.list_posts("general") == []


def test_revision_order_converges_on_all_hosts(hosts: tuple[Store, Store, Store]) -> None:
    source, first, second = hosts
    post = source.publish(ALICE, "post", "general", "First title", "Original")
    source.revise(ALICE, "edit-one", post.post_id, "Second title", "Revision one")
    current = source.revise(ALICE, "edit-two", post.post_id, "Final title", "Revision two")
    history = sorted(events(source), key=lambda event: event.clock)
    for event in history:
        first.accept(event)
    for event in reversed(history):
        second.accept(event)
    assert source.get_post(post.post_id) == first.get_post(post.post_id)
    assert first.get_post(post.post_id) == second.get_post(post.post_id) == current


def test_other_author_and_same_actor_on_other_host_cannot_revise(
    hosts: tuple[Store, Store, Store],
) -> None:
    owner, peer, _ = hosts
    post = owner.publish(ALICE, "post", "general", "Title", "Original")
    peer.accept(event_for(owner, post, "create"))
    for store, actor in ((owner, "meshcore:bob"), (peer, ALICE)):
        with pytest.raises(BBSError, match="originating author"):
            store.revise(actor, "edit", post.post_id, "Title", "Changed")
    forged = resign(
        peer,
        event_for(owner, post, "create"),
        kind="revise",
        origin=peer.origin,
        public_key=peer.public_key,
        clock=100,
        body="Changed",
    )
    owner.accept(forged)
    assert owner.get_post(post.post_id) == post


def test_forged_creation_cannot_replace_another_hosts_post(
    hosts: tuple[Store, Store, Store],
) -> None:
    owner, attacker, replica = hosts
    original = owner.publish(ALICE, "post", "general", "Title", "Original")
    creation = event_for(owner, original, "create")
    replica.accept(creation)
    forged = resign(
        attacker,
        creation,
        origin=attacker.origin,
        public_key=attacker.public_key,
        body="Fake original",
        clock=100,
    )
    with pytest.raises(BBSError, match="identity"):
        replica.accept(forged)
    assert replica.get_post(original.post_id) == original


def test_peer_moderator_permission_only_allows_removal(
    hosts: tuple[Store, Store, Store],
) -> None:
    owner, moderator, replica = hosts
    post = owner.publish(ALICE, "post", "general", "Title", "Original")
    creation = event_for(owner, post, "create")
    replica.accept(creation)
    replica.grants[moderator.origin] = Grant(moderator.public_key, BOARDS, can_moderate=True)
    forged_edit = resign(
        moderator,
        creation,
        origin=moderator.origin,
        public_key=moderator.public_key,
        author="operator:moderator",
        kind="revise",
        clock=10,
        body="Changed",
    )
    replica.accept(forged_edit)
    assert replica.get_post(post.post_id).body == "Original"
    removal = resign(moderator, forged_edit, kind="remove", clock=11, body="")
    replica.accept(removal)
    assert replica.get_post(post.post_id).deleted


def test_feed_corrections_keep_identity_and_discussion_and_repeated_import_is_noop(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    original = store.import_article("newsletter", "issue-9", "news", "September", "First")
    reply = store.publish(ALICE, "reply", "news", "", "Great issue", parent_id=original.post_id)
    assert store.import_article("newsletter", "issue-9", "news", "September", "First") == original
    corrected = store.import_article("newsletter", "issue-9", "news", "September", "Correction")
    assert corrected.post_id == original.post_id
    assert corrected.revision_id != original.revision_id
    assert store.get_post(reply.post_id).parent_id == corrected.post_id
    count = len(events(store))
    repeated = store.import_article("newsletter", "issue-9", "news", "September", "Correction")
    assert repeated == corrected
    assert len(events(store)) == count
    reverted = store.import_article("newsletter", "issue-9", "news", "September", "First")
    assert reverted.body == "First"
    assert reverted.revision_id not in {original.revision_id, corrected.revision_id}


def test_independent_feed_imports_dedupe_across_hosts(hosts: tuple[Store, Store, Store]) -> None:
    first, second, replica = hosts
    a = first.import_article("newsletter", "issue-9", "news", "September", "Hello")
    b = second.import_article("newsletter", "issue-9", "news", "September", "Hello")
    assert a.post_id == b.post_id
    for source in (first, second):
        for event in events(source):
            replica.accept(event)
    assert len(replica.list_posts("news")) == 1
    assert replica.get_post(a.post_id).body == "Hello"
    distinct = first.import_article("newsletter", "issue-10", "news", "September", "Hello")
    assert distinct.post_id != a.post_id


def test_removed_newsletter_is_not_restored_by_reimport(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    post = store.import_article("newsletter", "issue-9", "news", "September", "Withdrawn")
    removed = store.revise(
        "feed:newsletter", "withdraw", post.post_id, "September", "", remove=True
    )
    assert store.import_article("newsletter", "issue-9", "news", "New title", "New body") == removed
    assert store.list_posts("news") == []
    assert len(events(store)) == 2


def test_revision_retry_and_removal_retry_commit_once(hosts: tuple[Store, Store, Store]) -> None:
    store, _, _ = hosts
    post = store.publish(ALICE, "post", "general", "Title", "Original")
    corrected = store.revise(ALICE, "edit", post.post_id, "Corrected", "New body")
    assert store.revise(ALICE, "edit", post.post_id, "Corrected", "New body") == corrected
    with pytest.raises(BBSError, match="different content"):
        store.revise(ALICE, "edit", post.post_id, "Corrected", "Different body")
    removed = store.revise(ALICE, "delete", post.post_id, "Corrected", "", remove=True)
    assert store.revise(ALICE, "delete", post.post_id, "Corrected", "", remove=True) == removed
    with pytest.raises(BBSError, match="cannot be restored"):
        store.revise(ALICE, "restore", post.post_id, "Corrected", "Bring back")
    assert len(events(store)) == 3


@pytest.mark.parametrize("body", ["   \n ", "é" * (MAX_BODY_BYTES // 2 + 1)])
def test_rejected_publication_does_not_consume_operation_id(
    hosts: tuple[Store, Store, Store],
    body: str,
) -> None:
    store, _, _ = hosts
    with pytest.raises(BBSError):
        store.publish(ALICE, "request", "general", "Title", body)
    assert events(store) == []
    assert store.publish(ALICE, "request", "general", "Title", "Valid body").body == "Valid body"


def test_backup_reopens_with_posts_identity_and_retry_receipts(
    hosts: tuple[Store, Store, Store],
    tmp_path: Path,
) -> None:
    store, _, _ = hosts
    post = store.publish(ALICE, "request", "general", "Title", "Body")
    destination = tmp_path / "backup.db"
    store.backup(destination)
    restored = Store(destination, "colorado-mesh")
    try:
        assert restored.origin == store.origin
        assert restored.get_post(post.post_id) == post
        assert restored.publish(ALICE, "request", "general", "Title", "Body") == post
        assert len(events(restored)) == 1
    finally:
        restored.close()
    with pytest.raises(BBSError, match="already exists"):
        store.backup(destination)


def test_draft_parts_retry_gaps_ownership_restart_and_publish(tmp_path: Path) -> None:
    path = tmp_path / "drafts.db"
    store = Store(path, "colorado-mesh")
    draft = store.new_draft(ALICE, "general", "Long post")
    store.add_part(ALICE, draft, 2, "Second paragraph")
    with pytest.raises(BBSError, match="missing parts"):
        store.publish_draft(ALICE, draft)
    with pytest.raises(BBSError, match="sender"):
        store.add_part("meshcore:bob", draft, 1, "Hijacked")
    store.add_part(ALICE, draft, 1, "First paragraph")
    store.add_part(ALICE, draft, 1, "First paragraph")
    with pytest.raises(BBSError, match="different text"):
        store.add_part(ALICE, draft, 1, "Different paragraph")
    store.close()
    restarted = Store(path, "colorado-mesh")
    try:
        post = restarted.publish_draft(ALICE, draft)
        assert post.body == "First paragraph\nSecond paragraph"
        assert restarted.publish_draft(ALICE, draft) == post
        assert len(events(restarted)) == 1
        with pytest.raises(BBSError, match="editable"):
            restarted.add_part(ALICE, draft, 3, "Too late")
    finally:
        restarted.close()


def test_draft_publish_failure_rolls_back_and_leaves_editable_draft(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    draft = store.new_draft(ALICE, "general", "Long post")
    store.add_part(ALICE, draft, 1, "Text")
    store.db.execute(
        "CREATE TRIGGER fail_publish BEFORE UPDATE ON drafts "
        "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="storage failure"):
        store.publish_draft(ALICE, draft)
    assert events(store) == []
    assert store.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    assert store.draft(ALICE, draft)[0]["published"] is None
    store.db.execute("DROP TRIGGER fail_publish")
    assert store.publish_draft(ALICE, draft).body == "Text"


def test_draft_utf8_size_and_part_number_bounds(hosts: tuple[Store, Store, Store]) -> None:
    store, _, _ = hosts
    draft = store.new_draft(ALICE, "general", "Long post")
    for number in (0, 1025):
        with pytest.raises(BBSError, match="Invalid draft part"):
            store.add_part(ALICE, draft, number, "Text")
    store.add_part(ALICE, draft, 1, "é" * (MAX_BODY_BYTES // 2))
    with pytest.raises(BBSError, match="64 KiB"):
        store.add_part(ALICE, draft, 2, "x")
    assert len(store.publish_draft(ALICE, draft).body.encode()) == MAX_BODY_BYTES


def test_active_draft_limit_is_per_actor_and_publish_releases_slot(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    drafts = [store.new_draft(ALICE, "general", f"Post {number}") for number in range(10)]
    with pytest.raises(BBSError, match="limit 10"):
        store.new_draft(ALICE, "general", "One too many")
    assert store.new_draft("meshcore:bob", "general", "Bob's post")
    store.add_part(ALICE, drafts[0], 1, "Finished post")
    store.publish_draft(ALICE, drafts[0])
    assert store.new_draft(ALICE, "general", "Next post")


def test_inventory_and_export_respect_board_grants(hosts: tuple[Store, Store, Store]) -> None:
    store, _, _ = hosts
    store.publish(ALICE, "general", "general", "Public", "Hello")
    store.publish(ALICE, "news", "news", "News", "Issue")
    general = frozenset({"general"})
    ids = store.inventory(general)
    assert len(ids) == 1
    exported = store.export(store.inventory(BOARDS), general)
    assert all(event["board"] == "general" for event in exported)
    assert store.inventory(frozenset()) == []
    assert store.export(ids, frozenset()) == []
    assert store.missing(ids) == []
    with pytest.raises(BBSError, match="at most 32"):
        store.export(ids * 33, general)
    with pytest.raises(BBSError, match="inventory"):
        store.missing(["not-an-id"])


def test_inventory_pagination_covers_all_events_without_duplicates(
    hosts: tuple[Store, Store, Store],
) -> None:
    store, _, _ = hosts
    for number in range(133):
        store.publish(ALICE, f"post-{number}", "general", f"Post {number}", "Body")
    collected: list[str] = []
    after = ""
    while page := store.inventory(frozenset({"general"}), after=after, limit=17):
        assert len(page) <= 17
        assert page == sorted(page)
        assert page[0] > after
        collected.extend(page)
        after = page[-1]
    assert len(set(collected)) == len(collected) == 133


@pytest.mark.parametrize("operation", ["", "a" * 257])
def test_revision_requires_bounded_nonempty_operation_id(
    hosts: tuple[Store, Store, Store],
    operation: str,
) -> None:
    store, _, _ = hosts
    post = store.publish(ALICE, "original", "general", "Title", "Body")
    with pytest.raises(BBSError, match="operation ID"):
        store.revise(ALICE, operation, post.post_id, "Title", "Changed")
    assert store.get_post(post.post_id) == post


def test_extreme_peer_clock_cannot_disable_future_local_posts(
    hosts: tuple[Store, Store, Store],
) -> None:
    source, target, _ = hosts
    source.publish(ALICE, "remote", "general", "Remote", "Text")
    hostile = resign(source, events(source)[0], clock=2**63 - 1)
    with pytest.raises(BBSError):
        target.accept(hostile)
    assert events(target) == []
    post = target.publish("meshtastic:bob", "local", "general", "Local", "Still works")
    assert target.get_post(post.post_id).body == "Still works"
