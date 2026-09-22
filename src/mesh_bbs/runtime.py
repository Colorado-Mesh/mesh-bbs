"""Run configured services with bounded background work and clean shutdown."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from functools import partial
from typing import Any

from mesh_bbs.adapters.base import IncomingMessage
from mesh_bbs.airtime import AirtimeLimiter
from mesh_bbs.cli import open_store
from mesh_bbs.commands import CommandService
from mesh_bbs.config import HostConfig

logger = logging.getLogger(__name__)


async def serve(config: HostConfig, *, stop: asyncio.Event | None = None) -> None:
    from mesh_bbs.federation import FederationService
    from mesh_bbs.newsletters import NewsletterImporter
    from mesh_bbs.views import Views
    from mesh_bbs.web import ReadOnlyWebServer

    store = open_store(config)
    commands = CommandService(store, config.editors)
    stop = stop or asyncio.Event()
    adapters: list[Any] = []
    limiters: list[AirtimeLimiter] = []
    tasks: list[asyncio.Task[None]] = []
    workers: set[asyncio.Task[Any]] = set()
    web: ReadOnlyWebServer | None = None
    loop = asyncio.get_running_loop()

    async def in_worker(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        workers.add(task)
        task.add_done_callback(workers.discard)
        # Cancelling the coroutine must not abandon a thread holding a database operation.
        return await asyncio.shield(task)

    async def on_message(message: IncomingMessage) -> str:
        budget = 4096 if message.protocol in {"reticulum", "lxmf"} else 160
        actor = message.sender
        if not actor.startswith(message.protocol + ":"):
            actor = message.protocol + ":" + actor
        return str(
            await in_worker(
                commands.handle,
                actor,
                message.text,
                request_id=message.message_id,
                max_bytes=budget,
                request_ttl_seconds=86400 if message.protocol == "meshtastic" else None,
            )
        )

    try:
        public_host = config.bind_host if config.bind_host not in {"0.0.0.0", "::"} else "127.0.0.1"
        if ":" in public_host:
            public_host = f"[{public_host}]"
        base_url = config.public_url or f"http://{public_host}:{config.bind_port}"
        views = Views(store, config.name, base_url=base_url)
        web = ReadOnlyWebServer(views, config.bind_host, config.bind_port)
        web.start()
        peer_boards = {
            p.reticulum_identity: frozenset(p.allowed_boards)
            for p in config.peers
            if p.reticulum_identity
        }
        federation = FederationService(store, peer_boards)
        if config.reticulum.enabled:
            from mesh_bbs.adapters.reticulum import ReticulumAdapter

            assert config.reticulum.config_dir is not None
            reticulum = ReticulumAdapter(
                config_dir=config.reticulum.config_dir,
                state_dir=config.data_dir / "reticulum",
                name=config.name,
                command_handler=on_message,
                page_handler=views.page,
                sync_handler=federation.handle_request,
                trusted_peers=list(peer_boards),
                response_limit=512 * 1024,
                request_limit=16 * 1024,
            )
            await reticulum.start()
            adapters.append(reticulum)
            logger.info("Reticulum addresses: %s", reticulum.addresses)

            async def sync_loop() -> None:
                while True:
                    for peer in peer_boards:
                        try:
                            await federation.pull_peer(peer, partial(reticulum.request_peer, peer))
                        except Exception:
                            logger.exception(
                                "Replication with %s failed; retrying next cycle", peer
                            )
                    await asyncio.sleep(60)

            tasks.append(asyncio.create_task(sync_loop()))
        if config.meshcore.enabled:
            from mesh_bbs.adapters.meshcore import MeshCoreAdapter

            meshcore_budget = AirtimeLimiter(
                config.data_dir / "meshcore-airtime.sqlite3",
                f"{config.region}:meshcore",
                budget_seconds=config.meshcore.airtime_budget_seconds,
                window_seconds=config.meshcore.airtime_window_seconds,
                packet_airtime_seconds=config.meshcore.packet_airtime_seconds,
            )
            limiters.append(meshcore_budget)
            meshcore = MeshCoreAdapter(
                on_message,
                serial_port=config.meshcore.serial_port,
                tcp_host=config.meshcore.tcp_host,
                tcp_port=config.meshcore.tcp_port or 4000,
                min_interval=config.meshcore.min_interval,
                airtime_limiter=meshcore_budget,
            )
            await meshcore.start()
            adapters.append(meshcore)
        if config.meshtastic.enabled:
            from mesh_bbs.adapters.meshtastic import MeshtasticAdapter

            meshtastic_budget = AirtimeLimiter(
                config.data_dir / "meshtastic-airtime.sqlite3",
                f"{config.region}:meshtastic",
                budget_seconds=config.meshtastic.airtime_budget_seconds,
                window_seconds=config.meshtastic.airtime_window_seconds,
                packet_airtime_seconds=config.meshtastic.packet_airtime_seconds,
            )
            limiters.append(meshtastic_budget)
            meshtastic = MeshtasticAdapter(
                on_message,
                serial_port=config.meshtastic.serial_port,
                tcp_host=config.meshtastic.tcp_host,
                tcp_port=config.meshtastic.tcp_port or 4403,
                min_interval=config.meshtastic.min_interval,
                firmware_max_attempts=config.meshtastic.firmware_max_attempts,
                airtime_limiter=meshtastic_budget,
            )
            await meshtastic.start()
            adapters.append(meshtastic)
        importers = [NewsletterImporter(store, feed) for feed in config.feeds]

        async def feeds_loop() -> None:
            while True:
                for importer in importers:
                    try:
                        result = await in_worker(importer.poll_once)
                        if result.status not in {"not_due", "in_progress", "not_modified"}:
                            logger.info("Newsletter import: %s", result)
                    except Exception:
                        logger.exception("Newsletter poll failed")
                await asyncio.sleep(10)

        if importers:
            tasks.append(asyncio.create_task(feeds_loop()))
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        logger.info("%s serving region %s at %s", config.name, config.region, base_url)
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in reversed(adapters):
            try:
                await adapter.stop()
            except Exception:
                logger.exception("Transport shutdown failed")
        if web:
            await asyncio.to_thread(web.stop)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        for limiter in limiters:
            limiter.close()
        store.close()
