"""MeshCore companion-radio DMs, using meshcore 2.3.14.

MeshCore exposes a six-byte sender prefix, not a message ID. Resolve the
prefix against exactly one full contact key. Names and channel traffic cannot
identify authors. No Room Server commands or channel transmissions are used.

API: https://github.com/meshcore-dev/meshcore_py/tree/v2.3.14
160-byte limit: MeshCore src/helpers/BaseChatMesh.h and composeMsgPacket().
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from mesh_bbs.airtime import AirtimeLimiter

from .base import (
    DeliveryError,
    IncomingMessage,
    MessageHandler,
    QueuedRadioAdapter,
    validate_connection,
)


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
    ) -> None:
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
            client.auto_update_contacts = True
            result = await client.commands.get_contacts()
            if result.type == EventType.ERROR:
                raise ConnectionError("Could not load MeshCore contacts")
            self._subscription = client.subscribe(EventType.CONTACT_MSG_RECV, self._receive)
            self._start_worker()
            await client.start_auto_message_fetching()
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
        result = await self._client.commands.send_msg_with_retry(
            message.sender.removeprefix("meshcore:"),
            text,
            max_attempts=self.max_attempts,
            max_flood_attempts=self.max_attempts,
            flood_after=self.max_attempts,
            min_timeout=self.min_interval,
        )
        if result is None:
            raise DeliveryError("MeshCore reply was not acknowledged")

    async def stop(self) -> None:
        try:
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
