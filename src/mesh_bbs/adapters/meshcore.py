"""MeshCore companion-radio DMs, using meshcore 2.3.14.

MeshCore exposes a six-byte sender prefix, not a message ID. Resolve the
prefix against exactly one full contact key. Names and channel traffic cannot
identify authors. Opt-in channel notices direct readers to DMs; no Room Server is used.

API: https://github.com/meshcore-dev/meshcore_py/tree/v2.3.14
160-byte limit: MeshCore src/helpers/BaseChatMesh.h and composeMsgPacket().
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from mesh_bbs.advertisements import AdvertSchedule
from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.announcements import AnnouncementOutbox

from .base import (
    DeliveryError,
    IncomingMessage,
    MessageHandler,
    QueuedRadioAdapter,
    validate_connection,
)

LOG = logging.getLogger(__name__)


def parse_meshcore_message(
    payload: Mapping[str, Any], contacts: Mapping[str, Any]
) -> IncomingMessage | None:
    if payload.get("type") != "PRIV" or payload.get("txt_type") != 0:
        return None
    text = payload.get("text")
    prefix = payload.get("pubkey_prefix")
    if not isinstance(text, str) or not text or "\x00" in text:
        return None
    if len(text.encode("utf-8")) > 160 or not isinstance(prefix, str):
        return None
    prefix = prefix.lower()
    if len(prefix) != 12 or any(c not in "0123456789abcdef" for c in prefix):
        return None
    keys = set()
    for contact in contacts.values():
        if not isinstance(contact, Mapping):
            continue
        key = contact.get("public_key")
        if not isinstance(key, str):
            continue
        key = key.lower()
        if len(key) == 64 and all(c in "0123456789abcdef" for c in key) and key.startswith(prefix):
            keys.add(key)
    if len(keys) != 1:
        return None
    return IncomingMessage("meshcore", "meshcore:" + keys.pop(), text)


class MeshCoreAdapter(QueuedRadioAdapter):
    def __init__(
        self,
        handler: MessageHandler,
        *,
        serial_port: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int = 4000,
        max_bytes: int = 160,
        queue_size: int = 32,
        min_interval: float = 3.0,
        max_attempts: int = 2,
        airtime_limiter: AirtimeLimiter | None = None,
        announcements: AnnouncementOutbox | None = None,
        announcement_channel: int | None = None,
        announcement_channel_name: str | None = None,
        advert_interval_seconds: int = 0,
        advert_state_path: Path | None = None,
    ) -> None:
        if type(advert_interval_seconds) is not int or (
            advert_interval_seconds != 0 and not 3600 <= advert_interval_seconds <= 604800
        ):
            raise ValueError("Advertisement interval must be 0 or between 3600 and 604800 seconds")
        if advert_interval_seconds and (advert_state_path is None or airtime_limiter is None):
            raise ValueError("Advertisements require persistent schedule and airtime state")
        if announcements is not None and (
            airtime_limiter is None or announcement_channel is None or not announcement_channel_name
        ):
            raise ValueError("Announcements require persistent airtime state and a named channel")
        self.announcements = announcements
        self.announcement_channel = announcement_channel
        self.announcement_channel_name = announcement_channel_name
        validate_connection(serial_port, tcp_host, tcp_port)
        if not 64 <= max_bytes <= 160:
            raise ValueError("MeshCore max_bytes must be between 64 and 160")
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        super().__init__(
            handler,
            max_bytes=max_bytes,
            queue_size=queue_size,
            min_interval=min_interval,
            airtime_limiter=airtime_limiter,
            transmission_attempts=max_attempts,
        )
        self.serial_port = serial_port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.max_attempts = max_attempts
        self._client: Any = None
        self._subscription: Any = None
        self._send_lock = asyncio.Lock()
        self._advert_task: asyncio.Task[None] | None = None
        self.advert_interval_seconds = advert_interval_seconds
        self._advert_schedule = (
            AdvertSchedule(advert_state_path)
            if advert_interval_seconds and advert_state_path is not None
            else None
        )

    @property
    def connected(self) -> bool:
        client, worker = self._client, self._worker
        return bool(client is not None and client.is_connected and worker and not worker.done())

    async def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("adapter is already started")
        from meshcore import EventType, MeshCore

        if self.serial_port:
            connecting = asyncio.create_task(MeshCore.create_serial(self.serial_port))
        else:
            connecting = asyncio.create_task(MeshCore.create_tcp(self.tcp_host, self.tcp_port))
        try:
            client = await asyncio.shield(connecting)
        except asyncio.CancelledError:
            # The factory owns its connection until it returns; finish before closing it.
            try:
                self._client = await connecting
            finally:
                await self.stop()
            raise
        if client is None:
            raise ConnectionError("Could not connect to MeshCore companion")
        self._client = client
        try:
            await self._sync_clock()
            client.auto_update_contacts = True
            result = await client.commands.get_contacts()
            if result.type == EventType.ERROR:
                raise ConnectionError("Could not load MeshCore contacts")
            self._subscription = client.subscribe(EventType.CONTACT_MSG_RECV, self._receive)
            self._start_worker()
            await client.start_auto_message_fetching()
            self._start_announcements(
                self.announcements, self._prepare_announcement, self._send_announcement
            )
            if self._advert_schedule is not None:
                self._advert_task = asyncio.create_task(self._advert_loop())
        except BaseException:
            await self.stop()
            raise

    def _receive(self, event: Any) -> None:
        if self._client is None or not isinstance(event.payload, Mapping):
            return
        message = parse_meshcore_message(event.payload, self._client.contacts)
        if message is not None:
            self.enqueue(message)

    async def send_reply(self, message: IncomingMessage, text: str) -> None:
        if self._client is None:
            raise DeliveryError("MeshCore adapter is disconnected")
        if "\x00" in text or len(text.encode("utf-8")) > self.max_bytes:
            raise ValueError("MeshCore reply exceeds configured byte limit")
        async with self._send_lock:
            await self._space_transmission()
            try:
                result = await self._client.commands.send_msg_with_retry(
                    message.sender.removeprefix("meshcore:"),
                    text,
                    max_attempts=self.max_attempts,
                    max_flood_attempts=self.max_attempts,
                    flood_after=self.max_attempts,
                    min_timeout=self.min_interval,
                )
            finally:
                self._last_send = time.monotonic()
        if result is None:
            raise DeliveryError("MeshCore reply was not acknowledged")

    async def _prepare_announcement(self) -> int:
        from meshcore import EventType

        async with self._send_lock:
            channel = await self._client.commands.get_channel(self.announcement_channel)
            if (
                channel.type != EventType.CHANNEL_INFO
                or channel.payload.get("channel_name") != self.announcement_channel_name
            ):
                raise DeliveryError("MeshCore announcement channel does not match configuration")
            name = self.announcement_channel_name
            if (
                name
                and name.startswith("#")
                and channel.payload.get("channel_secret") != sha256(name.encode()).digest()[:16]
            ):
                raise DeliveryError("MeshCore hashtag channel has an unexpected key")
            return min(self.max_bytes, 160 - len(self._client.self_info["name"].encode()) - 2)

    async def _send_announcement(self, text: str) -> None:
        from meshcore import EventType

        async with self._send_lock:
            await self._space_transmission()
            try:
                result = await self._client.commands.send_chan_msg(self.announcement_channel, text)
                if result.type != EventType.OK:
                    raise DeliveryError("MeshCore channel notice was not accepted")
            finally:
                self._last_send = time.monotonic()

    async def _sync_clock(self) -> None:
        from meshcore import EventType

        now = int(time.time())
        current = await self._client.commands.get_time()
        if current.type != EventType.CURRENT_TIME:
            raise ConnectionError("Could not read MeshCore companion clock")
        if abs(current.payload["time"] - now) <= 5:
            return
        result = await self._client.commands.set_time(now)
        if result.type != EventType.OK:
            raise ConnectionError("Could not synchronize MeshCore companion clock")

    async def _space_transmission(self) -> None:
        await asyncio.sleep(max(0, self.min_interval - (time.monotonic() - self._last_send)))

    async def _advert_loop(self) -> None:
        from meshcore import EventType

        schedule, limiter = self._advert_schedule, self.airtime_limiter
        assert schedule is not None and limiter is not None
        while True:
            try:
                delay = schedule.delay(time.time(), self.advert_interval_seconds)
                if delay:
                    await asyncio.sleep(min(delay, 3600))
                    continue
                reservation = await limiter.acquire(1)
                try:
                    async with self._send_lock:
                        await self._space_transmission()
                        await self._sync_clock()
                        schedule.mark_attempt(time.time())
                        try:
                            result = await self._client.commands.send_advert(flood=True)
                        finally:
                            self._last_send = time.monotonic()
                        if result.type != EventType.OK:
                            raise DeliveryError("MeshCore flood advert was not accepted")
                        LOG.info("Scheduled MeshCore flood advert accepted by companion")
                finally:
                    limiter.finish(reservation)
            except Exception:
                LOG.exception("Scheduled MeshCore advertisement failed")
                await asyncio.sleep(60)

    async def stop(self) -> None:
        try:
            if self._advert_task is not None:
                self._advert_task.cancel()
                await asyncio.gather(self._advert_task, return_exceptions=True)
                self._advert_task = None
            await self._stop_worker()
        finally:
            client = self._client
            if client is not None:
                try:
                    if self._subscription is not None:
                        client.unsubscribe(self._subscription)
                        self._subscription = None
                    await client.stop_auto_message_fetching()
                finally:
                    await client.disconnect()
                    self._client = None
