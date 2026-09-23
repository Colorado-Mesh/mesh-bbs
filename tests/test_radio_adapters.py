import asyncio
import sys
import threading
import time
import types
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from mesh_bbs.adapters.base import DeliveryError, IncomingMessage, QueuedRadioAdapter
from mesh_bbs.adapters.meshcore import MeshCoreAdapter, parse_meshcore_message
from mesh_bbs.adapters.meshtastic import MeshtasticAdapter, parse_meshtastic_message

KEY = "aabbccddeeff" + "01" * 26
CONTACTS = {KEY: {"public_key": KEY, "adv_name": "Alice"}}


def meshcore_payload(text="help", **changes):
    return dict(
        type="PRIV",
        txt_type=0,
        pubkey_prefix=KEY[:12],
        sender_timestamp=1700000000,
        text=text,
        **changes,
    )


def meshtastic_packet(text="help", **changes):
    packet = {
        "from": 0x1234,
        "to": 0x5678,
        "id": 100,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }
    packet.update(changes)
    return packet


async def help_handler(message):
    return "Send boards to list boards."


def test_incoming_message_is_immutable():
    message = IncomingMessage("meshcore", KEY, "help")
    with pytest.raises(FrozenInstanceError):
        message.text = "publish stolen"  # type: ignore[misc]


def test_meshcore_resolves_full_key_and_has_no_native_message_id():
    message = parse_meshcore_message(meshcore_payload(), CONTACTS)
    assert message == IncomingMessage("meshcore", f"meshcore:{KEY}", "help")
    assert parse_meshcore_message(meshcore_payload(), {}) is None
    other_key = KEY[:12] + "02" * 26
    colliding = CONTACTS | {other_key: {"public_key": other_key}}
    assert parse_meshcore_message(meshcore_payload(), colliding) is None


@pytest.mark.parametrize(
    "change",
    [
        {"type": "CHAN"},
        {"txt_type": 2},
        {"pubkey_prefix": ""},
        {"pubkey_prefix": "g" * 12},
        {"text": "x\x00publish"},
        {"text": "é" * 81},
    ],
)
def test_meshcore_rejects_unusable_events(change):
    payload = meshcore_payload()
    payload.update(change)
    assert parse_meshcore_message(payload, CONTACTS) is None


def test_meshcore_byte_limit_and_retry_timestamp_are_not_an_operation_id():
    payload = meshcore_payload("é" * 80)
    assert parse_meshcore_message(payload, CONTACTS).text == "é" * 80
    assert parse_meshcore_message(payload, CONTACTS).message_id is None


def test_meshtastic_identifies_address_not_display_name_and_preserves_native_id():
    packet = meshtastic_packet(fromId="Mismatched nickname")
    assert parse_meshtastic_message(packet, 0x5678) == IncomingMessage(
        "meshtastic", "meshtastic:00001234", "help", "00000064"
    )
    packet.pop("id")
    assert parse_meshtastic_message(packet, 0x5678).message_id is None
    packet["id"] = 0
    assert parse_meshtastic_message(packet, 0x5678).message_id is None


@pytest.mark.parametrize(
    "change",
    [
        {"to": 0xFFFFFFFF},
        {"to": 4},
        {"from": 0x5678},
        {"from": 0},
        {"from": "1234"},
        {"from": True},
        {"decoded": {}},
        {"decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"\xff"}},
        {"decoded": {"portnum": "POSITION_APP", "text": "publish"}},
    ],
)
def test_meshtastic_ignores_broadcast_echo_and_malformed_packets(change):
    assert parse_meshtastic_message(meshtastic_packet(**change), 0x5678) is None


def test_meshtastic_utf8_limit():
    assert parse_meshtastic_message(meshtastic_packet("é" * 116), 0x5678) is not None
    assert parse_meshtastic_message(meshtastic_packet("é" * 117), 0x5678) is None


class MemoryRadio(QueuedRadioAdapter):
    def __init__(self, handler, **kwargs):
        super().__init__(handler, max_bytes=160, **kwargs)
        self.sent = []
        self.send_times = []

    async def send_reply(self, message, text):
        self.sent.append((message, text))
        self.send_times.append(asyncio.get_running_loop().time())


@pytest.mark.asyncio
async def test_bounded_queue_spacing_and_stop():
    radio = MemoryRadio(help_handler, queue_size=2, min_interval=0.01)
    message = IncomingMessage("test", "alice", "help")
    assert not radio.enqueue(message)
    radio._start_worker()
    assert radio.enqueue(message)
    assert radio.enqueue(message)
    assert not radio.enqueue(message)
    await radio.drain()
    assert radio.dropped == 1
    assert radio.acknowledged == 2
    assert radio.send_times[1] - radio.send_times[0] >= 0.009
    await radio._stop_worker()
    assert not radio.enqueue(message)


@pytest.mark.asyncio
async def test_thread_callbacks_reserve_queue_capacity_before_scheduling():
    radio = MemoryRadio(help_handler, queue_size=2, min_interval=0)
    radio._start_worker()
    message = IncomingMessage("test", "alice", "help")
    loop = asyncio.get_running_loop()
    accepted = [radio.enqueue_threadsafe(loop, message) for _ in range(100)]
    assert sum(accepted) == 2
    assert radio.dropped == 98
    await asyncio.sleep(0)
    await radio.drain()
    assert len(radio.sent) == 2
    await radio._stop_worker()


@pytest.mark.asyncio
async def test_handler_failure_does_not_kill_worker_or_send_unbounded_response():
    calls = 0

    async def handler(message):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bad command")
        return "é" * 500

    radio = MemoryRadio(handler, min_interval=0)
    radio._start_worker()
    message = IncomingMessage("test", "alice", "help")
    radio.enqueue(message)
    radio.enqueue(message)
    await radio.drain()
    assert radio.failed == 1
    assert len(radio.sent) == 1
    assert radio.sent[0][1] == "Response too long for this radio. Ask for a smaller page."
    await radio._stop_worker()


class FakeMeshCore:
    def __init__(self):
        self.contacts = CONTACTS
        self.commands = self
        self.subscribers = {}
        self.sent = []
        self.closed = False
        self.fetching = False
        self.ack = True
        self.clock = 0
        self.adverts = []
        self.advert_sent = asyncio.Event()

    async def get_time(self):
        return SimpleNamespace(type="time", payload={"time": self.clock})

    async def set_time(self, timestamp):
        self.clock = timestamp
        return SimpleNamespace(type="ok")

    async def send_advert(self, *, flood):
        self.adverts.append(flood)
        self.advert_sent.set()
        return SimpleNamespace(type="ok")

    async def get_contacts(self):
        return SimpleNamespace(type="contacts", payload=self.contacts)

    def subscribe(self, event, callback):
        self.subscribers[event] = callback
        return event

    def unsubscribe(self, subscription):
        self.subscribers.pop(subscription)

    async def start_auto_message_fetching(self):
        self.fetching = True

    async def stop_auto_message_fetching(self):
        self.fetching = False

    async def disconnect(self):
        self.closed = True

    async def send_msg_with_retry(self, destination, text, **kwargs):
        self.sent.append((destination, text, kwargs))
        return SimpleNamespace(type="message_sent") if self.ack else None


def install_meshcore(monkeypatch, radio):
    module = types.ModuleType("meshcore")

    async def create_serial(port):
        assert port == "/dev/fake"
        return radio

    module.MeshCore = SimpleNamespace(create_serial=create_serial)
    module.EventType = SimpleNamespace(
        ERROR="error",
        OK="ok",
        CURRENT_TIME="time",
        CONTACT_MSG_RECV="direct",
        CHANNEL_MSG_RECV="channel",
        CHANNEL_INFO="channel_info",
    )
    monkeypatch.setitem(sys.modules, "meshcore", module)


@pytest.mark.asyncio
async def test_meshcore_lifecycle_and_real_api_contract(monkeypatch):
    radio = FakeMeshCore()
    install_meshcore(monkeypatch, radio)
    adapter = MeshCoreAdapter(help_handler, serial_port="/dev/fake", min_interval=0)
    await adapter.start()
    assert abs(radio.clock - time.time()) < 5
    assert "channel" not in radio.subscribers  # Non-announcing companions stay silent.
    with pytest.raises(RuntimeError):
        await adapter.start()
    radio.subscribers["direct"](SimpleNamespace(payload=meshcore_payload()))
    await adapter.drain()
    assert radio.sent[0][:2] == (KEY, "Send boards to list boards.")
    assert radio.sent[0][2]["max_attempts"] == 2
    assert radio.sent[0][2]["flood_after"] == 2
    assert adapter.acknowledged == 1
    radio.ack = False
    radio.subscribers["direct"](SimpleNamespace(payload=meshcore_payload()))
    await adapter.drain()
    assert adapter.failed == 1
    await adapter.stop()
    assert radio.closed and not radio.fetching and not radio.subscribers


@pytest.mark.asyncio
async def test_meshcore_shutdown_cancels_pending_channel_help(monkeypatch, tmp_path):
    from mesh_bbs.airtime import AirtimeLimiter
    from mesh_bbs.announcements import AnnouncementOutbox
    from mesh_bbs.store import Store

    store = Store(tmp_path / "bbs.db", "test")
    budget = AirtimeLimiter(tmp_path / "airtime.db", "test")
    radio = FakeMeshCore()
    install_meshcore(monkeypatch, radio)
    adapter = MeshCoreAdapter(
        help_handler,
        serial_port="/dev/fake",
        airtime_limiter=budget,
        announcements=AnnouncementOutbox(store, "meshcore"),
        announcement_channel=1,
        announcement_channel_name="#bbs",
    )
    try:
        await adapter.start()
        await adapter._send_lock.acquire()
        radio.subscribers["channel"](
            SimpleNamespace(
                payload={
                    "type": "CHAN",
                    "txt_type": 0,
                    "channel_idx": 1,
                    "sender_timestamp": 1790186400,
                    "text": "Reader: help",
                }
            )
        )
        pending = adapter._channel_help_task
        await asyncio.sleep(0)
        await asyncio.wait_for(adapter.stop(), 1)
        assert pending.cancelled()
        assert not radio.subscribers and radio.closed
        assert budget._db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    finally:
        adapter._send_lock.release()
        await adapter.stop()
        budget.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "uncertain", "wrong_channel", "busy", "short_packet"])
async def test_meshcore_channel_help_is_bounded_owned_and_restart_safe(
    monkeypatch, tmp_path, outcome
):
    from hashlib import sha256

    from mesh_bbs.airtime import AirtimeLimiter
    from mesh_bbs.announcements import AnnouncementOutbox
    from mesh_bbs.store import Store

    store = Store(tmp_path / "bbs.db", "test")
    box = AnnouncementOutbox(store, "meshcore")
    budget = AirtimeLimiter(tmp_path / "airtime.db", "test")
    radio = FakeMeshCore()
    radio.self_info = {"name": "coloradomesh.org-bbs"}
    notices = []

    async def get_channel(index):
        assert index == 1
        return SimpleNamespace(
            type="channel_info",
            payload={
                "channel_name": "#other" if outcome == "wrong_channel" else "#bbs",
                "channel_secret": sha256(b"#bbs").digest()[:16],
            },
        )

    async def send_channel(index, text):
        assert index == 1
        notices.append(text)
        if outcome == "uncertain":
            raise ConnectionError("lost serial response")
        return SimpleNamespace(type="ok")

    radio.get_channel = get_channel
    radio.send_chan_msg = send_channel
    install_meshcore(monkeypatch, radio)
    if outcome == "busy":
        held, _ = budget.try_reserve(10)
        budget.finish(held)
        held, _ = budget.try_reserve(2)
        budget.finish(held)

    async def never_handle_channel(message):
        pytest.fail("Channel text reached the authenticated DM command handler")

    try:
        for _attempt in range(2):
            adapter = MeshCoreAdapter(
                never_handle_channel,
                serial_port="/dev/fake",
                min_interval=0,
                max_bytes=64 if outcome == "short_packet" else 160,
                airtime_limiter=budget,
                announcements=box,
                announcement_channel=1,
                announcement_channel_name="#bbs",
            )
            await adapter.start()
            receive = radio.subscribers["channel"]
            event = SimpleNamespace(
                payload={
                    "type": "CHAN",
                    "txt_type": 0,
                    "channel_idx": 1,
                    "sender_timestamp": 1790186400,
                    "text": "Reader: help",
                }
            )
            receive(SimpleNamespace(payload={**event.payload, "channel_idx": 0}))
            assert adapter._channel_help_task is None
            receive(event)
            first = adapter._channel_help_task
            for _ in range(100):
                receive(event)
                assert adapter._channel_help_task is first
            await asyncio.wait_for(first, 1)
            receive(event)  # The same event after the previous task completed.
            await asyncio.wait_for(adapter._channel_help_task, 1)
            await adapter.stop()
            assert not radio.subscribers
        expected = outcome in {"ok", "uncertain", "short_packet"}
        assert len(notices) == int(expected)
        if expected:
            assert "help" in notices[0] and "number" in notices[0]
            assert len(notices[0].encode()) <= (64 if outcome == "short_packet" else 139)
            assert budget._db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
        assert (
            budget._db.execute(
                "SELECT count(*) FROM reservations WHERE expires IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert not store.list_posts("general")
    finally:
        await adapter.stop()
        budget.close()
        store.close()


@pytest.mark.asyncio
async def test_meshcore_advert_schedule_survives_reconnect(monkeypatch, tmp_path):
    from mesh_bbs.airtime import AirtimeLimiter

    budget = AirtimeLimiter(tmp_path / "airtime.db", "test")
    path = tmp_path / "advert.json"
    try:
        for expected in (True, False):
            radio = FakeMeshCore()
            install_meshcore(monkeypatch, radio)
            adapter = MeshCoreAdapter(
                help_handler,
                serial_port="/dev/fake",
                min_interval=0,
                advert_interval_seconds=86400,
                advert_state_path=path,
                airtime_limiter=budget,
            )
            try:
                await adapter.start()
                if expected:
                    await asyncio.wait_for(radio.advert_sent.wait(), 1)
                    assert radio.adverts == [True]
                else:
                    await asyncio.sleep(0)
                    assert radio.adverts == []
            finally:
                await adapter.stop()
        assert budget.try_reserve(10)[0] is not None
        assert budget.try_reserve(2)[0] is None  # Advertisement consumed one packet.
    finally:
        budget.close()


@pytest.mark.asyncio
async def test_meshcore_failed_clock_sync_closes_connection(monkeypatch):
    radio = FakeMeshCore()
    install_meshcore(monkeypatch, radio)

    async def rejected(timestamp):
        return SimpleNamespace(type="error")

    radio.set_time = rejected
    adapter = MeshCoreAdapter(help_handler, serial_port="/dev/fake")
    with pytest.raises(ConnectionError, match="synchronize"):
        await adapter.start()
    assert radio.closed
    assert not radio.fetching


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["serial", "tcp"])
async def test_meshcore_cancelled_start_closes_connection_when_factory_returns(
    monkeypatch, connection
):
    radio = FakeMeshCore()
    install_meshcore(monkeypatch, radio)
    started, release = asyncio.Event(), asyncio.Event()

    async def connect(*args):
        started.set()
        await release.wait()
        return radio

    monkeypatch.setattr(
        sys.modules["meshcore"].MeshCore, f"create_{connection}", connect, raising=False
    )
    options = {"serial_port": "/dev/fake"} if connection == "serial" else {"tcp_host": "fake"}
    adapter = MeshCoreAdapter(help_handler, **options)
    task = asyncio.create_task(adapter.start())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert radio.closed
    assert not radio.fetching and not radio.subscribers
    assert adapter._client is None and adapter._worker is None


class FakePub:
    def __init__(self):
        self.callbacks = {}

    def subscribe(self, callback, topic):
        self.callbacks[topic] = callback

    def unsubscribe(self, callback, topic):
        assert self.callbacks.pop(topic) == callback


class FakeMeshtastic:
    def __init__(self):
        self.myInfo = SimpleNamespace(my_node_num=0x5678)
        self.responseHandlers = {}
        self.sent = []
        self.closed = False
        self.error = "NONE"

    def close(self):
        self.closed = True

    def sendData(self, data, **kwargs):
        self.sent.append((data, kwargs))
        self.responseHandlers[123] = SimpleNamespace(callback=kwargs["onResponse"])
        if self.error is not None:
            kwargs["onResponse"](
                {"decoded": {"requestId": 123, "routing": {"errorReason": self.error}}}
            )
        return SimpleNamespace(id=123)


def install_meshtastic(monkeypatch, radio, pub):
    module = types.ModuleType("meshtastic")
    protobuf = types.ModuleType("meshtastic.protobuf")
    protobuf.portnums_pb2 = SimpleNamespace(PortNum=SimpleNamespace(TEXT_MESSAGE_APP=1))
    serial = types.ModuleType("meshtastic.serial_interface")

    def create_serial(*, devPath, timeout):
        assert devPath == "/dev/fake"
        assert timeout == 30
        return radio

    serial.SerialInterface = create_serial
    pubsub = types.ModuleType("pubsub")
    pubsub.pub = pub
    for name, value in {
        "meshtastic": module,
        "meshtastic.protobuf": protobuf,
        "meshtastic.serial_interface": serial,
        "pubsub": pubsub,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)


@pytest.mark.asyncio
async def test_meshtastic_lifecycle_early_ack_and_interface_filter(monkeypatch):
    radio, pub = FakeMeshtastic(), FakePub()
    install_meshtastic(monkeypatch, radio, pub)
    adapter = MeshtasticAdapter(help_handler, serial_port="/dev/fake", min_interval=0)
    await adapter.start()
    callback = pub.callbacks["meshtastic.receive.text"]
    callback(meshtastic_packet(), object())
    callback(meshtastic_packet(), radio)
    await asyncio.sleep(0)
    await adapter.drain()
    assert len(radio.sent) == 1
    data, sent = radio.sent[0]
    assert data == b"Send boards to list boards."
    assert sent["destinationId"] == 0x1234
    assert sent["wantAck"] and sent["onResponseAckPermitted"]
    assert sent["portNum"] == 1
    assert adapter.acknowledged == 1
    assert not radio.responseHandlers
    await adapter.stop()
    assert radio.closed and not pub.callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["serial", "tcp"])
async def test_meshtastic_cancelled_start_closes_connection_when_constructor_returns(
    monkeypatch, connection
):
    radio, pub = FakeMeshtastic(), FakePub()
    install_meshtastic(monkeypatch, radio, pub)
    started, release = threading.Event(), threading.Event()

    def connect(**kwargs):
        started.set()
        assert release.wait(2)
        return radio

    module = types.ModuleType(f"meshtastic.{connection}_interface")
    setattr(module, "SerialInterface" if connection == "serial" else "TCPInterface", connect)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    options = {"serial_port": "/dev/fake"} if connection == "serial" else {"tcp_host": "fake"}
    adapter = MeshtasticAdapter(help_handler, **options)
    task = asyncio.create_task(adapter.start())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert radio.closed
    assert not pub.callbacks
    assert adapter._client is None and adapter._worker is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["NO_RESPONSE", None])
async def test_meshtastic_nak_timeout_and_callback_cleanup(monkeypatch, error):
    radio, pub = FakeMeshtastic(), FakePub()
    radio.error = error
    install_meshtastic(monkeypatch, radio, pub)
    adapter = MeshtasticAdapter(help_handler, serial_port="/dev/fake", ack_timeout=0.01)
    await adapter.start()
    message = parse_meshtastic_message(meshtastic_packet(), 0x5678)
    with pytest.raises(DeliveryError):
        await adapter.send_reply(message, "hello")
    assert len(radio.sent) == 1
    assert not radio.responseHandlers
    await adapter.stop()


@pytest.mark.asyncio
async def test_meshtastic_cancelled_send_cleans_up_late_callback(monkeypatch):
    started, release = threading.Event(), threading.Event()

    class SlowMeshtastic(FakeMeshtastic):
        def sendData(self, data, **kwargs):
            started.set()
            assert release.wait(2)
            return super().sendData(data, **kwargs)

    radio, pub = SlowMeshtastic(), FakePub()
    install_meshtastic(monkeypatch, radio, pub)
    adapter = MeshtasticAdapter(help_handler, serial_port="/dev/fake")
    await adapter.start()
    message = parse_meshtastic_message(meshtastic_packet(), 0x5678)
    task = asyncio.create_task(adapter.send_reply(message, "hello"))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not radio.responseHandlers
    await adapter.stop()


@pytest.mark.asyncio
async def test_meshtastic_missing_local_info_closes_interface(monkeypatch):
    radio, pub = FakeMeshtastic(), FakePub()
    radio.myInfo = None
    install_meshtastic(monkeypatch, radio, pub)
    adapter = MeshtasticAdapter(help_handler, serial_port="/dev/fake")
    with pytest.raises(ConnectionError, match="local node number"):
        await adapter.start()
    assert radio.closed
    assert not pub.callbacks


@pytest.mark.parametrize(
    "adapter,kwargs",
    [
        (MeshCoreAdapter, {}),
        (MeshCoreAdapter, {"serial_port": "/dev/fake", "tcp_host": "localhost"}),
        (MeshCoreAdapter, {"serial_port": "/dev/fake", "max_bytes": 161}),
        (MeshCoreAdapter, {"serial_port": "/dev/fake", "max_attempts": 4}),
        (MeshtasticAdapter, {"serial_port": "/dev/fake", "max_bytes": 234}),
        (MeshtasticAdapter, {"serial_port": "/dev/fake", "max_attempts": 2}),
        (MeshtasticAdapter, {"serial_port": "/dev/fake", "queue_size": 0}),
        (MeshtasticAdapter, {"serial_port": "/dev/fake", "ack_timeout": float("inf")}),
    ],
)
def test_adapter_configuration_is_explicit_and_bounded(adapter, kwargs):
    with pytest.raises(ValueError):
        adapter(help_handler, **kwargs)
