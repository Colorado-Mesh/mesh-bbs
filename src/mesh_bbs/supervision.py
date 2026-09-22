"""Recover radio connections without stopping local readers or background work."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

LOG = logging.getLogger(__name__)


class RadioConnection(Protocol):
    @property
    def connected(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class RadioSupervisor:
    def __init__(
        self,
        name: str,
        factory: Callable[[], RadioConnection],
        *,
        initial_delay: float = 1.0,
        maximum_delay: float = 60.0,
        poll_interval: float = 1.0,
        stable_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        intervals = (initial_delay, maximum_delay, poll_interval, stable_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in intervals):
            raise ValueError("Radio recovery intervals must be finite and positive")
        if initial_delay > maximum_delay:
            raise ValueError("Initial recovery delay cannot exceed its maximum")
        self.name, self.factory = name, factory
        self.initial_delay, self.maximum_delay = initial_delay, maximum_delay
        self.poll_interval, self.stable_seconds = poll_interval, stable_seconds
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._state = "connecting"
        self._attempts = 0
        self._connections = 0
        self._retry_delay = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "attempts": self._attempts,
                "reconnections": max(0, self._connections - 1),
                "retry_delay_seconds": self._retry_delay,
            }

    def _state_changed(self, state: str, delay: float = 0) -> None:
        with self._lock:
            self._state, self._retry_delay = state, delay
            if state == "connecting":
                self._attempts += 1
            elif state == "online":
                self._connections += 1

    async def run(self) -> None:
        delay = self.initial_delay
        try:
            while True:
                adapter = None
                online_since = None
                failed = False
                self._state_changed("connecting")
                try:
                    adapter = self.factory()
                    await adapter.start()
                    if not adapter.connected:
                        raise ConnectionError("Radio did not complete startup")
                    online_since = self._clock()
                    self._state_changed("online")
                    LOG.info("%s radio connected", self.name)
                    while adapter.connected:
                        await self._sleep(self.poll_interval)
                    raise ConnectionError("Radio connection ended")
                except asyncio.CancelledError:
                    self._state_changed("stopping")
                    raise
                except (ImportError, ValueError):
                    failed = True
                    self._state_changed("failed")
                    LOG.exception("%s radio configuration or dependency is invalid", self.name)
                except Exception as error:
                    self._state_changed("retrying")
                    LOG.warning("%s radio unavailable; reconnecting: %s", self.name, error)
                finally:
                    if adapter is not None:
                        try:
                            await adapter.stop()
                        except Exception:
                            failed = True
                            self._state_changed("failed")
                            LOG.exception("%s radio cleanup failed; restart required", self.name)
                if failed:
                    return
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                if online_since is not None and self._clock() - online_since >= self.stable_seconds:
                    delay = self.initial_delay
                self._state_changed("retrying", delay)
                await self._sleep(delay)
                delay = min(delay * 2, self.maximum_delay)
        except asyncio.CancelledError:
            self._state_changed("stopped")
            raise


def radio_readiness(radios: Mapping[str, RadioSupervisor]) -> tuple[bool, dict[str, Any]]:
    states = {name: supervisor.snapshot() for name, supervisor in radios.items()}
    ready = all(state["state"] == "online" for state in states.values())
    return ready, {"status": "ready" if ready else "degraded", "radios": states}
