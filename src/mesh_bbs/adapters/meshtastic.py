"""Meshtastic DMs using meshtastic 2.7.11, with firmware-managed retries.

The node number is a gateway-attested address, not a verified human identity.
Native 32-bit packet IDs identify radio retries only for a limited time; the
application must not use them as permanent post IDs. Writes need their own
operation IDs. Only exact help on the configured announcement channel is handled
publicly; posting and reading happen in DMs.

API: https://github.com/meshtastic/python/tree/2.7.11/meshtastic
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Mapping
from typing import Any

from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.announcements import AnnouncementOutbox
from mesh_bbs.channel_help import ChannelHelpGate, instructions

from .base import (
    DeliveryError,
    IncomingMessage,
    MessageHandler,
    QueuedRadioAdapter,
    validate_connection,
)

LOG = logging.getLogger(__name__)


def channel_help_fingerprint(packet: Mapping[str, Any], local: int, channel: int) -> str | None:
    sender, packet_id = packet.get("from"), packet.get("id")
    decoded = packet.get("decoded")
    if (
        type(sender) is not int
        or not 0 < sender < 0xFFFFFFFF
        or sender == local
        or type(packet_id) is not int
        or not 0 < packet_id <= 0xFFFFFFFF
        or packet.get("to") != 0xFFFFFFFF
        or type(packet.get("channel", 0)) is not int
        or packet.get("channel", 0) != channel
        or not isinstance(decoded, Mapping)
        or decoded.get("portnum") != "TEXT_MESSAGE_APP"
        or not isinstance(decoded.get("text"), str)
        or len(decoded["text"].encode()) > 233
        or decoded["text"].strip().casefold() != "help"
    ):
        return None
    return f"{channel}:{sender:08x}:{packet_id:08x}"


def parse_meshtastic_message(
    packet: Mapping[str, Any], local_node_num: int
) -> IncomingMessage | None:
    sender = packet.get("from")
    if type(sender) is not int or not 0 < sender < 0xFFFFFFFF:
        return None
    if sender == local_node_num or packet.get("to") != local_node_num:
        return None
    decoded = packet.get("decoded")
    if not isinstance(decoded, Mapping) or decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return None
    text = decoded.get("text")
    if not isinstance(text, str) or not text or "\x00" in text:
        return None
    if len(text.encode("utf-8")) > 233:
        return None
    packet_id = packet.get("id")
    message_id = (
        f"{packet_id:08x}" if type(packet_id) is int and 0 < packet_id <= 0xFFFFFFFF else None
    )
    return IncomingMessage("meshtastic", f"meshtastic:{sender:08x}", text, message_id)


class MeshtasticAdapter(QueuedRadioAdapter):
    def __init__(
        self,
        handler: MessageHandler,
        *,
        serial_port: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int = 4403,
        max_bytes: int = 160,
        queue_size: int = 32,
        min_interval: float = 3.0,
        max_attempts: int = 1,
        ack_timeout: float = 90.0,
        airtime_limiter: AirtimeLimiter | None = None,
        announcements: AnnouncementOutbox | None = None,
        announcement_channel: int | None = None,
        announcement_channel_name: str | None = None,
        firmware_max_attempts: int = 4,
    ) -> None:
        if announcements is not None and (
            airtime_limiter is None or announcement_channel is None or not announcement_channel_name
        ):
            raise ValueError("Announcements require persistent airtime state and a named channel")
        self.announcements = announcements
        self.announcement_channel = announcement_channel
        self.announcement_channel_name = announcement_channel_name
        self._help_gate = (
            ChannelHelpGate(announcements.store, "meshtastic") if announcements else None
        )
        self._help_slot = threading.BoundedSemaphore(1)
        self._channel_help_task: asyncio.Task[None] | None = None
        validate_connection(serial_port, tcp_host, tcp_port)
        if not 64 <= max_bytes <= 233:
            raise ValueError("Meshtastic max_bytes must be between 64 and 233")
        if max_attempts != 1:
            raise ValueError("Meshtastic firmware owns retries; max_attempts must be 1")
        if not math.isfinite(ack_timeout) or not 0 < ack_timeout <= 300:
            raise ValueError("ack_timeout must be between 0 and 300 seconds")
        if type(firmware_max_attempts) is not int or not 1 <= firmware_max_attempts <= 10:
            raise ValueError("firmware_max_attempts must be between 1 and 10")
        super().__init__(
            handler,
            max_bytes=max_bytes,
            queue_size=queue_size,
            min_interval=min_interval,
            airtime_limiter=airtime_limiter,
            transmission_attempts=firmware_max_attempts,
        )
        self.serial_port = serial_port
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.ack_timeout = ack_timeout
        self._send_lock = asyncio.Lock()
        self._client: Any = None
        self._pub: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._portnum: Any = None

    @property
    def connected(self) -> bool:
        client, worker = self._client, self._worker
        return bool(
            client is not None and client.isConnected.is_set() and worker and not worker.done()
        )

    async def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("adapter is already started")
        from meshtastic.protobuf import portnums_pb2
        from pubsub import pub

        self._loop = asyncio.get_running_loop()
        self._portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        if self.serial_port:
            from meshtastic.serial_interface import SerialInterface

            connecting = asyncio.create_task(
                asyncio.to_thread(SerialInterface, devPath=self.serial_port, timeout=30)
            )
        else:
            from meshtastic.tcp_interface import TCPInterface

            connecting = asyncio.create_task(
                asyncio.to_thread(
                    TCPInterface, hostname=self.tcp_host, portNumber=self.tcp_port, timeout=30
                )
            )
        try:
            client = await asyncio.shield(connecting)
        except asyncio.CancelledError:
            # The constructor's thread continues after cancellation and may open a radio.
            try:
                self._client = await connecting
            finally:
                await self.stop()
            raise
        self._client = client
        try:
            if client.myInfo is None:
                raise ConnectionError("Meshtastic did not report its local node number")
            self._start_worker()
            pub.subscribe(self._receive, "meshtastic.receive.text")
            self._pub = pub
            self._start_announcements(
                self.announcements, self._prepare_announcement, self._send_announcement
            )
        except BaseException:
            await self.stop()
            raise

    def _receive(self, packet: Any, interface: Any) -> None:
        if interface is not self._client or self._loop is None or not self._accepting:
            return
        if not isinstance(packet, Mapping):
            return
        if interface.myInfo is None:
            return
        message = parse_meshtastic_message(packet, interface.myInfo.my_node_num)
        if message is not None:
            self.enqueue_threadsafe(self._loop, message)
        elif self._help_gate is not None and self.announcement_channel is not None:
            fingerprint = channel_help_fingerprint(
                packet, interface.myInfo.my_node_num, self.announcement_channel
            )
            if fingerprint is not None and self._help_slot.acquire(blocking=False):
                try:
                    self._loop.call_soon_threadsafe(self._start_channel_help, fingerprint)
                except RuntimeError:
                    self._help_slot.release()

    def _start_channel_help(self, fingerprint: str) -> None:
        if not self._accepting:
            self._help_slot.release()
            return
        self._channel_help_task = asyncio.create_task(self._answer_channel_help(fingerprint))
        self._channel_help_task.add_done_callback(lambda _: self._help_slot.release())

    async def _answer_channel_help(self, fingerprint: str) -> None:
        gate, limiter = self._help_gate, self.airtime_limiter
        assert gate is not None and limiter is not None
        reservation = None
        try:
            if not gate.available(fingerprint, time.time()):
                return
            max_bytes = await self._prepare_announcement()
            text = instructions(f"!{self._client.myInfo.my_node_num:08x}", max_bytes)
            reservation, _ = limiter.try_reserve(self.transmission_attempts)
            if reservation is None or not gate.claim(fingerprint, time.time()):
                return
            await self._send_announcement(text)
            LOG.info("Meshtastic channel help accepted by companion")
        except Exception:
            LOG.exception("Meshtastic channel help failed; no uncertain send retry")
        finally:
            if reservation is not None:
                limiter.finish(reservation)

    async def send_reply(self, message: IncomingMessage, text: str) -> None:
        async with self._send_lock:
            await self._space_transmission()
            try:
                await self._send_reply(message, text)
            finally:
                self._last_send = time.monotonic()

    async def _send_reply(self, message: IncomingMessage, text: str) -> None:
        if self._client is None:
            raise DeliveryError("Meshtastic adapter is disconnected")
        if "\x00" in text or len(text.encode("utf-8")) > self.max_bytes:
            raise ValueError("Meshtastic reply exceeds configured byte limit")
        loop = asyncio.get_running_loop()
        acknowledged: asyncio.Future[None] = loop.create_future()

        def finish(packet: Any) -> None:
            if acknowledged.done():
                return
            decoded = packet.get("decoded") if isinstance(packet, Mapping) else None
            routing = decoded.get("routing") if isinstance(decoded, Mapping) else None
            if not isinstance(routing, Mapping):
                acknowledged.set_exception(DeliveryError("Unexpected Meshtastic response"))
            elif routing.get("errorReason", "NONE") != "NONE":
                acknowledged.set_exception(DeliveryError("Meshtastic returned a delivery error"))
            else:
                acknowledged.set_result(None)

        def on_response(packet: Any) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, packet)

        client = self._client
        sending = asyncio.create_task(
            asyncio.to_thread(
                client.sendData,
                text.encode("utf-8"),
                destinationId=int(message.sender.removeprefix("meshtastic:"), 16),
                portNum=self._portnum,
                wantAck=True,
                wantResponse=False,
                onResponse=on_response,
                onResponseAckPermitted=True,
            )
        )
        try:
            await asyncio.shield(sending)
            await asyncio.wait_for(acknowledged, timeout=self.ack_timeout)
        except TimeoutError as exc:
            raise DeliveryError("Meshtastic reply acknowledgement timed out") from exc
        finally:
            # A cancelled to_thread call keeps running. Let it finish registering
            # its callback before removing it and closing the interface.
            try:
                await sending
            finally:
                acknowledged.cancel()
                if not acknowledged.cancelled():
                    acknowledged.exception()
                for packet_id, handler in list(client.responseHandlers.items()):
                    if handler.callback is on_response:
                        client.responseHandlers.pop(packet_id, None)

    async def _space_transmission(self) -> None:
        await asyncio.sleep(max(0, self.min_interval - (time.monotonic() - self._last_send)))

    async def _prepare_announcement(self) -> int:
        channels = self._client.localNode.channels
        channel = next((c for c in channels if c.index == self.announcement_channel), None)
        if (
            channel is None
            or not channel.role
            or channel.settings.name != self.announcement_channel_name
        ):
            raise DeliveryError("Meshtastic announcement channel does not match configuration")
        return self.max_bytes

    async def _send_announcement(self, text: str) -> None:
        async with self._send_lock:
            await self._space_transmission()
            sending = asyncio.create_task(
                asyncio.to_thread(
                    self._client.sendData,
                    text.encode("utf-8"),
                    destinationId="^all",
                    portNum=self._portnum,
                    channelIndex=self.announcement_channel,
                    wantAck=False,
                    wantResponse=False,
                )
            )
            try:
                await asyncio.shield(sending)
            finally:
                try:
                    await sending  # Do not close the radio under a still-running SDK call.
                finally:
                    self._last_send = time.monotonic()

    async def stop(self) -> None:
        self._accepting = False
        try:
            if self._pub is not None:
                self._pub.unsubscribe(self._receive, "meshtastic.receive.text")
                self._pub = None
            if self._channel_help_task is not None:
                self._channel_help_task.cancel()
                await asyncio.gather(self._channel_help_task, return_exceptions=True)
                self._channel_help_task = None
            await self._stop_worker()
        finally:
            self._loop = None
            client = self._client
            if client is not None:
                await asyncio.to_thread(client.close)
                self._client = None
