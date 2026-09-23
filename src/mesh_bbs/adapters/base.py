"""Bounded request processing shared by the optional radio adapters."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.announcements import AnnouncementOutbox

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingMessage:
    protocol: str
    sender: str
    text: str
    message_id: str | None = None


MessageHandler = Callable[[IncomingMessage], Awaitable[str]]


class DeliveryError(RuntimeError):
    """The transport rejected a reply or did not acknowledge it in time."""


class QueuedRadioAdapter(ABC):
    """Process one request and one bounded reply at a time per radio."""

    def __init__(
        self,
        handler: MessageHandler,
        *,
        max_bytes: int,
        queue_size: int = 32,
        min_interval: float = 3.0,
        airtime_limiter: AirtimeLimiter | None = None,
        transmission_attempts: int = 1,
        per_sender_queue_size: int = 4,
        sender_requests_per_minute: int = 12,
        request_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= queue_size <= 1024:
            raise ValueError("queue_size must be between 1 and 1024")
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("min_interval must be finite and nonnegative")
        if type(per_sender_queue_size) is not int or not 1 <= per_sender_queue_size <= 1024:
            raise ValueError("per_sender_queue_size must be between 1 and 1024")
        if type(sender_requests_per_minute) is not int or not 1 <= sender_requests_per_minute <= 60:
            raise ValueError("sender_requests_per_minute must be between 1 and 60")
        if airtime_limiter is not None:
            airtime_limiter.validate_attempts(transmission_attempts)
        self._announcement_task: asyncio.Task[None] | None = None
        self.handler = handler
        self.max_bytes = max_bytes
        self.min_interval = min_interval
        self.airtime_limiter = airtime_limiter
        self.transmission_attempts = transmission_attempts
        self.per_sender_queue_size = per_sender_queue_size
        self.sender_requests_per_minute = sender_requests_per_minute
        self._request_clock = request_clock
        self._admission_lock = threading.Lock()
        self._sender_pending: dict[str, int] = {}
        self._sender_requests: dict[str, deque[float]] = {}
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(queue_size)
        self._slots = threading.BoundedSemaphore(queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False
        self._last_send = -math.inf
        self.dropped = 0
        self.rate_limited = 0
        self.failed = 0
        self.acknowledged = 0

    def enqueue(self, message: IncomingMessage) -> bool:
        """Called on the event loop; reject overload without spawning work."""
        if not self._reserve(message):
            return False
        self._enqueue_reserved(message)
        return True

    def enqueue_threadsafe(self, loop: asyncio.AbstractEventLoop, message: IncomingMessage) -> bool:
        """Reserve capacity before scheduling a radio thread's callback."""
        if not self._reserve(message):
            return False
        try:
            loop.call_soon_threadsafe(self._enqueue_reserved, message)
        except RuntimeError:
            self._release(message)
            return False
        return True

    def _reserve(self, message: IncomingMessage) -> bool:
        with self._admission_lock:
            if not self._accepting:
                return False
            now = self._request_clock()
            for sender, recent in list(self._sender_requests.items()):
                while recent and recent[0] <= now - 60:
                    recent.popleft()
                if not recent and not self._sender_pending.get(sender):
                    del self._sender_requests[sender]
            requests = self._sender_requests.get(message.sender)
            pending = self._sender_pending.get(message.sender, 0)
            if (
                pending >= self.per_sender_queue_size
                or (requests is not None and len(requests) >= self.sender_requests_per_minute)
                or (requests is None and len(self._sender_requests) >= 1024)
            ):
                self.dropped += 1
                self.rate_limited += 1
                return False
            if not self._slots.acquire(blocking=False):
                self.dropped += 1
                return False
            self._sender_requests.setdefault(message.sender, deque()).append(now)
            self._sender_pending[message.sender] = pending + 1
            return True

    def _release(self, message: IncomingMessage) -> None:
        with self._admission_lock:
            pending = self._sender_pending[message.sender] - 1
            if pending:
                self._sender_pending[message.sender] = pending
            else:
                del self._sender_pending[message.sender]
            self._slots.release()

    def _enqueue_reserved(self, message: IncomingMessage) -> None:
        if self._accepting:
            self._queue.put_nowait(message)
        else:
            self._release(message)

    def _start_worker(self) -> None:
        if self._worker is not None:
            raise RuntimeError("adapter is already started")
        self._accepting = True
        self._worker = asyncio.create_task(self._run())

    def _start_announcements(
        self,
        outbox: AnnouncementOutbox | None,
        prepare: Callable[[], Awaitable[int]],
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        if outbox is not None:
            assert self.airtime_limiter is not None
            self._announcement_task = asyncio.create_task(
                outbox.run(self.airtime_limiter, prepare, send, self.transmission_attempts)
            )

    async def _stop_worker(self) -> None:
        if self._announcement_task is not None:
            self._announcement_task.cancel()
            await asyncio.gather(self._announcement_task, return_exceptions=True)
            self._announcement_task = None
        self._accepting = False
        worker, self._worker = self._worker, None
        try:
            if worker is not None:
                worker.cancel()
                result = (await asyncio.gather(worker, return_exceptions=True))[0]
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise result
        finally:
            while not self._queue.empty():
                message = self._queue.get_nowait()
                self._queue.task_done()
                self._release(message)

    async def drain(self) -> None:
        """Wait until every accepted in-memory request has been processed."""
        await self._queue.join()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            message = await self._queue.get()
            reservation = None
            try:
                if self.airtime_limiter is not None:
                    reservation = await self.airtime_limiter.acquire(self.transmission_attempts)
                reply = await self.handler(message)
                if not reply:
                    continue
                if "\x00" in reply or len(reply.encode("utf-8")) > self.max_bytes:
                    logger.error("Command response exceeds the radio text contract")
                    reply = "Response too long for this radio. Ask for a smaller page."
                delay = self.min_interval - (loop.time() - self._last_send)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_send = loop.time()
                await self.send_reply(message, reply)
                self.acknowledged += 1
            except Exception:
                # Isolate handler/transport failures; the next request can still run.
                self.failed += 1
                logger.exception("Radio request failed")
            finally:
                try:
                    if reservation is not None:
                        assert self.airtime_limiter is not None
                        self.airtime_limiter.finish(reservation)
                finally:
                    self._queue.task_done()
                    self._release(message)

    @abstractmethod
    async def send_reply(self, message: IncomingMessage, text: str) -> None:
        """Send one frame and wait for the transport acknowledgement."""


def validate_connection(serial_port: str | None, tcp_host: str | None, tcp_port: int) -> None:
    if bool(serial_port) == bool(tcp_host):
        raise ValueError("configure exactly one serial_port or tcp_host")
    if not 1 <= tcp_port <= 65535:
        raise ValueError("tcp_port must be between 1 and 65535")
