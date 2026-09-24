from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from mesh_bbs.supervision import RadioSupervisor, radio_readiness


class Radio:
    def __init__(self, events: list[str], name: str, error: Exception | None = None) -> None:
        self.events, self.name, self.error = events, name, error
        self.connected = False

    async def start(self) -> None:
        self.events.append("start:" + self.name)
        if self.error:
            raise self.error
        self.connected = True

    async def stop(self) -> None:
        self.events.append("stop:" + self.name)
        self.connected = False


async def cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


async def test_failures_back_off_and_cleanup_precedes_replacement() -> None:
    events: list[str] = []
    delays: list[float] = []
    connected = asyncio.Event()
    radios = iter(
        [Radio(events, str(i), OSError("private device path")) for i in range(4)]
        + [Radio(events, "working")]
    )

    async def sleep(delay: float) -> None:
        if delay == 0.01:
            connected.set()
            await asyncio.Event().wait()
        delays.append(delay)

    supervisor = RadioSupervisor(
        "test",
        lambda: next(radios),
        initial_delay=1,
        maximum_delay=4,
        poll_interval=0.01,
        sleep=sleep,
    )
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(connected.wait(), 1)
    assert delays == [1, 2, 4, 4]
    assert events == [item for i in range(4) for item in (f"start:{i}", f"stop:{i}")] + [
        "start:working"
    ]
    assert supervisor.snapshot() == {
        "state": "online",
        "attempts": 5,
        "reconnections": 0,
        "retry_delay_seconds": 0,
    }
    assert "private" not in json.dumps(radio_readiness({"test": supervisor}))
    await cancel(task)
    assert events[-1] == "stop:working"
    assert supervisor.snapshot()["state"] == "stopped"


async def test_disconnect_reconnects_after_close_and_reports_readiness() -> None:
    events: list[str] = []
    first, second = Radio(events, "first"), Radio(events, "second")
    connections = iter((first, second))
    online = asyncio.Event()
    supervisor = RadioSupervisor(
        "test",
        lambda: next(connections),
        initial_delay=0.01,
        maximum_delay=0.1,
        poll_interval=0.01,
    )
    original_start = second.start

    async def start() -> None:
        await original_start()
        online.set()

    second.start = start
    assert radio_readiness({"test": supervisor})[0] is False
    task = asyncio.create_task(supervisor.run())
    try:
        for _ in range(100):
            if supervisor.snapshot()["state"] == "online":
                break
            await asyncio.sleep(0.001)
        assert radio_readiness({"test": supervisor})[0] is True
        first.connected = False
        await asyncio.wait_for(online.wait(), 1)
        assert supervisor.snapshot()["reconnections"] == 1
        assert events == ["start:first", "stop:first", "start:second"]
    finally:
        await cancel(task)
    assert events[-1] == "stop:second"
    assert radio_readiness({"test": supervisor})[0] is False


async def test_shutdown_interrupts_long_retry_wait() -> None:
    sleeping = asyncio.Event()
    events: list[str] = []

    async def sleep(delay: float) -> None:
        sleeping.set()
        await asyncio.sleep(delay)

    supervisor = RadioSupervisor(
        "test",
        lambda: Radio(events, "missing", OSError()),
        initial_delay=60,
        maximum_delay=60,
        sleep=sleep,
    )
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(sleeping.wait(), 1)
    assert supervisor.snapshot()["state"] == "retrying"
    await cancel(task)
    assert events == ["start:missing", "stop:missing"]


async def test_flapping_connection_keeps_backoff_until_a_stable_connection() -> None:
    events: list[str] = []
    radios = [
        Radio(events, "missing", OSError()),
        Radio(events, "flapping"),
        Radio(events, "stable"),
        Radio(events, "last"),
    ]
    attempts = []
    delays = []
    now = 0.0
    finished = asyncio.Event()

    def factory():
        radio = radios[len(attempts)]
        attempts.append(radio)
        return radio

    async def sleep(delay):
        nonlocal now
        if delay == 0.01:
            if attempts[-1] is radios[-1]:
                finished.set()
                await asyncio.Event().wait()
            now += 0.1 if attempts[-1] is radios[1] else 31
            attempts[-1].connected = False
        else:
            delays.append(delay)
            now += delay

    supervisor = RadioSupervisor(
        "test",
        factory,
        sleep=sleep,
        clock=lambda: now,
        poll_interval=0.01,
    )
    task = asyncio.create_task(supervisor.run())
    try:
        await asyncio.wait_for(finished.wait(), 1)
        assert delays == [1, 2, 1]
    finally:
        await cancel(task)


@pytest.mark.parametrize("error", [ImportError("missing library"), ValueError("bad config")])
async def test_configuration_failures_remain_visible_without_retry_storm(error: Exception) -> None:
    events: list[str] = []
    factory = Mock(return_value=Radio(events, "invalid", error))
    sleep = AsyncMock()
    supervisor = RadioSupervisor("test", factory, sleep=sleep)
    await supervisor.run()
    assert supervisor.snapshot()["state"] == "failed"
    factory.assert_called_once()
    sleep.assert_not_awaited()
    assert events == ["start:invalid", "stop:invalid"]


async def test_cleanup_failure_does_not_open_another_connection() -> None:
    events: list[str] = []
    adapter = Radio(events, "leaked", OSError())
    adapter.stop = AsyncMock(side_effect=OSError("cannot close"))
    factory = Mock(return_value=adapter)
    supervisor = RadioSupervisor("test", factory)
    await supervisor.run()
    assert supervisor.snapshot()["state"] == "failed"
    assert factory.call_count == 1


def test_local_only_host_is_ready() -> None:
    assert radio_readiness({}) == (True, {"status": "ready", "radios": {}})


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_recovery_intervals_are_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        RadioSupervisor("test", Mock(), initial_delay=value)


async def test_runtime_keeps_web_and_feeds_running_while_radio_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    from mesh_bbs import runtime
    from mesh_bbs.config import FeedConfig, HostConfig, RadioConfig
    from mesh_bbs.web import ReadOnlyWebServer

    servers: list[ReadOnlyWebServer] = []
    radio_started, feed_polled = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)

    def web(views, host, port, **kwargs):
        instance = ReadOnlyWebServer(views, host, 0, **kwargs)
        servers.append(instance)
        return instance

    budgets = []

    class MissingRadio(Radio):
        def __init__(self, handler, **options):
            super().__init__([], "missing", OSError("not attached"))
            budgets.append(options["airtime_limiter"])

        async def start(self):
            radio_started.set()
            await super().start()

    class Importer:
        def __init__(self, *_args):
            pass

        def poll_once(self):
            loop.call_soon_threadsafe(feed_polled.set)
            return Mock(status="not_modified")

    monkeypatch.setattr("mesh_bbs.web.ReadOnlyWebServer", web)
    monkeypatch.setattr("mesh_bbs.adapters.meshcore.MeshCoreAdapter", MissingRadio)
    monkeypatch.setattr("mesh_bbs.newsletters.NewsletterImporter", Importer)
    monkeypatch.setattr(
        runtime,
        "RadioSupervisor",
        partial(RadioSupervisor, initial_delay=0.01, maximum_delay=0.02, poll_interval=0.01),
    )
    config = HostConfig(
        "Recovery test",
        "test",
        tmp_path,
        meshcore=RadioConfig(enabled=True, serial_port="/dev/missing"),
        feeds=(FeedConfig("news", "https://example.org/feed"),),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.serve(config, stop=stop))
    try:
        await asyncio.wait_for(radio_started.wait(), 30)
        await asyncio.wait_for(feed_polled.wait(), 10)

        def get(path):
            connection = http.client.HTTPConnection(*servers[0].address, timeout=5)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        assert (await asyncio.to_thread(get, "/healthz"))[0] == 200
        code, body = await asyncio.to_thread(get, "/readyz")
        assert code == 503
        assert json.loads(body)["status"] == "degraded"
        assert b"/dev/" not in body
        assert (await asyncio.to_thread(get, "/boards/general"))[0] == 200
        for _ in range(100):
            if len(budgets) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(budgets) >= 2
        assert all(budget is budgets[0] for budget in budgets)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
    assert budgets[0]._closed


async def test_runtime_commits_meshcore_retry_receipt_with_bounded_lifetime(tmp_path, monkeypatch):
    import sqlite3
    import time

    from mesh_bbs import runtime
    from mesh_bbs.adapters.meshcore import parse_meshcore_message
    from mesh_bbs.config import HostConfig, RadioConfig
    from mesh_bbs.web import ReadOnlyWebServer

    ready = asyncio.Event()
    connected = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(
        "mesh_bbs.web.ReadOnlyWebServer",
        lambda views, host, port, **kwargs: ReadOnlyWebServer(views, host, 0, **kwargs),
    )

    class Companion(Radio):
        def __init__(self, handler, **options):
            super().__init__([], "companion")
            self.handler = handler
            connected.append(self)

        async def start(self):
            await super().start()
            ready.set()

    monkeypatch.setattr("mesh_bbs.adapters.meshcore.MeshCoreAdapter", Companion)
    config = HostConfig(
        "Retry test",
        "test",
        tmp_path,
        meshcore=RadioConfig(enabled=True, serial_port="/dev/fake"),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.serve(config, stop=stop))
    try:
        await asyncio.wait_for(ready.wait(), 10)
        key = "11" * 32
        message = parse_meshcore_message(
            dict(
                type="PRIV",
                txt_type=0,
                pubkey_prefix=key[:12],
                sender_timestamp=100,
                text="post general Test | One post",
            ),
            {key: {"public_key": key}},
        )
        before = time.time()
        reply = await connected[0].handler(message)
        assert await connected[0].handler(message) == reply
        with sqlite3.connect(f"file:{tmp_path / 'bbs.sqlite3'}?mode=ro", uri=True) as db:
            assert db.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
            operation, expiry = db.execute(
                "SELECT operation,expires FROM command_receipts"
            ).fetchone()
            assert operation == message.message_id
            assert before + 86400 <= expiry <= time.time() + 86400
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


@pytest.mark.parametrize("fails", [False, True])
async def test_runtime_manual_announce_keeps_service_running_and_limits_repeats(
    tmp_path, monkeypatch, caplog, fails
):
    import signal

    from mesh_bbs import runtime
    from mesh_bbs.config import HostConfig, ReticulumConfig

    callbacks = {}
    removed = []
    events = []
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    web = Mock()
    monkeypatch.setattr("mesh_bbs.web.ReadOnlyWebServer", lambda *args, **kwargs: web)

    class Reticulum:
        addresses = {"nomadnet": "test"}

        def __init__(self, **kwargs):
            pass

        async def start(self):
            events.append("start")
            started.set()

        def announce(self):
            events.append("announce")
            if fails:
                raise OSError("interface unavailable")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr("mesh_bbs.adapters.reticulum.ReticulumAdapter", Reticulum)
    config = HostConfig(
        "Test BBS", "test", tmp_path, reticulum=ReticulumConfig(True, tmp_path / "rns")
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.serve(config, stop=stop))
    try:
        await asyncio.wait_for(started.wait(), 5)
        callbacks[signal.SIGUSR1]()
        callbacks[signal.SIGUSR1]()
        assert events == ["start", "announce"]
        assert not task.done()
        web.stop.assert_not_called()
        if fails:
            assert "Manual Reticulum announce failed" in caplog.text
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert events == ["start", "announce", "stop"]
    assert signal.SIGUSR1 in removed


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic"])
async def test_only_designated_host_gets_announcements(tmp_path, monkeypatch, protocol):
    from mesh_bbs import runtime
    from mesh_bbs.cli import open_store
    from mesh_bbs.config import HostConfig, RadioConfig
    from mesh_bbs.web import ReadOnlyWebServer

    configs = [HostConfig("Test", "test", tmp_path / str(n)) for n in range(2)]
    with_store = open_store(configs[0])
    owner = with_store.origin
    with_store.close()
    created = []
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)

    class TestRadio(Radio):
        def __init__(self, handler, **options):
            super().__init__([], "test")
            created.append(options)

        async def start(self):
            await super().start()
            if len(created) == 2:
                ready.set()

    def web(views, host, port, **kwargs):
        return ReadOnlyWebServer(views, host, 0, **kwargs)

    monkeypatch.setattr("mesh_bbs.web.ReadOnlyWebServer", web)
    adapter_name = "MeshCoreAdapter" if protocol == "meshcore" else "MeshtasticAdapter"
    monkeypatch.setattr(f"mesh_bbs.adapters.{protocol}.{adapter_name}", TestRadio)
    radio = RadioConfig(
        enabled=True,
        serial_port="/dev/fake",
        announcement_owner=owner,
        announcement_channel=1,
        announcement_channel_name="BBS",
    )
    from dataclasses import replace

    configs = [replace(c, **{protocol: radio}) for c in configs]
    stop = asyncio.Event()
    tasks = [asyncio.create_task(runtime.serve(c, stop=stop)) for c in configs]
    try:
        await asyncio.wait_for(ready.wait(), 5)
        active = [c["announcements"] for c in created if c["announcements"] is not None]
        assert len(active) == 1 and active[0].store.origin == owner
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
