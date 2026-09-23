from __future__ import annotations

import asyncio
import errno
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from mesh_bbs.adapters.reticulum import SYNC_PATH, ReticulumAdapter, _bounded_object

PEER = "a1" * 16


def adapter(tmp_path: Path, **overrides: object) -> ReticulumAdapter:
    values = {
        "config_dir": tmp_path / "rns",
        "state_dir": tmp_path / "state",
        "name": "Test BBS",
        "trusted_peers": [PEER],
        "command_handler": AsyncMock(return_value="published P1"),
        "page_handler": Mock(return_value=b"#!c=0\n>Boards\n"),
        "sync_handler": Mock(return_value={"events": []}),
    }
    values.update(overrides)
    return ReticulumAdapter(**values)


def incoming(**overrides: object) -> SimpleNamespace:
    values = {
        "signature_validated": True,
        "content": b"boards",
        "source_hash": bytes.fromhex(PEER),
        "hash": bytes.fromhex("a2" * 32),
        "source": object(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def identity_service(tmp_path: Path) -> ReticulumAdapter:
    service = adapter(tmp_path)
    service._rns = pytest.importorskip("RNS")
    service.state_dir.mkdir()
    return service


def test_identity_is_private_when_created(
    identity_service: ReticulumAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = identity_service.state_dir / "identity"
    real_open = os.open
    created_modes = []

    def observe_open(file: Path, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(file, flags, *args, **kwargs)
        if file == path and flags & os.O_CREAT:
            created_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor

    monkeypatch.setattr(os, "open", observe_open)
    previous_umask = os.umask(0)
    try:
        identity = identity_service._load_identity()
    finally:
        os.umask(previous_umask)
    assert created_modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == identity.get_private_key()
    assert identity_service._rns.Identity.from_file(str(path)).hash == identity.hash


def test_identity_load_preserves_keys_and_tightens_permissions(
    identity_service: ReticulumAdapter,
) -> None:
    path = identity_service.state_dir / "identity"
    original = identity_service._rns.Identity()
    path.write_bytes(original.get_private_key())
    path.chmod(0o644)
    loaded = identity_service._load_identity()
    assert loaded.hash == original.hash
    assert loaded.get_private_key() == original.get_private_key()
    assert path.read_bytes() == original.get_private_key()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert identity_service._load_identity().hash == original.hash


@pytest.mark.parametrize("content", [b"", b"truncated", b"x" * 63, b"x" * 65, b"x" * 4096])
def test_corrupt_identity_is_not_replaced(
    identity_service: ReticulumAdapter, content: bytes
) -> None:
    path = identity_service.state_dir / "identity"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="Cannot load the existing"):
        identity_service._load_identity()
    assert path.read_bytes() == content


@pytest.mark.parametrize("target_exists", [False, True])
def test_identity_symlink_is_never_followed(tmp_path: Path, target_exists: bool) -> None:
    service = adapter(tmp_path)
    service.state_dir.mkdir()
    target = tmp_path / "other-identity"
    if target_exists:
        target.write_bytes(b"leave this alone")
        target.chmod(0o644)
    path = service.state_dir / "identity"
    path.symlink_to(target)
    with pytest.raises(ValueError, match="must be a regular file"):
        service._load_identity()
    assert path.is_symlink()
    assert target.exists() == target_exists
    if target_exists:
        assert target.read_bytes() == b"leave this alone"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_identity_rejects_non_regular_files(tmp_path: Path, kind: str) -> None:
    service = adapter(tmp_path)
    service.state_dir.mkdir()
    path = service.state_dir / "identity"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(ValueError, match="must be a regular file"):
        service._load_identity()


def test_identity_rejects_symlink_substituted_after_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = adapter(tmp_path)
    service.state_dir.mkdir()
    path = service.state_dir / "identity"
    path.write_bytes(b"original")
    target = tmp_path / "other-identity"
    target.write_bytes(b"leave this alone")
    target.chmod(0o644)
    real_open = os.open

    def swap_before_open(file: Path, flags: int, *args: Any, **kwargs: Any) -> int:
        if file == path and not flags & os.O_CREAT:
            path.unlink()
            path.symlink_to(target)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(OSError) as failure:
        service._load_identity()
    assert failure.value.errno == errno.ELOOP
    assert target.read_bytes() == b"leave this alone"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_identity_rejects_fifo_substituted_after_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = adapter(tmp_path)
    service.state_dir.mkdir()
    path = service.state_dir / "identity"
    path.write_bytes(b"original")
    real_open = os.open

    def swap_before_open(file: Path, flags: int, *args: Any, **kwargs: Any) -> int:
        if file == path and not flags & os.O_CREAT:
            path.unlink()
            os.mkfifo(path)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(ValueError, match="must be a regular file"):
        service._load_identity()


@pytest.mark.parametrize(
    "values",
    [
        {"trusted_peers": ["not-an-identity"]},
        {"trusted_peers": ["a1 " * 16]},
        {"queue_size": 0},
        {"queue_size": 1025},
        {"queue_size": True},
        {"delivery_timeout": float("nan")},
        {"announce_interval": float("inf")},
        {"name": ""},
        {"name": "x" * 129},
    ],
)
def test_configuration_rejects_invalid_limits_and_peer_ids(tmp_path: Path, values: dict) -> None:
    with pytest.raises(ValueError):
        adapter(tmp_path, **values)


async def test_start_requires_explicit_config_without_creating_profile(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    with pytest.raises(ValueError, match="explicit Reticulum config"):
        await service.start()
    assert not service.config_dir.exists()
    assert not service.state_dir.exists()


async def test_periodic_announces_keep_half_hour_deadline_across_restart_and_manual_send(
    tmp_path, monkeypatch
):
    clock = [10_000.0]
    monkeypatch.setattr("mesh_bbs.adapters.reticulum.time.time", lambda: clock[0])
    original = adapter(tmp_path)
    original.announce = Mock()
    original._startup_announce()
    assert original.announce_interval == 1800

    clock[0] += 400
    restarted = adapter(tmp_path)
    restarted.announce = Mock()
    restarted._startup_announce()
    restarted.announce()  # Manual announces must not postpone the scheduled one.
    assert restarted._announce_schedule.last_attempt == 10_000
    calls = []

    async def advance(delay):
        if restarted.announce.call_count >= 3 or len(calls) > 40:
            raise asyncio.CancelledError
        calls.append(delay)
        clock[0] += delay

    monkeypatch.setattr("mesh_bbs.adapters.reticulum.asyncio.sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await restarted._announcements()
    assert sum(calls) == 1400
    assert restarted.announce.call_count == 3
    assert adapter(tmp_path)._announce_schedule.last_attempt == 11_800


async def test_scheduled_announce_failure_does_not_kill_future_announces(
    tmp_path, monkeypatch, caplog
):
    clock = [10_000.0]
    monkeypatch.setattr("mesh_bbs.adapters.reticulum.time.time", lambda: clock[0])
    service = adapter(tmp_path)
    service.announce = Mock(side_effect=[OSError("interface offline"), None])
    sleeps = []

    async def advance(delay):
        if service.announce.call_count >= 2 or len(sleeps) > 40:
            raise asyncio.CancelledError
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr("mesh_bbs.adapters.reticulum.asyncio.sleep", advance)
    with pytest.raises(asyncio.CancelledError):
        await service._announcements()
    assert service.announce.call_count == 2
    assert sum(sleeps) == 1800
    assert "Scheduled Reticulum announce failed" in caplog.text


async def test_worker_receives_verified_sender_and_dedupe_id_from_foreign_thread(
    tmp_path: Path,
) -> None:
    service = adapter(tmp_path)
    service._running = True
    service._loop = asyncio.get_running_loop()
    service._send_reply = AsyncMock()
    worker = asyncio.create_task(service._command_worker())
    service._tasks = [worker]
    thread = threading.Thread(target=service._on_message, args=(incoming(),))
    thread.start()
    thread.join()
    await asyncio.sleep(0)
    await asyncio.wait_for(service._queue.join(), 1)
    message = service.command_handler.call_args.args[0]
    assert message.protocol == "reticulum"
    assert message.sender == PEER
    assert message.message_id == "a2" * 32
    assert message.text == "boards"
    service._send_reply.assert_awaited_once()
    await service.stop()


@pytest.mark.parametrize(
    "message",
    [
        incoming(signature_validated=False),
        incoming(content=b"\xff"),
        incoming(content="not-bytes"),
        incoming(content=b"x" * 65_537),
    ],
)
async def test_unverified_or_invalid_commands_are_not_admitted(
    tmp_path: Path, message: SimpleNamespace
) -> None:
    service = adapter(tmp_path)
    service._running = True
    service._loop = asyncio.get_running_loop()
    service._on_message(message)
    await asyncio.sleep(0)
    assert service._queue.empty()
    service.command_handler.assert_not_called()


async def test_queue_is_bounded_before_event_loop_callbacks_run(tmp_path: Path) -> None:
    service = adapter(tmp_path, queue_size=2)
    service._running = True
    service._loop = asyncio.get_running_loop()
    for _ in range(100):
        service._on_message(incoming())
    await asyncio.sleep(0)
    assert service._queue.qsize() == 2
    await service.stop()
    # All semaphore slots are returned during shutdown.
    assert service._slots.acquire(blocking=False)
    assert service._slots.acquire(blocking=False)
    assert not service._slots.acquire(blocking=False)


async def test_pending_callback_during_stop_releases_admission_slot(tmp_path: Path) -> None:
    service = adapter(tmp_path, queue_size=1)
    service._running = True
    service._loop = asyncio.get_running_loop()
    service._on_message(incoming())
    await service.stop()
    await asyncio.sleep(0)
    assert service._queue.empty()
    assert service._slots.acquire(blocking=False)


async def test_handler_failure_does_not_kill_worker(tmp_path: Path) -> None:
    service = adapter(tmp_path, command_handler=AsyncMock(side_effect=[ValueError("bad"), ""]))
    service._running = True
    service._loop = asyncio.get_running_loop()
    service._send_reply = AsyncMock()
    service._tasks = [asyncio.create_task(service._command_worker())]
    service._on_message(incoming())
    service._on_message(incoming())
    await asyncio.sleep(0)
    await asyncio.wait_for(service._queue.join(), 1)
    assert service.command_handler.await_count == 2
    service._send_reply.assert_not_called()
    await service.stop()


def test_sync_callback_checks_identity_independently_of_rns(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._running = True
    for identity in (None, SimpleNamespace(hash=b"x" * 16)):
        result = service._sync_request(SYNC_PATH, {}, b"r", b"l", identity, 0)
        assert result == {"error": "unauthorized"}
    service.sync_handler.assert_not_called()
    request = {"cursor": 5}
    peer = SimpleNamespace(hash=bytes.fromhex(PEER))
    assert service._sync_request(SYNC_PATH, request, b"r", b"l", peer, 0) == {"events": []}
    service.sync_handler.assert_called_once_with(PEER, request)


@pytest.mark.parametrize("payload", [[], "text", {1: "value"}, {"number": float("nan")}])
def test_sync_callback_rejects_non_json_object_payloads(tmp_path: Path, payload: object) -> None:
    service = adapter(tmp_path)
    service._running = True
    peer = SimpleNamespace(hash=bytes.fromhex(PEER))
    assert (
        service._sync_request(SYNC_PATH, payload, b"r", b"l", peer, 0)["error"] == "invalid_request"
    )
    service.sync_handler.assert_not_called()


def test_sync_and_pages_bound_handler_responses(tmp_path: Path) -> None:
    service = adapter(
        tmp_path,
        response_limit=100,
        sync_handler=Mock(return_value={"large": "x" * 101}),
        page_handler=Mock(return_value=b"x" * 101),
    )
    service._running = True
    peer = SimpleNamespace(hash=bytes.fromhex(PEER))
    assert service._sync_request(SYNC_PATH, {}, b"r", b"l", peer, 0)["error"] == "invalid_request"
    assert b">Unavailable" in service._page_request("/page/index.mu", None, b"r", b"l", None, 0)


def test_page_callback_passes_path_and_variables(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._running = True
    request = {"var_board": "general"}
    result = service._page_request("/page/board.mu", request, b"r", b"l", None, 0)
    assert result == b"#!c=0\n>Boards\n"
    service.page_handler.assert_called_once_with("/page/board.mu", request)


@pytest.mark.parametrize("payload", [None, {}, b""])
def test_page_callback_accepts_empty_nomadnet_requests(tmp_path: Path, payload: object) -> None:
    service = adapter(tmp_path)
    service._running = True
    assert service._page_request("/page/index.mu", payload, b"r", b"l", None, 0) == (
        b"#!c=0\n>Boards\n"
    )
    service.page_handler.assert_called_once_with("/page/index.mu", {})


@pytest.mark.parametrize(
    "payload", [b"invalid", b"\x80", "", [], {1: "value"}, {"var_board": "x" * 8192}]
)
def test_page_callback_rejects_invalid_payloads(tmp_path: Path, payload: object) -> None:
    service = adapter(tmp_path)
    service._running = True
    assert b">Unavailable" in service._page_request("/page/index.mu", payload, b"r", b"l", None, 0)
    service.page_handler.assert_not_called()
    # A rejected request must release its slot for the next reader.
    assert service._page_request("/page/index.mu", None, b"r", b"l", None, 0) == (
        b"#!c=0\n>Boards\n"
    )


async def test_request_peer_rejects_untrusted_identity_before_network(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._running = True
    with pytest.raises(PermissionError, match="allowlist"):
        await service.request_peer("ff" * 16, {})


async def test_request_peer_has_bounded_path_discovery(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._running = True
    service._rns = SimpleNamespace(
        Destination=SimpleNamespace(hash=Mock(return_value=b"d" * 16)),
        Transport=SimpleNamespace(has_path=Mock(return_value=False), request_path=Mock()),
    )
    with pytest.raises(TimeoutError):
        await service.request_peer(PEER, {}, timeout=0.02)
    service._rns.Transport.request_path.assert_called_once()


async def test_request_peer_checks_discovered_identity(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._running = True
    service._rns = SimpleNamespace(
        Destination=SimpleNamespace(hash=Mock(return_value=b"d" * 16)),
        Identity=SimpleNamespace(recall=Mock(return_value=SimpleNamespace(hash=b"wrong"))),
        Transport=SimpleNamespace(has_path=Mock(return_value=True)),
    )
    with pytest.raises(PermissionError, match="pinned peer"):
        await service.request_peer(PEER, {})


async def test_exchange_caps_response_and_ignores_late_callback(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._loop = asyncio.get_running_loop()
    link = SimpleNamespace(request=Mock())

    def request(path: str, **kwargs: object) -> object:
        assert path == SYNC_PATH
        assert kwargs["max_response_size"] == service.response_limit
        response_callback = kwargs["response_callback"]
        response_callback(SimpleNamespace(response={"events": []}))
        response_callback(SimpleNamespace(response={"duplicate_callback": True}))
        return object()

    link.request.side_effect = request
    assert await service._exchange(link, {}, 1) == {"events": []}


async def test_exchange_reports_send_failure(tmp_path: Path) -> None:
    service = adapter(tmp_path)
    service._loop = asyncio.get_running_loop()
    link = SimpleNamespace(request=Mock(return_value=False))
    with pytest.raises(ConnectionError, match="refused"):
        await service._exchange(link, {}, 1)


def test_object_limit_counts_utf8_bytes() -> None:
    with pytest.raises(ValueError, match="byte limit"):
        _bounded_object({"body": "é" * 20}, 40)


def unknown_sender_service(tmp_path: Path, **kwargs: Any) -> ReticulumAdapter:
    service = adapter(tmp_path, **kwargs)
    service._running = True
    service._loop = asyncio.get_running_loop()
    service._rns = SimpleNamespace(
        Identity=SimpleNamespace(recall=Mock(return_value=None)),
        Transport=SimpleNamespace(request_path=Mock()),
    )
    service._lxmf = SimpleNamespace(
        LXMessage=SimpleNamespace(SOURCE_UNKNOWN=1, unpack_from_bytes=Mock())
    )
    return service


def unknown_message(**kwargs: Any) -> SimpleNamespace:
    return incoming(
        signature_validated=False, unverified_reason=1, packed=b"signed bytes", **kwargs
    )


async def test_unknown_sender_is_discovered_and_reverified(tmp_path: Path) -> None:
    service = unknown_sender_service(tmp_path)
    service._rns.Identity.recall.side_effect = [None, object()]
    verified = incoming(source_blackholed=False)
    service._lxmf.LXMessage.unpack_from_bytes.return_value = verified
    service._tasks = [asyncio.create_task(service._identity_worker())]
    service._on_message(unknown_message())
    await asyncio.sleep(0)
    await asyncio.wait_for(service._identity_queue.join(), 1)
    await asyncio.sleep(0)
    service._rns.Transport.request_path.assert_called_once_with(bytes.fromhex(PEER))
    service._lxmf.LXMessage.unpack_from_bytes.assert_called_once_with(b"signed bytes")
    assert service._queue.get_nowait() is verified
    service._queue.task_done()
    service._slots.release()
    await service.stop()


@pytest.mark.parametrize("invalid_signature,blackholed", [(True, False), (False, True)])
async def test_discovery_does_not_bypass_signature_or_blackhole_checks(
    tmp_path: Path, invalid_signature: bool, blackholed: bool
) -> None:
    service = unknown_sender_service(tmp_path)
    service._rns.Identity.recall.return_value = object()
    service._lxmf.LXMessage.unpack_from_bytes.return_value = incoming(
        signature_validated=not invalid_signature, source_blackholed=blackholed
    )
    service._tasks = [asyncio.create_task(service._identity_worker())]
    service._on_message(unknown_message())
    await asyncio.sleep(0)
    await asyncio.wait_for(service._identity_queue.join(), 1)
    assert service._queue.empty()
    await service.stop()


async def test_unknown_sender_timeout_does_not_stall_verified_commands(tmp_path: Path) -> None:
    service = unknown_sender_service(tmp_path, identity_timeout=0.02)
    service._send_reply = AsyncMock()
    service._tasks = [
        asyncio.create_task(service._identity_worker()),
        asyncio.create_task(service._command_worker()),
    ]
    service._on_message(unknown_message())
    service._on_message(incoming())
    await asyncio.sleep(0)
    await asyncio.wait_for(service._queue.join(), 1)
    service.command_handler.assert_awaited_once()
    await asyncio.wait_for(service._identity_queue.join(), 1)
    service._lxmf.LXMessage.unpack_from_bytes.assert_not_called()
    await service.stop()


async def test_unknown_sender_queue_is_bounded_and_released_on_shutdown(tmp_path: Path) -> None:
    service = unknown_sender_service(tmp_path, queue_size=2)
    for _ in range(100):
        service._on_message(unknown_message())
    await asyncio.sleep(0)
    assert service._identity_queue.qsize() == 2
    service._tasks = [asyncio.create_task(service._identity_worker())]
    await asyncio.sleep(0)
    await service.stop()
    assert service._identity_queue.empty()
    assert service._identity_slots.acquire(blocking=False)
    assert service._identity_slots.acquire(blocking=False)
    assert not service._identity_slots.acquire(blocking=False)


async def test_unknown_sender_callback_pending_at_shutdown_returns_slot(tmp_path: Path) -> None:
    service = unknown_sender_service(tmp_path, queue_size=1)
    service._on_message(unknown_message())
    await service.stop()
    await asyncio.sleep(0)
    assert service._identity_queue.empty()
    assert service._identity_slots.acquire(blocking=False)


@pytest.mark.parametrize("changes", [{"unverified_reason": 2}, {"packed": b"x" * 70000}])
async def test_invalid_signatures_and_oversized_packets_do_not_trigger_discovery(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    service = unknown_sender_service(tmp_path)
    message = unknown_message()
    for key, value in changes.items():
        setattr(message, key, value)
    service._on_message(message)
    await asyncio.sleep(0)
    assert service._identity_queue.empty()
    service._rns.Transport.request_path.assert_not_called()
    await service.stop()
