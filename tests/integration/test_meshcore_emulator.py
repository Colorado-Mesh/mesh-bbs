"""The real MeshCore SDK and BBS against a loopback companion wire emulator.

Wire layout verified against meshcore 2.3.14: tcp_cx.py, reader.py, and
commands/{device,contact,messaging}.py. The corresponding primary sources are
https://github.com/meshcore-dev/meshcore_py/tree/v2.3.14 and
https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md.
This exercises protocol framing and application integration, not firmware or RF.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from mesh_bbs.adapters.meshcore import MeshCoreAdapter
from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.announcements import AnnouncementOutbox
from mesh_bbs.commands import CommandService
from mesh_bbs.store import Store
from mesh_bbs.supervision import RadioSupervisor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

READER_KEY = bytes.fromhex("aabbccddeeff" + "01" * 26)
HOST_KEY = bytes.fromhex("11" * 32)
ACTOR = "meshcore:" + READER_KEY.hex()


@dataclass(frozen=True)
class SentText:
    attempt: int
    timestamp: int
    destination: bytes
    text: str


class CompanionEmulator:
    """Only the companion commands this adapter uses, on a private TCP socket."""

    def __init__(self):
        self.server = None
        self.writer = None
        self.connections = 0
        self.handshakes = []
        self.commands = []
        self.channel_notices = asyncio.Queue(maxsize=16)
        self.incoming = deque()
        self.outgoing = asyncio.Queue(maxsize=64)
        self.tasks = set()
        self.errors = []
        self.drop_ack_attempts = 0
        self.reject_attempts = 0
        self.disconnect_on_send = False
        self.early_ack = False
        self._sequence = 0

    async def start(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def _accept(self, reader, writer):
        task = asyncio.create_task(self._serve(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _serve(self, reader, writer):
        self.connections += 1
        try:
            assert self.connections <= 4, "Unbounded adapter reconnect loop"
            assert self.writer is None, "Two clients own the emulated radio"
            self.writer = writer
            while True:
                async with asyncio.timeout(10):
                    header = await reader.readexactly(3)
                    assert header[:1] == b"<"
                    length = int.from_bytes(header[1:], "little")
                    assert 0 < length <= 300
                    payload = await reader.readexactly(length)
                self.commands.append(payload)
                assert len(self.commands) <= 128, "Unbounded companion command loop"
                await self._command(payload)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()
            if self.writer is writer:
                self.writer = None

    async def frames(self, *payloads, fragmented=False):
        assert self.writer is not None
        encoded = b"".join(b">" + struct.pack("<H", len(data)) + data for data in payloads)
        if fragmented:
            for part in (encoded[:1], encoded[1:3], encoded[3:9], encoded[9:]):
                self.writer.write(part)
                await self.writer.drain()
                await asyncio.sleep(0)
        else:
            self.writer.write(encoded)
            await self.writer.drain()

    async def _command(self, payload):
        command = payload[0]
        if command == 1:
            assert payload == b"\x01\x03      mccli"
            self.handshakes.append(payload)
            await self.frames(
                b"\x05\x01\x14\x16"
                + HOST_KEY
                + struct.pack("<ii", 0, 0)
                + b"\x00\x00\x00\x01"
                + struct.pack("<II", 910525, 62500)
                + b"\x07\x05BBS emulator",
                fragmented=True,
            )
        elif command == 5:
            await self.frames(b"\x09" + struct.pack("<I", 1700000000))
        elif command == 6:
            assert abs(int.from_bytes(payload[1:], "little") - time.time()) < 5
            await self.frames(b"\x00")
        elif command == 31:
            assert payload == b"\x1f\x01"
            await self.frames(
                b"\x12\x01" + b"#bbs".ljust(32, b"\x00") + sha256(b"#bbs").digest()[:16]
            )
        elif command == 3:
            assert payload[1:3] == b"\x00\x01"
            assert abs(int.from_bytes(payload[3:7], "little") - time.time()) < 5
            text = payload[7:].decode()
            assert len(payload[7:]) <= 160 - len("BBS emulator: ")
            self.channel_notices.put_nowait(text)
            await self.frames(b"\x00")
        elif command == 4:
            assert payload == b"\x04"
            contact = (
                b"\x03"
                + READER_KEY
                + b"\x01\x00\x00"
                + bytes(64)
                + b"Reader".ljust(32, b"\x00")
                + struct.pack("<IiiI", 1700000000, 0, 0, 1700000000)
            )
            # A single TCP write contains the start, contact, and end frames.
            await self.frames(b"\x02\x01\x00\x00\x00", contact, b"\x04\x00\xf1\x53\x65")
        elif command == 10:
            assert payload == b"\x0a"
            await self.frames(self.incoming.popleft() if self.incoming else b"\x0a")
        elif command == 2:
            assert payload[1] == 0
            assert 13 < len(payload) <= 173
            message = SentText(
                payload[2],
                int.from_bytes(payload[3:7], "little"),
                payload[7:13],
                payload[13:].decode("utf-8"),
            )
            assert message.destination == READER_KEY[:6]
            assert message.attempt < 2
            self.outgoing.put_nowait(message)
            if self.disconnect_on_send:
                self.disconnect_on_send = False
                self.writer.close()
                return
            if self.reject_attempts:
                self.reject_attempts -= 1
                await self.frames(b"\x01\x03")
                return
            self._sequence += 1
            ack_code = struct.pack("<I", self._sequence)
            sent = b"\x06\x00" + ack_code + struct.pack("<I", 20)
            ack = b"\x82" + ack_code + struct.pack("<I", 5)
            if self.drop_ack_attempts:
                self.drop_ack_attempts -= 1
                await self.frames(sent)
            elif self.early_ack:
                await self.frames(ack, sent)
            else:
                await self.frames(sent, ack)
        else:
            raise AssertionError(f"Unexpected companion command: {payload.hex()}")

    async def deliver(self, text):
        assert len(text.encode("utf-8")) <= 160
        assert len(self.incoming) < 16
        self.incoming.append(
            b"\x10\x08\x00\x00"
            + READER_KEY[:6]
            + b"\xff\x00"
            + struct.pack("<I", 1700000000)
            + text.encode("utf-8")
        )
        await self.frames(b"\x83", fragmented=True)

    async def next_reply(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def deliver_channel(self, text, channel=1):
        self.incoming.append(
            b"\x11\x08\x00\x00"
            + bytes([channel, 0xFF, 0])
            + struct.pack("<I", 1790186400)
            + text.encode()
        )
        await self.frames(b"\x83", fragmented=True)

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.writer is not None:
            self.writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not self.errors, self.errors


@asynccontextmanager
async def connected_bbs(tmp_path: Path):
    pytest.importorskip("meshcore")
    store = Store(tmp_path / "bbs.sqlite3", "colorado-mesh")
    service = CommandService(store)

    async def command(message):
        assert message.protocol == "meshcore"
        assert message.sender == ACTOR
        assert message.message_id is None
        return service.handle(
            message.sender, message.text, request_id=message.message_id, max_bytes=160
        )

    radio = CompanionEmulator()
    adapter = None
    try:
        async with asyncio.timeout(15):
            port = await radio.start()
            adapter = MeshCoreAdapter(command, tcp_host="127.0.0.1", tcp_port=port, min_interval=0)
            await adapter.start()
            yield radio, adapter, store
    finally:
        try:
            if adapter is not None:
                await asyncio.wait_for(adapter.stop(), 3)
        finally:
            await radio.close()
            store.close()


async def exchange(radio, adapter, text, *, attempts=1):
    await radio.deliver(text)
    sent = [await radio.next_reply() for _ in range(attempts)]
    await asyncio.wait_for(adapter.drain(), 2)
    assert [message.attempt for message in sent] == list(range(attempts))
    assert len({message.timestamp for message in sent}) == 1
    assert len({message.text for message in sent}) == 1
    assert all(len(message.text.encode("utf-8")) <= 160 for message in sent)
    return sent[-1].text


async def draft(radio, adapter):
    created = await exchange(radio, adapter, "@draft new general Foothills meetup")
    assert created.startswith("Draft ")
    return created.split()[1].rstrip(".")


async def test_sdk_publish_lost_ack_retry_and_utf8_paged_read(tmp_path):
    async with connected_bbs(tmp_path) as (radio, adapter, store):
        assert adapter._client.contacts[READER_KEY.hex()]["adv_name"] == "Reader"
        assert adapter._client.self_info["public_key"] == HOST_KEY.hex()
        draft_id = await draft(radio, adapter)
        parts = ["é" * 50, "Bring a radio. " * 6]
        for number, part in enumerate(parts, 1):
            response = await exchange(
                radio, adapter, f"@part-{number} add {draft_id} {number} {part}"
            )
            assert response.startswith(f"Saved part {number}.")
        radio.drop_ack_attempts = 1
        saved = await exchange(radio, adapter, f"@publish publish {draft_id}", attempts=2)
        assert saved.startswith("Saved locally as ")
        post_id = saved.split()[3].rstrip(".")
        radio.early_ack = True
        assert await exchange(radio, adapter, f"@publish publish {draft_id}") == saved
        assert len(store.list_posts("general")) == 1
        post = store.get_post(post_id)
        assert post.author == ACTOR
        assert post.body == "\n".join(parts).strip()

        first = await exchange(radio, adapter, f"@read read {post_id}")
        assert first.endswith("\n[more]")
        assert await exchange(radio, adapter, f"@read read {post_id}") == first
        second = await exchange(radio, adapter, "@page-2 more")
        assert await exchange(radio, adapter, "@page-2 more") == second
        assert first.removesuffix("\n[more]") + second == (
            f"{post_id} Foothills meetup\n{post.body}"
        )
        assert adapter.failed == 0 and adapter.acknowledged == 9
        assert radio.outgoing.empty()
        assert len(radio.handshakes) == 1


async def test_sdk_firmware_rejects_delivery_without_duplicate_publish(tmp_path):
    async with connected_bbs(tmp_path) as (radio, adapter, store):
        draft_id = await draft(radio, adapter)
        await exchange(radio, adapter, f"@part add {draft_id} 1 Saturday at the park")
        radio.reject_attempts = 2
        response = await exchange(radio, adapter, f"@publish publish {draft_id}", attempts=2)
        assert adapter.failed == 1
        assert len(store.list_posts("general")) == 1
        assert await exchange(radio, adapter, f"@publish publish {draft_id}") == response
        assert len(store.list_posts("general")) == 1
        assert adapter.acknowledged == 3
        assert radio.outgoing.empty()


async def test_sdk_disconnect_after_publish_and_reconnect_replays_receipt(tmp_path):
    pytest.importorskip("meshcore")
    from meshcore import EventType

    radio = CompanionEmulator()
    store = Store(tmp_path / "bbs.sqlite3", "colorado-mesh")
    service = CommandService(store)
    budget = AirtimeLimiter(
        tmp_path / "airtime.sqlite3",
        "meshcore",
        budget_seconds=10,
        window_seconds=60,
        packet_airtime_seconds=1,
    )
    adapters = []
    task = None

    async def command(message):
        assert message.sender == ACTOR
        return service.handle(message.sender, message.text, max_bytes=160)

    async def online(supervisor, connections):
        async with asyncio.timeout(2):
            while len(adapters) != connections or supervisor.snapshot()["state"] != "online":
                assert not task.done(), "Radio supervisor stopped unexpectedly"
                await asyncio.sleep(0.005)
        return adapters[-1]

    try:
        async with asyncio.timeout(15):
            port = await radio.start()

            def factory():
                adapter = MeshCoreAdapter(
                    command,
                    tcp_host="127.0.0.1",
                    tcp_port=port,
                    min_interval=0,
                    airtime_limiter=budget,
                )
                adapters.append(adapter)
                return adapter

            supervisor = RadioSupervisor(
                "meshcore",
                factory,
                initial_delay=0.01,
                maximum_delay=0.1,
                poll_interval=0.01,
                stable_seconds=0.1,
            )
            task = asyncio.create_task(supervisor.run())
            original = await online(supervisor, 1)
            draft_id = await draft(radio, original)
            await exchange(radio, original, f"@part add {draft_id} 1 Saved through an outage")
            disconnected = asyncio.Event()
            client = original._client
            client.subscribe(EventType.DISCONNECTED, lambda event: disconnected.set())
            radio.disconnect_on_send = True
            await radio.deliver(f"@publish publish {draft_id}")
            lost_reply = await radio.next_reply()
            await asyncio.wait_for(disconnected.wait(), 2)
            assert not original.connected
            assert len(store.list_posts("general")) == 1

            recovered = await online(supervisor, 2)
            assert not client.dispatcher.running
            assert recovered is not original
            assert recovered._client.contacts[READER_KEY.hex()]["adv_name"] == "Reader"
            assert original.airtime_limiter is recovered.airtime_limiter is budget
            retried = await exchange(radio, recovered, f"@publish publish {draft_id}")
            assert retried == lost_reply.text
            post_id = lost_reply.text.split()[3].rstrip(".")
            assert await exchange(radio, recovered, f"read {post_id}") == (
                f"{post_id} Foothills meetup\nSaved through an outage"
            )
            assert len(store.list_posts("general")) == 1
            assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
            assert len(radio.handshakes) == 2
            assert supervisor.snapshot()["reconnections"] == 1
            # Five accepted commands reserve two attempts each. Recreating the
            # SDK connection must not erase the pre-outage airtime debt.
            reservation, delay = budget.try_reserve(1)
            assert reservation is None and 50 < delay <= 60
    finally:
        try:
            if task is not None:
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        finally:
            budget.close()
            store.close()
            await radio.close()


async def test_sdk_channel_notice_is_one_bounded_packet_and_points_to_dm(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    service = CommandService(store)
    box = AnnouncementOutbox(store, "meshcore")
    post = store.publish("local:a", "new", "general", "New meeting", "Body")
    radio = CompanionEmulator()
    budget = AirtimeLimiter(tmp_path / "airtime.db", "test:meshcore")
    adapter = None
    try:
        port = await radio.start()

        async def command(message):
            return service.handle(message.sender, message.text, request_id=message.message_id)

        adapter = MeshCoreAdapter(
            command,
            tcp_host="127.0.0.1",
            tcp_port=port,
            min_interval=0,
            airtime_limiter=budget,
            announcements=box,
            announcement_channel=1,
            announcement_channel_name="#bbs",
        )
        await adapter.start()
        text = await asyncio.wait_for(radio.channel_notices.get(), 3)
        assert "private message: read #1" in text
        assert text.startswith('New post in general:\n"New meeting"')
        assert store.get_post("#1").post_id == post.post_id
        assert "Body" in await exchange(radio, adapter, "read #1")
        assert not box.poll(time.time())
    finally:
        if adapter:
            await adapter.stop()
        await radio.close()
        budget.close()
        store.close()


async def test_sdk_channel_help_then_dm_help_and_more(tmp_path):
    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshcore")
    service = CommandService(store)
    radio = CompanionEmulator()
    budget = AirtimeLimiter(tmp_path / "airtime.db", "test:meshcore")
    adapter = None

    async def command(message):
        assert message.sender == ACTOR
        return service.handle(message.sender, message.text)

    try:
        port = await radio.start()
        adapter = MeshCoreAdapter(
            command,
            tcp_host="127.0.0.1",
            tcp_port=port,
            min_interval=0,
            airtime_limiter=budget,
            announcements=box,
            announcement_channel=1,
            announcement_channel_name="#bbs",
        )
        await adapter.start()
        await radio.deliver_channel("Reader: help")
        text = await asyncio.wait_for(radio.channel_notices.get(), 3)
        assert "Send BBS emulator a private message: help" in text
        assert "Choose a number" in text
        first = await exchange(radio, adapter, "help")
        assert "1 News & newsletters" in first
        assert "3 Write/resume" in first
        assert "general" in await exchange(radio, adapter, "2")
        await radio.deliver_channel("Reader: help")  # Repeated packet stays silent.
        await radio.deliver_channel("Other: help", channel=0)
        assert "general" in await exchange(radio, adapter, "boards")
        assert radio.channel_notices.empty()
        assert not store.list_posts("general")
        assert adapter.failed == 0
    finally:
        if adapter:
            await adapter.stop()
        await radio.close()
        budget.close()
        store.close()


async def test_guided_menu_create_board_and_long_post_over_meshcore(tmp_path):
    async with connected_bbs(tmp_path) as (radio, adapter, store):
        messages = [
            ("help", "1 News"),
            ("3", "general"),
            ("2", "board name"),
            ("hiking", "title"),
            ("Saturday hike", "text"),
            ("Meet at nine. " * 10, "saved"),
            ("Bring water.", "saved"),
            ("done", "DRAFT:"),
            ("next", "publish |"),
            ("publish", "Posted"),
            ("publish", "Already posted"),
        ]
        for command, expected in messages:
            response = await exchange(radio, adapter, command)
            assert expected in response, response
        posts = store.list_posts("hiking")
        assert len(posts) == 1 and posts[0].author == ACTOR
        assert posts[0].body.endswith("\nBring water.")
        assert radio.outgoing.empty()
