from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mesh_bbs.cli import main
from mesh_bbs.config import HostConfig, save_config
from mesh_bbs.events import MAX_BODY_BYTES, BBSError
from mesh_bbs.store import Store
from mesh_bbs.web_access import WRITE_LIMIT, AccessDenied, WebAccess


@pytest.fixture
def access(tmp_path: Path) -> Iterator[WebAccess]:
    store = Store(tmp_path / "bbs.sqlite3", "test")
    yield WebAccess(store)
    store.close()


def post(**changes: Any) -> dict[str, Any]:
    return {
        "board": "general",
        "title": "Saturday meetup",
        "body": "Meet at nine.",
        "parent_id": "",
        "operation": "submit-1",
        **changes,
    }


def test_token_is_secret_and_user_identity_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "bbs.sqlite3"
    store = Store(path, "test")
    access = WebAccess(store)
    token = access.create("alice")
    assert len(token) == 43
    user = access.authenticate(token)
    assert user.actor == "web:alice"
    assert not user.editor
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(user) and digest not in repr(user)
    stored = store.db.execute("SELECT token_hash FROM web_users").fetchone()[0]
    assert stored == digest
    assert token not in " ".join(store.db.iterdump())
    store.close()
    reopened = Store(path, "test")
    try:
        restored = WebAccess(reopened)
        assert restored.authenticate(token).actor == user.actor
        listing = restored.list_users()
        assert listing == [
            {"name": "alice", "actor": "web:alice", "editor": False, "revoked": False}
        ]
        assert token not in json.dumps(listing) and digest not in json.dumps(listing)
    finally:
        reopened.close()


@pytest.mark.parametrize("name", ["", "Alice", "../alice", "alice:bob", "a--b", "a" * 65])
def test_contributor_names_are_validated(access: WebAccess, name: str) -> None:
    with pytest.raises(ValueError):
        access.create(name)
    assert access.list_users() == []


def test_name_cannot_be_reused_before_or_after_revocation(access: WebAccess) -> None:
    token = access.create("alice")
    with pytest.raises(BBSError, match="already exists"):
        access.create("alice")
    access.revoke("alice")
    access.revoke("alice")
    with pytest.raises(BBSError, match="already exists"):
        access.create("alice")
    with pytest.raises(AccessDenied) as error:
        access.authenticate(token)
    assert error.value.status == 401
    assert access.list_users()[0]["revoked"] is True
    with pytest.raises(BBSError, match="not found"):
        access.revoke("missing")


@pytest.mark.parametrize("token", ["", "bad", "a" * 43, "é" * 43, "a" * 44, None, 42])
def test_invalid_tokens_are_rejected(access: WebAccess, token: Any) -> None:
    access.create("alice")
    with pytest.raises(AccessDenied):
        access.authenticate(token)


def test_publish_rechecks_revocation_even_with_previously_authenticated_user(
    access: WebAccess,
) -> None:
    user = access.authenticate(access.create("alice"))
    access.revoke("alice")
    with pytest.raises(AccessDenied):
        access.publish(user, post())
    assert access.store.list_posts("general") == []


def test_revoked_key_cannot_replay_an_existing_submission(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    access.publish(user, post())
    access.revoke("alice")
    with pytest.raises(AccessDenied):
        access.publish(user, post())


def test_caller_cannot_forge_actor_or_editor_flag(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    with pytest.raises(AccessDenied):
        access.publish(replace(user, actor="local:operator"), post())
    with pytest.raises(AccessDenied) as error:
        access.publish(replace(user, editor=True), post(board="news"))
    assert error.value.status == 403


@pytest.mark.parametrize("configured", [False, True])
def test_news_is_read_only_even_for_editors(access: WebAccess, configured: bool) -> None:
    token = access.create("editor", editor=not configured)
    if configured:
        access = WebAccess(access.store, editors=("web:editor",))
    editor = access.authenticate(token)
    reader = access.authenticate(access.create("reader"))
    assert editor.editor
    root = access.store.import_article("colorado", "issue", "news", "Newsletter", "Text")
    for user in (editor, reader):
        for parent in ("", root.post_id):
            with pytest.raises(AccessDenied) as error:
                access.publish(user, post(board="news", parent_id=parent))
            assert error.value.status == 403
    assert len(access.store.list_posts("news", thread_id=root.post_id)) == 1


def test_editor_permission_is_rechecked_at_publication(access: WebAccess) -> None:
    token = access.create("editor")
    promoted = WebAccess(access.store, editors=("web:editor",))
    user = promoted.authenticate(token)
    assert user.editor
    with pytest.raises(AccessDenied):
        access.publish(user, post(board="news"))


def test_submissions_retry_once_but_equal_text_can_be_posted_twice(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    original = access.publish(user, post())
    assert access.publish(user, post()) == original
    another = access.publish(user, post(operation="submit-2"))
    assert another.post_id != original.post_id
    assert len(access.store.list_posts("general")) == 2
    with pytest.raises(BBSError, match="different content"):
        access.publish(user, post(body="Changed after uncertain delivery"))


def test_concurrent_submit_retries_commit_only_once(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        posts = list(pool.map(lambda _: access.publish(user, post()), range(32)))
    assert len({item.post_id for item in posts}) == 1
    assert len(access.store.list_posts("general")) == 1
    writes = access.store.db.execute("SELECT writes FROM web_users").fetchone()[0]
    assert len(json.loads(writes)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"board": "general"},
        post(actor="local:operator"),
        post(source_id="newsletter"),
        post(title=None),
        post(board="unknown"),
        post(title=" "),
        post(body="\n\t "),
        post(title="é" * 129),
        post(body="é" * (MAX_BODY_BYTES // 2 + 1)),
        post(body="\ud800"),
        post(parent_id="abcd1234"),
        post(operation=""),
        post(operation="x" * 65),
        post(operation="request id"),
        post(operation="../../private"),
    ],
)
def test_invalid_payload_cannot_change_storage(access: WebAccess, payload: dict[str, Any]) -> None:
    user = access.authenticate(access.create("alice"))
    with pytest.raises(BBSError):
        access.publish(user, payload)
    assert access.store.list_posts("general") == []
    assert access.store.db.execute("SELECT writes FROM web_users").fetchone()[0] == "[]"


def test_utf8_maximum_post_is_accepted(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    saved = access.publish(user, post(title="é" * 128, body="é" * (MAX_BODY_BYTES // 2)))
    assert len(saved.body.encode()) == MAX_BODY_BYTES


def test_cross_board_and_removed_parent_replies_are_rejected(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    root = access.publish(user, post())
    access.store.create_board(user.actor, "other")
    with pytest.raises(BBSError, match="different board"):
        access.publish(user, post(board="other", parent_id=root.post_id, operation="reply"))
    reply = access.publish(user, post(parent_id=root.post_id, operation="reply", title=""))
    access.store.remove("remove-parent", root.post_id)
    assert access.publish(user, post(parent_id=root.post_id, operation="reply", title="")) == reply
    with pytest.raises(BBSError, match="removed"):
        access.publish(user, post(parent_id=root.post_id, operation="new-reply", title=""))


def test_write_limit_is_sliding_persistent_per_user_and_excludes_retries(
    access: WebAccess,
) -> None:
    clock = [100.0]
    access = WebAccess(access.store, clock=lambda: clock[0])
    token = access.create("alice")
    user = access.authenticate(token)
    original = access.publish(user, post())
    for number in range(1, WRITE_LIMIT):
        access.publish(user, post(operation=f"submission-{number}"))
    restarted = WebAccess(access.store, clock=lambda: clock[0])
    for timestamp in (100.0, 159.99, 90.0):
        clock[0] = timestamp
        with pytest.raises(AccessDenied) as error:
            restarted.publish(user, post(operation="too-many"))
        assert error.value.status == 429
    assert restarted.publish(user, post()) == original
    bob = access.authenticate(access.create("bob"))
    assert restarted.publish(bob, post()).author == "web:bob"
    clock[0] = 160.0
    restarted.publish(user, post(operation="next-window"))
    writes = access.store.db.execute(
        "SELECT writes FROM web_users WHERE actor=?", (user.actor,)
    ).fetchone()[0]
    assert json.loads(writes) == [160.0]


def test_quota_failure_rolls_back_post_and_deduplication_receipt(access: WebAccess) -> None:
    user = access.authenticate(access.create("alice"))
    access.store.db.execute(
        "CREATE TRIGGER fail_quota BEFORE UPDATE ON web_users "
        "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        access.publish(user, post())
    assert access.store.list_posts("general") == []
    assert access.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    access.store.db.execute("DROP TRIGGER fail_quota")
    assert access.publish(user, post()).body == "Meet at nine."


def test_cli_access_creation_listing_and_revocation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.toml"
    save_config(HostConfig("Test", "test", tmp_path / "data"), path)
    prefix = ["--config", str(path), "web-access"]
    assert main([*prefix, "create", "alice", "--editor"]) == 0
    captured = capsys.readouterr()
    token = captured.out.strip()
    assert len(token) == 43 and "shown only once" in captured.err
    assert main([*prefix, "list"]) == 0
    listing = capsys.readouterr().out
    assert token not in listing and "token_hash" not in listing
    assert json.loads(listing)[0]["editor"] is True
    assert main([*prefix, "revoke", "alice"]) == 0
    assert "remains reserved" in capsys.readouterr().out
    assert main([*prefix, "create", "alice"]) == 1
    assert "already exists" in capsys.readouterr().err
