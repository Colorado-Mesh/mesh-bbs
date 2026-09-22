from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from mesh_bbs.cli import main, open_store
from mesh_bbs.commands import CommandService
from mesh_bbs.config import HostConfig, RadioConfig, load_config, save_config
from mesh_bbs.events import BBSError, Event
from mesh_bbs.store import Grant, Store


@pytest.fixture
def configuration(tmp_path: Path) -> tuple[HostConfig, Path]:
    config = HostConfig("Test community", "cli-test", tmp_path / "data")
    path = tmp_path / "config.toml"
    save_config(config, path)
    return config, path


def test_operator_edit_remove_and_retry(configuration, tmp_path: Path, capsys) -> None:
    config, path = configuration
    args = ["--config", str(path)]
    body = tmp_path / "post.txt"
    body.write_text("Initial edition")
    assert (
        main(
            [*args, "post", "news", "September", "--body-file", str(body), "--operation", "create"]
        )
        == 0
    )
    store = open_store(config)
    post = store.list_posts("news")[0]
    store.close()
    body.write_text("Corrected edition")
    edit = [*args, "edit", post.post_id, "--body-file", str(body), "--operation", "correct"]
    assert main(edit) == main(edit) == 0
    store = open_store(config)
    assert store.get_post(post.post_id).body == "Corrected edition"
    assert len(store.inventory(frozenset({"news"}))) == 2
    store.close()
    removal = [*args, "remove", post.post_id, "--operation", "withdraw"]
    assert main(removal) == main(removal) == 0
    store = open_store(config)
    assert store.get_post(post.post_id).deleted
    assert len(store.inventory(frozenset({"news"}))) == 3
    store.close()
    assert "Removed locally" in capsys.readouterr().out


def test_operator_removal_of_gateway_authored_post_replicates(tmp_path: Path) -> None:
    owner = Store(tmp_path / "one.db", "test")
    peer = Store(tmp_path / "two.db", "test")
    try:
        peer.grants[owner.origin] = Grant(owner.public_key, frozenset({"general"}))
        post = owner.publish("meshtastic:12345678", "one", "general", "Title", "Text")
        owner.remove("moderation", post.post_id)
        for value in owner.export(owner.inventory(frozenset({"general"})), frozenset({"general"})):
            peer.accept(Event.from_dict(value))
        assert peer.get_post(post.post_id).deleted
    finally:
        owner.close()
        peer.close()


def test_cross_host_removal_needs_moderator_grant(tmp_path: Path) -> None:
    owner = Store(tmp_path / "one.db", "test")
    moderator = Store(tmp_path / "two.db", "test")
    ordinary_peer = Store(tmp_path / "three.db", "test")
    trusted_peer = Store(tmp_path / "four.db", "test")
    boards = frozenset({"general"})
    try:
        post = owner.publish("local:operator", "one", "general", "Title", "Text")
        for destination in (moderator, ordinary_peer, trusted_peer):
            destination.grants[owner.origin] = Grant(owner.public_key, boards)
            destination.accept(Event.from_dict(owner.export(owner.inventory(boards), boards)[0]))
        moderator.remove("moderation", post.post_id)
        ordinary_peer.grants[moderator.origin] = Grant(moderator.public_key, boards)
        trusted_peer.grants[moderator.origin] = Grant(
            moderator.public_key, boards, can_moderate=True
        )
        for value in moderator.export(moderator.inventory(boards), boards):
            ordinary_peer.accept(Event.from_dict(value))
            trusted_peer.accept(Event.from_dict(value))
        assert not ordinary_peer.get_post(post.post_id).deleted
        assert trusted_peer.get_post(post.post_id).deleted
    finally:
        for store in (owner, moderator, ordinary_peer, trusted_peer):
            store.close()


def test_remove_operation_id_cannot_target_another_post(configuration) -> None:
    config, _ = configuration
    store = open_store(config)
    try:
        first = store.publish("local:operator", "one", "news", "Title", "Text")
        second = store.publish("local:operator", "two", "news", "Title", "Text")
        store.remove("same", first.post_id)
        with pytest.raises(BBSError, match="different content"):
            store.remove("same", second.post_id)
        assert not store.get_post(second.post_id).deleted
    finally:
        store.close()


def test_cli_errors_are_nonzero_and_do_not_publish(configuration, tmp_path: Path, capsys) -> None:
    config, path = configuration
    body = tmp_path / "bad.txt"
    body.write_bytes(b"\xff")
    assert (
        main(
            [
                "--config",
                str(path),
                "post",
                "general",
                "Bad",
                "--operation",
                "bad",
                "--body-file",
                str(body),
            ]
        )
        == 1
    )
    assert "Error:" in capsys.readouterr().err
    store = open_store(config)
    assert not store.list_posts("general")
    store.close()


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "slow"])
def test_invalid_radio_budget_is_rejected(value) -> None:
    with pytest.raises(ValueError):
        RadioConfig(airtime_budget_seconds=value)


def test_radio_budget_roundtrip(configuration, tmp_path: Path) -> None:
    config, _ = configuration
    radio = RadioConfig(
        serial_port="/dev/test",
        enabled=True,
        min_interval=4.5,
        airtime_budget_seconds=150.0,
        airtime_window_seconds=600.0,
        packet_airtime_seconds=2.5,
        firmware_max_attempts=3,
    )
    changed = replace(config, meshcore=radio)
    path = tmp_path / "radio.toml"
    save_config(changed, path)
    assert load_config(path) == changed


def test_radio_lists_can_reach_posts_after_first_page(configuration) -> None:
    config, _ = configuration
    store = open_store(config)
    service = CommandService(store)
    try:
        roots = [
            store.publish("local:operator", str(i), "general", f"Title {i}", "Text")
            for i in range(55)
        ]
        first = service.handle("local:operator", "threads general", max_bytes=8192)
        cursor = first.split("Next: ")[1]
        second = service.handle("local:operator", cursor, max_bytes=8192)
        assert roots[0].post_id[:12] in second
        assert roots[-1].post_id[:12] not in second
        root = roots[0]
        replies = [
            store.publish(
                "local:operator",
                f"reply-{i}",
                "general",
                f"Reply {i}",
                "Text",
                parent_id=root.post_id,
            )
            for i in range(55)
        ]
        first = service.handle("local:operator", f"thread {root.post_id}", max_bytes=8192)
        second = service.handle("local:operator", first.split("Next: ")[1], max_bytes=8192)
        assert replies[-1].post_id[:12] in second
        assert root.post_id[:12] not in second
    finally:
        store.close()


def test_init_only_emits_public_identity(configuration, capsys) -> None:
    _, path = configuration
    assert main(["--config", str(path), "init"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"name", "region", "origin", "public_key", "database"}


def test_terminal_cli_uses_bounded_binary_frames_and_read_only_policy(configuration) -> None:
    _, path = configuration
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mesh_bbs.cli",
            "--config",
            str(path),
            "terminal",
            "--callsign",
            "N0CALL-2",
            "--boards",
            "general",
            "--max-bytes",
            "128",
        ],
        input=b"boards\rnew general Should be rejected\rquit\r",
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b"packet:N0CALL-2" in result.stdout
    assert b"Boards: general\r\n" in result.stdout
    assert b"This terminal is read only" in result.stdout
    assert b"news" not in result.stdout
    assert result.stdout.endswith(b"Goodbye.\r\n")
    assert all(len(frame) + 2 <= 128 for frame in result.stdout.split(b"\r\n") if frame)
