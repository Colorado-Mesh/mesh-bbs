"""LXMF commands, NomadNet pages, and trusted-peer Reticulum exchanges.

The application owns durable commands and replication transactions. LXMF delivery
receipts only acknowledge transport delivery; successful commands need an explicit
application response and an idempotency key supplied by the command layer.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import os
import re
import signal
import stat
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import IncomingMessage

LOG = logging.getLogger(__name__)
SYNC_PATH = "/sync/v1/exchange"
PAGE_PATHS = (
    "/page/index.mu",
    "/page/board.mu",
    "/page/thread.mu",
    "/page/post.mu",
)


def _dependencies() -> tuple[Any, Any]:
    try:
        return importlib.import_module("RNS"), importlib.import_module("LXMF")
    except ImportError as error:
        raise RuntimeError("Install mesh-bbs[reticulum] to enable Reticulum") from error


def _identity_hash(value: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise ValueError("Reticulum peer identities must be 32 hexadecimal characters")
    return value.lower()


def _bounded_object(value: Any, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Expected an object with string keys")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Payload must contain JSON-compatible values") from error
    if len(encoded) > limit:
        raise ValueError("Payload exceeds the configured byte limit")
    return value


class ReticulumAdapter:
    """Own one RNS runtime, using explicitly configured state directories.

    ``start`` must run on the main thread in the application's asyncio loop.
    ``command_handler`` runs in that loop. ``page_handler(path, data)`` and
    ``sync_handler(authenticated_peer_identity, data)`` run on RNS worker threads
    and must use thread-safe application storage. The sync handler must enforce
    any board permissions in addition to the transport's peer allowlist.

    RNS has a process-wide singleton. Stopping this adapter shuts that runtime
    down; restart the process to start a new one. Tests use subprocesses and
    temporary configurations containing only loopback interfaces.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        state_dir: Path,
        name: str,
        command_handler: Callable[[IncomingMessage], Awaitable[str]],
        page_handler: Callable[[str, dict[str, Any]], bytes],
        sync_handler: Callable[[str, dict[str, Any]], dict[str, Any]],
        trusted_peers: list[str],
        queue_size: int = 32,
        command_limit: int = 65_536,
        response_limit: int = 524_288,
        request_limit: int = 262_144,
        delivery_timeout: float = 60.0,
        announce_interval: float = 1_800.0,
    ) -> None:
        limits = (queue_size, command_limit, response_limit, request_limit)
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("Queue and payload limits must be positive")
        if queue_size > 1024:
            raise ValueError("Queue size must not exceed 1024")
        if not all(
            math.isfinite(value) and value > 0 for value in (delivery_timeout, announce_interval)
        ):
            raise ValueError("Timeout and announce interval must be positive")
        if not name or len(name.encode("utf-8")) > 128:
            raise ValueError("Service name must contain 1 to 128 UTF-8 bytes")
        self.config_dir = config_dir.expanduser().resolve()
        self.state_dir = state_dir.expanduser().resolve()
        self.name = name
        self.command_handler = command_handler
        self.page_handler = page_handler
        self.sync_handler = sync_handler
        self.trusted_peers = frozenset(_identity_hash(peer) for peer in trusted_peers)
        self.command_limit = command_limit
        self.response_limit = response_limit
        self.request_limit = request_limit
        self.delivery_timeout = delivery_timeout
        self.announce_interval = announce_interval
        self._slots = threading.BoundedSemaphore(queue_size)
        self._requests = threading.BoundedSemaphore(8)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)
        self._tasks: list[asyncio.Task[None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._started = False
        self._rns: Any = None
        self._lxmf: Any = None
        self._router: Any = None
        self._identity: Any = None
        self._source: Any = None
        self._pages: Any = None
        self._sync: Any = None
        self._links: set[Any] = set()
        self._peer_locks = {peer: asyncio.Lock() for peer in self.trusted_peers}

    @property
    def identity_hash(self) -> str:
        if self._identity is None:
            raise RuntimeError("Reticulum adapter has not started")
        return str(self._identity.hash.hex())

    @property
    def addresses(self) -> dict[str, str]:
        if self._source is None:
            raise RuntimeError("Reticulum adapter has not started")
        return {
            "identity": self.identity_hash,
            "lxmf": self._source.hash.hex(),
            "nomadnet": self._pages.hash.hex(),
            "sync": self._sync.hash.hex(),
        }

    def _load_identity(self) -> Any:
        path = self.state_dir / "identity"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("Reticulum service identity must be a regular file") from None
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as key_file:
                if not stat.S_ISREG(os.fstat(key_file.fileno()).st_mode):
                    raise ValueError("Reticulum service identity must be a regular file") from None
                os.fchmod(key_file.fileno(), 0o600)
                key_size = self._rns.Identity.KEYSIZE // 8
                private_key = key_file.read(key_size + 1)
            identity = self._rns.Identity(create_keys=False)
            if len(private_key) != key_size or not identity.load_private_key(private_key):
                raise ValueError("Cannot load the existing Reticulum service identity") from None
            return identity
        else:
            with os.fdopen(descriptor, "wb") as key_file:
                os.fchmod(key_file.fileno(), 0o600)
                identity = self._rns.Identity()
                key_file.write(identity.get_private_key())
                key_file.flush()
                os.fsync(key_file.fileno())
            return identity

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("A Reticulum adapter can only be started once per process")
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Reticulum must start on the main thread")
        if not (self.config_dir / "config").is_file():
            raise ValueError("An explicit Reticulum config file is required; no default is created")
        self._rns, self._lxmf = _dependencies()
        if self._rns.Reticulum.get_instance() is not None:
            raise RuntimeError("Another Reticulum runtime already owns this process")
        self._loop = asyncio.get_running_loop()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous_signals = {
            number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            self._rns.Reticulum(configdir=str(self.config_dir))
            self._started = True
            self._identity = self._load_identity()
            self._router = self._lxmf.LXMRouter(
                identity=self._identity,
                storagepath=str(self.state_dir),
                autopeer=False,
                delivery_limit=(self.command_limit + 4096) / 1000,
            )
            self._source = self._router.register_delivery_identity(
                self._identity, display_name=self.name
            )
            self._pages = self._destination("nomadnetwork", "node")
            self._pages.set_max_request_size(8192)
            for path in PAGE_PATHS:
                self._pages.register_request_handler(
                    path, self._page_request, allow=self._rns.Destination.ALLOW_ALL
                )
            self._sync = self._destination("meshbbs", "sync")
            self._sync.set_max_request_size(self.request_limit)
            self._sync.register_request_handler(
                SYNC_PATH,
                self._sync_request,
                allow=self._rns.Destination.ALLOW_LIST,
                allowed_list=[bytes.fromhex(peer) for peer in self.trusted_peers],
            )
            self._running = True
            self._router.register_delivery_callback(self._on_message)
            self._tasks = [
                asyncio.create_task(self._command_worker()),
                asyncio.create_task(self._announcements()),
            ]
            self.announce()
        except BaseException:
            await self.stop()
            raise
        finally:
            # Libraries install process handlers; the daemon owns its signal policy.
            for number, handler in previous_signals.items():
                signal.signal(number, handler)

    def _destination(self, app: str, aspect: str) -> Any:
        return self._rns.Destination(
            self._identity, self._rns.Destination.IN, self._rns.Destination.SINGLE, app, aspect
        )

    def announce(self) -> None:
        if not self._running:
            raise RuntimeError("Reticulum adapter is not running")
        self._router.announce(self._source.hash)
        self._pages.announce(app_data=self.name.encode("utf-8"))
        self._sync.announce()

    async def _announcements(self) -> None:
        while True:
            await asyncio.sleep(self.announce_interval)
            self.announce()

    async def drain(self) -> None:
        """Wait for admitted commands and their transport replies to finish."""
        await self._queue.join()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            self._slots.release()
        for link in tuple(self._links):
            link.teardown()
        self._links.clear()
        if self._router is not None:
            self._router.register_delivery_callback(None)
            await asyncio.to_thread(self._router.exit_handler)
        if self._started:
            await asyncio.to_thread(self._rns.Reticulum.exit_handler)

    def _schedule(self, callback: Callable[..., Any], *args: Any) -> bool:
        if self._loop is None or self._loop.is_closed():
            return False
        try:
            self._loop.call_soon_threadsafe(callback, *args)
            return True
        except RuntimeError:
            return False

    def _on_message(self, message: Any) -> None:
        if not self._running or not message.signature_validated:
            return
        if not isinstance(message.content, bytes) or len(message.content) > self.command_limit:
            return
        try:
            message.content.decode("utf-8")
        except UnicodeDecodeError:
            return
        # Bound work before scheduling, including callbacks awaiting their loop turn.
        if not self._slots.acquire(blocking=False):
            LOG.warning("Reticulum command queue is full; no application receipt was issued")
            return
        if not self._schedule(self._enqueue, message):
            self._slots.release()

    def _enqueue(self, message: Any) -> None:
        if self._running:
            self._queue.put_nowait(message)
        else:
            self._slots.release()

    async def _command_worker(self) -> None:
        from .base import IncomingMessage

        while True:
            message = await self._queue.get()
            try:
                response = await self.command_handler(
                    IncomingMessage(
                        protocol="reticulum",
                        sender=message.source_hash.hex(),
                        text=message.content.decode("utf-8"),
                        message_id=message.hash.hex(),
                    )
                )
                if response == "":
                    continue
                if (
                    not isinstance(response, str)
                    or len(response.encode("utf-8")) > self.response_limit
                ):
                    raise ValueError("Command response exceeds the adapter limit")
                await self._send_reply(message, response)
            except Exception:
                LOG.exception("Reticulum command failed; client may retry its operation ID")
            finally:
                self._queue.task_done()
                self._slots.release()

    async def _send_reply(self, incoming: Any, text: str) -> None:
        destination = incoming.source
        if destination is None:
            raise ValueError("Verified LXMF sender has no reply destination")
        message = self._lxmf.LXMessage(
            destination, self._source, text, desired_method=self._lxmf.LXMessage.DIRECT
        )
        assert self._loop is not None
        delivered: asyncio.Future[bool] = self._loop.create_future()

        def finish(ok: bool) -> None:
            if not delivered.done():
                delivered.set_result(ok)

        message.register_delivery_callback(lambda _: self._schedule(finish, True))
        message.register_failed_callback(lambda _: self._schedule(finish, False))
        try:
            await asyncio.to_thread(self._router.handle_outbound, message)
            if not await asyncio.wait_for(delivered, timeout=self.delivery_timeout):
                raise ConnectionError("LXMF reply delivery failed")
        finally:
            if message.hash is not None:
                await asyncio.to_thread(self._router.cancel_outbound, message.hash)

    def _page_request(
        self,
        path: str,
        data: Any,
        request_id: bytes,
        link_id: bytes,
        remote_identity: Any,
        requested_at: float,
    ) -> bytes:
        if not self._running or not self._requests.acquire(blocking=False):
            return b"#!c=0\n>Busy\nPlease try again shortly.\n"
        try:
            # NomadNet sends nil for no variables; Mesh Client's Rust
            # LinkClient sends an empty binary. Nonempty variables are maps.
            request = _bounded_object({} if data is None or data == b"" else data, 8192)
            response = self.page_handler(path, request)
            if not isinstance(response, bytes) or len(response) > self.response_limit:
                raise ValueError("Page response exceeds the adapter limit")
            return response
        except Exception:
            LOG.exception("NomadNet page request failed")
            return b"#!c=0\n>Unavailable\nThe requested page is unavailable.\n"
        finally:
            self._requests.release()

    def _sync_request(
        self,
        path: str,
        data: Any,
        request_id: bytes,
        link_id: bytes,
        remote_identity: Any,
        requested_at: float,
    ) -> dict[str, Any]:
        peer = remote_identity.hash.hex() if remote_identity is not None else ""
        if not self._running or peer not in self.trusted_peers:
            return {"error": "unauthorized"}
        if not self._requests.acquire(blocking=False):
            return {"error": "busy"}
        try:
            request = _bounded_object(data, self.request_limit)
            return _bounded_object(self.sync_handler(peer, request), self.response_limit)
        except Exception:
            LOG.exception("Reticulum sync request failed")
            return {"error": "invalid_request"}
        finally:
            self._requests.release()

    async def request_peer(
        self, peer_identity: str, request: dict[str, Any], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        """Exchange one bounded object with a pinned, allowlisted peer identity.

        The overall timeout includes lock contention, discovery, link setup, and
        transfer. A timeout never advances a replication cursor; that belongs to
        the application after its response has committed.
        """
        peer = _identity_hash(peer_identity)
        if not self._running:
            raise RuntimeError("Reticulum adapter is not running")
        if peer not in self.trusted_peers:
            raise PermissionError("Reticulum peer is not in the configured allowlist")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Peer request timeout must be positive")
        _bounded_object(request, self.request_limit - 128)
        async with asyncio.timeout(timeout), self._peer_locks[peer]:
            destination_hash = self._rns.Destination.hash(bytes.fromhex(peer), "meshbbs", "sync")
            if not self._rns.Transport.has_path(destination_hash):
                self._rns.Transport.request_path(destination_hash)
            while not self._rns.Transport.has_path(destination_hash):
                await asyncio.sleep(0.05)
            identity = self._rns.Identity.recall(destination_hash)
            if identity is None or identity.hash.hex() != peer:
                raise PermissionError(
                    "Discovered destination does not match the pinned peer identity"
                )
            destination = self._rns.Destination(
                identity, self._rns.Destination.OUT, self._rns.Destination.SINGLE, "meshbbs", "sync"
            )
            if destination.hash != destination_hash:
                raise PermissionError("Discovered destination does not match the sync service")
            link = self._rns.Link(destination)
            self._links.add(link)
            try:
                while link.status != self._rns.Link.ACTIVE:
                    if link.status == self._rns.Link.CLOSED:
                        raise ConnectionError("Reticulum peer link closed before establishment")
                    await asyncio.sleep(0.05)
                link.identify(self._identity)
                return await self._exchange(link, request, timeout)
            finally:
                self._links.discard(link)
                link.teardown()

    async def _exchange(self, link: Any, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self._loop is not None
        response: asyncio.Future[Any] = self._loop.create_future()

        def succeeded(receipt: Any) -> None:
            if not response.done():
                response.set_result(receipt.response)

        def failed(receipt: Any) -> None:
            if not response.done():
                response.set_exception(ConnectionError("Reticulum sync exchange failed"))

        receipt = link.request(
            SYNC_PATH,
            data=request,
            response_callback=lambda result: self._schedule(succeeded, result),
            failed_callback=lambda result: self._schedule(failed, result),
            timeout=timeout,
            max_response_size=self.response_limit,
        )
        if receipt is False:
            raise ConnectionError("Reticulum refused to send the sync exchange")
        return _bounded_object(await response, self.response_limit)
