from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mesh_bbs.commands import CommandService
from mesh_bbs.config import FeedConfig, HostConfig
from mesh_bbs.events import BBSError
from mesh_bbs.feeds import FetchResult
from mesh_bbs.newsletters import NewsletterImporter
from mesh_bbs.runtime import serve
from mesh_bbs.store import Store


def test_database_with_private_key_is_private_in_an_existing_directory(tmp_path: Path) -> None:
    directory = tmp_path / "existing-data"
    directory.mkdir(mode=0o755)
    path = directory / "bbs.sqlite3"
    previous_umask = os.umask(0o022)
    try:
        store = Store(path, "test")
        try:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    assert stat.S_IMODE(sidecar.stat().st_mode) & 0o077 == 0
        finally:
            store.close()
    finally:
        os.umask(previous_umask)


def test_backup_with_private_key_is_private_in_shared_directory(tmp_path: Path) -> None:
    store = Store(tmp_path / "source" / "bbs.sqlite3", "test")
    destination = tmp_path / "backup.sqlite3"
    previous_umask = os.umask(0o022)
    try:
        store.backup(destination)
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    finally:
        store.close()
        os.umask(previous_umask)


def test_backup_refuses_dangling_symlink_without_creating_its_target(tmp_path: Path) -> None:
    store = Store(tmp_path / "source" / "bbs.sqlite3", "test")
    target = tmp_path / "unexpected.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    destination.symlink_to(target)
    try:
        with pytest.raises((BBSError, OSError)):
            store.backup(destination)
        assert not target.exists()
        assert destination.is_symlink()
    finally:
        store.close()


@pytest.mark.parametrize("target_exists", [False, True])
def test_database_refuses_symlink_path(tmp_path: Path, target_exists: bool) -> None:
    target = tmp_path / "target.sqlite3"
    if target_exists:
        source = Store(target, "test")
        source.close()
        original = target.read_bytes()
    else:
        original = None
    symlink = tmp_path / "bbs.sqlite3"
    symlink.symlink_to(target)
    with pytest.raises((BBSError, OSError)):
        Store(symlink, "test")
    assert symlink.is_symlink()
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original


def test_removed_post_stops_an_already_open_read_cursor(tmp_path: Path) -> None:
    store = Store(tmp_path / "bbs.sqlite3", "test")
    service = CommandService(store)
    try:
        post = store.publish("local:operator", "post", "general", "Title", "Secret " * 150)
        first = service.handle("meshcore:reader", f"read {post.post_id}", max_bytes=100)
        assert "[more]" in first
        store.revise("local:operator", "remove", post.post_id, "Title", "", remove=True)
        remaining = service.handle("meshcore:reader", "more", max_bytes=100)
        assert "Secret" not in remaining
        assert "removed" in remaining.lower() or "no open page" in remaining.lower()
    finally:
        store.close()


def test_correction_keeps_an_open_cursor_on_its_original_revision(tmp_path: Path) -> None:
    store = Store(tmp_path / "bbs.sqlite3", "test")
    service = CommandService(store)
    try:
        post = store.publish("local:operator", "post", "general", "Title", "Original " * 150)
        first = service.handle("meshcore:reader", f"read {post.post_id}", max_bytes=100)
        assert "[more]" in first
        store.revise("local:operator", "correct", post.post_id, "Title", "Corrected " * 150)
        remaining = service.handle("meshcore:reader", "more", max_bytes=100)
        assert "Original" in remaining
        assert "Corrected" not in remaining
    finally:
        store.close()


@pytest.mark.parametrize("verb", ["read", "more"])
def test_removed_post_cannot_be_replayed_from_command_receipt(tmp_path: Path, verb: str) -> None:
    store = Store(tmp_path / "bbs.sqlite3", "test")
    service = CommandService(store)
    try:
        post = store.publish("local:operator", "post", "general", "Title", "Withdrawn " * 150)
        command = f"read {post.post_id}"
        if verb == "more":
            service.handle("meshcore:reader", command, max_bytes=100)
            command = "more"
        first = service.handle(
            "meshcore:reader", command, max_bytes=100, request_id="retried-request"
        )
        assert "Withdrawn" in first
        store.revise("local:operator", "remove", post.post_id, "Title", "", remove=True)
        replay = service.handle(
            "meshcore:reader", command, max_bytes=100, request_id="retried-request"
        )
        assert "Withdrawn" not in replay
    finally:
        store.close()


def newsletter_feed(*items: tuple[str, str]) -> bytes:
    entries = "".join(
        f"<item><guid>{title}</guid><title>{title}</title>"
        f"<pubDate>{published}</pubDate><description>Issue text</description></item>"
        for title, published in items
    )
    return f'<rss version="2.0"><channel><title>Mesh News</title>{entries}</channel></rss>'.encode()


@pytest.mark.parametrize("backfill", [False, True])
def test_news_latest_uses_publication_order_not_import_arrival(
    tmp_path: Path,
    backfill: bool,
) -> None:
    store = Store(tmp_path / "bbs.sqlite3", "test")
    newest = ("September issue", "Tue, 22 Sep 2026 12:00:00 GMT")
    oldest = ("August issue", "Sat, 22 Aug 2026 12:00:00 GMT")
    documents = (
        [newsletter_feed(newest), newsletter_feed(oldest)]
        if backfill
        else [newsletter_feed(newest, oldest)]
    )
    remaining = iter(documents)

    def fetch(
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        return FetchResult(200, next(remaining), None, None)

    try:
        importer = NewsletterImporter(
            store, FeedConfig("newsletter", "https://example.org/news.xml"), fetcher=fetch
        )
        for _ in documents:
            assert importer.poll_once(force=True).status == "saved_locally"
        assert store.list_posts("news")[0].title == newest[0]
        response = CommandService(store).handle("meshcore:reader", "news latest", max_bytes=512)
        assert newest[0] in response
    finally:
        store.close()


async def test_shutdown_drains_inflight_newsletter_before_closing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    database_available: list[bool] = []

    class BlockingImporter:
        def __init__(self, store: Store, feed: FeedConfig) -> None:
            self.store = store

        def poll_once(self) -> SimpleNamespace:
            started.set()
            assert release.wait(5), "Test did not release its simulated HTTP fetch"
            try:
                with self.store.transaction():
                    self.store.db.execute("SELECT 1")
                database_available.append(True)
            except sqlite3.ProgrammingError:
                database_available.append(False)
            return SimpleNamespace(status="not_modified")

    class OfflineWeb:
        def __init__(self, *_args: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr("mesh_bbs.newsletters.NewsletterImporter", BlockingImporter)
    monkeypatch.setattr("mesh_bbs.web.ReadOnlyWebServer", OfflineWeb)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    config = HostConfig(
        "Review test",
        "test",
        tmp_path,
        feeds=(FeedConfig("newsletter", "https://example.org/feed"),),
    )
    stop = asyncio.Event()
    server = asyncio.create_task(serve(config, stop=stop))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        stop.set()
        await asyncio.sleep(0.05)
        finished_before_poll = server.done()
    finally:
        release.set()
        await asyncio.wait_for(server, 3)
    assert not finished_before_poll, "Service shutdown returned while a poll still used its DB"
    assert database_available == [True]
