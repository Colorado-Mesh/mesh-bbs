"""Meshtastic SDK TCP/protobuf emulation on loopback; no firmware or RF proof.

The framing, startup exchange, queue status and routing replies follow the
installed meshtastic 2.7.11 stream_interface.py and mesh_interface.py. SDK methods
are not replaced: TCPInterface serializes, parses and dispatches every packet.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mesh_bbs.adapters.base import IncomingMessage
from mesh_bbs.adapters.meshtastic import MeshtasticAdapter
from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.announcements import AnnouncementOutbox
from mesh_bbs.commands import CommandService
from mesh_bbs.store import Store
from mesh_bbs.supervision import RadioSupervisor

mesh_pb2 = pytest.importorskip("meshtastic.protobuf.mesh_pb2")
portnums_pb2 = pytest.importorskip("meshtastic.protobuf.portnums_pb2")
channel_pb2 = pytest.importorskip("meshtastic.protobuf.channel_pb2")

pytestmark = pytest.mark.integration
LOCAL_NODE = 0xAABBCCDD
REMOTE_NODE = 0x11112222
REMOTE_ACTOR = f"meshtastic:{REMOTE_NODE:08x}"
FRAME_MAGIC = b"\x94\xc3"


class MeshtasticRadioEmulator:
    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.finished = asyncio.Event()
        self.failure: Exception | None = None
        self.outgoing: asyncio.Queue[Any] = asyncio.Queue(maxsize=32)
        self.config_ids: list[int] = []
        self.auto_ack = True
        self.packet_id = 0x10000000

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port, limit=1024)
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server:
            self.server.close()
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            await asyncio.wait_for(self.finished.wait(), timeout=3)
        if self.server:
            await asyncio.wait_for(self.server.wait_closed(), timeout=3)
        assert self.failure is None, f"Meshtastic protocol emulator failed: {self.failure!r}"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        try:
            assert await reader.readexactly(32) == b"\xc3" * 32
            while True:
                header = await reader.readexactly(4)
                assert header[:2] == FRAME_MAGIC
                size = int.from_bytes(header[2:], "big")
                assert 0 < size <= 512
                message = mesh_pb2.ToRadio.FromString(await reader.readexactly(size))
                kind = message.WhichOneof("payload_variant")
                if kind == "want_config_id":
                    await self._configure(message.want_config_id)
                elif kind == "packet":
                    packet = message.packet
                    assert packet.decoded.portnum == portnums_pb2.TEXT_MESSAGE_APP
                    if packet.to == 0xFFFFFFFF:
                        assert packet.channel == 1
                        assert not packet.want_ack and not packet.decoded.want_response
                    else:
                        assert packet.to == REMOTE_NODE
                        assert packet.want_ack and not packet.decoded.want_response
                    assert 0 < len(packet.decoded.payload) <= 160
                    await self._send(
                        mesh_pb2.FromRadio(
                            queueStatus=mesh_pb2.QueueStatus(
                                free=16, maxlen=16, mesh_packet_id=packet.id
                            )
                        )
                    )
                    if self.auto_ack and packet.want_ack:
                        await self.acknowledge(packet.id)
                    self.outgoing.put_nowait(packet)
                else:
                    assert kind in {"heartbeat", "disconnect"}
                    if kind == "disconnect":
                        return
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                self.failure = exc
        except Exception as exc:
            self.failure = exc
        finally:
            writer.close()
            await writer.wait_closed()
            self.finished.set()

    async def _send(self, message: Any) -> None:
        assert self.writer is not None
        payload = message.SerializeToString()
        assert 0 < len(payload) <= 512
        self.writer.write(FRAME_MAGIC + len(payload).to_bytes(2, "big") + payload)
        await self.writer.drain()

    async def _configure(self, config_id: int) -> None:
        self.config_ids.append(config_id)
        await self._send(mesh_pb2.FromRadio(my_info=mesh_pb2.MyNodeInfo(my_node_num=LOCAL_NODE)))
        for node, name in ((LOCAL_NODE, "BBS emulator"), (REMOTE_NODE, "Reader emulator")):
            await self._send(
                mesh_pb2.FromRadio(
                    node_info=mesh_pb2.NodeInfo(
                        num=node,
                        user=mesh_pb2.User(id=f"!{node:08x}", long_name=name),
                    )
                )
            )
        await self._send(
            mesh_pb2.FromRadio(
                channel=channel_pb2.Channel(
                    index=0,
                    role=channel_pb2.Channel.PRIMARY,
                    settings=channel_pb2.ChannelSettings(name="emulator"),
                )
            )
        )
        await self._send(
            mesh_pb2.FromRadio(
                channel=channel_pb2.Channel(
                    index=1,
                    role=channel_pb2.Channel.SECONDARY,
                    settings=channel_pb2.ChannelSettings(name="BBS"),
                )
            )
        )
        config = mesh_pb2.FromRadio()
        config.config.lora.hop_limit = 3
        await self._send(config)
        await self._send(mesh_pb2.FromRadio(config_complete_id=config_id))

    async def text(self, text: str, packet_id: int, *, to: int = LOCAL_NODE) -> None:
        assert len(text.encode("utf-8")) <= 233
        message = mesh_pb2.FromRadio()
        setattr(message.packet, "from", REMOTE_NODE)
        message.packet.to = to
        message.packet.id = packet_id
        message.packet.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
        message.packet.decoded.payload = text.encode("utf-8")
        await self._send(message)

    async def acknowledge(self, request_id: int, *, error_reason: int = 0) -> None:
        self.packet_id += 1
        message = mesh_pb2.FromRadio()
        setattr(message.packet, "from", REMOTE_NODE)
        message.packet.to = LOCAL_NODE
        message.packet.id = self.packet_id
        message.packet.decoded.portnum = portnums_pb2.ROUTING_APP
        message.packet.decoded.request_id = request_id
        message.packet.decoded.payload = mesh_pb2.Routing(
            error_reason=error_reason
        ).SerializeToString()
        await self._send(message)

    async def response(self) -> str:
        packet = await asyncio.wait_for(self.outgoing.get(), timeout=3)
        return str(packet.decoded.payload.decode("utf-8"))

    async def command(self, text: str, packet_id: int) -> str:
        await self.text(text, packet_id)
        return await self.response()


@dataclass
class RadioSession:
    radio: MeshtasticRadioEmulator
    adapter: MeshtasticAdapter
    store: Store
    received: list[IncomingMessage]


@pytest.fixture
async def radio_session(tmp_path: Path) -> AsyncIterator[RadioSession]:
    store = Store(tmp_path / "bbs.sqlite3", "test-mesh")
    commands = CommandService(store)
    received: list[IncomingMessage] = []

    async def handle(message: IncomingMessage) -> str:
        received.append(message)
        return commands.handle(
            message.sender,
            message.text,
            request_id=message.message_id,
            request_ttl_seconds=86400,
            max_bytes=160,
        )

    radio = MeshtasticRadioEmulator()
    adapter = None
    try:
        port = await radio.start()
        adapter = MeshtasticAdapter(
            handle, tcp_host="127.0.0.1", tcp_port=port, min_interval=0, ack_timeout=1
        )
        await asyncio.wait_for(adapter.start(), timeout=5)
        assert radio.config_ids
        assert adapter._client.myInfo.my_node_num == LOCAL_NODE
        assert adapter._client.isConnected.is_set()
        yield RadioSession(radio, adapter, store, received)
    finally:
        try:
            if adapter is not None:
                await asyncio.wait_for(adapter.stop(), timeout=5)
        finally:
            try:
                await radio.close()
            finally:
                store.close()


async def test_meshtastic_sdk_publishes_retries_and_reads_through_tcp(
    radio_session: RadioSession,
) -> None:
    radio, adapter, store = radio_session.radio, radio_session.adapter, radio_session.store
    first = await radio.command("new general Trail cleanup", 101)
    match = re.search(r"\b[0-9a-f]{8}\b", first)
    assert match, first
    draft = match[0]
    assert await radio.command("new general Trail cleanup", 101) == first
    assert store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1

    parts = ["Bring water. " + "é" * 85, "Meet Saturday. " + "あ" * 55]
    for number, part in enumerate(parts, start=1):
        assert (await radio.command(f"add {draft} {number} {part}", 102 + number)).startswith(
            "Saved part"
        )
    saved = await radio.command(f"publish {draft}", 105)
    assert saved.startswith("Saved locally"), saved
    assert await radio.command(f"publish {draft}", 105) == saved
    posts = store.list_posts("general")
    assert len(posts) == 1
    assert posts[0].author == REMOTE_ACTOR
    assert posts[0].body == "\n".join(parts)

    # A following DM acts as an ordered barrier through the SDK publisher thread.
    await radio.text("new general Broadcast must not create a draft", 106, to=0xFFFFFFFF)
    assert "general" in await radio.command("boards", 107)
    assert store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    assert all(message.message_id != "0000006a" for message in radio_session.received)

    page = await radio.command(f"read {posts[0].post_id[:12]}", 108)
    pages = []
    for packet_id in range(109, 120):
        assert len(page.encode("utf-8")) <= 160
        pages.append(page.removesuffix("\n[more]"))
        if not page.endswith("\n[more]"):
            break
        page = await radio.command("more", packet_id)
    else:
        pytest.fail("a bounded post did not finish paging")
    assert "".join(pages) == f"{posts[0].post_id[:12]} Trail cleanup\n{posts[0].body}"
    await asyncio.wait_for(adapter.drain(), timeout=3)
    assert adapter.failed == 0
    assert adapter.acknowledged == len(radio_session.received)
    assert adapter._client.responseHandlers == {}
    assert radio.outgoing.empty()


@pytest.mark.parametrize(
    "routing_error", [None, mesh_pb2.Routing.NO_ROUTE], ids=["lost-ack", "nak"]
)
async def test_meshtastic_failed_delivery_keeps_saved_operation_retryable(
    radio_session: RadioSession,
    routing_error: int | None,
) -> None:
    radio, adapter, store = radio_session.radio, radio_session.adapter, radio_session.store
    radio.auto_ack = False
    await radio.text("new general Lost confirmation", 201)
    packet = await asyncio.wait_for(radio.outgoing.get(), timeout=3)
    first = packet.decoded.payload.decode("utf-8")
    assert first.startswith("Draft ")
    if routing_error is not None:
        await radio.acknowledge(packet.id, error_reason=routing_error)
    await asyncio.wait_for(adapter.drain(), timeout=3)
    assert adapter.failed == 1
    assert adapter.acknowledged == 0
    assert adapter._client.responseHandlers == {}
    assert store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1

    radio.auto_ack = True
    assert await radio.command("new general Lost confirmation", 201) == first
    await asyncio.wait_for(adapter.drain(), timeout=3)
    assert adapter.acknowledged == 1
    assert adapter.failed == 1
    assert store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    assert adapter._client.responseHandlers == {}
    assert radio.outgoing.empty()


async def test_meshtastic_supervisor_recovers_publication_after_tcp_radio_outage(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "recovery.sqlite3", "test-mesh")
    commands = CommandService(store)
    limiter = AirtimeLimiter(
        tmp_path / "airtime.sqlite3",
        "emulated-meshtastic",
        budget_seconds=20,
        window_seconds=60,
        packet_airtime_seconds=1,
    )
    first_radio, recovered_radio = MeshtasticRadioEmulator(), MeshtasticRadioEmulator()
    adapters: list[MeshtasticAdapter] = []
    supervising: asyncio.Task[None] | None = None

    async def handle(message: IncomingMessage) -> str:
        return commands.handle(
            message.sender,
            message.text,
            request_id=message.message_id,
            request_ttl_seconds=86400,
            max_bytes=160,
        )

    try:
        port = await first_radio.start()

        def connect() -> MeshtasticAdapter:
            adapter = MeshtasticAdapter(
                handle,
                tcp_host="127.0.0.1",
                tcp_port=port,
                min_interval=0,
                ack_timeout=1,
                airtime_limiter=limiter,
            )
            adapters.append(adapter)
            return adapter

        supervisor = RadioSupervisor(
            "meshtastic",
            connect,
            initial_delay=0.1,
            maximum_delay=0.2,
            poll_interval=0.01,
            stable_seconds=0.1,
        )
        supervising = asyncio.create_task(supervisor.run())

        async def state(expected: str) -> None:
            async with asyncio.timeout(5):
                while supervisor.snapshot()["state"] != expected:
                    assert supervising is not None and not supervising.done()
                    await asyncio.sleep(0.01)

        await state("online")
        draft_reply = await first_radio.command("new general Radio outage", 301)
        match = re.search(r"\b[0-9a-f]{8}\b", draft_reply)
        assert match, draft_reply
        draft = match[0]
        assert (
            await first_radio.command(f"add {draft} 1 Saved before the radio failed.", 302)
        ).startswith("Saved part")
        first_radio.auto_ack = False
        saved = await first_radio.command(f"publish {draft}", 303)
        assert saved.startswith("Saved locally"), saved
        original = store.list_posts("general")[0]

        # Stop listening as well as the active stream so the SDK's own reconnect
        # fails. Recovery must construct a fresh TCPInterface and redo config.
        await first_radio.close()
        await state("retrying")
        assert not adapters[0].connected
        await recovered_radio.start(port)
        await state("online")
        assert len(adapters) >= 2
        assert recovered_radio.config_ids
        assert supervisor.snapshot()["reconnections"] == 1
        assert all(adapter.airtime_limiter is limiter for adapter in adapters)
        assert await recovered_radio.command(f"publish {draft}", 303) == saved
        assert await recovered_radio.command(f"read {original.post_id[:12]}", 304) == (
            f"{original.post_id[:12]} Radio outage\nSaved before the radio failed."
        )
        await asyncio.wait_for(adapters[-1].drain(), timeout=3)
        assert store.list_posts("general") == [original]
        assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert adapters[-1].acknowledged == 2
        assert adapters[-1]._client.responseHandlers == {}
        assert limiter.try_reserve(1)[0] is None
    finally:
        try:
            if supervising is not None:
                supervising.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(supervising, timeout=5)
        finally:
            try:
                await first_radio.close()
                await recovered_radio.close()
            finally:
                limiter.close()
                store.close()


@pytest.mark.asyncio
async def test_sdk_channel_notice_uses_configured_secondary_channel_without_ack(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshtastic")
    post = store.publish("local:a", "new", "news", "New issue", "Body")
    radio = MeshtasticRadioEmulator()
    budget = AirtimeLimiter(tmp_path / "airtime.db", "test:meshtastic")
    adapter = None
    try:
        port = await radio.start()

        async def command(message):
            return ""

        adapter = MeshtasticAdapter(
            command,
            tcp_host="127.0.0.1",
            tcp_port=port,
            min_interval=0,
            airtime_limiter=budget,
            announcements=box,
            announcement_channel=1,
            announcement_channel_name="BBS",
        )
        await adapter.start()
        packet = await asyncio.wait_for(radio.outgoing.get(), 5)
        assert packet.to == 0xFFFFFFFF and packet.channel == 1
        assert not packet.want_ack and not packet.decoded.want_response
        assert f"read {post.post_id[:12]}; more" in packet.decoded.payload.decode()
        assert not box.poll(time.time())
    finally:
        if adapter:
            await adapter.stop()
        await radio.close()
        budget.close()
        store.close()
