from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mesh_bbs.adapters.base import IncomingMessage, QueuedRadioAdapter
from mesh_bbs.adapters.meshcore import MeshCoreAdapter
from mesh_bbs.adapters.meshtastic import MeshtasticAdapter
from mesh_bbs.supervision import RadioSupervisor, radio_readiness


async def reply(message: IncomingMessage) -> str:
    return "OK"


async def test_meshcore_failed_start_cleanup_prevents_a_second_connection(monkeypatch) -> None:
    class Client:
        def __init__(self):
            self.commands = self
            self.close_attempts = 0

        async def get_contacts(self):
            raise ConnectionError("Handshake interrupted")

        async def stop_auto_message_fetching(self):
            pass

        async def disconnect(self):
            self.close_attempts += 1
            raise OSError("Connection remains open")

    client = Client()
    sdk = types.ModuleType("meshcore")
    connect = AsyncMock(return_value=client)
    sdk.MeshCore = SimpleNamespace(create_serial=connect)
    sdk.EventType = SimpleNamespace(ERROR="error", CONTACT_MSG_RECV="direct")
    monkeypatch.setitem(sys.modules, "meshcore", sdk)
    retry = AsyncMock(side_effect=AssertionError("Must not reopen after failed cleanup"))
    supervisor = RadioSupervisor(
        "meshcore", lambda: MeshCoreAdapter(reply, serial_port="fake"), sleep=retry
    )

    await supervisor.run()

    assert supervisor.snapshot()["state"] == "failed"
    connect.assert_awaited_once()
    retry.assert_not_awaited()
    assert client.close_attempts >= 1


async def test_meshtastic_failed_start_cleanup_prevents_a_second_connection(monkeypatch) -> None:
    class Client:
        myInfo = None

        def __init__(self):
            self.close_attempts = 0

        def close(self):
            self.close_attempts += 1
            raise OSError("Connection remains open")

    client = Client()
    connections = []

    def connect(**kwargs):
        connections.append(kwargs)
        return client

    sdk = types.ModuleType("meshtastic")
    protobuf = types.ModuleType("meshtastic.protobuf")
    protobuf.portnums_pb2 = SimpleNamespace(PortNum=SimpleNamespace(TEXT_MESSAGE_APP=1))
    serial = types.ModuleType("meshtastic.serial_interface")
    serial.SerialInterface = connect
    pubsub = types.ModuleType("pubsub")
    pubsub.pub = SimpleNamespace()
    for name, module in (
        ("meshtastic", sdk),
        ("meshtastic.protobuf", protobuf),
        ("meshtastic.serial_interface", serial),
        ("pubsub", pubsub),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    retry = AsyncMock(side_effect=AssertionError("Must not reopen after failed cleanup"))
    supervisor = RadioSupervisor(
        "meshtastic", lambda: MeshtasticAdapter(reply, serial_port="fake"), sleep=retry
    )

    await supervisor.run()

    assert supervisor.snapshot()["state"] == "failed"
    assert len(connections) == 1
    retry.assert_not_awaited()
    assert client.close_attempts >= 1


async def test_parent_cancellation_is_not_swallowed_while_stopping_worker() -> None:
    handling, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(message):
        handling.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
        return "OK"

    class Radio(QueuedRadioAdapter):
        async def send_reply(self, message, text):
            pass

    radio = Radio(handler, max_bytes=160)
    radio._start_worker()
    assert radio.enqueue(IncomingMessage("test", "reader", "help"))
    await asyncio.wait_for(handling.wait(), 1)
    stopping = asyncio.create_task(radio._stop_worker())
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 1)
    finally:
        release.set()
        await radio._stop_worker()
    assert radio._worker is None
    await asyncio.wait_for(radio.drain(), 1)


async def test_readiness_is_degraded_before_cancelled_connection_finishes_closing() -> None:
    closing, release = asyncio.Event(), asyncio.Event()

    class Radio:
        connected = False

        async def start(self):
            self.connected = True

        async def stop(self):
            self.connected = False
            closing.set()
            await release.wait()

    radio = Radio()
    supervisor = RadioSupervisor("test", lambda: radio)
    running = asyncio.create_task(supervisor.run())
    try:
        for _ in range(100):
            if supervisor.snapshot()["state"] == "online":
                break
            await asyncio.sleep(0)
        assert radio_readiness({"test": supervisor})[0]
        running.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        assert not radio.connected
        assert not radio_readiness({"test": supervisor})[0]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 1)
    assert supervisor.snapshot()["state"] == "stopped"


async def test_cleanup_that_swallows_cancellation_does_not_restart_the_radio() -> None:
    closing = asyncio.Event()
    starts = []

    class Radio:
        connected = False

        async def start(self):
            starts.append(self)
            raise ConnectionError("Radio unavailable")

        async def stop(self):
            closing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    retry = AsyncMock(side_effect=AssertionError("Cancelled supervisor must not retry"))
    supervisor = RadioSupervisor("test", Radio, sleep=retry)
    running = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(closing.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 1)
    assert supervisor.snapshot()["state"] == "stopped"
    assert len(starts) == 1
    retry.assert_not_awaited()
